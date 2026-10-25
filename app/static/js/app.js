/* CascadeIQ frontend bootstrap — wires controls, graph, rails and dock. */

import {
  store, on, connect, setParams, inject, clearFaults, loadTopology,
} from "./state.js";
import { GraphView } from "./graph.js";
import {
  renderHeader, renderPrediction, renderRecovery, renderTimeline, showTip, fmtClock,
} from "./panels.js";
import {
  initTabs, initLogs, initWhatIf, initTopologyStudio, initReport,
  refreshStudio, currentTopology, setLogCutoff, pushSystemLog,
} from "./dock.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }));

let graph;
let paramTimer = null;

/* ────────────────────────── controls ────────────────────────── */

function fillRange(input) {
  const min = +input.min, max = +input.max, v = +input.value;
  input.style.setProperty("--fill", `${((v - min) / (max - min)) * 100}%`);
}

function bindRange(id, key, transform = Number) {
  const el = $(id);
  const out = $(id + "Out");
  fillRange(el);
  el.addEventListener("input", () => {
    const v = transform(el.value);
    if (out) out.textContent = el.value;
    fillRange(el);
    clearTimeout(paramTimer);
    paramTimer = setTimeout(() => setParams({ [key]: v }), 90);
  });
}

function initControls() {
  bindRange("pSeverity", "severity", (v) => parseInt(v, 10));
  bindRange("pTraffic", "traffic");
  bindRange("pCascade", "cascade_speed");
  bindRange("pTimeout", "timeout_ms", (v) => parseInt(v, 10));
  bindRange("pRetries", "retry_aggression");

  $("pBreaker").addEventListener("change", (e) =>
    setParams({ circuit_breaker: e.target.checked }));
  $("pRateLimit").addEventListener("change", (e) =>
    setParams({ rate_limiting: e.target.checked }));

  $("pFaultType").addEventListener("change", (e) => {
    setParams({ fault_type: e.target.value });
    const f = store.faults[e.target.value];
    $("faultHint").textContent = f ? `${f.label} — ${f.summary}` : "—";
  });

  $("btnInject").addEventListener("click", () => {
    const node = $("targetNode").value;
    if (!node) return;
    inject(node, $("pFaultType").value, +$("pSeverity").value);
    pushSystemLog("ERROR", `manual fault injection → ${node}`);
  });
  $("btnClear").addEventListener("click", () => {
    clearFaults();
    pushSystemLog("INFO", "operator cleared all faults");
  });

  $("btnPlay").addEventListener("click", togglePlay);
  $("speedSel").addEventListener("change", (e) =>
    setParams({ playback: parseFloat(e.target.value) }));

  $("btnChaos").addEventListener("click", () => {
    const next = $("btnChaos").getAttribute("aria-pressed") !== "true";
    setParams({ chaos_mode: next });
    $("btnChaos").setAttribute("aria-pressed", String(next));
    pushSystemLog("WARN", next ? "chaos engineering mode ENABLED" : "chaos mode disabled");
  });

  $("btnReset").addEventListener("click", () => {
    clearFaults();
    setParams({ paused: false, chaos_mode: false, traffic: 55,
                retry_aggression: 1.5, circuit_breaker: false,
                rate_limiting: false, cascade_speed: 1, playback: 1 });
    syncControlsFromState();
    $("btnChaos").setAttribute("aria-pressed", "false");
    setScrub(-1);
    pushSystemLog("INFO", "operator reset the simulation to defaults");
  });

  $("btnRelayout").addEventListener("click", () => graph.relayout());
  $("btnGoEditor").addEventListener("click", () => $("tab-topology").click());

  $("btnLoadTopo").addEventListener("click", () => {
    const id = $("topoPreset").value;
    if (id) loadTopology(id);
  });

  initTimelineControls();
}

function togglePlay() {
  const paused = !store.state?.params?.paused;
  setParams({ paused });
  const btn = $("btnPlay");
  btn.setAttribute("aria-pressed", String(!paused));
  btn.setAttribute("aria-label", paused ? "Resume simulation" : "Pause simulation");
  btn.querySelector(".i-pause").hidden = paused;
  btn.querySelector(".i-play").hidden = !paused;
}

