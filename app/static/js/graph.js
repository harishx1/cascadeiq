/* Force-directed dependency graph rendered on canvas.
   Positions are physics-driven; node/edge appearance is driven entirely by
   the live snapshot so nothing here knows what any service actually does. */

const HAS_RRECT = typeof CanvasRenderingContext2D !== "undefined" &&
  typeof CanvasRenderingContext2D.prototype.roundRect === "function";

const HEALTH = {
  healthy:  { fill: "#123b31", stroke: "#2ee6a8", glow: "rgba(46,230,168,.55)",  text: "#2ee6a8" },
  degraded: { fill: "#3d2f14", stroke: "#f7b955", glow: "rgba(247,185,85,.55)",  text: "#f7b955" },
  failing:  { fill: "#3d1f18", stroke: "#ff7a5c", glow: "rgba(255,122,92,.6)",   text: "#ff7a5c" },
  down:     { fill: "#3d1022", stroke: "#ff3d71", glow: "rgba(255,61,113,.7)",   text: "#ff3d71" },
};

const KIND_SHAPE = {
  gateway: "hex", database: "square", storage: "square",
  external: "diamond", queue: "ring", stream: "ring",
  cache: "circle", service: "circle", function: "circle", ml: "circle",
};

const PAD = 56;

export class GraphView {
  constructor(canvas, handlers = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.h = handlers;
    this.nodes = [];
    this.edges = [];
    this.byIdx = new Map();
    this.edgeIdx = new Map();
    this.alpha = 0;
    this.selectedId = null;
    this.hoverId = null;
    this.kbIndex = -1;
    this.time = 0;
    this.running = true;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    this.view = { x: 0, y: 0, k: 1 };
    this._resize();
    this._bind();
    this._loop();
  }

  /* ------------------------------------------------------------ lifecycle */

