"""Recovery strategy builder.

Given the *current* state of any simulation, score a set of generic recovery
strategies against (a) the observed fault class, (b) the node roles involved
and (c) the resilience features that are actually switched on.

The output is a ranked, explainable comparison — not a fixed answer.
"""

from __future__ import annotations

from typing import Any

from .simulation import FAULTS, Health, Simulation

# How well each strategy suits each fault class (percentage-point bonus).
FAULT_MATCH: dict[str, dict[str, float]] = {
    "cpu_spike":      {"restart": 24, "rollback": 12, "failover": 6,  "isolate": 18},
    "memory_leak":    {"restart": 28, "rollback": 18, "failover": 4,  "isolate": 12},
    "network_latency": {"restart": 2,  "rollback": 0,  "failover": 28, "isolate": 26},
    "disk_full":      {"restart": -10, "rollback": 4,  "failover": 26, "isolate": 22},
    "auth_expiry":    {"restart": 4,  "rollback": 32, "failover": 4,  "isolate": 8},
    "slow_query":     {"restart": 6,  "rollback": 30, "failover": 14, "isolate": 14},
    "oom_kill":       {"restart": 26, "rollback": 14, "failover": 14, "isolate": 14},
    "cache_eviction": {"restart": 16, "rollback": 4,  "failover": 22, "isolate": 18},
    "unknown":        {"restart": 10, "rollback": 10, "failover": 12, "isolate": 14},
}

# How well each strategy suits the *role* of the failing node.
KIND_MATCH: dict[str, dict[str, float]] = {
    "restart":   {"database": -20, "external": -26, "cache": 6,  "queue": 4,
                  "stream": 4, "storage": -6, "service": 10, "function": 10,
                  "gateway": 8, "ml": 6},
    "rollback":  {"database": -8,  "external": -18, "cache": 0,  "queue": 2,
                  "stream": 2, "storage": -4, "service": 12, "function": 10,
                  "gateway": 8, "ml": 8},
    "failover":  {"database": 14, "external": 16, "cache": 8,  "queue": 6,
                  "stream": 6, "storage": 18, "service": 6, "function": 4,
                  "gateway": 8, "ml": 4},
    "isolate":   {"database": 8,  "external": 14, "cache": 6,  "queue": 8,
                  "stream": 8, "storage": 6, "service": 12, "function": 10,
                  "gateway": 14, "ml": 8},
}

STRATEGY_META: dict[str, dict[str, Any]] = {
    "isolate": {
        "label": "Isolate & shed load",
        "icon": "shield",
        "blurb": "Open circuit breakers on failing links and throttle the edge so "
                 "healthy services stop absorbing the blast.",
        "base_success": 46, "base_risk": 14, "base_time": 4.0,
        "steps": [
            "Open breakers on every link carrying >55% failure pressure",
            "Shed traffic above the rate-limit threshold at the gateway",
            "Hold degraded nodes on cached / fallback responses",
        ],
        "tradeoffs": [
            "Does not repair the underlying fault",
            "Some requests return degraded responses while isolated",
        ],
        "effects": {"break_edges": True, "trip_pressure": True, "relief": 48,
                    "clear_fault": False},
    },
    "restart": {
        "label": "Rolling restart of affected services",
        "icon": "refresh",
        "blurb": "Recycle the failing processes one pod at a time so capacity is "
                 "never fully removed.",
        "base_success": 54, "base_risk": 30, "base_time": 14.0,
        "steps": [
            "Drain connections from the affected deployment",
            "Terminate and replace one replica at a time",
            "Re-run health checks before advancing to the next replica",
        ],
        "tradeoffs": [
            "In-flight requests on the recycled replica are dropped",
            "Only helps if the fault is process-local (not data or network)",
        ],
        "effects": {"clear_fault": True, "relief": 76},
    },
    "rollback": {
        "label": "Roll back to last known good release",
        "icon": "undo",
        "blurb": "Revert the most recent deployment — the fastest fix when the "
                 "fault is code, config or credential related.",
        "base_success": 56, "base_risk": 24, "base_time": 20.0,
        "steps": [
            "Identify the last release that held a clean baseline",
            "Shift traffic to the previous image tag",
            "Revoke and reissue any credentials issued by the bad release",
        ],
        "tradeoffs": [
            "Recent feature work is temporarily unavailable",
            "Useless when the fault is infrastructure-side",
        ],
        "effects": {"clear_fault": True, "relief": 86},
    },
    "failover": {
        "label": "Fail over to standby / scale out",
        "icon": "swap",
        "blurb": "Promote the standby replica and add capacity so the surviving "
                 "path absorbs the traffic.",
        "base_success": 52, "base_risk": 40, "base_time": 26.0,
        "steps": [
            "Promote the standby and confirm replication caught up",
            "Add replicas to the affected deployment",
            "Re-point dependency connection strings at the standby",
        ],
        "tradeoffs": [
            "Possible write loss between primary and standby",
            "Longest time to complete of the four options",
        ],
        "effects": {"clear_fault": True, "relief": 70, "scale_out": True},
    },
}