function syncControlsFromState() {
  const p = store.state?.params;
  if (!p) return;
  const map = {
    pSeverity: p.severity, pTraffic: p.traffic, pCascade: p.cascade_speed,
    pTimeout: p.timeout_ms, pRetries: p.retry_aggression,
  };
  for (const [id, v] of Object.entries(map)) {
    if (v == null) continue;
    const el = $(id);
    el.value = String(v);
    const out = $(id + "Out");
    if (out) out.textContent = String(v);
    fillRange(el);
  }
  $("pBreaker").checked = !!p.circuit_breaker;
  $("pRateLimit").checked = !!p.rate_limiting;
  if (p.fault_type) $("pFaultType").value = p.fault_type;
  $("speedSel").value = String(p.playback ?? 1);
  $("btnChaos").setAttribute("aria-pressed", String(!!p.chaos_mode));
  const paused = !!p.paused;
  $("btnPlay").setAttribute("aria-pressed", String(!paused));
  $("btnPlay").querySelector(".i-pause").hidden = paused;
  $("btnPlay").querySelector(".i-play").hidden = !paused;
}

/* ────────────────────────── timeline ────────────────────────── */

function setScrub(index) {
  const max = store.history.length - 1;
  store.scrub = index < 0 || index >= max ? -1 : index;
  setLogCutoff(store.scrub < 0 ? null : store.history[store.scrub].t);
  renderAll();
}

function initTimelineControls() {
  $("scrub").addEventListener("input", (e) => {
    const v = +e.target.value;
    const max = store.history.length - 1;
    setScrub(v >= max ? -1 : v);
  });
  const jump = (secs) => {
    const h = store.history;
    if (!h.length) return;
    const cur = store.scrub < 0 ? h[h.length - 1].t : h[store.scrub].t;
    const target = cur + secs;
    let idx = h.length - 1;
    for (let i = 0; i < h.length; i++) { if (h[i].t >= target) { idx = i; break; } }
    setScrub(idx);
  };
  $("btnStepBack").addEventListener("click", () => jump(-5));
  $("btnStepFwd").addEventListener("click", () => jump(5));
  $("btnLive").addEventListener("click", () => setScrub(-1));
  $("btnBackLive").addEventListener("click", () => setScrub(-1));
}

/* ────────────────────────── display state ────────────────────────── */

/** Live snapshot, or a reconstructed historical one while scrubbing. */
function displayState() {
  const live = store.state;
  if (!live) return null;
  if (store.scrub < 0) return live;

  const h = store.history[store.scrub];
  const meta = new Map(live.nodes.map((n) => [n.id, n]));
  const counts = { healthy: 0, degraded: 0, failing: 0, down: 0 };
  const nodes = live.nodes.map((n) => {
    const s = h.nodes[n.id];
    if (!s) return { ...n, isRoot: false, isPredicted: false };
    const [, health, cpu, mem, latency, err, thr, fault] = s;
    counts[health] = (counts[health] || 0) + 1;
    return {
      ...n, health, stress: s[0], cpu, mem, latency,
      error_rate: err, throughput: thr,
      fault: fault ? (n.fault || { type: "historical", severity: 5 }) : null,
      isRoot: false, isPredicted: false,
    };
  });
  const edgeMap = h.edges || {};
  const edges = live.edges.map((e) => {
    const s = edgeMap[e.id];
    return s ? { ...e, flow: s[0], tripped: !!s[1] } : e;
  });
  return {
    ...live, nodes, edges,
    t: h.t,
    health_summary: { ...counts, total: nodes.length,
      blast_radius: (counts.degraded || 0) + (counts.failing || 0) + (counts.down || 0) },
    uptime: nodes.length
      ? (counts.healthy / nodes.length) * 100 : 100,
    timeline: (live.timeline || []).filter((m) => m.t <= h.t),
  };
}

/* ────────────────────────── rendering ────────────────────────── */

function decorateNodes(nodes) {
  const rootId = store.analysis?.root_cause_id;
  const preds = new Set(
    (store.analysis?.next_failures || [])
      .filter((f) => f.confidence >= 45)
      .map((f) => f.id));
  return nodes.map((n) => ({
    ...n,
    isRoot: !!rootId && n.id === rootId,
    isPredicted: preds.has(n.id),
  }));
}

function renderGraph() {
  const s = displayState();
  if (!s) return;
  graph.setData(decorateNodes(s.nodes), s.edges);
}

