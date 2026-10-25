"""Realistic log synthesis.

Log lines are generated from *node kind* + *observable state* + *active fault*,
never from a hardcoded list of application names — so a topology the engine has
never seen still produces believable output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .simulation import Simulation

from .simulation import FAULTS

LEVEL_BY_HEALTH = {
    "healthy": "INFO",
    "degraded": "WARN",
    "failing": "ERROR",
    "down": "FATAL",
}

AMBIENT: dict[str, list[str]] = {
    "gateway": [
        "GET /api/{resource} 200 {ms}ms — bucket tokens 942/1000",
        "upstream pool: {n} active, {n2} idle, 0 queued",
        "tls session resumed for {ip} — handshake {ms2}ms",
    ],
    "service": [
        "GET /v1/{resource} 200 in {ms}ms (p50 {ms2}ms)",
        "processed {n} events in {ms}ms — batch ok",
        "connection pool {n}/{n2} in use, 0 waiting",
        "published heartbeat — revision {rev}",
    ],
    "function": [
        "invocation {rev} completed in {ms}ms — memory 128MiB",
        "cold start {ms}ms, handler ready",
        "concurrency {n}/{n2} — no throttling",
    ],
    "database": [
        "checkpoint complete: wrote {n} buffers in {ms}ms",
        "autovacuum scanned table public.{resource} ({n2} pages)",
        "replication lag {ms2}ms — streaming from primary",
        "connection accepted from 10.4.{n}.{n2}:5432",
    ],
    "cache": [
        "hit ratio 97.{n}% over 10,000 ops",
        "expired {n} keys (ttl sweep)",
        "used 3.{n}GiB of 8.0GiB — no eviction pressure",
    ],
    "queue": [
        "committed offset {rev} partition {n}",
        "consumer lag {n2} msgs — within tolerance",
        "retention purge removed {n} segments",
    ],
    "stream": [
        "rebalanced partition {n} → consumer-{n2}",
        "ISR stable: {n} replicas in sync",
        "produced {n2} records to topic {resource}",
    ],
    "storage": [
        "PUT /objects/{resource} 201 — {n} bytes",
        "lifecycle policy moved {n} objects to cold tier",
        "fsync {ms}ms — volume healthy",
    ],
    "external": [
        "200 OK from provider rtt {ms}ms",
        "rate-limit remaining {n} of 5000",
        "webhook delivered — attempt 1, 202 accepted",
    ],
    "ml": [
        "batch inference complete: {n} rows in {ms}ms",
        "feature vector cache warm — {n2} distinct keys",
        "model serving p95 {ms2}ms",
    ],
}

DEGRADED: dict[str, list[str]] = {
    "gateway": [
        "upstream {res} returning 502 — retrying ({n}/3)",
        "request queue depth {n2}, shedding {n} req/s",
        "pool exhausted for upstream {res}",
    ],
    "service": [
        "p99 latency {ms}ms exceeds SLO 250ms (window=60s)",
        "thread pool at {n2}% capacity",
        "dependency call to {res} took {ms}ms (budget 40ms)",
        "retry {n}/3 against {res} — backing off 400ms",
    ],
    "function": [
        "handler timeout at {ms}ms — attempt {n}/3",
        "throttled: concurrency at limit",
    ],
    "database": [
        "connection pool exhausted ({n}/{n2}), {n} waiting",
        "query exceeded statement_timeout: SELECT * FROM {resource}",
        "replication lag {ms}ms — write backlog forming",
        "lock wait detected on relation {resource}",
    ],
    "cache": [
        "hit ratio dropped to {n2}%",
        "eviction pressure: {n} keys/sec evicted",
    ],
    "queue": [
        "consumer lag growing: {n} messages behind",
        "producer ack latency {ms}ms (was {ms2}ms)",
    ],
    "stream": [
        "under-replicated: {n} partitions missing ISR",
        "producer request timeout after {ms}ms",
    ],
    "storage": [
        "write latency {ms}ms — volume degraded",
        "free space {n2}% — approaching threshold",
    ],
    "external": [
        "provider responding in {ms}ms (baseline {ms2}ms)",
        "rate-limit exhausted — queuing {n} requests",
    ],
    "ml": [
        "feature store read {ms}ms — model serving degraded",
        "inference timeout {ms}ms, falling back to cached scores",
    ],
}

FAILING: dict[str, list[str]] = {
    "gateway": [
        "5xx rate {n2}% in last 15s",
        "all upstreams for {res} unhealthy — returning 503",
    ],
    "service": [
        "upstream timeout after {ms}ms — retry {n}/3 exhausted",
        "5xx rate {n2}% in last 15s",
        "unhandled exception connecting to {res}: connection reset by peer",
    ],
    "function": [
        "invocation failed — {res} unavailable",
        "dead-lettered {n} messages after max retries",
    ],
    "database": [
        "ECONNREFUSED 10.4.{n}.{n2}:5432 — cannot accept new sessions",
        "write path failing: cannot flush WAL",
        "{n} sessions waiting on lock held by cancelled backend",
    ],
    "cache": [
        "connection refused — cache node marked critical",
    ],
    "queue": [
        "no leader for partition {n} — produce requests failing",
        "consumer group {res} lost assignment",
    ],
    "stream": [
        "producer delivery failed: not enough in-sync replicas",
    ],
    "storage": [
        "I/O error on volume — reads returning EIO",
    ],
    "external": [
        "provider returned 503 — circuit consideration in {ms}ms",
    ],
    "ml": [
        "model backend unreachable — no fallback available",
    ],
}

DOWN: dict[str, list[str]] = {
    "gateway": ["health check failed 3/3 — node removed from balancer"],
    "service": [
        "health check failed 3/3 — node removed from balancer",
        "process terminated (exit code 137)",
        "accept() stopped — no listening socket",
    ],
    "function": ["executor lost — draining queued invocations"],
    "database": [
        "postmaster shut down — refusing connections",
        "ECONNREFUSED for all pooled connections",
    ],
    "cache": ["server closed all connections — node marked dead"],
    "queue": ["broker unreachable — cluster unavailable"],
    "stream": ["topic unreplicated — produce requests rejected"],
    "storage": ["volume detached — filesystem read-only"],
    "external": ["provider unreachable after 3 consecutive failures"],
    "ml": ["model server crashed — inference returning 503"],
}

FAULT_LINES: dict[str, list[str]] = {
    "cpu_spike": [
        "CPU {pct}% across {n} cores — run queue {n2}",
        "request queue depth {n2}, rejecting new work",
        "scheduler latency {ms}ms (baseline {ms2}ms)",
    ],
    "memory_leak": [
        "heap {pct}GiB/{tot}GiB — GC pause {ms}ms",
        "allocation failure after {n2} retries",
        "RSS growth {n}MiB/min over last 10 windows",
    ],
    "network_latency": [
        "RTT to peer {ms}ms (baseline {ms2}ms), 2.1% packet loss",
        "retransmits {n2} in 5s — TCP window collapsed",
        "keepalive missed {n} consecutive beats",
    ],
    "disk_full": [
        "write failed: ENOSPC on /var/lib/data (0 bytes free)",
        "WAL cannot be flushed — volume at 100%",
        "fsync returned EIO — {n2} ops queued",
    ],
    "auth_expiry": [
        "401 Unauthorized: token expired (exp=1761234000)",
        "credential validation rejected by identity provider",
        "signature verification failed for service token",
    ],
    "slow_query": [
        "lock wait timeout: relation {resource} blocked by pid {n2}",
        "sequential scan of {resource} took {ms}ms",
        "connection pool saturated by long-running statement",
    ],
    "oom_kill": [
        "kernel killed process {n2} (exit OOM)",
        "cgroup memory limit exceeded — {pct}GiB used",
        "restart attempt {n} after fatal allocation error",
    ],
    "cache_eviction": [
        "eviction storm: {n2} evictions/min, hit ratio {pct}%",
        "maxmemory reached — rejecting writes with OOM command",
        "origin absorbing full read load ({n} req/s)",
    ],
}


def _fill(template: str, rng: Any) -> str:
    return (
        template.replace("{ms}", str(rng.randint(40, 2400)))
        .replace("{ms2}", str(rng.randint(4, 90)))
        .replace("{n2}", str(rng.randint(5, 999)))
        .replace("{n}", str(rng.randint(2, 40)))
        .replace("{rev}", str(rng.randint(100000, 999999)))
        .replace("{pct}", str(rng.randint(88, 99)))
        .replace("{tot}", str(rng.choice([4, 8, 16])))
        .replace("{ip}", f"10.{rng.randint(0, 9)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}")
        .replace("{resource}", rng.choice(
            ["orders", "users", "sessions", "payments", "inventory", "events", "products"]))
        .replace("{res}", rng.choice(
            ["catalog", "ledger", "identity", "inventory", "notify", "session"]))
    )


def _pick(rng: Any, pool: list[str]) -> str:
    return _fill(rng.choice(pool), rng)


def generate_logs(sim: "Simulation") -> None:
    rng = sim.rng
    p = sim.params

    hot = [(nid, s) for nid, s in sim.nodes.items() if s.stress > 14]
    hot.sort(key=lambda kv: kv[1].stress, reverse=True)

    # 1. fault-specific chatter on the nodes actually carrying a fault
    for nid, s in hot:
        if s.fault and rng.random() < 0.55:
            lines = FAULT_LINES.get(s.fault["type"], [])
            if lines:
                sim._log("ERROR", nid, _pick(rng, lines), fault=s.fault["type"])

    # 2. state-driven chatter on the worst-affected nodes
    picks = hot[:6]
    for nid, s in picks:
        chance = 0.10 + (s.stress / 100.0) * 0.55
        if rng.random() > chance:
            continue
        if s.health == "down":
            pool = DOWN.get(_kind_of(sim, nid), DOWN["service"])
            sim._log("FATAL", nid, _pick(rng, pool))
        elif s.health == "failing":
            pool = FAILING.get(_kind_of(sim, nid), FAILING["service"])
            sim._log("ERROR", nid, _pick(rng, pool))
        elif s.health == "degraded":
            pool = DEGRADED.get(_kind_of(sim, nid), DEGRADED["service"])
            sim._log("WARN", nid, _pick(rng, pool))

    # 3. ambient healthy traffic so the stream never looks dead
    ambient_budget = 1 if p.traffic > 20 else 0
    healthy = [nid for nid, s in sim.nodes.items() if s.health == "healthy"]
    if healthy and rng.random() < 0.7:
        for _ in range(ambient_budget):
            nid = rng.choice(healthy)
            kind = _kind_of(sim, nid)
            pool = AMBIENT.get(kind, AMBIENT["service"])
            sim._log("INFO", nid, _pick(rng, pool))

    # 4. occasional analytics-style lines from the predictor
    if sim.tick_count % 40 == 0 and sim.last_prediction:
        pred = sim.last_prediction
        if pred.get("root_cause_id"):
            rc = pred["root_cause"]
            sim._log("INFO", "predictor",
                     f"root-cause isolation → {rc['label']} "
                     f"({rc['confidence']:.0f}% confidence, reach {rc['reach']})")
        elif pred.get("next_failures"):
            nf = pred["next_failures"][0]
            sim._log("INFO", "predictor",
                     f"forecast: {nf['label']} at risk — {nf['confidence']:.0f}% "
                     f"within ~{nf['eta']:.0f}s")


def _kind_of(sim: "Simulation", nid: str) -> str:
    node = sim.topology.node_map().get(nid)
    return node.kind if node else "service"
