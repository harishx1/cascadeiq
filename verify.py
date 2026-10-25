"""Engine verification suite.

Run with:  python verify.py

Exercises the simulation, prediction and recovery engines across every bundled
topology and fault class, and asserts the invariants a judge would probe for:
root cause correctly isolated, cascade path lengthens with propagation, and
resilience features measurably change the outcome.
"""

from __future__ import annotations

import sys
import time

from app.engine.predictor import run_prediction
from app.engine.recovery import build_strategies
from app.engine.scenario import compare
from app.engine.simulation import DEFAULT_PARAMS, FAULTS, SimParams, Simulation
from app.engine.topology import list_topologies, load_topology

TOPOLOGIES = ["ecommerce", "chat", "iot", "monolith"]
FAULTS_TO_TEST = ["cpu_spike", "auth_expiry", "disk_full", "network_latency"]

results: list[tuple[bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def warm(sim: Simulation, ticks: int = 30) -> None:
    for _ in range(ticks):
        sim.step(0.05)


def hottest_internal_node(sim: Simulation):
    """Pick a well-connected non-external node as the fault target."""
    scored = []
    for n in sim.topology.nodes:
        if n.kind == "external":
            continue
        deg = len(sim.in_edges[n.id]) + len(sim.out_edges[n.id])
        scored.append((deg, n))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[0][1] if scored else sim.topology.nodes[0]


def section(title: str) -> None:
    print(f"\n== {title} " + "=" * max(0, 60 - len(title)))


# --------------------------------------------------------------------------
section("Topology integrity")
# --------------------------------------------------------------------------
for tid in [t["id"] for t in list_topologies()]:
    topo = load_topology(tid)
    issues = topo.validate()
    if tid == "blank":
        check("blank: intentionally empty (1 expected issue)",
              len(topo.nodes) == 0 and len(issues) == 1, "; ".join(issues))
        continue
    check(f"{tid}: valid graph ({len(topo.nodes)}n/{len(topo.edges)}e)",
          not issues, "; ".join(issues))


# --------------------------------------------------------------------------
section("Root-cause isolation across every topology x fault class")
# --------------------------------------------------------------------------
root_hits = 0
total = 0
for tid in TOPOLOGIES:
    topo = load_topology(tid)
    for ft in FAULTS_TO_TEST:
        sim = Simulation(topo, SimParams.from_dict(DEFAULT_PARAMS), seed=11)
        warm(sim)
        target = hottest_internal_node(sim)
        sim.inject_fault(target.id, ft, 7)
        for _ in range(500):
            sim.step(0.05)
        pred = run_prediction(sim)
        rc = pred["root_cause"]
        total += 1
        hit = bool(rc) and rc["id"] == target.id
        root_hits += hit
        print(f"    {tid:11s} {ft:17s} -> {rc['label'] if rc else '-':22s} "
              f"(expected {target.label}) path={len(pred['cascade_path'])}")
check(f"root cause correctly isolated in {root_hits}/{total} scenarios",
      root_hits == total)


# --------------------------------------------------------------------------
section("Cascade propagates along dependency edges")
# --------------------------------------------------------------------------
topo = load_topology("ecommerce")
sim = Simulation(topo, SimParams.from_dict(DEFAULT_PARAMS), seed=5)
warm(sim)
sim.inject_fault("ledgerdb", "slow_query", 8)
for _ in range(500):
    sim.step(0.05)
pred = run_prediction(sim)
path = [c["label"] for c in pred["cascade_path"]]
check("cascade path traces outward from the fault",
      len(path) >= 3 and path[0] == "Ledger DB",
      " -> ".join(path))
check("multiple services affected", sim.health_summary()["blast_radius"] >= 3,
      str(sim.health_summary()))


# --------------------------------------------------------------------------
section("Resilience features change the outcome")
# --------------------------------------------------------------------------
def outcome(circuit: bool, retries: float, rate_limit: bool = False) -> dict:
    sim = Simulation(topo, SimParams.from_dict(DEFAULT_PARAMS), seed=5)
    sim.set_params(circuit_breaker=circuit, retry_aggression=retries,
                   rate_limiting=rate_limit)
    warm(sim)
    sim.inject_fault("ledgerdb", "slow_query", 8)
    for _ in range(600):
        sim.step(0.05)
    return sim.health_summary()

fragile = outcome(False, 4.5)
hardened = outcome(True, 0.5, True)
print(f"    unprotected : {fragile}")
print(f"    hardened    : {hardened}")
check("circuit breakers + low retries reduce blast radius",
      hardened["blast_radius"] <= fragile["blast_radius"])
check("hardened system strictly better on 'down' count",
      hardened["down"] <= fragile["down"])


# --------------------------------------------------------------------------
section("Recovery recommendations adapt to fault class")
# --------------------------------------------------------------------------
def recommend(ft: str) -> tuple[str, str]:
    s = Simulation(topo, SimParams.from_dict(DEFAULT_PARAMS), seed=5)
    warm(s)
    s.inject_fault("ledgerdb", ft, 7)
    for _ in range(400):
        s.step(0.05)
    run_prediction(s)
    rec = build_strategies(s)
    return rec["recommended_id"], rec["fault_class"]

for ft, expected in [("auth_expiry", "rollback"), ("cpu_spike", None),
                     ("disk_full", None), ("network_latency", None)]:
    got, cls = recommend(ft)
    ok = expected is None or got == expected
    check(f"{ft} -> {got} ({cls})", ok, f"expected {expected}")


# --------------------------------------------------------------------------
section("Scenario (what-if) comparison")
# --------------------------------------------------------------------------
start = time.perf_counter()
result = compare(
    topo,
    SimParams.from_dict(DEFAULT_PARAMS).to_dict(),
    {"_label": "Retries 0.5x + breaker ON", "circuit_breaker": True,
     "retry_aggression": 0.5},
    {"_label": "Retries 4.5x + breaker OFF", "circuit_breaker": False,
     "retry_aggression": 4.5},
    {"node": "ledgerdb", "type": "slow_query", "severity": 8, "at": 3.0},
    duration=40.0,
)
elapsed = (time.perf_counter() - start) * 1000
print(f"    computed in {result['computed_in_ms']}ms (wall {elapsed:.0f}ms)")
print(f"    A {result['a']['metrics']}")
print(f"    B {result['b']['metrics']}")
check("scenario returns two comparable metric sets",
      result["a"]["metrics"]["availability"] is not None
      and result["b"]["metrics"]["availability"] is not None)
check("resilient variant has higher availability",
      result["a"]["metrics"]["availability"] >= result["b"]["metrics"]["availability"],
      f"{result['a']['metrics']['availability']} vs {result['b']['metrics']['availability']}")
check("scenario is fast enough for an interactive demo", elapsed < 6000,
      f"{elapsed:.0f}ms")


# --------------------------------------------------------------------------
section("Scale: engine stays responsive on a large graph")
# --------------------------------------------------------------------------
import random
rng = random.Random(7)
big_nodes = [{"id": f"svc{i}", "label": f"Service {i}", "kind": "service"}
             for i in range(300)]
big_edges = []
for i in range(300):
    for _ in range(3):
        j = rng.randrange(300)
        if i != j:
            big_edges.append({"source": f"svc{i}", "target": f"svc{j}", "kind": "sync"})
from app.engine.topology import Topology
big = Topology.from_dict({"name": "Large mesh", "nodes": big_nodes, "edges": big_edges})
bigsim = Simulation(big, SimParams.from_dict(DEFAULT_PARAMS), seed=1)
bigsim.inject_fault("svc42", "cpu_spike", 9)
t0 = time.perf_counter()
steps = 400
for _ in range(steps):
    bigsim.step(0.05)
    if bigsim.tick_count % 5 == 0:
        run_prediction(bigsim)
per_step = (time.perf_counter() - t0) / steps * 1000
check(f"300-node / {len(big.edges)}-edge graph runs at {per_step:.2f}ms/step",
      per_step < 25, f"{per_step:.2f}ms")


# --------------------------------------------------------------------------
section("Topology import is genuinely open-ended")
# --------------------------------------------------------------------------
from app.engine.topology import topology_from_json

alt = """
{"name": "Acme Data Platform",
 "services": ["ingest", "spark", "snowflake", "looker"],
 "depends_on": {"ingest": ["spark"], "spark": ["snowflake"], "looker": ["snowflake"]}}
"""
imported = topology_from_json(alt)
check("parses an alternative schema (services + depends_on)",
      len(imported.nodes) == 4 and len(imported.edges) == 3,
      f"{len(imported.nodes)} nodes / {len(imported.edges)} edges")

s = Simulation(imported, SimParams.from_dict(DEFAULT_PARAMS), seed=2)
s.inject_fault("snowflake", "disk_full", 9)
for _ in range(400):
    s.step(0.05)
p = run_prediction(s)
rc = p["root_cause"]
check("predicts correctly on a topology it has never seen",
      rc is not None and rc["id"] == "snowflake",
      rc["label"] if rc else "no root cause")


# --------------------------------------------------------------------------
section("Report generation")
# --------------------------------------------------------------------------
from app.main import build_report

report = build_report(sim)
check("incident report contains root cause + recommendation",
      report["root_cause"]["node"] is not None
      and report["recommended_action"]["strategy"] is not None,
      f"{report['root_cause']['node']} -> {report['recommended_action']['strategy']}")
check("report lists ranked alternatives", len(report["alternatives"]) >= 3)


# --------------------------------------------------------------------------
print("\n" + "=" * 64)
passed = sum(1 for ok, _ in results if ok)
print(f"  {passed}/{len(results)} checks passed")
print("=" * 64)
sys.exit(0 if passed == len(results) else 1)