function renderAll() {
  const s = displayState();
  if (!s) return;
  renderHeader(s);
  renderGraph();
  renderTimeline(s);
}

/* ────────────────────────── pickers ────────────────────────── */

function populatePickers() {
  const nodes = store.state?.nodes || [];
  const opts = nodes
    .map((n) => `<option value="${esc(n.id)}">${esc(n.label)} — ${esc(n.kind)}</option>`)
    .join("");
  const prevTarget = $("targetNode").value;
  const prevWi = $("wiNode").value;
  $("targetNode").innerHTML = opts;
  $("wiNode").innerHTML = opts;
  if (nodes.some((n) => n.id === prevTarget)) $("targetNode").value = prevTarget;
  if (nodes.some((n) => n.id === prevWi)) $("wiNode").value = prevWi;
  else if ($("targetNode").value) $("wiNode").value = $("targetNode").value;

  const faults = store.faults || {};
  if ($("pFaultType").childElementCount !== Object.keys(faults).length) {
    $("pFaultType").innerHTML = Object.entries(faults)
      .map(([k, f]) => `<option value="${esc(k)}">${esc(f.label)}</option>`).join("");
    $("wiType").innerHTML = Object.entries(faults)
      .map(([k, f]) => `<option value="${esc(k)}">${esc(f.label)}</option>`).join("");
    if (store.state?.params?.fault_type) {
      $("pFaultType").value = store.state.params.fault_type;
    }
  }
  const cur = faults[$("pFaultType").value];
  if (cur) $("faultHint").textContent = `${cur.label} — ${cur.summary}`;

  const tops = store.topologies || [];
  if ($("topoPreset").childElementCount !== tops.length) {
    $("topoPreset").innerHTML = tops.map((t) =>
      `<option value="${esc(t.id)}">${esc(t.name)} · ${t.nodes}n</option>`).join("");
  }
}

function renderTopologyHud() {
  const s = store.state;
  if (!s) return;
  $("gTopoName").textContent = s.topology.name;
  const st = s.health_summary || {};
  $("gTopoSub").textContent =
    `${s.nodes.length} services · ${s.edges.length} deps · ${st.blast_radius || 0} affected`;
  $("topoMeta").textContent = s.topology.description || "—";
}

/* ────────────────────────── boot ────────────────────────── */

function boot() {
  const activateTab = initTabs();
  initLogs();
  initWhatIf();
  initTopologyStudio();
  initReport();

  graph = new GraphView($("graph"), {
    onHover: (node, x, y) => showTip(node, x, y),
    onSelect: (node) => {
      if (!node) return;
      store.selected = node.id;
      if ([...$("targetNode").options].some((o) => o.value === node.id)) {
        $("targetNode").value = node.id;
      }
    },
    onCursor: (node) => {
      if (node && [...$("targetNode").options].some((o) => o.value === node.id)) {
        $("targetNode").value = node.id;
      }
    },
  });

  window.__g = graph;

  initControls();

  on("init", () => {
    populatePickers();
    syncControlsFromState();
    renderTopologyHud();
    renderAll();
    refreshStudio();
  });

  on("state", () => {
    if (store.scrub < 0) renderAll();
    else renderTimeline(displayState());
    renderTopologyHud();
    if ($("btnChaos").getAttribute("aria-pressed") !==
        String(!!store.state?.params?.chaos_mode)) {
      $("btnChaos").setAttribute("aria-pressed",
        String(!!store.state?.params?.chaos_mode));
    }
  });

  on("analysis", (msg) => {
    renderPrediction(msg.data);
    renderRecovery(msg.recovery);
    if (store.scrub < 0) renderGraph();
  });

  on("recovery_applied", (d) => {
    if (d) pushSystemLog("WARN", `recovery applied: ${d.label}`);
    renderAll();
  });

  on("topology_applied", (m) => {
    populatePickers();
    renderTopologyHud();
    renderAll();
    pushSystemLog("INFO", `topology replaced — ${m.nodes} services loaded`);
  });

  on("connection", (ok) => {
    const el = $("connState");
    el.className = `conn ${ok ? "conn-on" : "conn-off"}`;
    el.querySelector("span").textContent = ok ? "live" : "reconnecting…";
  });

  on("server_error", (m) => pushSystemLog("ERROR", `server: ${m}`));

  connect();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
