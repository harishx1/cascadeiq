"""Topology model — a fully generic, technology-agnostic dependency graph.

Design principle (this is the answer to "how does it adapt to any app?"):
the prediction engine never inspects *what* a service does or *what* language
it is written in. It only ever sees a directed graph of nodes (any unit of
work) and edges (any dependency), plus a handful of universally observable
signals: latency, error rate, throughput, CPU, memory.

Any architecture — monolith, microservices, event-driven, serverless,
batch/ETL, IoT pipelines — can be expressed in this model.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

TOPOLOGY_DIR = Path(__file__).resolve().parent.parent / "topologies"


class NodeKind(str, Enum):
    """What kind of thing a node is. Purely cosmetic + sets base metrics."""

    GATEWAY = "gateway"
    SERVICE = "service"
    FUNCTION = "function"      # serverless / worker
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"
    STREAM = "stream"
    STORAGE = "storage"
    EXTERNAL = "external"
    ML = "ml"


class EdgeKind(str, Enum):
    """How two nodes talk to each other. Drives propagation behaviour."""

    SYNC = "sync"        # blocking HTTP / gRPC call
    ASYNC = "async"      # queue / job hand-off (failure is delayed + buffered)
    DB = "db"            # data dependency (contagious, slow to recover)
    CACHE = "cache"      # cache reads (degrades gracefully)
    STREAM = "stream"    # event bus / pub-sub


class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"
    DOWN = "down"

    @property
    def rank(self) -> int:
        return {"healthy": 0, "degraded": 1, "failing": 2, "down": 3}[self.value]


# Base characteristics per node kind. Chosen so that databases, caches and
# queues *look* different on the dashboard without any kind-specific logic in
# the predictor — the predictor only ever reads the derived numbers.
KIND_DEFAULTS: dict[str, dict[str, float]] = {
    "gateway":  {"base_latency": 12,  "base_cpu": 22, "base_mem": 34, "capacity": 900, "criticality": 1.0},
    "service":  {"base_latency": 45,  "base_cpu": 38, "base_mem": 48, "capacity": 520, "criticality": 0.8},
    "function": {"base_latency": 90,  "base_cpu": 26, "base_mem": 30, "capacity": 340, "criticality": 0.6},
    "database": {"base_latency": 8,   "base_cpu": 44, "base_mem": 62, "capacity": 700, "criticality": 1.0},
    "cache":    {"base_latency": 2,   "base_cpu": 18, "base_mem": 70, "capacity": 1200, "criticality": 0.5},
    "queue":    {"base_latency": 5,   "base_cpu": 20, "base_mem": 40, "capacity": 800, "criticality": 0.7},
    "stream":   {"base_latency": 6,   "base_cpu": 28, "base_mem": 52, "capacity": 950, "criticality": 0.7},
    "storage":  {"base_latency": 30,  "base_cpu": 15, "base_mem": 25, "capacity": 400, "criticality": 0.8},
    "external": {"base_latency": 140, "base_cpu": 0,  "base_mem": 0,  "capacity": 300, "criticality": 0.4},
    "ml":       {"base_latency": 220, "base_cpu": 70, "base_mem": 66, "capacity": 180, "criticality": 0.6},
}

EDGE_DEFAULTS: dict[str, dict[str, float]] = {
    # weight = how strongly a fault on the target stresses the source
    # latency = how long the dependency call takes (drives timeout behaviour)
    "sync":   {"weight": 0.9,  "latency": 45},
    "async":  {"weight": 0.55, "latency": 8},
    "db":     {"weight": 0.95, "latency": 20},
    "cache":  {"weight": 0.5,  "latency": 3},
    "stream": {"weight": 0.5,  "latency": 10},
}

# Once a dependency is genuinely *down* its dependents get slammed regardless
# of protocol — the link is gone. This is the tipping point that turns gradual
# degradation into an accelerating cascade, and it works identically for a
# synchronous call, a Kafka topic or a database.
DEAD_BOOST: dict[str, float] = {
    "sync": 38.0, "db": 42.0, "cache": 26.0, "async": 34.0, "stream": 36.0,
}
DEGRADED_FACTOR = {"healthy": 0.0, "degraded": 0.0, "failing": 0.5, "down": 1.0}


@dataclass
class Node:
    id: str
    label: str
    kind: str = NodeKind.SERVICE.value
    description: str = ""
    layer: int = 0
    x: float | None = None
    y: float | None = None

    def defaults(self) -> dict[str, float]:
        return KIND_DEFAULTS.get(self.kind, KIND_DEFAULTS["service"])


@dataclass
class Edge:
    id: str
    source: str   # caller  (the dependent)
    target: str   # callee  (the dependency)
    kind: str = EdgeKind.SYNC.value
    weight: float | None = None   # None -> derive from kind
    latency: float | None = None

    def resolved_weight(self) -> float:
        if self.weight is not None:
            return float(self.weight)
        return EDGE_DEFAULTS.get(self.kind, EDGE_DEFAULTS["sync"])["weight"]

    def resolved_latency(self) -> float:
        if self.latency is not None:
            return float(self.latency)
        return EDGE_DEFAULTS.get(self.kind, EDGE_DEFAULTS["sync"])["latency"]


@dataclass
class Topology:
    """A technology-agnostic service dependency graph."""

    name: str
    description: str = ""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    _cache: dict[str, "Node"] | None = field(default=None, init=False, repr=False)
    _cache_len: int = field(default=-1, init=False, repr=False)

    # ---- lookups -----------------------------------------------------
    def node_map(self) -> dict[str, "Node"]:
        """Memoised id -> Node lookup (called in the inner simulation loops)."""
        if self._cache is None or self._cache_len != len(self.nodes):
            self._cache = {n.id: n for n in self.nodes}
            self._cache_len = len(self.nodes)
        return self._cache

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}

    def out_edges(self) -> dict[str, list[Edge]]:
        """node -> edges where it is the caller (its dependencies)."""
        m: dict[str, list[Edge]] = {n.id: [] for n in self.nodes}
        for e in self.edges:
            if e.source in m:
                m[e.source].append(e)
        return m

    def in_edges(self) -> dict[str, list[Edge]]:
        """node -> edges where it is the callee (its callers / dependents)."""
        m: dict[str, list[Edge]] = {n.id: [] for n in self.nodes}
        for e in self.edges:
            if e.target in m:
                m[e.target].append(e)
        return m

    # ---- construction ------------------------------------------------
    def compute_layers(self) -> None:
        """Assign each node a depth: 0 = depends on nothing (leaf/external).

        Layout puts high-depth nodes (gateways / entry points) at the top and
        low-depth nodes (databases, external APIs) at the bottom — the classic
        architecture-diagram convention.
        """
        outs = self.out_edges()
        depth: dict[str, int] = {}
        visiting: set[str] = set()

        def dfs(nid: str) -> int:
            if nid in depth:
                return depth[nid]
            if nid in visiting:      # cycle guard (event loops can be cyclic)
                return 0
            visiting.add(nid)
            d = 0
            for e in outs.get(nid, []):
                if e.target in self.node_ids():
                    d = max(d, 1 + dfs(e.target))
            visiting.discard(nid)
            depth[nid] = d
            return d

        for n in self.nodes:
            dfs(n.id)
        max_d = max(depth.values(), default=0)
        for n in self.nodes:
            # invert: gateway (max depth) -> layer 0 (top)
            n.layer = max_d - depth.get(n.id, 0)

    def errors(self) -> list[str]:
        """Structural problems that make the graph unusable."""
        issues: list[str] = []
        ids = self.node_ids()
        if not self.nodes:
            issues.append("Topology has no nodes.")
        seen_edges: set[tuple[str, str]] = set()
        for e in self.edges:
            if e.source not in ids:
                issues.append(f"Edge {e.id}: unknown source '{e.source}'.")
            if e.target not in ids:
                issues.append(f"Edge {e.id}: unknown target '{e.target}'.")
            if e.source == e.target:
                issues.append(f"Edge {e.id}: self-loop on '{e.source}'.")
            key = (e.source, e.target)
            if key in seen_edges:
                issues.append(f"Duplicate edge {e.source} -> {e.target}.")
            seen_edges.add(key)
        return issues

    def warnings(self) -> list[str]:
        """Advisory issues — the graph still runs, it just has loose ends."""
        orphans = [n.id for n in self.nodes
                   if not self.out_edges()[n.id] and not self.in_edges()[n.id]]
        return [f"Unconnected nodes: {', '.join(orphans)}."] if orphans else []

    def validate(self) -> list[str]:
        return self.errors() + self.warnings()

    def fingerprint(self) -> str:
        raw = json.dumps(
            {
                "nodes": sorted(n.id for n in self.nodes),
                "edges": sorted(f"{e.source}->{e.target}" for e in self.edges),
            },
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    # ---- (de)serialisation ------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "meta": self.meta,
            "nodes": [
                {
                    "id": n.id, "label": n.label, "kind": n.kind,
                    "description": n.description, "layer": n.layer,
                    "x": n.x, "y": n.y,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "id": e.id, "source": e.source, "target": e.target,
                    "kind": e.kind, "weight": e.weight, "latency": e.latency,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Topology":
        nodes = [
            Node(
                id=str(d["id"]),
                label=str(d.get("label", d["id"])),
                kind=str(d.get("kind", NodeKind.SERVICE.value)),
                description=str(d.get("description", "")),
                layer=int(d.get("layer", 0)),
                x=d.get("x"),
                y=d.get("y"),
            )
            for d in data.get("nodes", [])
        ]
        edges = []
        for i, d in enumerate(data.get("edges", [])):
            edges.append(
                Edge(
                    id=str(d.get("id", f"e{i}")),
                    source=str(d["source"]),
                    target=str(d["target"]),
                    kind=str(d.get("kind", EdgeKind.SYNC.value)),
                    weight=d.get("weight"),
                    latency=d.get("latency"),
                )
            )
        topo = cls(
            name=str(data.get("name", "Untitled topology")),
            description=str(data.get("description", "")),
            nodes=nodes,
            edges=edges,
            meta=dict(data.get("meta", {})),
        )
        topo.compute_layers()
        return topo

    def clone(self) -> "Topology":
        return Topology.from_dict(copy.deepcopy(self.to_dict()))


# --------------------------------------------------------------------------
# Preset registry — every preset is just data, proving nothing is hardcoded.
# --------------------------------------------------------------------------

def list_topologies() -> list[dict[str, Any]]:
    out = []
    if not TOPOLOGY_DIR.exists():
        return out
    for path in sorted(TOPOLOGY_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "id": path.stem,
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "nodes": len(data.get("nodes", [])),
                "edges": len(data.get("edges", [])),
                "tags": data.get("meta", {}).get("tags", []),
            }
        )
    return out


def load_topology(topology_id: str) -> Topology:
    """Load a preset by id, or parse `topology_id` inline as JSON."""
    safe = Path(topology_id).name
    path = TOPOLOGY_DIR / f"{safe}.json"
    if path.exists():
        return Topology.from_dict(json.loads(path.read_text(encoding="utf-8")))
    if topology_id.lstrip().startswith("{"):
        return Topology.from_dict(json.loads(topology_id))
    raise FileNotFoundError(f"Unknown topology '{topology_id}'.")


def topology_from_json(text: str) -> Topology:
    """Parse arbitrary JSON/YAML-ish topology supplied by the user."""
    data = json.loads(text)
    if isinstance(data, list):            # bare list of nodes
        data = {"name": "Imported topology", "nodes": data, "edges": []}
    elif "services" in data and "nodes" not in data:
        # tolerate a common alternative schema: {"services":[...], "depends_on":{...}}
        data = _normalise_alt_schema(data)
    return Topology.from_dict(data)


def _normalise_alt_schema(data: dict[str, Any]) -> dict[str, Any]:
    services = data.get("services", [])
    nodes = []
    for s in services:
        if isinstance(s, str):
            nodes.append({"id": s, "label": s, "kind": "service"})
        else:
            nodes.append(s)
    edges = []
    deps = data.get("depends_on", data.get("dependencies", {}))
    if isinstance(deps, dict):
        for src, targets in deps.items():
            if not isinstance(targets, list):
                targets = [targets]
            for t in targets:
                edges.append({"source": src, "target": t})
    elif isinstance(deps, list):
        for d in deps:
            edges.append(d)
    return {
        "name": data.get("name", "Imported topology"),
        "description": data.get("description", ""),
        "nodes": nodes,
        "edges": edges,
    }
