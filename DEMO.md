# CascadeIQ — demo script & evaluator Q&A

> Keep this open on a second screen during the pitch.

---

## The 90-second talk track

**0:00 — Hook (10 s)**
> "Every dashboard in this room shows you what *already* broke. We built the one that
> tells you what's about to break, why, and exactly what to do about it — before the
> system goes down. And it works on *your* architecture, not ours."

**0:10 — Set the stage (15 s)**
> "This is a live simulation of an e-commerce platform: 23 services, 25 dependencies.
> Everything you see is running right now — the graph, the logs, the prediction engine."

*(point at the left rail)*
> "All the physics are on sliders: traffic, cascade speed, timeout, retry aggression,
> circuit breaker, rate limiting. Nothing is faked."

**0:25 — The incident (25 s)**
> "Let's put a lock contention fault on the ledger database at severity 8."

*(click `⚡ Inject failure`, wait ~8 s)*

> "Watch the cascade — it's following dependency edges, not a script. And look here…"

*(gesture at the right rail)*

> "Root cause: **Ledger DB, 99% confidence** — it's the only node carrying the fault and
> the earliest anomaly on the timeline, and everything else is reachable from it along
> the failure-direction graph. Here's the exact cascade path, here are the **next three
> services predicted to fail**, with confidence and ETA — and here are four recovery
> strategies ranked by success, risk, blast radius and time."

**0:50 — Prove it's not static (20 s)**
> "Now the part that matters. Two things."

*(1) open **Topology studio → Export current**, highlight the JSON)*
> "This is the entire system — nodes, edges, kinds. Delete it, paste in a completely
> different industry's architecture, hit Apply, and every analysis above re-runs from
> scratch. There is no e-commerce code anywhere in this engine."

*(2) open **What-if compare → Run comparison)**
> "Same fault, two configurations. Hardened: **96% availability, blast radius 1**.
> Fragile: **64% availability, blast radius 10**. The recommendation engine didn't just
> detect the difference — it *prescribed* the fix."

**1:10 — Close (15 s)**
> "Scrub the timeline to replay any moment, hit **Incident report** for a one-click
> write-up you can hand to on-call, or flip **Chaos** and let it attack itself. One
> engine, any architecture, real-time predictions — that's CascadeIQ."

---

## Live demo beats (in order)

| # | Action | What to point at |
| --- | --- | --- |
| 1 | Load page, let it run 5 s | health pills, live logs, moving edges |
| 2 | Drag `Cascade speed` to 0.25× | propagation visibly slows — it's a model, not a video |
| 3 | Inject `slow_query` on **Ledger DB**, severity 8 | right rail: root cause ring, cascade path |
| 4 | Wait for the forecast card | predicted next failures with ETA |
| 5 | Point at the ranked recovery cards | success / risk / blast / time table |
| 6 | Drag the **timeline scrubber** back | whole UI rewinds, `Reviewing history` badge appears → `Return to live` |
| 7 | **What-if compare → Run** | 96% vs 64% availability, blast chart |
| 8 | **Topology studio → Export**, then change topology preset | everything re-derives |
| 9 | **Incident report → Generate → Download HTML** | complete write-up, standalone file |
| 10 | Flip **Chaos** for 10 s | the system attacks itself, engine keeps up |

**Fallback if something breaks:** `Reset` in the top bar, then reload the page. The server
holds all state; the browser is a pure view.

---

## Evaluator Q&A

### The hard ones

**Q: How does this adapt to a brand-new application?**
> The engine consumes exactly two things: a node list `{id, label, kind}` and an edge list
> `{source, target, kind}`. Failure direction, root-cause ranking and strategy scoring are
> computed from graph structure plus six universal observables — stress, CPU, memory,
> latency, error rate, throughput. Paste a new topology in the studio and every panel
> recomputes. Demo: switch to *IoT Telemetry* and inject a fault — different answer,
> same engine.

