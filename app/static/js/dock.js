/* Dock panels: live logs, what-if comparison, topology studio, incident report. */

import {
  store, on, send, updateTopology, runScenario, requestReport,
} from "./state.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }));

const LEVELS = { INFO: 0, WARN: 1, ERROR: 2, FATAL: 3 };

/* ══════════════════════════════ TABS ══════════════════════════════ */

export function initTabs() {
  const tabs = [...document.querySelectorAll(".tab")];
  const activate = (tab) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      const panel = document.getElementById(t.getAttribute("aria-controls"));
      panel.classList.toggle("is-active", on);
    });
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => activate(t));
    t.addEventListener("keydown", (e) => {
      let n = null;
      if (e.key === "ArrowRight") n = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft") n = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") n = tabs[0];
      else if (e.key === "End") n = tabs[tabs.length - 1];
      if (n) { e.preventDefault(); n.focus(); activate(n); }
    });
  });
  return (id) => activate(document.getElementById(id));
}

/* ══════════════════════════════ LOGS ══════════════════════════════ */

const MAX_LINES = 400;
let logBuf = [];
let labelMap = new Map();
let logCutoff = null;

/** While reviewing history, only show log lines up to this simulated time. */
export function setLogCutoff(t) {
  logCutoff = t;
  flushLogs();
}

export function initLogs() {
  const stream = $("logStream");
  ["logLevel", "logSearch"].forEach((id) => $(id).addEventListener("input", flushLogs));
  $("logFollow").addEventListener("change", () => {
    if ($("logFollow").checked) stream.scrollTop = stream.scrollHeight;
  });

  on("init", (msg) => {
    const seed = msg?.data?.logs;
    if (Array.isArray(seed) && seed.length) {
      logBuf = seed.slice();
      flushLogs();
    }
  });

  on("state", (s) => {
    for (const n of s.nodes) labelMap.set(n.id, n.label);
    const delta = s.logs_delta;
    if (!delta || !delta.length) return;
    for (const l of delta) logBuf.push(l);
    if (logBuf.length > 1200) logBuf.splice(0, logBuf.length - 1200);
    flushLogs();
  });
  flushLogs();
}

function flushLogs() {
  const min = LEVELS[$("logLevel").value] ?? 1;
  const q = ($("logSearch").value || "").toLowerCase();
  const rows = [];
  for (let i = logBuf.length - 1; i >= 0 && rows.length < MAX_LINES; i--) {
    const l = logBuf[i];
    if (logCutoff !== null && l.t > logCutoff) continue;
    if ((LEVELS[l.level] ?? 0) < min) continue;
    if (q && !l.msg.toLowerCase().includes(q) && !String(l.node).includes(q)) continue;
    rows.push(l);
  }
  rows.reverse();
  const stream = $("logStream");
  stream.innerHTML = rows.map((l) => {
    const t = l.t.toFixed(2).padStart(7, " ");
    const name = labelMap.get(l.node) || l.node;
    return `<div class="ln lv-${l.level}"><span class="ln-t">${t}</span>` +
           `<span class="ln-lv">${l.level}</span>` +
           `<span class="ln-nd" title="${esc(l.node)}">${esc(name)}</span>` +
           `<span class="ln-msg">${esc(l.msg)}</span></div>`;
  }).join("");
  $("logCount").textContent = `${rows.length} line${rows.length === 1 ? "" : "s"}`;
  if ($("logFollow").checked) stream.scrollTop = stream.scrollHeight;
}

export function pushSystemLog(level, msg) {
  logBuf.push({ t: store.state?.t ?? 0, level, node: "system", msg });
  flushLogs();
}

/* ══════════════════════════ WHAT-IF ══════════════════════════ */