def build_strategies(sim: Simulation) -> list[dict[str, Any]]:
    p = sim.params
    anomalous = [nid for nid, s in sim.nodes.items() if s.is_anomalous]
    down = [nid for nid, s in sim.nodes.items() if s.health == Health.DOWN.value]
    if not anomalous:
        anomalous = [nid for nid, s in sim.nodes.items() if s.stress > 18]

    # which fault are we actually fighting?
    fault_type = "unknown"
    root_kind = "service"
    root_id = sim.root_cause_id
    if root_id and sim.nodes[root_id].fault:
        fault_type = sim.nodes[root_id].fault["type"]
    elif root_id is None:
        for nid in anomalous:
            if sim.nodes[nid].fault:
                fault_type = sim.nodes[nid].fault["type"]
                root_id = root_id or nid
                break
    elif root_id and not sim.nodes[root_id].fault:
        # root cause is a *victim* of an upstream fault — look for any live fault
        for nid, s in sim.nodes.items():
            if s.fault:
                fault_type = s.fault["type"]
                root_id = nid
                break
    if root_id and root_id in sim.topology.node_map():
        root_kind = sim.topology.node_map()[root_id].kind

    severity = p.severity if fault_type != "unknown" else 4
    n_affected = len(anomalous)
    n_down = len(down)
    fault_label = FAULTS.get(fault_type, {}).get("label", "unclassified anomaly")

    # targets each strategy would actually act on
    def targets_for(strategy: str) -> list[str]:
        if not anomalous:
            return []
        if strategy == "restart":
            return [n for n in anomalous
                    if sim.topology.node_map()[n].kind not in ("external",)]
        if strategy == "failover":
            return [n for n in anomalous
                    if sim.topology.node_map()[n].kind in
                    ("database", "cache", "storage", "queue", "stream", "service")]
        if strategy == "rollback":
            return [n for n in anomalous
                    if sim.topology.node_map()[n].kind not in ("external", "database")]
        return anomalous

    strategies: list[dict[str, Any]] = []
    for sid, meta in STRATEGY_META.items():
        targets = targets_for(sid)
        matched_targets = targets if targets else anomalous

        bonus = FAULT_MATCH.get(fault_type, FAULT_MATCH["unknown"]).get(sid, 0.0)
        bonus += KIND_MATCH.get(sid, {}).get(root_kind, 0.0)

        # resilience features already in play change the calculus
        if sid == "isolate":
            if p.circuit_breaker:
                bonus += 12
            if p.rate_limiting:
                bonus += 8
            if p.retry_aggression <= 1.0:
                bonus += 5
        if sid == "restart" and p.retry_aggression >= 3.0:
            bonus -= 6      # restarts with aggressive retries tend to re-break
        if sid == "failover" and n_down >= 3:
            bonus += 6
        if sid == "rollback" and fault_type in ("auth_expiry", "slow_query"):
            bonus += 6

        success = meta["base_success"] + bonus
        success -= max(0, (severity - 5)) * 2.4
        success += max(0, 6 - n_affected) * 1.2
        if not targets and sid in ("restart", "rollback", "failover"):
            success -= 12
        success = max(6.0, min(97.0, success))

        risk = meta["base_risk"] + max(0, (severity - 5)) * 2.0
        if root_kind == "database" and sid == "restart":
            risk += 14
        if sid == "failover":
            risk += max(0, n_down - 2) * 3.0
        if sid == "isolate" and n_affected > 4:
            risk += 5
        risk = max(4.0, min(92.0, risk))

        est_time = meta["base_time"] + len(matched_targets) * (
            3.2 if sid == "restart" else 2.2 if sid == "rollback"
            else 4.0 if sid == "failover" else 0.6)
        if sid == "failover" and p.circuit_breaker:
            est_time *= 0.85
        est_time = round(est_time, 1)

        # what the world looks like afterwards
        if sid == "isolate":
            blast_after = round(n_down * 0.55 + max(0, n_affected - n_down) * 0.5)
        elif sid == "restart":
            blast_after = round(n_affected * (0.34 - success / 400.0))
        elif sid == "rollback":
            blast_after = round(n_affected * (0.26 - success / 460.0))
        else:
            blast_after = round(n_affected * (0.30 - success / 430.0))
        blast_after = max(0, blast_after)

        # overall desirability: success dominates, then blast radius, then speed
        score = (success * 1.0
                 - risk * 0.30
                 - blast_after * 2.6
                 - min(est_time, 60) * 0.42)

        strategies.append({
            "id": sid,
            "label": meta["label"],
            "icon": meta["icon"],
            "blurb": meta["blurb"],
            "steps": meta["steps"],
            "tradeoffs": meta["tradeoffs"],
            "success": round(success, 1),
            "risk": round(risk, 1),
            "estimated_time": est_time,
            "blast_radius_after": blast_after,
            "targets": matched_targets,
            "target_count": len(matched_targets),
            "score": round(score, 2),
            "effects": meta["effects"],
            "matched_fault": fault_label,
        })

    strategies.sort(key=lambda s: s["score"], reverse=True)
    for i, s in enumerate(strategies):
        s["recommended"] = i == 0
        s["rank"] = i + 1

    factors = [
        f"Fighting: {fault_label} on a {root_kind} node",
        f"{n_affected} node(s) currently degraded, {n_down} fully down",
        f"Circuit breakers {'ENABLED' if p.circuit_breaker else 'disabled'}",
        f"Rate limiting {'ENABLED' if p.rate_limiting else 'disabled'}",
        f"Retry aggression {p.retry_aggression:g}× (amplification "
        f"{'high' if p.retry_aggression > 2.5 else 'moderate' if p.retry_aggression > 1 else 'low'})",
        f"Fault severity {severity}/10",
    ]

    out = {
        "generated_at": round(sim.t, 2),
        "fault_class": fault_label,
        "fault_type": fault_type,
        "root_kind": root_kind,
        "root_cause_id": root_id,
        "strategies": strategies,
        "decision_factors": factors,
        "recommended_id": strategies[0]["id"] if strategies else None,
        "rationale": _rationale(strategies, fault_label, root_kind, n_affected),
    }
    sim.last_recovery = out
    return out


def _rationale(strategies: list[dict[str, Any]], fault_label: str,
               root_kind: str, n_affected: int) -> str:
    if not strategies:
        return "No action required — all services within baseline."
    best = strategies[0]
    runner = strategies[1] if len(strategies) > 1 else None
    msg = (f"{best['label']} is recommended for a {fault_label.lower()} on a "
           f"{root_kind}: {best['success']:.0f}% projected success at "
           f"{best['estimated_time']}s, leaving ~{best['blast_radius_after']} "
           f"node(s) affected.")
    if runner:
        msg += (f" {runner['label']} scores lower ({runner['success']:.0f}% "
                f"success, {runner['risk']:.0f}% risk).")
    if n_affected:
        msg += " All estimates are recomputed every tick from live graph state."
    return msg
