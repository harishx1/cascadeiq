"""Headless what-if runner.

Executes the *same* topology under two different parameter sets, entirely
offline, and returns comparable outcome metrics plus a blast-radius timeline
for charting. Used by the split-screen comparison panel.
"""

from __future__ import annotations

import time
from typing import Any

from .simulation import Simulation, SimParams
from .topology import Topology


def _metrics(sim: Simulation, series: list[dict[str, Any]]) -> dict[str, Any]:
    peak_blast = max((s["blast"] for s in series), default=0)
    peak_down = max((s["down"] for s in series), default=0)
    total = len(sim.nodes) or 1

    time_to_first_down = next((s["t"] for s in series if s["down"] > 0), None)

    # time until >=50% of the fleet is unhealthy
    time_to_half = next(
        (s["t"] for s in series if s["blast"] >= max(1, total * 0.5)), None)

    # area under the "unhealthy node" curve -> total service-units lost
    lost = sum(s["blast"] for s in series)
    availability = round(100.0 * (1.0 - lost / (total * max(1, len(series)))), 2)

    err_area = round(sum(s["err"] for s in series) / max(1, len(series)), 2)

    return {
        "peak_blast_radius": peak_blast,
        "peak_down": peak_down,
        "peak_blast_pct": round(peak_blast / total * 100, 1),
        "time_to_first_down": time_to_first_down,
        "time_to_half_fleet": time_to_half,
        "service_units_lost": lost,
        "availability": availability,
        "mean_error_rate": err_area,
        "survived": peak_down < total,
        "total_nodes": total,
    }


def run_variant(topology: Topology, base_params: dict[str, Any],
                overrides: dict[str, Any], fault: dict[str, Any],
                duration: float = 45.0, seed: int = 7) -> dict[str, Any]:
    """Run one scenario and return its metrics + timeline series."""
    merged = {**base_params, **overrides}
    merged["chaos_mode"] = False
    merged["paused"] = False
    merged["playback"] = 1.0
    merged["cascade_speed"] = float(merged.get("cascade_speed", 1.0))

    sim = Simulation(topology.clone(), SimParams.from_dict(merged), seed=seed)

    series: list[dict[str, Any]] = []
    fault_at = float(fault.get("at", 3.0))
    injected = False
    t = 0.0
    dt = 0.2

    while t < duration:
        if not injected and t >= fault_at:
            sim.inject_fault(
                fault["node"],
                fault.get("type", merged.get("fault_type", "cpu_spike")),
                int(fault.get("severity", merged.get("severity", 5))),
            )
            injected = True
        sim.step(real_dt=dt)
        t += dt * float(merged["cascade_speed"]) * float(merged.get("playback", 1.0))

        if int(t * 5) % 2 == 0:
            s = sim.health_summary()
            mean_err = sum(nd.error_rate for nd in sim.nodes.values())
            mean_err = mean_err / max(1, len(sim.nodes))
            series.append({
                "t": round(t, 2),
                "blast": s.get("blast_radius", 0),
                "down": s.get("down", 0),
                "err": round(mean_err, 2),
            })

    # de-duplicate the coalesced sampling
    deduped: list[dict[str, Any]] = []
    for row in series:
        if not deduped or deduped[-1]["t"] != row["t"]:
            deduped.append(row)

    return {
        "params": {k: v for k, v in merged.items()},
        "metrics": _metrics(sim, deduped),
        "series": deduped[:: max(1, len(deduped) // 90)] if deduped else [],
        "final_nodes": {
            nid: {"health": s.health, "stress": round(s.stress, 1),
                  "error_rate": round(s.error_rate, 2)}
            for nid, s in sim.nodes.items()
        },
        "timeline": sim.timeline[-25:],
    }


def compare(topology: Topology, base_params: dict[str, Any],
            variant_a: dict[str, Any], variant_b: dict[str, Any],
            fault: dict[str, Any], duration: float = 45.0) -> dict[str, Any]:
    start = time.perf_counter()
    a = run_variant(topology, base_params, variant_a, fault, duration, seed=7)
    b = run_variant(topology, base_params, variant_b, fault, duration, seed=7)
    elapsed = round((time.perf_counter() - start) * 1000, 1)

    ma, mb = a["metrics"], b["metrics"]

    def delta(key: str) -> float | None:
        va, vb = ma.get(key), mb.get(key)
        if va is None or vb is None:
            return None
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            return round(vb - va, 2)
        return None

    return {
        "label_a": variant_a.get("_label", "Variant A"),
        "label_b": variant_b.get("_label", "Variant B"),
        "fault": fault,
        "duration": duration,
        "a": a,
        "b": b,
        "delta": {
            "availability": delta("availability"),
            "peak_blast_radius": delta("peak_blast_radius"),
            "peak_down": delta("peak_down"),
            "service_units_lost": delta("service_units_lost"),
            "mean_error_rate": delta("mean_error_rate"),
        },
        "computed_in_ms": elapsed,
    }