const VARIANTS = [
  { id: "baseline", label: "Baseline (as configured)", p: {} },
  { id: "hardened", label: "Fully hardened · breaker + limits + 0.5× retries",
    p: { circuit_breaker: true, rate_limiting: true, retry_aggression: 0.5 } },
  { id: "fragile", label: "No resilience · 4.5× retries, no breaker",
    p: { circuit_breaker: false, rate_limiting: false, retry_aggression: 4.5 } },
  { id: "breaker_on", label: "Circuit breakers ON", p: { circuit_breaker: true } },
  { id: "breaker_off", label: "Circuit breakers OFF", p: { circuit_breaker: false } },
  { id: "retry_low", label: "Retries 0.5× (calm)", p: { retry_aggression: 0.5 } },
  { id: "retry_high", label: "Retries 4.5× (retry storm)", p: { retry_aggression: 4.5 } },
  { id: "ratelimit", label: "Edge rate limiting ON", p: { rate_limiting: true } },
  { id: "peak", label: "Peak traffic 95%", p: { traffic: 95 } },
  { id: "slowmo", label: "Cascade at 0.25× (slow motion)", p: { cascade_speed: 0.25 } },
];

export function initWhatIf() {
  for (const id of ["variantA", "variantB"]) {
    $(id).innerHTML = VARIANTS.map((v, i) =>
      `<option value="${v.id}">${esc(v.label)}</option>`).join("");
  }
  $("variantA").value = "hardened";
  $("variantB").value = "fragile";

  $("btnRunScenario").addEventListener("click", () => {
    const a = VARIANTS.find((v) => v.id === $("variantA").value);
    const b = VARIANTS.find((v) => v.id === $("variantB").value);
    const node = $("wiNode").value;
    const type = $("wiType").value;
    if (!node) { $("wiStatus").textContent = "Load a topology with at least one service first."; return; }
    $("wiStatus").textContent = "Running both variants head-to-head…";
    $("btnRunScenario").disabled = true;
    runScenario({
      a: { ...a.p, _label: a.label },
      b: { ...b.p, _label: b.label },
      fault: { node, type, severity: 8, at: 3.0 },
      duration: 40,
    });
  });

  on("scenario_result", renderScenario);
  on("scenario_running", () => { $("wiStatus").textContent = "Simulating…"; });
  on("server_error", (m) => {
    if ($("panel-whatif").classList.contains("is-active")) $("wiStatus").textContent = m;
  });
}

function renderScenario(r) {
  $("btnRunScenario").disabled = false;
  $("wiStatus").textContent = `Computed in ${r.computed_in_ms} ms · ${r.duration}s simulated`;

  const card = (tag, cls, label, m) => `
    <div class="wi-card">
      <h4><span class="wi-tag ${cls}">${tag}</span> ${esc(label)}</h4>
      <div class="wi-stats">
        <div class="stat s-ok"><b>${m.availability.toFixed(1)}%</b><span>availability</span></div>
        <div class="stat s-blast"><b>${m.peak_blast_radius}</b><span>peak blast</span></div>
        <div class="stat s-risk"><b>${m.peak_down}</b><span>nodes down</span></div>
        <div class="stat s-time"><b>${m.mean_error_rate.toFixed(1)}%</b><span>mean errors</span></div>
      </div>
    </div>`;

  const d = r.delta || {};
  const chip = (label, val, goodWhenLower) => {
    if (val === null || val === undefined) return "";
    const positive = val > 0;
    const good = goodWhenLower ? !positive : positive;
    const cls = val === 0 ? "" : good ? "good" : "bad";
    const sign = val > 0 ? "+" : "";
    return `<span class="chip-d ${cls}">${esc(label)} ${sign}${val}</span>`;
  };

  const chart = blastChart(r.a.series, r.b.series, r.label_a, r.label_b);

  $("wiResult").innerHTML = `
    <div class="wi-cards">
      ${card("A", "tag-a", r.label_a, r.a.metrics)}
      ${card("B", "tag-b", r.label_b, r.b.metrics)}
    </div>
    <div class="wi-chart">
      <h4>Blast radius over time — services affected</h4>
      ${chart}
      <div class="wi-delta">
        ${chip("availability", d.availability, false)}
        ${chip("peak blast", d.peak_blast_radius, true)}
        ${chip("nodes down", d.peak_down, true)}
        ${chip("mean errors", d.mean_error_rate, true)}
        ${chip("service-units lost", d.service_units_lost, true)}
      </div>
    </div>`;
}

