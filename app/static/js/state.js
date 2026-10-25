/* Central store + realtime WebSocket client. */

const listeners = new Map();

export const store = {
  /** latest full snapshot from the server */
  state: null,
  /** latest prediction payload */
  analysis: null,
  /** latest recovery comparison */
  recovery: null,
  /** compact historical snapshots: [t, nodes{}, edges{}] */
  history: [],
  topologies: [],
  faults: {},
  /** index into history being reviewed, or -1 for live */
  scrub: -1,
  connected: false,
  /** node selected by the user (graph click / keyboard) */
  selected: null,
  report: null,
  scenario: null,
};

export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
  return () => listeners.get(type)?.delete(fn);
}

export function emit(type, payload) {
  const set = listeners.get(type);
  if (set) for (const fn of set) fn(payload);
}

/* ------------------------------------------------------------------ socket */

let ws = null;
let retries = 0;
let queued = [];

export function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  } else {
    queued.push(msg);
  }
}

function setConnected(on) {
  if (store.connected === on) return;
  store.connected = on;
  emit("connection", on);
}

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener("open", () => {
    retries = 0;
    setConnected(true);
    send({ type: "hello" });
    while (queued.length) ws.send(JSON.stringify(queued.shift()));
  });

  ws.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    route(msg);
  });

  ws.addEventListener("close", () => {
    setConnected(false);
    const delay = Math.min(4000, 400 * ++retries);
    setTimeout(connect, delay);
  });

  ws.addEventListener("error", () => ws.close());
}

function route(msg) {
  switch (msg.type) {
    case "init": {
      store.state = msg.data;
      store.topologies = msg.topologies || [];
      store.faults = msg.faults || {};
      store.history = Array.isArray(msg.data.history) ? msg.data.history : [];
      store.scrub = -1;
      resetHistoryClock();
      emit("init", msg);
      emit("state", store.state);
      break;
    }
    case "state": {
      const prev = store.state;
      const s = msg.data;
      // the wire carries a delta; keep the running event list locally so the
      // scrubber keeps its markers
      const base = (Array.isArray(s.timeline) && s.timeline.length)
        ? s.timeline
        : (Array.isArray(prev?.timeline) ? prev.timeline : []);
      if (Array.isArray(s.timeline_delta) && s.timeline_delta.length) {
        s.timeline = base.concat(s.timeline_delta);
        if (s.timeline.length > 120) s.timeline = s.timeline.slice(-120);
      } else {
        s.timeline = base;
      }
      store.state = s;
      appendHistory(s);
      emit("state", s);
      break;
    }
    case "analysis":
      store.analysis = msg.data;
      store.recovery = msg.recovery || store.recovery;
      emit("analysis", msg);
      break;
    case "recovery_applied":
      emit("recovery_applied", msg.data);
      break;
    case "topology_applied":
      store.topologies = msg.topologies || store.topologies;
      store.history = [];
      store.scrub = -1;
      resetHistoryClock();
      emit("topology_applied", msg);
      break;
    case "history":
      store.history = msg.data || [];
      lastHistoryT = store.history.length
        ? store.history[store.history.length - 1].t : 0;
      emit("history", msg.data);
      break;
    case "scenario_result":
      store.scenario = msg.data;
      emit("scenario_result", msg.data);
      break;
    case "scenario_running":
      emit("scenario_running");
      break;
    case "report":
      store.report = msg.data;
      emit("report", msg.data);
      break;
    case "validation":
      emit("validation", msg.issues || []);
      break;
    case "error":
      emit("server_error", msg.message);
      break;
    default:
      emit("message", msg);
  }
}

/* Compact client-side history so the scrubber works without extra round trips. */
let lastHistoryT = 0;

/** A new topology or a reset rewinds simulated time — start sampling again. */
export function resetHistoryClock() { lastHistoryT = 0; }

function appendHistory(s) {
  if (typeof s.t !== "number") return;
  if (s.t < lastHistoryT) lastHistoryT = 0;
  if (s.t - lastHistoryT < 0.2) return;
  lastHistoryT = s.t;
  const nodes = {};
  for (const n of s.nodes) {
    nodes[n.id] = [n.stress, n.health, n.cpu, n.mem, n.latency,
                   n.error_rate, n.throughput, n.fault ? 1 : 0];
  }
  const edges = {};
  for (const e of s.edges) edges[e.id] = [e.flow, e.tripped ? 1 : 0];
  store.history.push({ t: Number(s.t.toFixed(2)), tick: s.tick, nodes, edges });
  if (store.history.length > 900) store.history.splice(0, store.history.length - 900);
}

/* --------------------------------------------------------------- shortcuts */

export const setParams = (values) => send({ type: "params", values });
export const inject = (node, fault_type, severity) =>
  send({ type: "inject", node, fault_type, severity });
export const clearFaults = () => send({ type: "clear" });
export const applyRecovery = (strategy) => send({ type: "recovery", strategy });
export const loadTopology = (id) => send({ type: "load_topology", id });
export const updateTopology = (topology) => send({ type: "update_topology", topology });
export const requestHistory = () => send({ type: "history" });
export const requestReport = () => send({ type: "report" });
export const requestAnalysis = () => send({ type: "analysis" });
export const runScenario = (run) => send({ type: "scenario", run });
