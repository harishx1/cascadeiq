/* Right-rail + header + timeline rendering. */

import { store, applyRecovery } from "./state.js";

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function fmtClock(sec) {
  const s = Math.max(0, sec || 0);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${String(m).padStart(2, "0")}:${r.toFixed(1).padStart(4, "0")}`;
}

/* ───────────────────────────── header ───────────────────────────── */

export function renderHeader(state) {
  if (!state) return;
  $("simClock").textContent = fmtClock(state.t);
  const s = state.health_summary || {};
  $("hpHealthy").textContent = s.healthy ?? 0;
  $("hpDegraded").textContent = s.degraded ?? 0;
  $("hpFailing").textContent = s.failing ?? 0;
  $("hpDown").textContent = s.down ?? 0;
  $("uptimeVal").textContent = `${(state.uptime ?? 100).toFixed(1)}%`;
  $("peakVal").textContent = state.peaks?.blast ?? 0;
  $("dockStat").textContent =
    `${state.nodes.length} services · ${state.edges.length} dependencies · ${state.topology.name}`;
}

/* ─────────────────────────── prediction ─────────────────────────── */

function ring(conf, color) {
  const r = 22, c = 2 * Math.PI * r;
  const off = c * (1 - conf / 100);
  return `<svg width="54" height="54" viewBox="0 0 54 54" aria-hidden="true">
    <circle cx="27" cy="27" r="${r}" fill="none" stroke="#1d2637" stroke-width="6"/>
    <circle cx="27" cy="27" r="${r}" fill="none" stroke="${color}" stroke-width="6"
      stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/>
  </svg><b style="color:${color}">${Math.round(conf)}%</b>`;
}

export function renderPrediction(a) {
  const rootEl = $("rootCause");
  const badge = $("rcBadge");
  const pathEl = $("cascadePath");
  const nextEl = $("nextFailures");

  if (!a) {
    badge.className = "badge badge-idle";
    badge.textContent = "waiting";
    return;
  }

  $("rationale").textContent = a.headline || "—";

  const rc = a.root_cause;
  if (!rc) {
    badge.className = "badge badge-idle";
    badge.textContent = a.anomalous_count ? "unclear" : "nominal";
    rootEl.innerHTML = `<p class="empty">${
      a.anomalous_count ? "Anomalies present — origin not yet isolated."
                        : "No anomalies detected. All services within baseline."}</p>`;
  } else {
    badge.className = "badge badge-alert";
    badge.textContent = "isolated";
    const fault = rc.fault
      ? `${esc(rc.fault_label)} · severity ${rc.fault.severity}/10`
      : "propagated stress — no direct fault on this node";
    rootEl.innerHTML = `
      <div class="rc-meter">
        <div class="rc-ring">${ring(rc.confidence, "#ff3d71")}</div>
        <div style="min-width:0">
          <p class="rc-name">${esc(rc.label)}</p>
          <p class="rc-sub">${esc(fault)}<br>kind: ${esc(rc.kind)} · reached ${rc.reach} node(s)</p>
        </div>
      </div>
      <p class="rc-why">${esc(rc.why)}</p>`;
  }

  // cascade path
  const path = a.cascade_path || [];
  if (!path.length) {
    pathEl.innerHTML = `<p class="empty">No propagation observed yet.</p>`;
  } else {
    pathEl.innerHTML = path.map((c, i) => `
      <div class="path-chip${i ? " has-from" : ""}">
        <span class="step">${i + 1}</span>
        <span>${esc(c.label)}</span>
        <span class="kind">${esc(c.kind)}</span>
      </div>`).join("");
  }

  // predicted next failures
  const next = a.next_failures || [];
  if (!next.length) {
    nextEl.innerHTML = `<p class="empty">Nothing predicted — no propagation path under current settings.</p>`;
    return;
  }
  nextEl.innerHTML = next.map((f) => {
    const col = f.confidence > 70 ? "#ff7a5c" : f.confidence > 40 ? "#f7b955" : "#38bdf8";
    const drivers = (f.drivers || []).slice(0, 2)
      .map((d) => `${esc(d.label)} ${Math.round(d.contribution)}`)
      .join("  ·  ");
    return `<div class="nf">
      <span class="nf-name"><i class="dot" style="background:${col}"></i>${esc(f.label)}</span>
      <span class="nf-eta">~${f.eta.toFixed(0)}s</span>
      <span class="nf-bar"><i style="width:${f.confidence}%;background:${col}"></i></span>
      <span class="nf-src">${f.confidence.toFixed(0)}% confidence${drivers ? ` — driven by ${drivers}` : ""}</span>
    </div>`;
  }).join("");
}

/* ─────────────────────────── recovery ─────────────────────────── */

export function renderRecovery(r) {
  const list = $("recoveryList");
  const factors = $("decisionFactors");
  const rationale = $("rationale");

  if (!r || !r.strategies || !r.strategies.length) {
    list.innerHTML = `<p class="empty">No incident to recover from.</p>`;
    factors.innerHTML = "";
    if (!r) rationale.textContent = "Awaiting an incident.";
    return;
  }
  if (r.rationale) {
    rationale.textContent = r.rationale;
    rationale.title = r.rationale;
  }

  list.innerHTML = r.strategies.map((s) => {
    const ok = s.success.toFixed(0);
    // only the winning strategy keeps its explanation in the flow — the others
    // carry it as a tooltip so the ranked list stays scannable
    return `<article class="rec ${s.recommended ? "is-rec" : ""}">
      <div class="rec-top">
        <span class="rec-rank">#${s.rank}</span>
        <h4 class="rec-title" title="${esc(s.label)} — ${esc(s.blurb)}">${esc(s.label)}</h4>
      </div>
      ${s.recommended ? `<p class="rec-blurb">${esc(s.blurb)}</p>` : ""}
      <div class="rec-stats">
        <div class="stat s-ok"><b>${ok}%</b><span>success</span></div>
        <div class="stat s-risk"><b>${s.risk.toFixed(0)}%</b><span>risk</span></div>
        <div class="stat s-time"><b>${s.estimated_time.toFixed(0)}s</b><span>time</span></div>
        <div class="stat s-blast"><b>${s.blast_radius_after}</b><span>left</span></div>
      </div>
      <div class="rec-actions">
        ${s.recommended ? '<span class="rec-flag">Recommended</span>' : ""}
        <button class="btn ${s.recommended ? "btn-primary" : "btn-soft"}"
                type="button" data-apply="${esc(s.id)}"
                aria-label="Apply ${esc(s.label)}">Apply</button>
      </div>
    </article>`;
  }).join("");

  list.querySelectorAll("[data-apply]").forEach((b) => {
    b.addEventListener("click", () => applyRecovery(b.dataset.apply));
  });

  factors.innerHTML = (r.decision_factors || [])
    .map((f) => `<li>${esc(f)}</li>`).join("");
}

/* ───────────────────────────── timeline ───────────────────────────── */

export function renderTimeline(state) {
  const scrub = $("scrub");
  const hist = store.history;
  const max = Math.max(0, hist.length - 1);
  const live = store.scrub < 0;

  if (+scrub.max !== max) scrub.max = String(max);
  scrub.value = live ? String(max) : String(store.scrub);
  $("tlClock").textContent = fmtClock(state ? state.t : 0);
  $("tlEnd").textContent = hist.length ? fmtClock(hist[hist.length - 1].t) : "00:00.0";
  $("btnLive").setAttribute("aria-pressed", live ? "true" : "false");
  $("scrubBadge").hidden = live;
  $("btnBackLive").hidden = live;

  // markers are drawn proportionally across the track
  const marks = state?.timeline || [];
  const host = $("tlMarkers");
  if (host.childElementCount !== marks.length) {
    host.innerHTML = marks.map((m) =>
      `<i class="mk mk-${m.level === "critical" ? "critical" : m.level === "warn" ? "warn" : m.level === "ok" ? "ok" : "info"}"></i>`
    ).join("");
  }
  const span = hist.length ? (hist[hist.length - 1].t - hist[0].t) : 0;
  const kids = host.children;
  for (let i = 0; i < marks.length; i++) {
    const pct = span > 0 ? ((marks[i].t - hist[0].t) / span) * 100 : 0;
    kids[i].style.left = `${Math.max(0, Math.min(100, pct))}%`;
    kids[i].title = `${fmtClock(marks[i].t)} — ${marks[i].title}`;
  }
}

/* ───────────────────────────── tooltip ───────────────────────────── */

export function showTip(node, sx, sy) {
  const tip = $("graphTip");
  if (!node) { tip.hidden = true; return; }
  const wrap = tip.parentElement.getBoundingClientRect();
  const col = { healthy: "#2ee6a8", degraded: "#f7b955", failing: "#ff7a5c", down: "#ff3d71" }[node.health] || "#94a3b8";
  tip.innerHTML = `
    <span class="gk">${esc(node.kind)}</span>
    <h4 style="color:${col}">${esc(node.label)}</h4>
    <dl>
      <dt>health</dt><dd style="color:${col}">${esc(node.health)}</dd>
      <dt>stress</dt><dd>${node.stress.toFixed(0)}%</dd>
      <dt>latency</dt><dd>${node.latency.toFixed(0)} ms</dd>
      <dt>errors</dt><dd>${node.error_rate.toFixed(1)}%</dd>
      <dt>throughput</dt><dd>${node.throughput.toFixed(0)}/s</dd>
      <dt>cpu / mem</dt><dd>${node.cpu.toFixed(0)}% / ${node.mem.toFixed(0)}%</dd>
      ${node.fault ? `<dt>fault</dt><dd style="color:#ff3d71">${esc(node.fault.type)}</dd>` : ""}
    </dl>`;
  tip.hidden = false;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = sx + 16, y = sy - th / 2;
  if (x + tw > wrap.width - 8) x = sx - tw - 16;
  y = Math.max(6, Math.min(wrap.height - th - 6, y));
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
