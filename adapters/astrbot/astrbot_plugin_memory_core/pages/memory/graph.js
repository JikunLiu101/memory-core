/* A small force-directed graph on a canvas (no libraries). Good for a few hundred nodes.
 * Nodes: {id, label, type: "entity" | "memory", kind, size}. Links: {source, target, weight}. */
(function () {
  "use strict";

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  class MemGraph {
    constructor(canvas, options) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.onSelect = (options && options.onSelect) || function () {};
      this.nodes = [];
      this.links = [];
      this.byId = new Map();
      this.neighbors = new Map();
      this.view = { k: 1, x: 0, y: 0 };
      this.hover = null;
      this.selected = null;
      this.alpha = 0;
      this.running = false;
      this.visible = true;
      this._bind();
      this.resize();
    }

    setData(nodes, links) {
      const old = this.byId;
      this.nodes = nodes.map((n) => {
        const was = old.get(n.id);
        return Object.assign({ x: 0, y: 0, vx: 0, vy: 0 }, was ? { x: was.x, y: was.y } : {}, n, { fixed: false });
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.links = links
        .map((l) => ({ source: this.byId.get(l.source), target: this.byId.get(l.target), weight: l.weight || 1 }))
        .filter((l) => l.source && l.target && l.source !== l.target);
      this.neighbors = new Map(this.nodes.map((n) => [n.id, new Set()]));
      for (const l of this.links) {
        this.neighbors.get(l.source.id).add(l.target.id);
        this.neighbors.get(l.target.id).add(l.source.id);
      }
      const fresh = this.nodes.filter((n) => !old.has(n.id));
      const r = 40 + Math.sqrt(this.nodes.length) * 30;
      fresh.forEach((n, i) => {
        const a = i * 2.399963; // golden angle: an even first spread
        const d = r * Math.sqrt((i + 0.5) / Math.max(1, fresh.length));
        n.x = Math.cos(a) * d;
        n.y = Math.sin(a) * d;
      });
      if (this.selected && !this.byId.has(this.selected.id)) this.selected = null;
      if (old.size === 0 || fresh.length) this.fit = true;
      this.reheat(old.size === 0 ? 1 : 0.6);
    }

    relayout() {
      const r = 40 + Math.sqrt(this.nodes.length) * 30;
      this.nodes.forEach((n, i) => {
        const a = i * 2.399963;
        const d = r * Math.sqrt((i + 0.5) / Math.max(1, this.nodes.length));
        n.x = Math.cos(a) * d; n.y = Math.sin(a) * d; n.vx = 0; n.vy = 0; n.fixed = false;
      });
      this.fit = true;
      this.reheat(1);
    }

    select(id) {
      this.selected = id ? this.byId.get(id) || null : null;
      if (this.selected) {
        const { w, h } = this.size;
        this.view.x = w / 2 - this.selected.x * this.view.k;
        this.view.y = h / 2 - this.selected.y * this.view.k;
      }
      this.draw();
    }

    setVisible(visible) {
      this.visible = visible;
      if (visible) { this.resize(); this.reheat(Math.max(this.alpha, 0.05)); }
    }

    reheat(alpha) {
      this.alpha = Math.max(this.alpha, alpha);
      if (!this.running && this.visible) {
        this.running = true;
        requestAnimationFrame(() => this._frame());
      }
    }

    resize() {
      const rect = this.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, rect.width), h = Math.max(1, rect.height);
      if (this.size && this.size.w > 1) {
        this.view.x += (w - this.size.w) / 2;
        this.view.y += (h - this.size.h) / 2;
      } else {
        this.view.x = w / 2; this.view.y = h / 2;
      }
      this.size = { w, h };
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }

    radius(n) {
      return n.type === "entity" ? 6 + Math.min(16, Math.sqrt(n.size || 1) * 3) : 3.5 + (n.size || 0.5) * 3;
    }

    // ------------------------------------------------------------------ simulation

    _tick() {
      const nodes = this.nodes, a = this.alpha;
      for (let i = 0; i < nodes.length; i++) {
        const p = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const q = nodes[j];
          let dx = q.x - p.x, dy = q.y - p.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.25; }
          if (d2 > 360000) continue;
          const charge = (p.type === "entity" ? 3 : 1) * (q.type === "entity" ? 3 : 1);
          const d = Math.sqrt(d2);
          let f = (700 * charge * a) / d2;
          const gap = this.radius(p) + this.radius(q) + 6;
          if (d < gap) f += (gap - d) * 0.5; // keep circles from overlapping
          p.vx -= (dx / d) * f; p.vy -= (dy / d) * f;
          q.vx += (dx / d) * f; q.vy += (dy / d) * f;
        }
      }
      for (const l of this.links) {
        const s = l.source, t = l.target;
        const both = s.type === "entity" && t.type === "entity";
        const rest = both ? 110 / Math.sqrt(l.weight) : 55;
        const dx = t.x - s.x, dy = t.y - s.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = ((d - rest) / d) * 0.08 * a * (both ? Math.min(3, l.weight) : 1);
        s.vx += dx * f; s.vy += dy * f;
        t.vx -= dx * f; t.vy -= dy * f;
      }
      for (const n of nodes) {
        n.vx -= n.x * 0.02 * a; n.vy -= n.y * 0.02 * a;
        if (n.fixed) { n.vx = 0; n.vy = 0; continue; }
        n.vx *= 0.82; n.vy *= 0.82;
        n.x += Math.max(-30, Math.min(30, n.vx));
        n.y += Math.max(-30, Math.min(30, n.vy));
      }
      this.alpha *= 0.985;
    }

    _frame() {
      if (!this.visible) { this.running = false; return; }
      const steps = this.nodes.length > 300 ? 1 : 3;
      for (let i = 0; i < steps && this.alpha > 0.01; i++) this._tick();
      if (this.fit) this._fit(this.alpha < 0.05);
      this.draw();
      if (this.alpha > 0.01 || this.drag) requestAnimationFrame(() => this._frame());
      else { this.running = false; this.fit = false; }
    }

    _fit(final) {
      if (!this.nodes.length) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of this.nodes) {
        x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y);
      }
      const { w, h } = this.size, pad = 60, bottom = 50; // room for labels, and the legend at the bottom
      const k = Math.min(2, Math.max(0.2, Math.min((w - pad * 2) / Math.max(1, x1 - x0),
        (h - pad * 2 - bottom) / Math.max(1, y1 - y0))));
      this.view.k = k;
      this.view.x = w / 2 - ((x0 + x1) / 2) * k;
      this.view.y = (h - bottom) / 2 - ((y0 + y1) / 2) * k;
      if (final) this.fit = false;
    }

    // ------------------------------------------------------------------ drawing

    draw() {
      const ctx = this.ctx, { w, h } = this.size || { w: 0, h: 0 };
      ctx.clearRect(0, 0, w, h);
      if (!this.nodes.length) return;
      const v = this.view;
      const focus = this.hover || this.selected;
      const near = focus ? this.neighbors.get(focus.id) : null;
      const color = {
        edge: css("--edge"), strong: css("--edge-strong"), text: css("--text"), muted: css("--muted"),
        surface: css("--surface"), memory: css("--k-memory"),
      };
      const kind = (n) => (n.type === "memory" ? color.memory : css("--k-" + n.kind) || css("--k-thing"));
      ctx.save();
      ctx.translate(v.x, v.y);
      ctx.scale(v.k, v.k);
      ctx.lineCap = "round";
      for (const l of this.links) {
        const on = focus && (l.source === focus || l.target === focus);
        ctx.strokeStyle = on ? color.strong : color.edge;
        ctx.globalAlpha = focus && !on ? 0.35 : 1;
        ctx.lineWidth = ((l.source.type === "entity" && l.target.type === "entity" ? 0.8 + Math.min(5, l.weight) : 1) * (on ? 1.6 : 1)) / Math.sqrt(v.k);
        ctx.beginPath(); ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      for (const n of this.nodes) {
        const dim = focus && n !== focus && !(near && near.has(n.id));
        ctx.globalAlpha = dim ? 0.3 : 1;
        ctx.fillStyle = kind(n);
        ctx.beginPath(); ctx.arc(n.x, n.y, this.radius(n), 0, Math.PI * 2); ctx.fill();
        if (n === this.selected || n === this.hover) {
          ctx.lineWidth = 2.5 / v.k; ctx.strokeStyle = color.text; ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (const n of this.nodes) {
        // entities are always named (when zoomed in enough, or next to the focused node); a memory only when focused
        const show = n === focus || (n.type === "entity" && (focus ? near.has(n.id) : v.k > 0.45));
        if (!show || !n.label) continue;
        const label = n.label;
        const size = (n.type === "entity" ? 12 : 11) / v.k;
        ctx.font = `${n.type === "entity" ? 600 : 400} ${size}px system-ui, "PingFang SC", "Microsoft YaHei", sans-serif`;
        const y = n.y + this.radius(n) + 3 / v.k;
        ctx.lineWidth = 3 / v.k; ctx.strokeStyle = color.surface; ctx.strokeText(label, n.x, y);
        ctx.fillStyle = n.type === "entity" ? color.text : color.muted; ctx.fillText(label, n.x, y);
      }
      ctx.restore();
    }

    // ------------------------------------------------------------------ interaction

    _point(e) {
      const rect = this.canvas.getBoundingClientRect();
      const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
      return { sx, sy, x: (sx - this.view.x) / this.view.k, y: (sy - this.view.y) / this.view.k };
    }

    _hit(p) {
      let best = null, bestD = Infinity;
      for (const n of this.nodes) {
        const r = this.radius(n) + 4 / this.view.k;
        const d = (n.x - p.x) ** 2 + (n.y - p.y) ** 2;
        if (d <= r * r && d < bestD) { best = n; bestD = d; }
      }
      return best;
    }

    _bind() {
      const c = this.canvas;
      c.addEventListener("pointerdown", (e) => {
        const p = this._point(e);
        const node = this._hit(p);
        this.drag = { node, sx: p.sx, sy: p.sy, vx: this.view.x, vy: this.view.y, moved: false };
        if (node) node.fixed = true;
        c.setPointerCapture(e.pointerId);
        c.classList.add("dragging");
      });
      c.addEventListener("pointermove", (e) => {
        const p = this._point(e);
        const d = this.drag;
        if (d) {
          if (Math.abs(p.sx - d.sx) + Math.abs(p.sy - d.sy) > 3) d.moved = true;
          if (d.node) { d.node.x = p.x; d.node.y = p.y; this.reheat(0.25); }
          else { this.view.x = d.vx + p.sx - d.sx; this.view.y = d.vy + p.sy - d.sy; this.fit = false; this.draw(); }
          return;
        }
        const hit = this._hit(p);
        if (hit !== this.hover) { this.hover = hit; c.style.cursor = hit ? "pointer" : ""; this.draw(); }
      });
      const end = (e) => {
        const d = this.drag;
        this.drag = null;
        c.classList.remove("dragging");
        if (!d) return;
        if (d.node) d.node.fixed = false;
        if (!d.moved) {
          const node = d.node || null;
          this.selected = node;
          this.draw();
          this.onSelect(node);
        }
        try { c.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
      };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
      c.addEventListener("pointerleave", () => { if (!this.drag && this.hover) { this.hover = null; this.draw(); } });
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const p = this._point(e);
        const k = Math.min(4, Math.max(0.15, this.view.k * Math.exp(-e.deltaY * 0.0015)));
        this.view.x = p.sx - p.x * k;
        this.view.y = p.sy - p.y * k;
        this.view.k = k;
        this.fit = false;
        this.draw();
      }, { passive: false });
      if (window.ResizeObserver) new ResizeObserver(() => this.resize()).observe(c);
      else window.addEventListener("resize", () => this.resize());
    }
  }

  window.MemGraph = MemGraph;
})();