  _resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = this.canvas.getBoundingClientRect();
    this.w = Math.max(320, r.width);
    this.hgt = Math.max(240, r.height);
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.hgt * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._cacheOverlay();
    if (this.nodes.length) { this._rescale(); this._relayout(false); }
  }

  /** Re-derive node radii after the canvas changes size, keeping positions. */
  _rescale() {
    const layers = this.nodes.map((n) => n.layer ?? 0);
    const m = this._metrics(layers);
    this.rCap = m.rCap;
    for (const n of this.nodes) n.r = Math.min(m.rCap, 14 + n.deg * 1.5);
  }

  _bind() {
    new ResizeObserver(() => this._resize()).observe(this.canvas);

    this.canvas.addEventListener("pointermove", (e) => this._onMove(e));
    this.canvas.addEventListener("pointerdown", (e) => this._onDown(e));
    this.canvas.addEventListener("pointerup", () => this._endDrag());
    this.canvas.addEventListener("pointerleave", () => {
      this._endDrag();
      this.hoverId = null;
      this.h.onHover?.(null);
    });
    this.canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = this.canvas.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      const k = Math.min(3, Math.max(0.35, this.view.k * factor));
      const real = k / this.view.k;
      this.view.x = mx - (mx - this.view.x) * real;
      this.view.y = my - (my - this.view.y) * real;
      this.view.k = k;
    }, { passive: false });

    this.canvas.addEventListener("keydown", (e) => this._onKey(e));
  }

  destroy() { this.running = false; }

  /* ---------------------------------------------------------------- data */

  setData(nodes, edges) {
    const ids = nodes.map((n) => n.id).join("|");
    const same = this.nodes.length === nodes.length &&
      this.nodes.every((n, i) => n.id === nodes[i]?.id);
    if (!same) this._build(nodes, edges);
    else this._refresh(nodes, edges);
  }

  /** Horizontal padding shrinks on narrow canvases so rows can use the width. */
  _padX() { return Math.max(12, Math.min(PAD, this.w * 0.05)); }

  /** Vertical padding shrinks on short canvases so rows get real separation. */
  _padY() { return Math.max(14, Math.min(PAD, this.hgt * 0.07)); }

  /** Row spacing for the current layer count, used to size nodes so that a
      node plus its caption always fits inside its own band without the
      caption of the row above or below ever colliding with it. */
  _metrics(layerList) {
    const p = this._padY();
    const maxLayer = Math.max(0, ...layerList);
    const band = Math.max(48, this.hgt - p * 2 - 34);
    const rowGap = band / Math.max(1, maxLayer);
    // a node needs r + gap + caption above AND below: 2r + 42 <= rowGap
    return { p, maxLayer, rowGap, rCap: Math.max(8, Math.min(25, (rowGap - 44) / 2)) };
  }

  /** Screen-space rects of the HUD chips, reused as no-label zones. */
  _cacheOverlay() {
    const host = this.canvas.parentElement;
    if (!host) { this._obs = []; return; }
    const cr = this.canvas.getBoundingClientRect();
    const obs = [];
    for (const el of host.querySelectorAll(".legend, .hud-tl, .hud-tr")) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        obs.push({
          x: r.left - cr.left - 7, y: r.top - cr.top - 5,
          w: r.width + 14, h: r.height + 10,
        });
      }
    }
    this._obs = obs;
  }

  _rowY(layer, maxLayer) {
    const p = this._padY();
    return p + (maxLayer === 0 ? 0.5 : layer / maxLayer) * (this.hgt - p * 2 - 34);
  }

  _build(nodes, edges) {
    const cols = new Map();
    for (const n of nodes) {
      const l = n.layer ?? 0;
      if (!cols.has(l)) cols.set(l, []);
      cols.get(l).push(n);
    }
    const maxLayer = Math.max(0, ...[...cols.keys()]);
    const m = this._metrics([...cols.keys()]);

    this.nodes = nodes.map((n) => {
      const l = n.layer ?? 0;
      const col = cols.get(l);
      const i = col.indexOf(n);
      const y = this._rowY(l, maxLayer);
      const x = this.w * ((i + 1) / (col.length + 1));
      const deg = edges.filter((e) => e.source === n.id || e.target === n.id).length;
      return {
        id: n.id, label: n.label, kind: n.kind, layer: l, ci: i,
        x, y, vx: 0, vy: 0, deg, pinned: false,
        r: Math.min(m.rCap, 14 + deg * 1.5),
        data: n,
      };
    });

    this.byIdx = new Map(this.nodes.map((n) => [n.id, n]));
    const norm = (id) => this.byIdx.get(id);
    this.edges = edges.map((e) => ({
      id: e.id, s: norm(e.source), t: norm(e.target),
      kind: e.kind, weight: e.weight ?? 1, data: e,
    })).filter((e) => e.s && e.t && e.s !== e.t);
    this.edgeIdx = new Map(this.edges.map((e) => [e.id, e]));
    this.alpha = 1;
    this.kbIndex = -1;
    this.rCap = m.rCap;
    this._relayout(true);
  }

  _refresh(nodes, edges) {
    for (const n of nodes) {
      const cur = this.byIdx.get(n.id);
      if (cur) {
        cur.data = n;
        cur.label = n.label;
        cur.kind = n.kind;
      }
    }
    // edge sets can change while the node list stays identical (the topology
    // studio adds dependencies) — rebuild the edge index in that case
    const sameEdges = edges.length === this.edges.length &&
      edges.every((e) => this.edgeIdx.has(e.id));
    if (sameEdges) {
      for (const e of edges) {
        const cur = this.edgeIdx.get(e.id);
        if (cur) cur.data = e;
      }
      return;
    }
    const norm = (id) => this.byIdx.get(id);
    this.edges = edges.map((e) => ({
      id: e.id, s: norm(e.source), t: norm(e.target),
      kind: e.kind, weight: e.weight ?? 1, data: e,
    })).filter((e) => e.s && e.t && e.s !== e.t);
    this.edgeIdx = new Map(this.edges.map((e) => [e.id, e]));
    for (const n of this.nodes) {
      n.deg = this.edges.reduce((k, e) => k + (e.s === n || e.t === n ? 1 : 0), 0);
      n.r = Math.min(this.rCap ?? 25, 14 + n.deg * 1.5);
    }
    this.alpha = Math.max(this.alpha, 0.55);
  }

  _relayout(rebuild) {
    if (rebuild) {
      const cols = new Map();
      for (const n of this.nodes) {
        const l = n.layer ?? 0;
        if (!cols.has(l)) cols.set(l, []);
        cols.get(l).push(n);
      }
      const m = this._metrics([...cols.keys()]);
      this.rCap = m.rCap;
      for (const [l, col] of cols) {
        const y = this._rowY(l, m.maxLayer);
        col.forEach((n, i) => {
          n.ci = i;
          n.y = y;
          n.x = this.w * ((i + 1) / (col.length + 1));
          n.vx = 0; n.vy = 0;
          n.pinned = false;
          n.r = Math.min(m.rCap, 14 + n.deg * 1.5);
        });
      }
    }
    this.alpha = 1;
  }

  relayout() { this._relayout(true); }

  /* ------------------------------------------------------------ physics */

  _tick() {
    if (this.alpha < 0.02) return;
    const a = this.alpha;
    const ns = this.nodes;
    const n = ns.length;

    // repulsion (O(n²) is fine below ~150 nodes; sampled beyond that)
    const step = n > 150 ? 2 : 1;
    for (let i = 0; i < n; i++) {
      const A = ns[i];
      for (let j = i + 1; j < n; j += step) {
        const B = ns[j];
        let dx = A.x - B.x, dy = A.y - B.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = (Math.random() - 0.5) * 2; dy = (Math.random() - 0.5) * 2; d2 = 4; }
        const f = (3400 * a) / d2;
        const d = Math.sqrt(d2);
        A.vx += (dx / d) * f; A.vy += (dy / d) * f;
        B.vx -= (dx / d) * f; B.vy -= (dy / d) * f;
      }
    }

    // springs
    for (const e of this.edges) {
      const A = e.s, B = e.t;
      const dx = B.x - A.x, dy = B.y - A.y;
      const d = Math.hypot(dx, dy) || 1;
      const rest = A.r + B.r + 76;
      const f = ((d - rest) * 0.032) * a;
      A.vx += (dx / d) * f; A.vy += (dy / d) * f;
      B.vx -= (dx / d) * f; B.vy -= (dy / d) * f;
    }

    // layer anchoring keeps the diagram reading top-to-bottom
    const cols = new Map();
    for (const nd of ns) {
      const l = nd.layer ?? 0;
      if (!cols.has(l)) cols.set(l, []);
      cols.get(l).push(nd);
    }
    const maxLayer = Math.max(0, ...[...cols.keys()]);
    const px = this._padX();
    const span = Math.max(120, this.w - px * 2);
    for (const [l, col] of cols) {
      const ty = this._rowY(l, maxLayer);
      col.forEach((nd, i) => {
        if (nd.pinned) return;
        const tx = px + span * ((i + 1) / (col.length + 1));
        nd.vy += (ty - nd.y) * 0.34 * a;
        nd.vx += (tx - nd.x) * 0.34 * a;
      });
    }

    for (const nd of ns) {
      if (nd.pinned || nd.id === this.dragId) { nd.vx = 0; nd.vy = 0; continue; }
      nd.vx *= 0.82; nd.vy *= 0.82;
      nd.x += nd.vx; nd.y += nd.vy;
      nd.x = Math.min(this.w - nd.r - 8, Math.max(nd.r + 8, nd.x));
      nd.y = Math.min(this.hgt - nd.r - 8, Math.max(nd.r + 8, nd.y));
    }
    this.alpha *= 0.985;
  }

  /* ------------------------------------------------------------ rendering */

  _loop() {
    if (!this.running) return;
    const step = () => {
      if (!this.running) return;
      this.time = performance.now();
      this._tick();
      this._draw();
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  _world() {
    const { x, y, k } = this.view;
    return (sx, sy) => [(sx - x) / k, (sy - y) / k];
  }

  _toScreen(wx, wy) {
    return [wx * this.view.k + this.view.x, wy * this.view.k + this.view.y];
  }

  _draw() {
    const c = this.ctx;
    c.save();
    c.clearRect(0, 0, this.w, this.hgt);
    this._grid(c);
    c.translate(this.view.x, this.view.y);
    c.scale(this.view.k, this.view.k);

    this._labelBoxes = [];
    const vx = this.view.x, vy = this.view.y, vk = this.view.k || 1;
    for (const o of this._obs || []) {
      this._labelBoxes.push({
        x: (o.x - vx) / vk, y: (o.y - vy) / vk,
        w: o.w / vk, h: o.h / vk, isObs: true,
      });
    }
    for (const e of this.edges) this._edge(c, e);
    // unhealthy and selected nodes are painted last so they sit on top and
    // their captions get first refusal of a free slot in the declutter pass
    const prio = (n) => {
      const d = n.data || {};
      const h = d.health;
      let p = h === "down" ? 3 : h === "failing" ? 2 : h === "degraded" ? 1 : 0;
      if (d.isRoot || d.isPredicted) p += 2;
      if (n.id === this.selectedId || n.id === this.hoverId) p += 4;
      return p;
    };
    const order = this.nodes.length < 80 ? this.nodes.slice()
      .sort((a, b) => prio(a) - prio(b)) : this.nodes;
    for (const n of order) this._node(c, n);

    c.restore();
  }

  _grid(c) {
    c.save();
    c.strokeStyle = "rgba(148,163,184,.045)";
    c.lineWidth = 1;
    for (let x = 0; x < this.w; x += 34) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, this.hgt); c.stroke(); }
    for (let y = 0; y < this.hgt; y += 34) { c.beginPath(); c.moveTo(0, y); c.lineTo(this.w, y); c.stroke(); }
    c.restore();
  }

  _edge(c, e) {
    const d = e.data;
    if (!d) return;
    const tripped = !!d.tripped;
    const load = Math.min(1, (d.propagated || 0) / 100);

    const mx = (e.s.x + e.t.x) / 2;
    const my = (e.s.y + e.t.y) / 2;
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const len = Math.hypot(dx, dy) || 1;
    const cx = mx - (dy / len) * len * 0.07;
    const cy = my + (dx / len) * len * 0.07;

    c.save();
    if (tripped) {
      c.setLineDash([4, 5]);
      c.strokeStyle = "rgba(247,185,85,.42)";
      c.lineWidth = 1.2;
    } else {
      c.setLineDash([]);
      const r = Math.round(120 + load * 135);
      const g = Math.round(140 - load * 70);
      const b = Math.round(165 - load * 90);
      c.strokeStyle = `rgba(${r},${g},${b},${0.17 + load * 0.62})`;
      c.lineWidth = 1 + load * 2.4;
    }
    c.beginPath();
    c.moveTo(e.s.x, e.s.y);
    c.quadraticCurveTo(cx, cy, e.t.x, e.t.y);
    c.stroke();
    c.setLineDash([]);

    // arrowhead pointing along request flow (source -> target)
    const at = 0.9;
    const px = (1 - at) * (1 - at) * e.s.x + 2 * (1 - at) * at * cx + at * at * e.t.x;
    const py = (1 - at) * (1 - at) * e.s.y + 2 * (1 - at) * at * cy + at * at * e.t.y;
    const ang = Math.atan2(e.t.y - py, e.t.x - px);
    const ac = tripped ? "rgba(247,185,85,.5)"
      : `rgba(${Math.round(140 + load * 115)},${Math.round(155 - load * 85)},${Math.round(180 - load * 105)},${0.3 + load * 0.6})`;
    c.fillStyle = ac;
    c.beginPath();
    c.moveTo(e.t.x - Math.cos(ang) * (e.t.r + 1), e.t.y - Math.sin(ang) * (e.t.r + 1));
    c.lineTo(e.t.x - Math.cos(ang) * (e.t.r + 9) - Math.sin(ang) * 4,
             e.t.y - Math.sin(ang) * (e.t.r + 9) + Math.cos(ang) * 4);
    c.lineTo(e.t.x - Math.cos(ang) * (e.t.r + 9) + Math.sin(ang) * 4,
             e.t.y - Math.sin(ang) * (e.t.r + 9) - Math.cos(ang) * 4);
    c.closePath();
    c.fill();

    // failure travelling *against* request flow: target -> source
    if (!tripped && load > 0.22 && !this.reduced) {
      const speed = 0.00034 + load * 0.00042;
      for (let i = 0; i < 3; i++) {
        const t = ((this.time * speed) + i / 3) % 1;
        const q = 1 - t;
        const x = q * q * e.s.x + 2 * q * t * cx + t * t * e.t.x;
        const y = q * q * e.s.y + 2 * q * t * cy + t * t * e.t.y;
        c.globalAlpha = 0.85 - i * 0.2;
        c.fillStyle = load > 0.6 ? "#ff7a5c" : "#f7b955";
        c.beginPath();
        c.arc(x, y, 2.4 + load * 1.6, 0, Math.PI * 2);
        c.fill();
      }
      c.globalAlpha = 1;
    }
    c.restore();
  }

  _shapePath(c, shape, x, y, r) {
    c.beginPath();
    if (shape === "hex") {
      for (let i = 0; i < 6; i++) {
        const a = Math.PI / 6 + (i * Math.PI) / 3;
        const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r;
        i ? c.lineTo(px, py) : c.moveTo(px, py);
      }
      c.closePath();
    } else if (shape === "square" || shape === "diamond") {
      const k = shape === "diamond" ? Math.SQRT1_2 : 0.86;
      const pts = shape === "diamond"
        ? [[0, -1], [1, 0], [0, 1], [-1, 0]]
        : [[-k, -k], [k, -k], [k, k], [-k, k]];
      pts.forEach(([ox, oy], i) => {
        const px = x + ox * r, py = y + oy * r;
        i ? c.lineTo(px, py) : c.moveTo(px, py);
      });
      c.closePath();
    } else {
      c.arc(x, y, r, 0, Math.PI * 2);
    }
  }

  _node(c, n) {
    const d = n.data;
    if (!d) return;
    const pal = HEALTH[d.health] || HEALTH.healthy;
    const shape = KIND_SHAPE[n.kind] || "circle";
    const isRoot = !!d.isRoot;
    const isPred = !!d.isPredicted;
    const isSel = n.id === this.selectedId || n.id === this.hoverId;
    const fault = !!d.fault;

    c.save();

    // outer glow for anything unhealthy
    if (d.health !== "healthy") {
      c.shadowColor = pal.glow;
      c.shadowBlur = 16;
    }

    // fill
    const g = c.createRadialGradient(n.x - n.r * 0.3, n.y - n.r * 0.4, 1, n.x, n.y, n.r);
    g.addColorStop(0, d.health === "healthy" ? "#1b5145" : pal.fill);
    g.addColorStop(1, d.health === "healthy" ? "#0d2a24" : "#0b0f17");
    c.fillStyle = g;
    this._shapePath(c, shape, n.x, n.y, n.r);
    c.fill();
    c.shadowBlur = 0;

    c.lineWidth = 1.7;
    c.strokeStyle = pal.stroke;
    c.stroke();

    if (shape === "ring") {
      c.beginPath();
      c.arc(n.x, n.y, n.r * 0.5, 0, Math.PI * 2);
      c.strokeStyle = pal.stroke;
      c.globalAlpha = 0.55;
      c.lineWidth = 1.2;
      c.stroke();
      c.globalAlpha = 1;
    }

    // stress ring
    const stress = Math.min(100, d.stress || 0) / 100;
    if (stress > 0.02) {
      c.beginPath();
      c.arc(n.x, n.y, n.r + 4.5, -Math.PI / 2, -Math.PI / 2 + stress * Math.PI * 2);
      c.strokeStyle = pal.stroke;
      c.globalAlpha = 0.75;
      c.lineWidth = 2.4;
      c.lineCap = "round";
      c.stroke();
      c.globalAlpha = 1;
    }

    // root-cause marker
    if (isRoot) {
      const p = this.reduced ? 0.5 : (Math.sin(this.time / 300) + 1) / 2;
      c.beginPath();
      c.arc(n.x, n.y, n.r + 9 + p * 6, 0, Math.PI * 2);
      c.strokeStyle = `rgba(255,61,113,${0.85 - p * 0.6})`;
      c.lineWidth = 2;
      c.stroke();
      c.beginPath();
      c.arc(n.x + n.r * 0.75, n.y - n.r * 0.75, 7, 0, Math.PI * 2);
      c.fillStyle = "#ff3d71";
      c.fill();
      c.fillStyle = "#1a0209";
      c.font = "800 9px ui-monospace, monospace";
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("!", n.x + n.r * 0.75, n.y - n.r * 0.75 + 0.5);
    }

    // predicted-next marker
    if (isPred && !isRoot) {
      c.beginPath();
      c.setLineDash([3, 3]);
      c.arc(n.x, n.y, n.r + 9, 0, Math.PI * 2);
      c.strokeStyle = "rgba(247,185,85,.85)";
      c.lineWidth = 1.6;
      c.stroke();
      c.setLineDash([]);
    }

    if (isSel) {
      c.beginPath();
      c.arc(n.x, n.y, n.r + 13, 0, Math.PI * 2);
      c.strokeStyle = "#38bdf8";
      c.lineWidth = 1.6;
      c.stroke();
    }

    if (fault) {
      c.fillStyle = "#ff3d71";
      c.beginPath();
      c.arc(n.x - n.r * 0.78, n.y - n.r * 0.78, 5, 0, Math.PI * 2);
      c.fill();
      c.fillStyle = "#fff";
      c.font = "800 8px ui-monospace, monospace";
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText("⚡", n.x - n.r * 0.78, n.y - n.r * 0.78);
    }

    // label: captions alternate above/below the node so wide names in a dense
    // row never collide, then a declutter pass avoids other labels and nodes.
    // On a crowded canvas only the services that matter right now are named,
    // so text is never stacked on text.
    const vital = isSel || isRoot || isPred || d.health !== "healthy" || fault;
    const roomy = this.nodes.length <= 14 || this.w >= 700;
    const showLabel = vital || (roomy && this.nodes.length <= 60);
    if (showLabel) {
      const text = n.label;
      const fs = roomy ? 10.5 : 10;
      c.font = `600 ${fs}px Inter, system-ui, sans-serif`;
      const tw = c.measureText(text).width;
      const bh = roomy ? 15 : 14;
      const bw = Math.min(this.w - 4, tw + 10), bx0 = Math.max(2, Math.min(this.w - bw - 2, n.x - bw / 2));
      const boxes = this._labelBoxes || (this._labelBoxes = []);
      const hitsBox = (x, y) => boxes.some((b) =>
        x < b.x + b.w && x + bw > b.x && y < b.y + b.h && y + bh > b.y);
      const hitsNode = (x, y) => this.nodes.some((m) => {
        if (m === n) return false;
        const cx = Math.max(x, Math.min(x + bw, m.x));
        const cy = Math.max(y, Math.min(y + bh, m.y));
        return Math.hypot(m.x - cx, m.y - cy) < m.r + 2;
      });
      const free = (x, y) => y >= 2 && y + bh <= this.hgt - 2 &&
        !hitsBox(x, y) && !hitsNode(x, y);

      const above = (n.ci ?? 0) % 2 === 1;
      const home = above ? n.y - n.r - 6 - bh : n.y + n.r + 6;
      const step = bh + 3;
      const slots = [{ dx: 0, dy: 0 }];
      for (let i = 1; i <= 8; i++) {
        slots.push({ dx: 0, dy: (above ? -1 : 1) * i * step });
        slots.push({ dx: 0, dy: (above ? 1 : -1) * i * step });
      }
      for (let i = 1; i <= 5; i++) {
        for (const s of [1, -1]) {
          slots.push({ dx: s * Math.ceil(i / 2) * bw * 0.55, dy: 0 });
          slots.push({ dx: s * Math.ceil(i / 2) * bw * 0.55, dy: (above ? -1 : 1) * step });
        }
      }

      let place = null;
      for (const s of slots) {
        const x = Math.max(2, Math.min(this.w - bw - 2, bx0 + s.dx));
        const y = home + s.dy;
        if (free(x, y)) { place = { x, y }; break; }
      }

      // if every slot is clean, drop the caption rather than stack text on text
      if (!place) {
        if (!vital) { c.restore(); return; }
        // required labels fall back to the slot with the least overlap
        let best = null, bestScore = Infinity;
        for (const s of slots) {
          const x = Math.max(2, Math.min(this.w - bw - 2, bx0 + s.dx));
          const y = home + s.dy;
          if (y < 2 || y + bh > this.hgt - 2) continue;
          let score = 0;
          for (const b of boxes) {
            const ow = Math.min(x + bw, b.x + b.w) - Math.max(x, b.x);
            const oh = Math.min(y + bh, b.y + b.h) - Math.max(y, b.y);
            if (ow > 0 && oh > 0) score += ow * oh * (b.isObs ? 6 : 1);
          }
          for (const m of this.nodes) {
            if (m === n) continue;
            const cx = Math.max(x, Math.min(x + bw, m.x));
            const cy = Math.max(y, Math.min(y + bh, m.y));
            const dx = m.x - cx, dy = m.y - cy;
            if (dx * dx + dy * dy < (m.r + 2) * (m.r + 2)) score += 500;
          }
          if (score < bestScore) { bestScore = score; best = { x, y }; }
        }
        place = best || { x: bx0, y: home };
      }

      const bx = place.x, by = place.y;
      boxes.push({ x: bx, y: by, w: bw, h: bh });
      c.fillStyle = "rgba(5,7,12,.86)";
      c.beginPath();
      if (HAS_RRECT) c.roundRect(bx, by, bw, bh, 4);
      else c.rect(bx, by, bw, bh);
      c.fill();
      c.fillStyle = d.health === "healthy" ? "#c3d0e2" : pal.text;
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(text, bx + bw / 2, by + bh / 2 + 0.5);
    }
    c.restore();
  }

  /* ---------------------------------------------------------- interaction */

  _pick(sx, sy) {
    const [wx, wy] = this._world()(sx, sy);
    let found = null, best = Infinity;
    for (const n of this.nodes) {
      const d = Math.hypot(n.x - wx, n.y - wy);
      if (d < n.r + 9 && d < best) { best = d; found = n; }
    }
    return found;
  }

  _onMove(e) {
    const r = this.canvas.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    if (this.dragId) {
      const [wx, wy] = this._world()(sx, sy);
      const n = this.byIdx.get(this.dragId);
      if (n) { n.x = wx; n.y = wy; n.vx = 0; n.vy = 0; this.dragMoved = true; this.alpha = Math.max(this.alpha, 0.28); }
      return;
    }
    if (this._pan) {
      this.view.x = e.clientX - this._pan.x;
      this.view.y = e.clientY - this._pan.y;
      return;
    }
    const hit = this._pick(sx, sy);
    const id = hit ? hit.id : null;
    if (id !== this.hoverId) {
      this.hoverId = id;
      this.canvas.style.cursor = hit ? "pointer" : "grab";
      this.h.onHover?.(hit ? hit.data : null, hit ? hit.x : 0, hit ? hit.y : 0);
    } else if (hit) {
      this.h.onHover?.(hit.data, hit.x, hit.y);
    }
  }

  _onDown(e) {
    const r = this.canvas.getBoundingClientRect();
    const hit = this._pick(e.clientX - r.left, e.clientY - r.top);
    if (hit) {
      this.dragId = hit.id;
      this.dragMoved = false;
      this.selectedId = hit.id;
      this.canvas.setPointerCapture?.(e.pointerId);
      this.h.onSelect?.(hit.data);
    } else {
      this._pan = { x: e.clientX - this.view.x, y: e.clientY - this.view.y };
      this.dragId = null;
      this.canvas.style.cursor = "grabbing";
    }
  }

  _endDrag() {
    if (this._pan) this.canvas.style.cursor = "grab";
    // A node the operator dragged stays where they put it until Relayout.
    if (this.dragId && this.dragMoved) {
      const n = this.byIdx.get(this.dragId);
      if (n) { n.pinned = true; n.vx = 0; n.vy = 0; }
    }
    this.dragId = null;
    this._pan = null;
    this.dragMoved = false;
  }

  _onKey(e) {
    if (!this.nodes.length) return;
    const k = e.key;
    if (["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End",
         "Enter", " "].includes(k)) e.preventDefault();
    if (k === "ArrowRight" || k === "ArrowDown") {
      this.kbIndex = (this.kbIndex + 1) % this.nodes.length;
    } else if (k === "ArrowLeft" || k === "ArrowUp") {
      this.kbIndex = (this.kbIndex - 1 + this.nodes.length) % this.nodes.length;
    } else if (k === "Home") this.kbIndex = 0;
    else if (k === "End") this.kbIndex = this.nodes.length - 1;
    else if (k === "Enter" || k === " ") {
      if (this.kbIndex >= 0) this.h.onSelect?.(this.nodes[this.kbIndex].data);
      return;
    } else if (k === "Escape") {
      this.kbIndex = -1; this.selectedId = null; this.h.onHover?.(null);
      return;
    } else return;

    const n = this.nodes[this.kbIndex];
    if (n) {
      this.selectedId = n.id;
      this.hoverId = n.id;
      const [sx, sy] = this._toScreen(n.x, n.y);
      this.h.onHover?.(n.data, sx, sy);
      this.h.onCursor?.(n.data);
    }
  }
}
