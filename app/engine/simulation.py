"""Event-driven cascade simulation engine.

The engine is deliberately *dumb* about what a service does. It only knows:

    nodes have metrics, edges carry pressure, pressure moves along edges.

Everything else — how many services there are, what language they use, whether
the topology is a monolith or a mesh — is data supplied at runtime.
"""

from __future__ import annotations

import heapq
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .topology import DEAD_BOOST, DEGRADED_FACTOR, Edge, EdgeKind, Health, Node, Topology

SIM_TICK = 0.1  # seconds of simulated time per nominal tick

# --------------------------------------------------------------------------
# Fault catalogue
# --------------------------------------------------------------------------

FAULTS: dict[str, dict[str, Any]] = {
    "cpu_spike": {
        "label": "CPU saturation",
        "summary": "Worker threads exhausted, request queue growing",
        "stress": 66, "cpu": 44, "mem": 6, "latency_mult": 3.2, "err": 10, "thr_mult": 0.75,
        "ramp": 4.0,
    },
    "memory_leak": {
        "label": "Memory leak",
        "summary": "Heap growth, GC pauses, allocation failures",
        "stress": 58, "cpu": 14, "mem": 42, "latency_mult": 2.4, "err": 7, "thr_mult": 0.8,
        "ramp": 9.0,
    },
    "network_latency": {
        "label": "Network latency",
        "summary": "Packet loss / degraded link between services",
        "stress": 62, "cpu": 3, "mem": 2, "latency_mult": 7.5, "err": 16, "thr_mult": 0.85,
        "ramp": 2.5,
    },
    "disk_full": {
        "label": "Disk exhaustion",
        "summary": "Volume at 100%, writes rejected",
        "stress": 74, "cpu": 6, "mem": 16, "latency_mult": 4.5, "err": 24, "thr_mult": 0.35,
        "ramp": 6.0,
    },
    "auth_expiry": {
        "label": "Credential expiry",
        "summary": "Service token / API key no longer valid",
        "stress": 78, "cpu": 2, "mem": 1, "latency_mult": 1.5, "err": 46, "thr_mult": 0.5,
        "ramp": 1.2,
    },
    "slow_query": {
        "label": "Slow query / lock contention",
        "summary": "Table lock held, connection pool saturated",
        "stress": 68, "cpu": 26, "mem": 8, "latency_mult": 8.0, "err": 9, "thr_mult": 0.55,
        "ramp": 3.5,
    },
    "oom_kill": {
        "label": "OOM kill",
        "summary": "Process terminated by the kernel",
        "stress": 92, "cpu": 0, "mem": 55, "latency_mult": 12.0, "err": 60, "thr_mult": 0.05,
        "ramp": 0.8,
    },
    "cache_eviction": {
        "label": "Cache thrash",
        "summary": "Hit rate collapse, origin absorbing full load",
        "stress": 54, "cpu": 22, "mem": 12, "latency_mult": 5.5, "err": 12, "thr_mult": 0.9,
        "ramp": 3.0,
    },
}

DEFAULT_PARAMS: dict[str, Any] = {
    "traffic": 55.0,          # 0-100 request volume
    "cascade_speed": 1.0,     # 0.25x - 4x simulation time scale
    "severity": 5,            # 1-10 injected fault strength
    "fault_type": "cpu_spike",
    "timeout_ms": 800,        # caller timeout budget
    "retry_aggression": 1.5,  # 0-5 retries on failure (amplifies load)
    "circuit_breaker": False, # isolate failing dependencies
    "rate_limiting": False,   # shed load at the edge
    "chaos_mode": False,
    "chaos_interval": 9.0,    # seconds between random faults
    "paused": False,
    "playback": 1.0,          # 0.25x - 4x real-time playback
}


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------
# Runtime state
# --------------------------------------------------------------------------

