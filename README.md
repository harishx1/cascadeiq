# CascadeIQ — the control tower for cascading software failures

> **"Most failure dashboards tell you what already broke. This tells you what's about to
> break, why, and exactly what to do about it — on any architecture you define."**

CascadeIQ answers Problem 01: *build an AI system that predicts cascading software
failures, identifies their root causes, and recommends the optimal recovery strategy
before an entire system crashes.*

It is a **live, controllable simulation** — not a slide deck. You draw or import the
architecture, inject a fault, and watch the engine forecast the blast radius, isolate the
origin, and rank four recovery strategies *while the cascade is still unfolding*. Every
parameter is on a slider, every scenario is replayable, and every number is recomputed
from scratch against whatever graph you feed it.

---

## Quick start

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** — no build step, no CDN, no network access required.

Run the engine test-suite (22 checks: topology integrity, root-cause isolation across
16 topology×fault combinations, cascade physics, resilience ablation, what-if scoring,
300-node performance, alternate-schema import, report generation):

```bash
python verify.py
```

---

## Deploy to Render

**Option A - Blueprint (recommended).** `render.yaml` ships with the repo:

1. Render dashboard -> **New + -> Blueprint -> Connect a repository**
2. Select `harishx1/cascadeiq`
3. Render reads `render.yaml` (name, build command, start command, `/health` check) -> **Apply**

**Option B - Manual web service.**

1. Render dashboard -> **New + -> Web Service -> Connect a repository** -> select `harishx1/cascadeiq`
2. Fill in:
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/health`
3. **Create Web Service** and wait for the first deploy.

Nothing else needs changing: the server already binds `0.0.0.0` and reads Render's `$PORT`,
and `/health` is a plain liveness endpoint.

**Live**: https://cascadeiq-vsaq.onrender.com

**Notes**

- The live stream uses WebSockets; they work on the Render free plan.
- The free plan sleeps after ~15 min without traffic. The first load after a cold start
  takes roughly 30-50 s, after which everything is live again.
- If the build rejects the interpreter version, add an env var `PYTHON_VERSION` = `3.13.9`.

---

## The 60-second demo

1. **Inject** — pick a service, pick a failure mode, set severity, hit `⚡ Inject failure`.
2. **Watch** — the cascade propagates live along dependency edges; health pills, availability
   and the timeline all move together.
3. **Root cause** — before the system finishes falling over, the right rail names the
   origin with a confidence ring, the exact cascade path, and *why* it was chosen.
4. **Forecast** — "predicted next failures" with per-service confidence and ETA.
5. **Recover** — four strategies ranked by success / risk / blast-radius / time, with the
   winning one called out and the decision factors spelled out.
6. **Prove it wasn't luck** — drag the time scrubber backwards, or open *What-if compare*
   and run the same fault against a hardened vs. a fragile configuration side by side.

Then hit **Reset**, switch topology to *IoT Telemetry*, and do it again — nothing about
the analysis was hard-coded to the first system.

---

## What's on screen

| Region | Contents |
| --- | --- |
| **Top bar** | simulation clock, speed, health pills (healthy / degraded / failing / down), availability, peak blast radius, Chaos mode, Reset, Incident report |
| **Left rail** | fault target, failure mode (8 classes), severity, inject/clear, traffic load, cascade speed, retry aggression, timeout, circuit breaker, rate limiting |
| **Centre** | live dependency graph on a custom canvas renderer — health colouring, root-cause & predicted markers, edge flow animation, drag-to-reposition, keyboard navigation, zoom/pan, Relayout |
| **Right rail** | root cause + confidence, cascade path chain, predicted next failures, ranked recovery strategies with decision factors |
| **Timeline** | scrubber with event markers, step ±5 s, Live / review-history mode |
| **Dock** | Live logs · What-if compare · Topology studio · Incident report · Architecture |

### Dock tabs

* **Live logs** — level + free-text filtering, follow-tail, simulated timestamps.
* **What-if compare** — run the *same* fault twice under two configurations (baseline,
  fully hardened, no resilience, breaker on/off, retry 0.5×/4.5×, rate limiting, peak
  traffic, slow-motion) and diff availability, peak blast radius, nodes down, mean error
  rate, plus a blast-radius-over-time chart.
* **Topology studio** — add / remove services, connect dependencies, validate, and
  import/export topology JSON (multiple schemas accepted).
* **Incident report** — one click produces a full write-up: impact, root cause, cascade
  path, forecast, recommended action, strategy comparison table, event timeline and model
  provenance. Downloadable as a standalone HTML file.
* **Architecture** — the system's own design, so an evaluator never has to ask.

---

## Why this answers the judging questions

### "How does it adapt to a new application?"

There is no per-application code anywhere. The engine only ever sees:

```
nodes:  { id, label, kind }            kind ∈ gateway|service|function|database|
                                        cache|queue|stream|storage|external|ml