function blastChart(a, b, la, lb) {
  const W = 700, H = 68, PAD = 20;
  const all = [...(a || []), ...(b || [])];
  if (!all.length) return '<p class="empty">No samples.</p>';
  const maxT = Math.max(...all.map((s) => s.t)) || 1;
  const maxV = Math.max(1, ...all.map((s) => s.blast));
  const pt = (s) => [
    PAD + (s.t / maxT) * (W - PAD * 2),
    H - PAD - (s.blast / maxV) * (H - PAD * 1.7),
  ];
  const path = (series) => (series || []).length
    ? `<polyline fill="none" stroke-width="2.2" stroke-linejoin="round"
         points="${series.map((s) => pt(s).map((v) => v.toFixed(1)).join(",")).join(" ")}"/>`
    : "";

  const grid = [0, 0.5, 1].map((f) => {
    const y = H - PAD - f * (H - PAD * 1.7);
    return `<line x1="${PAD}" y1="${y}" x2="${W - PAD}" y2="${y}"
              stroke="rgba(148,163,184,.12)" stroke-width="1"/>
            <text x="4" y="${y + 3.5}" fill="#7488a3" font-size="9"
              font-family="monospace">${Math.round(f * maxV)}</text>`;
  }).join("");

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}"
            role="img" aria-label="Blast radius over time for both variants">
    ${grid}
    <g stroke="#38bdf8">${path(a)}</g>
    <g stroke="#f7b955">${path(b)}</g>
    <g font-size="9.5" font-family="monospace" fill="#7488a3">
      <rect x="${W - 210}" y="6" width="9" height="3" fill="#38bdf8"/>
      <text x="${W - 197}" y="11">${esc(la)}</text>
      <rect x="${W - 210}" y="20" width="9" height="3" fill="#f7b955"/>
      <text x="${W - 197}" y="25">${esc(lb)}</text>
      <text x="${PAD}" y="${H - 6}">0s</text>
      <text x="${W - PAD - 24}" y="${H - 6}">${maxT.toFixed(0)}s</text>
    </g>
  </svg>`;
}

/* ════════════════════════ TOPOLOGY STUDIO ════════════════════════ */

const KINDS = ["gateway", "service", "function", "database", "cache",
               "queue", "stream", "storage", "external", "ml"];

export function initTopologyStudio() {
  $("nKind").innerHTML = KINDS.map((k) => `<option value="${k}">${k}</option>`).join("");
  $("eKind").innerHTML = ["sync", "db", "cache", "async", "stream"]
    .map((k) => `<option value="${k}">${k}</option>`).join("");

  $("nodeForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("nId").value.trim().replace(/\s+/g, "-").toLowerCase();
    const label = $("nLabel").value.trim() || id;
    if (!id) return;
    const topo = currentTopology();
    if (topo.nodes.some((n) => n.id === id)) {
      jsonMsg(`A service with id “${id}” already exists.`, true);
      return;
    }
    topo.nodes.push({ id, label, kind: $("nKind").value, description: "", layer: 0 });
    updateTopology(topo);
    pushSystemLog("INFO", `operator added service ${label} (${id})`);
    $("nId").value = ""; $("nLabel").value = "";
  });

  $("edgeForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const source = $("eFrom").value, target = $("eTo").value;
    if (!source || !target) return;
    if (source === target) { jsonMsg("A service cannot depend on itself.", true); return; }
    const topo = currentTopology();
    if (topo.edges.some((x) => x.source === source && x.target === target)) {
      jsonMsg(`Edge ${source} → ${target} already exists.`, true); return;
    }
    topo.edges.push({ source, target, kind: $("eKind").value });
    updateTopology(topo);
    pushSystemLog("INFO", `operator added dependency ${source} → ${target}`);
  });

  $("btnValidate").addEventListener("click", async () => {
    const raw = $("jsonBox").value.trim();
    if (!raw) { jsonMsg("Paste a topology JSON document first.", true); return; }
    try {
      const res = await fetch("/api/topologies/validate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload: raw }),
      });
      const data = await res.json();
      if (data.valid) {
        const warn = (data.warnings || []).length
          ? ` Warnings: ${data.warnings.join(" · ")}` : "";
        jsonMsg(`Valid — ${data.nodes} services, ${data.edges} dependencies, ` +
                `fingerprint ${data.fingerprint}.${warn}`);
      } else jsonMsg(`Issues: ${data.issues.join(" · ")}`, true);
    } catch (err) {
      jsonMsg(`Validation failed: ${err.message}`, true);
    }
  });

  $("btnApplyJson").addEventListener("click", () => {
    const raw = $("jsonBox").value.trim();
    if (!raw) { jsonMsg("Paste a topology JSON document first.", true); return; }
    try {
      const parsed = JSON.parse(raw);
      const topo = normalise(parsed);
      updateTopology(topo);
      jsonMsg("Topology applied — the predictor is already re-running against it.");
      pushSystemLog("INFO", `operator imported topology “${topo.name}” (${topo.nodes.length} nodes)`);
    } catch (err) {
      jsonMsg(`Parse error: ${err.message}`, true);
    }
  });

  $("btnExport").addEventListener("click", () => {
    $("jsonBox").value = JSON.stringify(currentTopology(), null, 2);
    jsonMsg("Current topology written to the box — copy it or download it.");
  });

  on("topology_applied", (m) => {
    jsonMsg(`Applied: ${m.nodes} services, ${m.edges} dependencies.` +
            (m.issues.length ? ` Warnings: ${m.issues.join(" · ")}` : ""));
    refreshStudio();
  });
  on("validation", (issues) => {
    jsonMsg(issues.length ? `Rejected: ${issues.join(" · ")}` : "Topology accepted.", !!issues.length);
  });
}

function jsonMsg(text, bad = false) {
  const el = $("jsonMsg");
  el.textContent = text;
  el.style.color = bad ? "#ff7a5c" : "";
}

export function normalise(data) {
  const nodes = (data.nodes || data.services || []).map((n) =>
    typeof n === "string"
      ? { id: n, label: n, kind: "service", description: "", layer: 0 }
      : { id: String(n.id), label: n.label || String(n.id),
          kind: n.kind || "service", description: n.description || "", layer: n.layer || 0 });
  let edges = data.edges || [];
  if (!edges.length && data.depends_on) {
    edges = [];
    if (Array.isArray(data.depends_on)) {
      // [{"source":…,"target":…}] or [["a","b"]]
      for (const e of data.depends_on) {
        if (Array.isArray(e)) edges.push({ source: e[0], target: e[1], kind: e[2] });
        else edges.push(e);
      }
    } else {
      for (const [src, tgts] of Object.entries(data.depends_on)) {
        for (const t of Array.isArray(tgts) ? tgts : [tgts]) edges.push({ source: src, target: t });
      }
    }
  }
  return {
    name: data.name || "Imported topology",
    description: data.description || "",
    meta: data.meta || {},
    nodes,
    edges: edges.map((e, i) => ({
      id: e.id || `e${i}`, source: String(e.source), target: String(e.target),
      kind: e.kind || "sync", weight: e.weight ?? null, latency: e.latency ?? null,
    })),
  };
}

export function currentTopology() {
  const s = store.state;
  if (!s) return { name: "empty", nodes: [], edges: [] };
  return {
    name: s.topology.name,
    description: s.topology.description,
    meta: s.meta || {},
    nodes: (s.nodes || []).map((n) => ({
      id: n.id, label: n.label, kind: n.kind,
      description: n.description, layer: n.layer, x: n.x, y: n.y,
    })),
    edges: (s.edges || []).map((e) => ({
      id: e.id, source: e.source, target: e.target,
      kind: e.kind, weight: e.weight, latency: e.latency ?? null,
    })),
  };
}

export function refreshStudio() {
  const topo = currentTopology();
  const opts = topo.nodes.map((n) => `<option value="${esc(n.id)}">${esc(n.label)}</option>`).join("");
  $("eFrom").innerHTML = opts;
  $("eTo").innerHTML = opts;
  if (topo.nodes.length > 1) $("eTo").selectedIndex = 1;
  $("tsCount").textContent = `${topo.nodes.length} / ${topo.edges.length} edges`;
  $("nodeList").innerHTML = topo.nodes.map((n) => `
    <li>
      <span>${esc(n.label)}</span>
      <span class="kn">${esc(n.kind)}</span>
      <button class="del" type="button" data-del="${esc(n.id)}"
              aria-label="Remove ${esc(n.label)}" title="Remove service">×</button>
    </li>`).join("") || '<li><span class="empty">No services — add one below.</span></li>';

  $("nodeList").querySelectorAll("[data-del]").forEach((b) => {
    b.addEventListener("click", () => {
      const id = b.dataset.del;
      const t = currentTopology();
      t.nodes = t.nodes.filter((n) => n.id !== id);
      t.edges = t.edges.filter((e) => e.source !== id && e.target !== id);
      updateTopology(t);
      pushSystemLog("WARN", `operator removed service ${id}`);
    });
  });
}

/* ══════════════════════════ REPORT ══════════════════════════ */

let lastReportHtml = "";

export function initReport() {
  $("btnGenReport").addEventListener("click", () => {
    $("repStatus").textContent = "Compiling incident report…";
    requestReport();
  });
  $("btnReportTop").addEventListener("click", () => {
    document.getElementById("tab-report").click();
    $("btnGenReport").click();
  });
  $("btnDlReport").addEventListener("click", () => {
    if (!lastReportHtml) return;
    const blob = new Blob([lastReportHtml], { type: "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "cascadeiq-incident-report.html";
    a.click();
    URL.revokeObjectURL(a.href);
  });
  on("report", renderReport);
}

function renderReport(r) {
  $("repStatus").textContent = `Generated ${r.generated} · ${r.sim_seconds}s simulated`;
  const rc = r.root_cause || {};
  const act = r.recommended_action || {};
  const cur = r.impact?.current || {};

  const rows = (r.alternatives || []).map((a, i) => `
    <tr>
      <td>#${i + 1}</td>
      <td>${esc(a.strategy)}${i === 0 ? ' <b style="color:#2ee6a8">★</b>' : ""}</td>
      <td class="num">${a.success.toFixed(1)}%</td>
      <td class="num">${a.risk.toFixed(1)}%</td>
      <td class="num">${a.time.toFixed(0)}s</td>
      <td class="num">${a.blast_after}</td>
    </tr>`).join("");

  const events = (r.timeline || []).map((e) => `
    <tr><td class="num">${e.t.toFixed(1)}s</td><td>${esc(e.kind)}</td>
        <td>${esc(e.title)}</td><td>${esc(e.detail || "")}</td></tr>`).join("");

  const forecast = (r.forecast || []).map((f) =>
    `<li>${esc(f.node)} — ${f.confidence.toFixed(0)}% within ~${f.eta.toFixed(0)}s</li>`).join("")
    || "<li>No further propagation predicted.</li>";

  $("repOut").innerHTML = `
    <h3>${esc(r.title)}</h3>
    <p class="rep-meta">Generated ${esc(r.generated)} · topology <b>${esc(r.topology)}</b>
      (fingerprint <span class="mono">${esc(r.fingerprint)}</span>) ·
      ${r.scale.nodes} services / ${r.scale.edges} dependencies ·
      ${r.sim_seconds}s simulated</p>

    <h4>Impact</h4>
    <div class="rep-grid">
      <div class="rep-box"><b style="color:#ff3d71">${r.impact.peak_blast_radius}</b><span>peak blast radius</span></div>
      <div class="rep-box"><b style="color:#ff7a5c">${r.impact.peak_down}</b><span>peak services down</span></div>
      <div class="rep-box"><b style="color:#2ee6a8">${r.impact.availability.toFixed(1)}%</b><span>availability</span></div>
      <div class="rep-box"><b>${cur.down ?? 0}</b><span>currently down</span></div>
    </div>

    <h4>Root cause</h4>
    <p>${rc.node
        ? `<b>${esc(rc.node)}</b> at <b>${rc.confidence.toFixed(1)}% confidence` +
          `${rc.fault ? ` — ${esc(rc.fault)}` : ""}.</b><br>${esc(rc.why || "")}`
        : "No root cause isolated during this incident."}</p>
    ${rc.path?.length ? `<p><b>Cascade path:</b> ${esc(rc.path.join("  →  "))}</p>` : ""}

    <h4>Forecast at report time</h4>
    <ul>${forecast}</ul>

    <h4>Recommended action</h4>
    <p><b>${esc(act.strategy || "—")}</b> —
       ${act.success != null ? `${act.success.toFixed(0)}% success, ` : ""}
       ${act.time != null ? `${act.time.toFixed(0)}s to complete, ` : ""}
       ${act.risk != null ? `${act.risk.toFixed(0)}% risk.` : ""}</p>
    <p>${esc(act.rationale || "")}</p>
    <ul>${(act.factors || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>

    <h4>Strategy comparison</h4>
    <table>
      <thead><tr><th>#</th><th>Strategy</th><th>Success</th><th>Risk</th>
        <th>Time</th><th>Left affected</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>

    <h4>Event timeline</h4>
    <table>
      <thead><tr><th>t</th><th>Type</th><th>Event</th><th>Detail</th></tr></thead>
      <tbody>${events}</tbody>
    </table>

    <h4>Model provenance</h4>
    <p class="rep-meta">${esc(r.model?.name || "")} — ${esc(r.model?.method || "")}.
       Evaluated over ${r.model?.nodes_seen ?? 0} nodes and
       ${r.model?.edges_seen ?? 0} edges. The engine has no per-application rules;
       every figure above was derived from graph structure and observable signals.</p>`;

  lastReportHtml = buildDownloadable(r);
  $("btnDlReport").disabled = false;
  $("repOut").scrollTop = 0;
}

function buildDownloadable(r) {
  const body = $("repOut").innerHTML;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>${esc(r.title)}</title>
<style>
 body{font:14px/1.6 system-ui,sans-serif;background:#0b0f17;color:#e6ecf5;
      max-width:900px;margin:0 auto;padding:32px}
 h3{margin:0 0 4px} h4{color:#5eead4;text-transform:uppercase;letter-spacing:.09em;
      font-size:11px;margin:22px 0 8px}
 .rep-meta{color:#7488a3;font-size:12px}
 .rep-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
 .rep-\boxed{background:#0f141e;border:1px solid #1d2637;border-radius:8px;padding:10px 12px}
 .rep-box b{display:block;font-size:18px}
 .rep-box span{font-size:10px;color:#7488a3;text-transform:uppercase}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1d2637}
 th{color:#7488a3;font-size:10px;text-transform:uppercase}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 li{font-size:13px} p{font-size:13px;color:#a9b7cb}
</style></head><body>${body}</body></html>`;
}