@dataclass
class NodeState:
    id: str
    stress: float = 0.0
    pressure: float = 0.0
    health: str = Health.HEALTHY.value
    cpu: float = 0.0
    mem: float = 0.0
    latency: float = 0.0
    error_rate: float = 0.0
    throughput: float = 0.0
    load: float = 0.0
    queue_depth: int = 0
    replicas: int = 1
    fault: dict[str, Any] | None = None
    first_anomaly: float | None = None
    last_state_change: float = 0.0
    pressure_parent: str | None = None
    pressure_from: float = 0.0
    predicted_by: str | None = None
    predicted_at: float | None = None
    recovery_hint: str | None = None

    @property
    def is_anomalous(self) -> bool:
        return self.health != Health.HEALTHY.value

    @property
    def fault_intensity(self) -> float:
        if not self.fault:
            return 0.0
        return float(self.fault.get("intensity", 0.0))


@dataclass
class EdgeState:
    id: str
    flow: float = 100.0          # 0-100 utilisation of this link
    propagated: float = 0.0      # how much failure pressure is crossing
    tripped: bool = False        # circuit breaker open
    tripped_at: float | None = None
    manual: bool = False         # opened by an operator isolation, not the breaker
    backlog: int = 0             # queued items (async only)
    latency: float = 0.0


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

@dataclass
class SimParams:
    traffic: float = 55.0
    cascade_speed: float = 1.0
    severity: int = 5
    fault_type: str = "cpu_spike"
    timeout_ms: int = 800
    retry_aggression: float = 1.5
    circuit_breaker: bool = False
    rate_limiting: bool = False
    chaos_mode: bool = False
    chaos_interval: float = 9.0
    paused: bool = False
    playback: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SimParams":
        p = cls()
        if data:
            for k, v in data.items():
                if hasattr(p, k):
                    setattr(p, k, v)
        return p


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