edges:  { source, target, kind }       kind ∈ sync|db|cache|async|stream
```

Failure propagation, root-cause ranking and recovery scoring are all derived from that
graph plus universally observable signals (stress, latency, error rate, throughput,
queue depth). Delete the e-commerce preset, paste in a healthcare or fintech topology, and
the same code produces a different, correct answer. **Topology studio → paste JSON → Apply**
is the whole adaptation story, and it takes ten seconds.

### "How does it scale?"

A 300-node / 894-edge graph runs the full simulation step in **~13 ms** — well inside a
60 fps budget. Node maps and failure adjacency are memoised per topology; the client
renderer samples repulsion beyond 150 nodes. Try it: import a large graph in the topology
studio and the UI stays interactive.

### "Is it static? Isn't this just DB / Auth / Payments?"

Every slider changes the physics, not the labels:

* **traffic load** → baseline utilisation and queue pressure
* **cascade speed** → propagation rate, including 0.25× slow motion
* **failure severity** → fault magnitude
* **retry aggression** → retry-storm amplification (0.5× calm → 4.5× stampede)
* **timeout** → how long a caller waits before it, too, degrades
* **circuit breaker** → whether callers shed a failing dependency or follow it down
* **rate limiting** → edge protection under peak load

Flip the circuit breaker off mid-incident and the blast radius visibly grows. That is a
different *outcome*, not a different colour.

### "Does it work on different tech stacks and workflows?"

The graph model is stack-agnostic by construction — a queue is a queue whether it's
Kafka, SQS or RabbitMQ. The importer tolerates several JSON schemas
(`nodes`/`edges`, `services`/`depends_on` as a map *or* a list, bare node arrays), and the
recovery layer is a strategy matrix keyed by fault class and node kind rather than by
product name. Swap the topology preset and every downstream analysis re-runs.

---

## How it works

```
app/engine/
  topology.py     generic graph model, layer inference, multi-schema importers, validation
  simulation.py   event-driven cascade engine — pressure model, DEAD_BOOST tipping point,
                  fault classes, snapshot / history / timeline
  predictor.py    failure-direction adjacency → root-cause onset ranking → cascade path
                  → next-failure forecast with confidence and ETA
  recovery.py     four generic strategies scored on success, risk, blast radius, time
  logs.py         kind/state/fault-aware synthetic log stream
  scenario.py     headless what-if runner used by the compare panel
app/main.py       FastAPI + WebSocket hub (delta-encoded logs/timeline), REST API, report builder
app/static/       zero-dependency frontend: ES modules + a hand-written canvas force layout
```

**Failure direction.** An edge `A → B` means *A depends on B*. Failures therefore travel
`target → source` on `sync`/`db`/`cache` edges and bidirectionally on `async`/`stream`
edges (a stuck consumer jams a topic just as surely as a slow broker). A `DEAD_BOOST`
term accelerates propagation once a dependency is fully down, which is what produces the
characteristic runaway cascade rather than a linear pile-up.

**Root cause** = earliest-anomaly onset ranking intersected with reachability over the
failure-direction subgraph. **Recovery** = four strategies (isolate, restart, rollback,
failover) scored against the *detected* fault class and node kind, then ranked.

**Frontend.** No React, no D3, no bundler, no CDN — the requirement was that the demo must
work offline on an unfamiliar machine, and a custom canvas renderer was the only way to
keep full control of the animation anyway.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` · `/api/health` | liveness + client/message counters |
| `GET` | `/api/topologies` · `/api/topologies/{id}` | list / fetch presets |
| `POST` | `/api/topologies/validate` | validate any JSON topology document |
| `GET` | `/api/faults` | the fault catalogue |
| `GET` | `/api/state` | full simulation snapshot |
| `WS` | `/ws` | live state, logs, timeline, analysis, scenario results, reports |

Client → server messages: `hello`, `params`, `inject`, `clear`, `reset`, `load_topology`,
`update_topology`, `recovery`, `scenario`, `report`, `history`.

---

## Verification

`verify.py` exercises the engine without a browser:

```
22/22 checks passed
```

Highlights: root cause correctly isolated in **16/16** topology×fault scenarios ·
circuit breakers + low retries cut blast radius from 10 → 1 · hardened variant availability
**96.1 % vs 63.6 %** · strategy recommendation adapts to fault class · 300-node graph at
**~13 ms/step** · alternative-schema import · unseen-topology prediction · report contains
root cause + ranked alternatives.

---

## Keyboard & accessibility

* `Tab` reaches every control; the graph is focusable and navigable with arrow keys,
  `Home`/`End`, and `Enter` to select.
* `Space` on the play/pause control toggles the simulation.
* Every icon-only button has an `aria-label`; state changes are announced via `aria-pressed`
  and live regions.
* `prefers-reduced-motion` disables pulsing and glow animation.
* All numeric readouts use tabular figures so live values don't jitter.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Port already in use | `--port 8001` and open that port instead |
| Blank page | confirm the server is running and open `/` (not `/static/`) |
| Graph feels crowded | hit **Relayout**, or drag nodes — dragged nodes stay put until you relayout |
| Nothing animates | the simulation may be paused — press play in the top bar |
