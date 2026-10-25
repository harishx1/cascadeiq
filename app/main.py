"""CascadeIQ — FastAPI application and realtime WebSocket hub."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine.predictor import run_prediction
from .engine.recovery import build_strategies
from .engine.scenario import compare
from .engine.simulation import FAULTS, DEFAULT_PARAMS, SimParams, Simulation
from .engine.topology import (Topology, list_topologies, load_topology,
                              topology_from_json)

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DEFAULT_TOPOLOGY = "ecommerce"

app = FastAPI(title="CascadeIQ", version="1.4.0")


# --------------------------------------------------------------------------
# Realtime hub — one shared simulation, many viewers
# --------------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.sim: Simulation | None = None
        self.task: asyncio.Task | None = None
        self.tick = 0
        self.log_cursor = 0
        self.tl_cursor = 0
        self.started_at = time.time()
        self.message_count = 0

    # -------------------------------------------------------------- lifecycle
    def boot(self, topology_id: str = DEFAULT_TOPOLOGY) -> None:
        try:
            topo = load_topology(topology_id)
        except (FileNotFoundError, json.JSONDecodeError):
            topo = load_topology(DEFAULT_TOPOLOGY)
        self.sim = Simulation(topo, SimParams.from_dict(DEFAULT_PARAMS), seed=1234)
        self.log_cursor = 0
        self.tl_cursor = 0

    async def ensure_running(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.05)
                if self.sim is None:
                    continue
                self.sim.step(0.05)
                self.tick += 1
                if self.tick % 2 == 0:
                    await self.broadcast_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the loop alive; surface the error
                print(f"[hub] loop error: {exc!r}")

    # ---------------------------------------------------------------- send
    async def broadcast(self, payload: dict[str, Any]) -> None:
        if not self.clients:
            return
        raw = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
        self.message_count += 1

    async def broadcast_state(self) -> None:
        sim = self.sim
        if sim is None:
            return
        snap = sim.snapshot()

        # incremental logs / timeline so the wire stays small
        logs = sim.logs
        if self.log_cursor > len(logs):
            self.log_cursor = 0
        logs_delta = logs[self.log_cursor:]
        self.log_cursor = len(logs)

        tl = sim.timeline
        if self.tl_cursor > len(tl):
            self.tl_cursor = 0
        tl_delta = tl[self.tl_cursor:]
        self.tl_cursor = len(tl)

        snap["logs"] = []
        snap["timeline"] = []
        snap["logs_delta"] = logs_delta
        snap["timeline_delta"] = tl_delta
        snap["server_time"] = time.time()
        await self.broadcast({"type": "state", "data": snap})

    # ------------------------------------------------------------ handling
    async def handle(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        sim = self.sim
        if sim is None:
            await ws.send_text(json.dumps({"type": "error", "message": "no simulation"}))
            return

        if kind == "hello":
            await ws.send_text(json.dumps({
                "type": "init",
                "data": sim.snapshot(include_history=True),
                "topologies": list_topologies(),
                "faults": FAULTS,
                "uptime_sec": round(time.time() - self.started_at, 1),
            }))
            sim.last_prediction = run_prediction(sim)
            sim.last_recovery = build_strategies(sim)
            await ws.send_text(json.dumps({"type": "analysis",
                                           "data": sim.last_prediction,
                                           "recovery": sim.last_recovery}))

        elif kind == "params":
            sim.set_params(**{k: v for k, v in msg.get("values", {}).items()
                              if k in DEFAULT_PARAMS or hasattr(sim.params, k)})
            await self.push_analysis()

        elif kind == "inject":
            sim.inject_fault(
                str(msg.get("node")),
                msg.get("fault_type") or sim.params.fault_type,
                int(msg.get("severity") or sim.params.severity),
            )
            await self.push_analysis()

        elif kind == "clear":
            sim.clear_faults()
            await self.push_analysis()

        elif kind == "recovery":
            applied = sim.apply_recovery(str(msg.get("strategy", "")))
            await ws.send_text(json.dumps({
                "type": "recovery_applied",
                "data": applied,
            }))
            await self.push_analysis()

        elif kind == "load_topology":
            try:
                topo = load_topology(str(msg.get("id")))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
                return
            self._swap(topo)
            await self.send_init()
            await self.push_analysis()

        elif kind == "update_topology":
            try:
                topo = Topology.from_dict(msg.get("topology", {}))
                errors = topo.errors()
                warns = topo.warnings()
                if errors and not msg.get("force"):
                    await ws.send_text(json.dumps({
                        "type": "validation", "issues": errors + warns,
                    }))
                    return
            except (KeyError, json.JSONDecodeError, TypeError) as exc:
                await ws.send_text(json.dumps({"type": "error",
                                               "message": f"Invalid topology: {exc}"}))
                return
            self._swap(topo)
            await self.send_init()
            await ws.send_text(json.dumps({
                "type": "topology_applied",
                "nodes": len(topo.nodes), "edges": len(topo.edges),
                "issues": warns,
                "topologies": list_topologies(),
            }))
            await self.push_analysis()

        elif kind == "history":
            await ws.send_text(json.dumps({"type": "history",
                                           "data": sim.history}))

        elif kind == "scenario":
            await ws.send_text(json.dumps({"type": "scenario_running"}))
            payload = msg.get("run", {})
            try:
                result = await asyncio.to_thread(
                    compare,
                    sim.topology.clone(),
                    sim.params.to_dict(),
                    payload.get("a", {"_label": "Variant A"}),
                    payload.get("b", {"_label": "Variant B"}),
                    payload.get("fault", {}),
                    float(payload.get("duration", 45.0)),
                )
                await ws.send_text(json.dumps({"type": "scenario_result",
                                               "data": result}, default=str))
            except Exception as exc:
                await ws.send_text(json.dumps({"type": "error",
                                               "message": f"scenario failed: {exc}"}))

        elif kind == "report":
            await ws.send_text(json.dumps({
                "type": "report",
                "data": build_report(sim),
            }, default=str))

        elif kind == "analysis":
            await self.push_analysis()

        else:
            await ws.send_text(json.dumps({"type": "error",
                                           "message": f"unknown message '{kind}'"}))

    async def push_analysis(self) -> None:
        sim = self.sim
        if sim is None:
            return
        sim.last_prediction = run_prediction(sim)
        sim.last_recovery = build_strategies(sim)
        await self.broadcast({"type": "analysis",
                              "data": sim.last_prediction,
                              "recovery": sim.last_recovery})

    def _swap(self, topo: Topology) -> None:
        params = self.sim.params if self.sim else SimParams.from_dict(DEFAULT_PARAMS)
        self.sim = Simulation(topo, params)
        self.log_cursor = 0
        self.tl_cursor = 0

    async def send_init(self) -> None:
        if self.sim is None:
            return
        await self.broadcast({"type": "init", "data": self.sim.snapshot(include_history=True),
                              "topologies": list_topologies(), "faults": FAULTS})


hub = Hub()


# --------------------------------------------------------------------------
# Incident report
# --------------------------------------------------------------------------

def build_report(sim: Simulation) -> dict[str, Any]:
    prediction = sim.last_prediction or run_prediction(sim)
    recovery = sim.last_recovery or build_strategies(sim)
    root = prediction.get("root_cause")
    summary = sim.health_summary()
    events = [e for e in sim.timeline if e["kind"] in
              ("FAULT", "STATE", "CIRCUIT", "RECOVERY")][:60]

    return {
        "title": f"Incident report — {sim.topology.name}",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sim_seconds": round(sim.t, 1),
        "topology": sim.topology.name,
        "fingerprint": sim.topology.fingerprint(),
        "scale": {"nodes": len(sim.nodes), "edges": len(sim.edges_by_id)},
        "impact": {
            "peak_blast_radius": sim.peak_blast,
            "peak_down": sim.peak_down,
            "current": summary,
            "availability": sim.uptime_score,
        },
        "root_cause": {
            "node": root.get("label") if root else None,
            "confidence": root.get("confidence") if root else None,
            "fault": root.get("fault_label") if root else None,
            "why": root.get("why") if root else None,
            "path": [c["label"] for c in prediction.get("cascade_path", [])],
        },
        "forecast": [
            {"node": f["label"], "confidence": f["confidence"], "eta": f["eta"]}
            for f in prediction.get("next_failures", [])
        ],
        "recommended_action": {
            "strategy": recovery["strategies"][0]["label"] if recovery.get("strategies") else None,
            "success": recovery["strategies"][0]["success"] if recovery.get("strategies") else None,
            "risk": recovery["strategies"][0]["risk"] if recovery.get("strategies") else None,
            "time": recovery["strategies"][0]["estimated_time"] if recovery.get("strategies") else None,
            "rationale": recovery.get("rationale"),
            "factors": recovery.get("decision_factors", []),
        },
        "alternatives": [
            {"strategy": s["label"], "success": s["success"], "risk": s["risk"],
             "time": s["estimated_time"], "blast_after": s["blast_radius_after"]}
            for s in recovery.get("strategies", [])
        ],
        "timeline": events,
        "model": prediction.get("model"),
    }


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------

class TopologyIn(BaseModel):
    payload: str


class TopologyDirect(BaseModel):
    topology: dict[str, Any]


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "clients": len(hub.clients),
            "messages": hub.message_count,
            "uptime_sec": round(time.time() - hub.started_at, 1)}


@app.get("/health")
def health_alias() -> dict[str, Any]:
    """Alias used by process supervisors and demo scripts."""
    return health()


@app.get("/api/topologies")
def api_topologies() -> list[dict[str, Any]]:
    return list_topologies()


@app.get("/api/topologies/{topology_id}")
def api_topology(topology_id: str) -> dict[str, Any]:
    try:
        return load_topology(topology_id).to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/topologies/validate")
def api_validate(payload: TopologyIn) -> dict[str, Any]:
    try:
        topo = topology_from_json(payload.payload)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"valid": False, "issues": [f"Parse error: {exc}"],
                "errors": [f"Parse error: {exc}"], "warnings": [],
                "nodes": 0, "edges": 0, "fingerprint": ""}
    errors, warns = topo.errors(), topo.warnings()
    return {"valid": not errors, "errors": errors, "warnings": warns,
            "issues": errors + warns,
            "nodes": len(topo.nodes), "edges": len(topo.edges),
            "fingerprint": topo.fingerprint()}


@app.post("/api/topologies/direct")
def api_direct(payload: TopologyDirect) -> dict[str, Any]:
    topo = Topology.from_dict(payload.topology)
    return {"issues": topo.validate(), "nodes": len(topo.nodes),
            "edges": len(topo.edges), "fingerprint": topo.fingerprint()}


@app.get("/api/faults")
def api_faults() -> dict[str, Any]:
    return FAULTS


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    if hub.sim is None:
        raise HTTPException(status_code=503, detail="simulation not ready")
    return hub.sim.snapshot()


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    hub.boot()
    await hub.ensure_running()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if hub.task:
        hub.task.cancel()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    await hub.ensure_running()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error",
                                               "message": "invalid JSON"}))
                continue
            await hub.handle(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False,
                log_level="warning")


if __name__ == "__main__":
    main()