class Simulation:
    """A single running instance of a topology under fault conditions."""

    def __init__(self, topology: Topology, params: SimParams | None = None,
                 seed: int | None = None) -> None:
        self.topology = topology
        self.params = params or SimParams()
        self.rng = random.Random(seed if seed is not None else time.time_ns())
        self.sim_id = f"sim-{topology.fingerprint()}-{time.time_ns() % 100000}"

        self.out_edges: dict[str, list[Edge]] = topology.out_edges()
        self.in_edges: dict[str, list[Edge]] = topology.in_edges()
        self.edges_by_id: dict[str, Edge] = {e.id: e for e in topology.edges}

        self.t: float = 0.0
        self.seq: int = 0
        self.event_queue: list[tuple[float, int, str, dict[str, Any]]] = []
        self.nodes: dict[str, NodeState] = {}
        self.edge_states: dict[str, EdgeState] = {}
        self.logs: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = []
        self.last_prediction: dict[str, Any] = {}
        self.last_recovery: dict[str, Any] = {}
        self.root_cause_id: str | None = None
        self.chaos_next: float = self.params.chaos_interval
        self.tick_count: int = 0
        self.uptime_score: float = 100.0
        self.peak_blast: int = 0
        self.peak_down: int = 0

        self._init_state()
        self._seed_baseline_logs()
        self._record_timeline("SYSTEM", "Simulation initialised",
                              detail=f"{len(topology.nodes)} services, "
                                     f"{len(topology.edges)} dependencies")

    # ---------------------------------------------------------------- setup
    def _init_state(self) -> None:
        for n in self.topology.nodes:
            d = n.defaults()
            st = NodeState(id=n.id)
            st.cpu = d["base_cpu"] * 0.6
            st.mem = d["base_mem"] * 0.6
            st.latency = d["base_latency"]
            st.throughput = d["capacity"] * 0.4
            self.nodes[n.id] = st
        for e in self.topology.edges:
            self.edge_states[e.id] = EdgeState(id=e.id, latency=e.resolved_latency())

    def _seed_baseline_logs(self) -> None:
        self._log("INFO", "system", "CascadeIQ simulation runtime v1.4.0 ready", bootstrap=True)
        self._log("INFO", "system",
                  f"Loaded topology '{self.topology.name}' "
                  f"({len(self.topology.nodes)} nodes / {len(self.topology.edges)} edges)",
                  bootstrap=True)
        self._log("INFO", "system",
                  "Anomaly detector armed — threshold: 3σ rolling baseline", bootstrap=True)

    # ------------------------------------------------------------- controls
    def set_params(self, **kwargs: Any) -> dict[str, Any]:
        for k, v in kwargs.items():
            if hasattr(self.params, k):
                setattr(self.params, k, v)
        return self.params.to_dict()

    def inject_fault(self, node_id: str, fault_type: str | None = None,
                     severity: int | None = None) -> dict[str, Any] | None:
        if node_id not in self.nodes:
            return None
        ftype = fault_type or self.params.fault_type
        if ftype not in FAULTS:
            ftype = "cpu_spike"
        sev = int(severity if severity is not None else self.params.severity)
        sev = max(1, min(10, sev))

        st = self.nodes[node_id]
        st.fault = {
            "type": ftype,
            "severity": sev,
            "since": self.t,
            "intensity": 0.0,
            "origin": "injected",
        }
        if st.first_anomaly is None:
            st.first_anomaly = self.t
        node = self.topology.node_map()[node_id]
        spec = FAULTS[ftype]
        self._record_timeline(
            "FAULT", f"Fault injected → {node.label}",
            node=node_id, level="critical",
            detail=f"{spec['label']} (severity {sev}/10) — {spec['summary']}")
        self._log("ERROR", node_id,
                  f"FAULT INJECTED: {spec['label'].lower()} — {spec['summary']}")
        self._log("WARN", node_id,
                  f"circuit={"open" if self.params.circuit_breaker else "disabled"} "
                  f"retries={self.params.retry_aggression:g}x timeout={self.params.timeout_ms}ms")
        self._schedule("fault_ramp", self.t + 0.5, {"node": node_id})
        return {"node": node_id, "type": ftype, "severity": sev}

    def clear_faults(self) -> int:
        n = 0
        for st in self.nodes.values():
            if st.fault:
                st.fault = None
                n += 1
        for st in self.nodes.values():
            st.first_anomaly = None
        for es in self.edge_states.values():
            es.tripped = False
            es.tripped_at = None
            es.manual = False
        self.root_cause_id = None
        self.last_prediction = {}
        self.last_recovery = {}
        if n:
            self._record_timeline("RECOVERY", "All faults cleared — full reset",
                                  level="ok", detail=f"{n} fault(s) removed")
            self._log("INFO", "system", f"operator cleared {n} active fault(s)")
        return n

    def apply_recovery(self, strategy_id: str) -> dict[str, Any] | None:
        """Execute a recovery strategy against the live simulation."""
        from .recovery import build_strategies  # local import avoids cycle

        built = build_strategies(self)
        strategies = built.get("strategies", built) if isinstance(built, dict) else built
        chosen = next((s for s in strategies if s.get("id") == strategy_id), None)
        if not chosen:
            return None

        effects = chosen.get("effects", {})
        targets = effects.get("targets", [])
        if not targets:
            targets = [n for n, s in self.nodes.items() if s.is_anomalous]

        for nid in targets:
            st = self.nodes.get(nid)
            if not st:
                continue
            if effects.get("clear_fault", True):
                st.fault = None
            relief = float(effects.get("relief", 60))
            st.stress = clamp(st.stress - relief)
            st.pressure = clamp(st.pressure - relief)
        severed = 0
        if effects.get("break_edges"):
            for e in self.topology.edges:
                es = self.edge_states.get(e.id)
                if not es or es.tripped:
                    continue
                tgt = self.nodes.get(e.target)
                if effects.get("trip_all"):
                    take = True
                elif effects.get("trip_pressure"):
                    take = bool(tgt) and tgt.stress > 55.0
                else:
                    take = False
                if take:
                    # operator-initiated: the automatic breaker must not undo it
                    es.tripped = True
                    es.manual = True
                    es.tripped_at = self.t
                    severed += 1
            if severed:
                self._record_timeline(
                    "RECOVERY", f"Opened {severed} link(s)",
                    level="ok", detail="failing dependencies isolated from healthy callers")
        if effects.get("scale_out"):
            for nid in targets:
                if nid in self.nodes:
                    self.nodes[nid].replicas = min(6, self.nodes[nid].replicas + 2)

        self._record_timeline(
            "RECOVERY", f"Executed → {chosen['label']}",
            level="ok",
            detail=f"targeted {len(targets)} node(s) · est. {chosen['estimated_time']}s · "
                   f"risk {chosen['risk']}%")
        self._log("WARN", "system",
                  f"RECOVERY ACTION: {chosen['label']} applied to {len(targets)} node(s)",
                  recovery=True)
        return chosen

    # ---------------------------------------------------------------- events
    def _schedule(self, kind: str, at: float, payload: dict[str, Any]) -> None:
        self.seq += 1
        heapq.heappush(self.event_queue, (at, self.seq, kind, payload))

    def _drain_events(self) -> None:
        while self.event_queue and self.event_queue[0][0] <= self.t:
            _, _, kind, payload = heapq.heappop(self.event_queue)
            if kind == "fault_ramp":
                st = self.nodes.get(payload["node"])
                if st and st.fault:
                    self._log("WARN", payload["node"],
                              f"{FAULTS[st.fault['type']]['label']} detected — "
                              f"intensity climbing ({st.fault['intensity'] * 100:.0f}%)")
            elif kind == "chaos":
                pass

    # ----------------------------------------------------------------- logs
    def _log(self, level: str, node: str, message: str, **extra: Any) -> None:
        entry = {
            "t": round(self.t, 2),
            "wall": time.time(),
            "level": level,
            "node": node,
            "msg": message,
        }
        entry.update(extra)
        self.logs.append(entry)
        if len(self.logs) > 900:
            del self.logs[: len(self.logs) - 900]

    def _record_timeline(self, kind: str, title: str, node: str | None = None,
                         level: str = "info", detail: str = "") -> None:
        self.timeline.append({
            "id": len(self.timeline),
            "t": round(self.t, 2),
            "kind": kind,
            "title": title,
            "node": node,
            "level": level,
            "detail": detail,
        })
        if len(self.timeline) > 400:
            del self.timeline[: len(self.timeline) - 400]

    # ------------------------------------------------------------------ step
    def step(self, real_dt: float = 0.05) -> None:
        if self.params.paused:
            return
        p = self.params
        dt_sim = real_dt * p.cascade_speed * p.playback
        k = dt_sim / SIM_TICK  # rate multiplier vs. the nominal tick
        self.t += dt_sim
        self.tick_count += 1
        self._drain_events()

        if p.chaos_mode and self.t >= self.chaos_next:
            self.chaos_next = self.t + p.chaos_interval * self.rng.uniform(0.6, 1.5)
            self._inject_chaos()

        self._advance_faults(k)
        self._compute_pressure(k)
        self._apply_circuit_breaker()
        self._update_metrics(k)
        self._update_health()
        self._update_edges()
        self._generate_logs()
        self._capture_history()

        if self.tick_count % 5 == 0:
            from .predictor import run_prediction
            self.last_prediction = run_prediction(self)
        if self.tick_count % 10 == 0:
            self._update_uptime()

    def _inject_chaos(self) -> None:
        candidates = [n for n, s in self.nodes.items() if not s.fault]
        if not candidates:
            return
        nid = self.rng.choice(candidates)
        ftype = self.rng.choice(list(FAULTS))
        sev = self.rng.randint(2, 8)
        self._log("WARN", "chaos",
                  f"CHAOS ENGINE: random fault '{FAULTS[ftype]['label']}' → {nid}")
        self.inject_fault(nid, ftype, sev)

    def _advance_faults(self, k: float) -> None:
        for st in self.nodes.values():
            if not st.fault:
                continue
            spec = FAULTS[st.fault["type"]]
            elapsed = self.t - float(st.fault["since"])
            ramp = max(0.15, float(spec["ramp"]))
            st.fault["intensity"] = min(1.0, elapsed / ramp + 0.05)
            if st.first_anomaly is None:
                st.first_anomaly = self.t
            _ = k

    # -------------------------------------------------------------- pressure
    def _stress_of(self, node_id: str) -> float:
        st = self.nodes.get(node_id)
        return 0.0 if not st else st.stress

    def _compute_pressure(self, k: float = 1.0) -> None:
        p = self.params
        incoming: dict[str, float] = {nid: 0.0 for nid in self.nodes}
        parent: dict[str, tuple[str, float]] = {nid: ("", 0.0) for nid in self.nodes}

        for e in self.topology.edges:
            src, tgt = e.source, e.target
            if src not in self.nodes or tgt not in self.nodes:
                continue
            es = self.edge_states.get(e.id)
            # an operator isolation severs the link outright; an automatic
            # breaker only throttles it
            if es and es.manual:
                blocked = 0.0
            else:
                blocked = 0.12 if (es and es.tripped) else 1.0
            w = e.resolved_weight()
            s_src = self._stress_of(src)
            s_tgt = self._stress_of(tgt)
            norm_tgt = (s_tgt / 100.0) ** 1.15
            norm_src = (s_src / 100.0) ** 1.15
            boost = DEAD_BOOST.get(e.kind, 34.0)
            dead_tgt = boost * DEGRADED_FACTOR.get(self.nodes[tgt].health, 0.0)
            dead_src = boost * DEGRADED_FACTOR.get(self.nodes[src].health, 0.0)

            if e.kind in (EdgeKind.SYNC.value, EdgeKind.DB.value, EdgeKind.CACHE.value):
                # dependency fails -> caller feels it
                add = (norm_tgt * w * 100.0 + dead_tgt) * blocked
                incoming[src] += add
                if add > parent[src][1]:
                    parent[src] = (tgt, add)
            else:
                # shared buffer (queue / stream): coupling runs both ways,
                # damped because the buffer absorbs some of the shock
                add_up = (norm_tgt * w * 85.0 + dead_tgt) * blocked
                incoming[src] += add_up
                if add_up > parent[src][1]:
                    parent[src] = (tgt, add_up)
                add_down = (norm_src * w * 62.0 + dead_src) * blocked
                incoming[tgt] += add_down
                if add_down > parent[tgt][1]:
                    parent[tgt] = (src, add_down)

        # --- no active fault, no cascade ----------------------------------
        # Neighbour coupling is what *propagates* an incident; it needs a
        # source of energy. Once every injected fault is gone (operator hit
        # Clear, or the recommended recovery removed the root cause) the
        # bidirectional queue/stream terms would otherwise let two linked
        # nodes keep each other above the cliff forever. Drop the propagation
        # term so the board walks back to its load-driven baseline instead of
        # dead-locking — this is what makes "Clear" and an applied recovery
        # visibly converge.
        if not any(st.fault for st in self.nodes.values()):
            incoming = {nid: 0.0 for nid in self.nodes}
            parent = {nid: ("", 0.0) for nid in self.nodes}

        # retry amplification: retries pile onto a dependency that is hurting
        retry_hits: dict[str, int] = {nid: 0 for nid in self.nodes}
        if p.retry_aggression > 0:
            for e in self.topology.edges:
                es = self.edge_states.get(e.id)
                tgt_st = self.nodes.get(e.target)
                if not tgt_st or (es and es.tripped) or e.source not in self.nodes:
                    continue
                if tgt_st.stress > 25:
                    retry_hits[e.target] += 1

        for nid, st in self.nodes.items():
            node = self.topology.node_map().get(nid)
            base = node.defaults() if node else {"capacity": 400, "criticality": 0.7}

            # --- fault pressure -----------------------------------------
            fault_p = 0.0
            if st.fault:
                spec = FAULTS[st.fault["type"]]
                sev_mult = 0.45 + float(st.fault["severity"]) / 10.0 * 1.15
                fault_p = float(spec["stress"]) * sev_mult * st.fault_intensity

            # --- load pressure ------------------------------------------
            traffic_mult = 0.35 + (p.traffic / 100.0) * 1.25
            if p.rate_limiting and p.traffic > 70:
                traffic_mult = 0.35 + 0.7 * 1.25
            demand = 42.0 * traffic_mult * float(base.get("criticality", 0.7))
            retry_load = retry_hits[nid] * p.retry_aggression * 6.0
            st.load = clamp(demand + retry_load + st.replicas * 0 + st.fault_intensity *
                            (14 if st.fault else 0), 0, 140)
            load_p = max(0.0, st.load - 82.0) * 1.1

            # --- decay ----------------------------------------------------
            neighbour_p = incoming[nid]
            target = min(100.0, fault_p + neighbour_p * 0.92 + load_p)
            rising = target > st.stress
            rate = (0.16 if rising else 0.075) * k
            if not rising and st.fault is None:
                rate = 0.11 * k          # clean nodes heal faster
            st.stress = clamp(st.stress + (target - st.stress) * min(1.0, rate))
            st.pressure = neighbour_p

            pn, pv = parent[nid]
            st.pressure_parent = pn if pv > 4 else None

    def _apply_circuit_breaker(self) -> None:
        # with no injected fault anywhere the incident is over: release both
        # automatic breakers and operator isolations so the graph re-converges
        incident = any(st.fault for st in self.nodes.values())
        if not incident:
            for es in self.edge_states.values():
                if es.tripped:
                    es.tripped = False
                    es.tripped_at = None
                    es.manual = False
            return
        if not self.params.circuit_breaker:
            for es in self.edge_states.values():
                if es.tripped and not es.manual:
                    es.tripped = False
                    es.tripped_at = None
            return
        for e in self.topology.edges:
            es = self.edge_states.get(e.id)
            tgt = self.nodes.get(e.target)
            src = self.nodes.get(e.source)
            if not es or not tgt or not src:
                continue
            if not es.tripped and tgt.stress > 55:
                es.tripped = True
                es.tripped_at = self.t
                self._record_timeline("CIRCUIT", f"Breaker opened → {e.target}",
                                      node=e.target, level="warn",
                                      detail=f"caller {e.source} isolated from failing dependency")
                self._log("WARN", e.source,
                          f"circuit breaker OPEN toward {e.target} after {tgt.stress:.0f}% stress")
            elif es.tripped and not es.manual and tgt.stress < 28:
                es.tripped = False
                es.tripped_at = None
                self._log("INFO", e.source, f"circuit breaker CLOSED toward {e.target} — recovered")

    # --------------------------------------------------------------- metrics
    def _update_metrics(self, k: float) -> None:
        p = self.params
        timeout_s = max(20.0, p.timeout_ms) / 1000.0
        for nid, st in self.nodes.items():
            node = self.topology.node_map().get(nid)
            if not node:
                continue
            d = node.defaults()
            s = st.stress

            fault = st.fault or {}
            spec = FAULTS.get(fault.get("type", ""), {})
            fi = st.fault_intensity

            cpu = d["base_cpu"] * (0.35 + st.load / 140.0) + s * 0.34 + float(spec.get("cpu", 0)) * fi
            mem = d["base_mem"] * (0.45 + st.load / 220.0) + s * 0.30 + float(spec.get("mem", 0)) * fi

            lat_mult = 1.0 + (float(spec.get("latency_mult", 1.0)) - 1.0) * fi
            edge_lat = 0.0
            for e in self.out_edges.get(nid, []):
                es = self.edge_states.get(e.id)
                if es and es.tripped:
                    edge_lat += e.resolved_latency() * 0.15   # fallback path is cheap
                else:
                    tgt = self.nodes.get(e.target)
                    if tgt:
                        edge_lat += e.resolved_latency() * (1.0 + (tgt.stress / 100.0) ** 1.6 * 9.0)
                if es:
                    es.latency = edge_lat

            base_lat = d["base_latency"]
            st.latency = (base_lat + edge_lat * 0.55) * (1.0 + (s / 100.0) ** 1.9 * 16.0) * lat_mult

            # error rate: flat near zero, then a steep sigmoid through the middle
            err = 100.0 * sigmoid((s - 57.0) / 6.5)
            err += float(spec.get("err", 0)) * fi * 0.55
            if st.latency > timeout_s * 1000.0:
                err = min(100.0, err + 18.0)
            st.error_rate = clamp(err)

            avail = 1.0 if st.health != Health.DOWN.value else 0.0
            thr_mult = float(spec.get("thr_mult", 1.0))
            thr_mult = thr_mult + (1.0 - thr_mult) * (1.0 - fi)
            st.throughput = max(0.0, d["capacity"] * (st.load / 100.0) *
                                (1.0 - st.error_rate / 100.0) * thr_mult * avail)
            st.cpu = clamp(cpu)
            st.mem = clamp(mem)
            st.queue_depth = int(max(0, (st.load - 70) * (2.5 if st.fault else 1.0)) *
                                 (1 + st.error_rate / 20))
            _ = k

    def _update_health(self) -> None:
        for nid, st in self.nodes.items():
            prev = st.health
            if st.stress >= 88 or st.error_rate >= 93:
                new = Health.DOWN.value
            elif st.stress >= 62 or st.error_rate >= 45:
                new = Health.FAILING.value
            elif st.stress >= 35 or st.error_rate >= 8:
                new = Health.DEGRADED.value
            else:
                new = Health.HEALTHY.value

            if new != prev:
                st.last_state_change = self.t
                if new != Health.HEALTHY.value and st.first_anomaly is None:
                    st.first_anomaly = self.t
                elif new == Health.HEALTHY.value and not st.fault:
                    st.first_anomaly = None
                node = self.topology.node_map().get(nid)
                label = node.label if node else nid

                if new == Health.DEGRADED.value:
                    self._log("WARN", nid, f"degraded — latency {st.latency:.0f}ms, "
                                           f"errors {st.error_rate:.1f}%")
                    self._record_timeline("STATE", f"{label} degraded", node=nid,
                                          level="warn", detail=f"stress {st.stress:.0f}%")
                elif new == Health.FAILING.value:
                    self._log("ERROR", nid, f"failing — error rate {st.error_rate:.1f}%, "
                                            f"p99 {st.latency:.0f}ms")
                    self._record_timeline("STATE", f"{label} failing", node=nid,
                                          level="critical",
                                          detail=f"error rate {st.error_rate:.1f}%")
                elif new == Health.DOWN.value:
                    self._log("FATAL", nid, f"UNREACHABLE — health check failed "
                                            f"(err {st.error_rate:.0f}%)")
                    self._record_timeline("STATE", f"{label} DOWN", node=nid,
                                          level="critical", detail="health check failing")
                else:
                    self._log("INFO", nid, "recovered — back within SLO")
                    self._record_timeline("STATE", f"{label} recovered", node=nid,
                                          level="ok", detail="metrics normalised")
            st.health = new

    def _update_edges(self) -> None:
        for e in self.topology.edges:
            es = self.edge_states.get(e.id)
            src = self.nodes.get(e.source)
            tgt = self.nodes.get(e.target)
            if not es or not src or not tgt:
                continue
            if es.tripped:
                es.flow = max(0.0, es.flow - 25 * self.params.cascade_speed)
                es.propagated = 4.0
            else:
                coupling = (tgt.stress + src.stress) / 200.0
                es.propagated = coupling * e.resolved_weight() * 100.0
                es.flow = clamp(100.0 - coupling * 55.0 - max(src.error_rate, tgt.error_rate) * 0.4)
            if e.kind in (EdgeKind.ASYNC.value, EdgeKind.STREAM.value):
                backlog_rate = 0.0
                if tgt.stress > 40:
                    backlog_rate += (tgt.stress - 40) * 0.6
                if src.error_rate > 30:
                    backlog_rate += 0  # producer already failing
                es.backlog = int(max(0, es.backlog + (backlog_rate - 6) *
                                     self.params.cascade_speed))
                es.backlog = min(es.backlog, 50000)

    # ----------------------------------------------------------------- logs
    def _generate_logs(self) -> None:
        from .logs import generate_logs
        generate_logs(self)

    # --------------------------------------------------------------- history
    def _capture_history(self) -> None:
        if self.tick_count % 2 != 0:
            return
        snap = {
            "t": round(self.t, 2),
            "tick": self.tick_count,
            "nodes": {
                nid: [round(s.stress, 1), s.health, round(s.cpu, 1),
                      round(s.mem, 1), round(s.latency, 1), round(s.error_rate, 2),
                      round(s.throughput, 1), 1 if s.fault else 0]
                for nid, s in self.nodes.items()
            },
            "edges": {eid: [round(es.flow, 1), 1 if es.tripped else 0]
                      for eid, es in self.edge_states.items()},
        }
        self.history.append(snap)
        if len(self.history) > 900:
            del self.history[: len(self.history) - 900]

    def _update_uptime(self) -> None:
        total = len(self.nodes) or 1
        summary = self.health_summary()
        avail = (summary.get("healthy", 0) / total) * 100.0
        self.uptime_score = round(avail, 2)
        self.peak_blast = max(self.peak_blast, summary.get("blast_radius", 0))
        self.peak_down = max(self.peak_down, summary.get("down", 0))

    # ------------------------------------------------------------- snapshot
    def snapshot(self, include_history: bool = False) -> dict[str, Any]:
        nodes = []
        for n in self.topology.nodes:
            s = self.nodes[n.id]
            nodes.append({
                "id": n.id, "label": n.label, "kind": n.kind,
                "description": n.description, "layer": n.layer,
                "x": n.x, "y": n.y,
                "health": s.health, "stress": round(s.stress, 1),
                "cpu": round(s.cpu, 1), "mem": round(s.mem, 1),
                "latency": round(s.latency, 1),
                "error_rate": round(s.error_rate, 2),
                "throughput": round(s.throughput, 1),
                "load": round(s.load, 1), "queue": s.queue_depth,
                "replicas": s.replicas,
                "fault": s.fault, "parent": s.pressure_parent,
                "first_anomaly": s.first_anomaly,
                "pressure": round(s.pressure, 1),
            })
        edges = []
        for e in self.topology.edges:
            es = self.edge_states[e.id]
            edges.append({
                "id": e.id, "source": e.source, "target": e.target,
                "kind": e.kind, "weight": e.resolved_weight(),
                "flow": round(es.flow, 1), "tripped": es.tripped,
                "propagated": round(es.propagated, 1),
                "backlog": es.backlog, "latency": round(es.latency, 1),
            })

        payload: dict[str, Any] = {
            "sim_id": self.sim_id,
            "t": round(self.t, 2),
            "tick": self.tick_count,
            "topology": {"name": self.topology.name,
                         "description": self.topology.description,
                         "fingerprint": self.topology.fingerprint()},
            "nodes": nodes,
            "edges": edges,
            "params": self.params.to_dict(),
            "logs": self.logs[-160:],
            "timeline": self.timeline[-60:],
            "prediction": self.last_prediction,
            "recovery": self.last_recovery,
            "root_cause": self.root_cause_id,
            "health_summary": self.health_summary(),
            "uptime": self.uptime_score,
            "peaks": {"blast": self.peak_blast, "down": self.peak_down},
            "faults": FAULTS,
        }
        if include_history:
            payload["history"] = self.history
        return payload

    def health_summary(self) -> dict[str, int]:
        out = {"healthy": 0, "degraded": 0, "failing": 0, "down": 0}
        for s in self.nodes.values():
            out[s.health] = out.get(s.health, 0) + 1
        out["total"] = len(self.nodes)
        out["blast_radius"] = out["degraded"] + out["failing"] + out["down"]
        return out