**Q: Does it scale past a toy system?**
> 300 nodes / 894 edges runs a full simulation step in ~13 ms (see `verify.py`), so it's
> comfortably inside a 60 fps budget. Node maps and failure adjacency are memoised per
> topology; the canvas renderer samples repulsion past 150 nodes. Import a large graph and
> try it — the UI stays interactive.

**Q: Isn't this hard-coded to DB / Auth / Payments?**
> Nothing is keyed on service names. The strategy matrix is indexed by *fault class × node
> kind*, and `kind` is a nine-value enum (`gateway, service, function, database, cache,
> queue, stream, storage, external, ml`). Move a slider and the *outcome* changes — flip the
> circuit breaker off mid-incident and the blast radius grows — so it isn't a canned
> animation.

**Q: What tech stacks and workflows does it support?**
> It's stack-agnostic by construction: a queue behaves like a queue whether it's Kafka, SQS
> or RabbitMQ. The importer accepts `nodes/edges`, `services/depends_on` as a map *or* an
> array, and bare node lists. Recovery is four generic strategies — isolate, restart,
> rollback, failover — scored against the observed fault, not against a vendor.

**Q: Is the "AI" real, or is it `if` statements?**
> Three learned-shaped components: an onset-ranking root-cause model (earliest anomaly ×
> reachability over the failure-direction subgraph), a probabilistic next-failure forecast
> with confidence and ETA, and a strategy scorer that evaluates four candidate actions
> against live system state. Every number is recomputed per frame from the current
> topology — you can watch the confidence ring move as the incident evolves.

**Q: What's the prediction actually worth if you only show it after the crash?**
> The forecast is produced *during* the cascade. Drag the timeline back to any point and the
> right rail shows what was known then — including predicted failures that had not yet
> happened. That's the whole point of the scrubber.

**Q: How is this better than an APM / observability tool?**
> Those tools are descriptive — they tell you what is broken. This is prescriptive: it names
> the origin, forecasts the next casualties, and ranks interventions with expected success
> and risk. The incident report is the artifact an on-call engineer actually wants.

**Q: What did you build in three hours?**
> Everything you can click: a custom event-driven cascade engine, a root-cause and forecast
> predictor, a recovery strategy scorer, a headless what-if simulator, a FastAPI/WebSocket
> backend, and a zero-dependency frontend with a hand-written canvas force-directed
> renderer. `verify.py` runs 22 end-to-end checks in seconds.

### Easy ones

| Question | Answer |
| --- | --- |
| Why no frameworks on the front end? | It has to work offline on an unknown machine — no CDN, no build step. And a custom canvas gave us total control of the animation. |
| Where does the data come from? | A simulation. Deliberately — it lets you inject, rewind and A/B any scenario on demand, which real incident data never lets you do. |
| Can I break it? | Try. Import a malformed JSON — the validator explains what's wrong. Self-loops, duplicate edges and unknown references are all caught. |
| What's the business case? | Reduced MTTR: detect → explain → prescribe in one screen, plus a shareable incident report. |
| Is the report real? | Yes — one click, standalone HTML, with root cause, cascade path, forecast, ranked strategies, event timeline and model provenance. Downloadable. |

---

## Keyboard & accessibility talking points

* The graph is focusable — arrow keys walk the services, `Enter` selects one.
* Every icon-only control has an `aria-label`; toggles expose `aria-pressed`.
* Live regions announce status changes (`aria-live="polite"`).
* `prefers-reduced-motion` turns off pulsing and glow.
* All figures use tabular numerals so live values don't jitter.

---

## If a demo goes wrong

| Symptom | Fix |
| --- | --- |
| Page won't load | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`, open `/` |
| Weird state | `Reset` in the top bar, then reload |
| Graph looks crowded | `Relayout`, or drag nodes (dragged nodes stay put until relayout) |
| Timeline feels stuck | press play — the simulation can be paused |
| Port busy | add `--port 8001` |
