/**
 * VerifyRTL — in-browser SVG waveform viewer (GTKWave-style).
 */
(function () {
  "use strict";

  const NAME_W = 220;
  const ROW_H = 28;
  const ROW_GAP = 4;
  const GROUP_HDR = 24;
  const MIN_ZOOM = 0.02;
  const MAX_ZOOM = 200;
  const LOD_THRESHOLD = 2000;
  const MAX_NODES = 10000;

  const GROUP_ORDER = ["inputs", "outputs", "inouts", "testbench", "unknown"];
  const GROUP_LABELS = {
    inputs: "INPUTS",
    outputs: "OUTPUTS",
    inouts: "INOUTS",
    testbench: "TESTBENCH",
    unknown: "OTHER",
  };

  function toNs(t, data) {
    return t * (data.timescale_to_ns || 1);
  }

  function formatTime(ns) {
    if (ns >= 1000) return (ns / 1000).toFixed(3) + " µs";
    return ns.toFixed(3) + " ns";
  }

  function parseBin(val, width, radix, signed) {
    const u = val.toUpperCase();
    if (u === "X" || u === "Z") return u;
    let bits = val.replace(/[^01]/g, "");
    if (!bits) bits = "0";
    bits = bits.padStart(width, "0").slice(-width);
    const n = parseInt(bits, 2);
    if (radix === "hex") {
      const hexW = Math.ceil(width / 4);
      return "0x" + n.toString(16).toUpperCase().padStart(hexW, "0");
    }
    if (radix === "dec") return String(n);
    if (radix === "bin") return "0b" + bits;
    if (signed && width > 0) {
      const mask = 1 << (width - 1);
      const v = (n ^ mask) - mask;
      return String(v);
    }
    return String(n);
  }

  function downsample(transitions, minPxGap, timeToX) {
    if (transitions.length <= LOD_THRESHOLD) return transitions;
    const out = [transitions[0]];
    let lastX = timeToX(transitions[0].time);
    for (let i = 1; i < transitions.length; i++) {
      const x = timeToX(transitions[i].time);
      if (x - lastX >= minPxGap || i === transitions.length - 1) {
        out.push(transitions[i]);
        lastX = x;
      }
    }
    return out.slice(0, 500);
  }

  function buildGroupedSignals(data, filter, collapsed) {
    const groups = {};
    (data.signals || []).forEach((sig) => {
      if (filter && !sig.name.toLowerCase().includes(filter.toLowerCase())) return;
      const g = sig.group || "unknown";
      if (!groups[g]) groups[g] = [];
      groups[g].push(sig);
    });
    Object.keys(groups).forEach((g) => {
      groups[g].sort((a, b) => a.name.localeCompare(b.name));
    });
    return { groups, collapsed };
  }

  function WaveformViewer(container, data, options) {
    this.container = container;
    this.data = data;
    this.options = options || {};
    this.nsMult = data.timescale_to_ns || 1;
    this.endNs = (data.end_time || 0) * this.nsMult;
    this.zoom = 1;
    this.panNs = 0;
    this.filter = "";
    this.collapsed = { testbench: true };
    this.radix = {};
    this.signed = {};
    this.selected = null;
    this.markerNs = null;
    this.cursorNs = null;
    this.dragging = false;
    this.dragStartX = 0;
    this.dragStartPan = 0;
    this.minimapDrag = false;
    this._listeners = [];
    this._init();
  }

  WaveformViewer.prototype._init = function () {
    const el = this.container;
    el.innerHTML = "";
    el.classList.add("waveform-viewer");
    el.tabIndex = 0;

    el.innerHTML = `
      <div class="wv-controls">
        <button type="button" class="wv-btn" data-act="zoom-out" title="Zoom out">−</button>
        <button type="button" class="wv-btn" data-act="zoom-in" title="Zoom in">+</button>
        <button type="button" class="wv-btn" data-act="fit" title="Fit (0)">⤢ fit</button>
        <span class="wv-sep"></span>
        <span class="wv-time-label">Time: <span class="wv-cursor-time">—</span></span>
        <span class="wv-sep"></span>
        <label class="wv-filter">⌕ <input type="text" class="wv-filter-input" placeholder="filter signals" /></label>
      </div>
      <div class="wv-body">
        <div class="wv-names"></div>
        <div class="wv-wave-wrap">
          <svg class="wv-svg" xmlns="http://www.w3.org/2000/svg"></svg>
          <div class="wv-tooltip hidden"></div>
        </div>
      </div>
      <div class="wv-minimap">
        <svg class="wv-minimap-svg" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div class="wv-banner hidden"></div>
    `;

    this.ctrl = el.querySelector(".wv-controls");
    this.namesEl = el.querySelector(".wv-names");
    this.waveWrap = el.querySelector(".wv-wave-wrap");
    this.svg = el.querySelector(".wv-svg");
    this.tooltip = el.querySelector(".wv-tooltip");
    this.minimapSvg = el.querySelector(".wv-minimap-svg");
    this.banner = el.querySelector(".wv-banner");
    this.filterInput = el.querySelector(".wv-filter-input");
    this.cursorTimeEl = el.querySelector(".wv-cursor-time");

    this._bind();
    this.fit();
    this._render();
  };

  WaveformViewer.prototype._bind = function () {
    const self = this;
    const on = (node, ev, fn) => {
      node.addEventListener(ev, fn);
      self._listeners.push([node, ev, fn]);
    };

    this.ctrl.querySelector('[data-act="zoom-in"]').addEventListener("click", () => self.zoomAt(1.25, self._viewCenterNs()));
    this.ctrl.querySelector('[data-act="zoom-out"]').addEventListener("click", () => self.zoomAt(1 / 1.25, self._viewCenterNs()));
    this.ctrl.querySelector('[data-act="fit"]').addEventListener("click", () => self.fit());

    this.filterInput.addEventListener("input", () => {
      self.filter = self.filterInput.value.trim();
      self._render();
    });

    this.waveWrap.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const factor = e.ctrlKey ? 1.05 : 1.25;
        const z = e.deltaY < 0 ? factor : 1 / factor;
        const rect = self.waveWrap.getBoundingClientRect();
        const frac = (e.clientX - rect.left) / rect.width;
        const viewW = self._viewWidthNs();
        const anchor = self.panNs + frac * viewW;
        self.zoomAt(z, anchor);
      },
      { passive: false }
    );

    on(this.waveWrap, "mousedown", (e) => {
      if (e.button !== 0) return;
      self.dragging = true;
      self.dragStartX = e.clientX;
      self.dragStartPan = self.panNs;
    });
    on(window, "mousemove", (e) => {
      if (self.dragging) {
        const dx = e.clientX - self.dragStartX;
        self.panNs = self.dragStartPan - dx / self.zoom;
        self._render();
        return;
      }
      const rect = self.waveWrap.getBoundingClientRect();
      if (e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom) {
        const x = e.clientX - rect.left;
        self.cursorNs = self.panNs + x / self.zoom;
        self._updateCursor();
      }
    });
    on(window, "mouseup", () => {
      self.dragging = false;
      self.minimapDrag = false;
    });

    on(this.waveWrap, "click", (e) => {
      const rect = self.waveWrap.getBoundingClientRect();
      self.markerNs = self.panNs + (e.clientX - rect.left) / self.zoom;
      self._render();
    });

    on(this.container, "keydown", (e) => {
      const viewW = self._viewWidthNs();
      if (e.key === "+" || e.key === "=") self.zoomAt(1.25, self.panNs + viewW / 2);
      if (e.key === "-") self.zoomAt(1 / 1.25, self.panNs + viewW / 2);
      if (e.key === "0") self.fit();
      if (e.key === "ArrowLeft") {
        self.panNs -= viewW * 0.1;
        self._render();
      }
      if (e.key === "ArrowRight") {
        self.panNs += viewW * 0.1;
        self._render();
      }
      if (e.key === "Home") {
        self.panNs = 0;
        self._render();
      }
      if (e.key === "End") {
        self.panNs = Math.max(0, self.endNs - viewW);
        self._render();
      }
    });

    on(this.minimapSvg, "mousedown", (e) => {
      self.minimapDrag = true;
      self._minimapJump(e);
    });
    on(this.minimapSvg, "mousemove", (e) => {
      if (self.minimapDrag) self._minimapJump(e);
    });
  };

  WaveformViewer.prototype._viewWidthNs = function () {
    return Math.max(this.waveWrap.clientWidth, 100) / this.zoom;
  };

  WaveformViewer.prototype._viewCenterNs = function () {
    return this.panNs + this._viewWidthNs() / 2;
  };

  WaveformViewer.prototype.zoomAt = function (factor, anchorNs) {
    const newZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, this.zoom * factor));
    const viewW = this.waveWrap.clientWidth / this.zoom;
    const newViewW = this.waveWrap.clientWidth / newZoom;
    this.panNs = anchorNs - (anchorNs - this.panNs) * (newViewW / viewW);
    this.zoom = newZoom;
    this._render();
  };

  WaveformViewer.prototype.fit = function () {
    const w = Math.max(this.waveWrap.clientWidth, 100);
    this.zoom = w / Math.max(this.endNs, 1);
    this.panNs = 0;
    this._render();
  };

  WaveformViewer.prototype._timeToX = function (tRaw) {
    return (toNs(tRaw, this.data) - this.panNs) * this.zoom;
  };

  WaveformViewer.prototype._xToNs = function (x) {
    return this.panNs + x / this.zoom;
  };

  WaveformViewer.prototype._valueAt = function (sig, ns) {
    const tr = sig.transitions;
    if (!tr.length) return { raw: "0", bits: "0" };
    let val = tr[0].value;
    for (let i = 0; i < tr.length; i++) {
      if (toNs(tr[i].time, this.data) > ns) break;
      val = tr[i].value;
    }
    return { raw: val, bits: val };
  };

  WaveformViewer.prototype._updateCursor = function () {
    if (this.cursorNs == null) return;
    this.cursorTimeEl.textContent = formatTime(this.cursorNs);
    const lines = [`t = ${formatTime(this.cursorNs)}`];
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || this.collapsed[gid]) return;
      sigs.forEach((sig) => {
        const v = this._valueAt(sig, this.cursorNs);
        const rad = this.radix[sig.name] || "hex";
        const disp = parseBin(v.bits, sig.width, rad, this.signed[sig.name]);
        lines.push(`${sig.name.padEnd(10)} = ${disp}`);
      });
    });
    this.tooltip.textContent = lines.join("\n");
    this.tooltip.classList.remove("hidden");
    const rect = this.waveWrap.getBoundingClientRect();
    const cx = (this.cursorNs - this.panNs) * this.zoom;
    let left = cx + 12;
    if (left + 200 > rect.width) left = cx - 212;
    this.tooltip.style.left = Math.max(4, left) + "px";
    this.tooltip.style.top = "8px";
    const line = this.svg.querySelector(".wv-cursor-line");
    if (line) line.setAttribute("x1", cx).setAttribute("x2", cx);
  };

  WaveformViewer.prototype._minimapJump = function (e) {
    const rect = this.minimapSvg.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    const viewW = this._viewWidthNs();
    this.panNs = Math.max(0, Math.min(this.endNs - viewW, frac * this.endNs - viewW / 2));
    this._render();
  };

  WaveformViewer.prototype._render = function () {
    const w = Math.max(this.waveWrap.clientWidth, 100);
    const h = this._layoutHeight();
    this.waveWrap.style.height = h + "px";
    this.namesEl.style.height = h + "px";

    this._renderNames();
    this._renderSvg(w, h);
    this._renderMinimap();
    this._updateCursor();
  };

  WaveformViewer.prototype._layoutHeight = function () {
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    let rows = 0;
    GROUP_ORDER.forEach((gid) => {
      if (!groups[gid] || !groups[gid].length) return;
      rows += GROUP_HDR;
      if (!this.collapsed[gid]) rows += groups[gid].length * (ROW_H + ROW_GAP);
    });
    return Math.max(rows, 120) + 32;
  };

  WaveformViewer.prototype._renderNames = function () {
    const self = this;
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    let y = 28;
    let html = `<div class="wv-ruler-spacer"></div>`;
    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || !sigs.length) return;
      const collapsed = !!this.collapsed[gid];
      const arrow = collapsed ? "▶" : "▼";
      html += `<div class="wv-group-hdr" data-group="${gid}" style="top:${y}px">
        <span class="wv-group-toggle">${arrow} ${GROUP_LABELS[gid] || gid.toUpperCase()}${collapsed ? ` (${sigs.length})` : ""}</span>
      </div>`;
      y += GROUP_HDR;
      if (!collapsed) {
        sigs.forEach((sig) => {
          const sel = this.selected === sig.name ? " wv-sig-selected" : "";
          const dim = this.selected && this.selected !== sig.name ? " wv-sig-dim" : "";
          const rng = sig.width > 1 ? `[${sig.width - 1}:0]` : "";
          html += `<div class="wv-sig-row${sel}${dim}" data-sig="${sig.name}" style="height:${ROW_H}px;margin-bottom:${ROW_GAP}px">
            <span class="wv-sig-name">${sig.name}</span>
            <span class="wv-sig-width">${rng}</span>
          </div>`;
          y += ROW_H + ROW_GAP;
        });
      }
    });
    this.namesEl.innerHTML = html;

    this.namesEl.querySelectorAll(".wv-group-hdr").forEach((hdr) => {
      hdr.addEventListener("click", () => {
        const g = hdr.dataset.group;
        self.collapsed[g] = !self.collapsed[g];
        self._render();
      });
    });
    this.namesEl.querySelectorAll(".wv-sig-row").forEach((row) => {
      row.addEventListener("click", () => {
        self.selected = self.selected === row.dataset.sig ? null : row.dataset.sig;
        self._render();
      });
      row.addEventListener("dblclick", () => {
        self._fitSignal(row.dataset.sig);
      });
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        self._showRadixMenu(e, row.dataset.sig);
      });
    });
  };

  WaveformViewer.prototype._fitSignal = function (name) {
    const sig = (this.data.signals || []).find((s) => s.name === name);
    if (!sig || !sig.transitions.length) return;
    const t0 = toNs(sig.transitions[0].time, this.data);
    const t1 = toNs(sig.transitions[sig.transitions.length - 1].time, this.data);
    const span = Math.max(t1 - t0, 1);
    const w = this.waveWrap.clientWidth;
    this.zoom = w / span;
    this.panNs = Math.max(0, t0 - span * 0.05);
    this._render();
  };

  WaveformViewer.prototype._showRadixMenu = function (e, sigName) {
    const old = document.querySelector(".wv-ctx-menu");
    if (old) old.remove();
    const menu = document.createElement("div");
    menu.className = "wv-ctx-menu";
    menu.style.left = e.clientX + "px";
    menu.style.top = e.clientY + "px";
    ["hex", "decimal", "binary", "unsigned", "signed"].forEach((opt) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = opt;
      btn.addEventListener("click", () => {
        if (opt === "signed") this.signed[sigName] = true;
        else if (opt === "unsigned") this.signed[sigName] = false;
        else this.radix[sigName] = opt === "decimal" ? "dec" : opt === "binary" ? "bin" : "hex";
        menu.remove();
        this._render();
      });
      menu.appendChild(btn);
    });
    document.body.appendChild(menu);
    const close = () => {
      menu.remove();
      document.removeEventListener("click", close);
    };
    setTimeout(() => document.addEventListener("click", close), 0);
  };

  WaveformViewer.prototype._renderSvg = function (w, h) {
    const ns = "http://www.w3.org/2000/svg";
    while (this.svg.firstChild) this.svg.removeChild(this.svg.firstChild);
    this.svg.setAttribute("width", w);
    this.svg.setAttribute("height", h);

    const defs = document.createElementNS(ns, "defs");
    const patX = document.createElementNS(ns, "pattern");
    patX.setAttribute("id", "wv-hatch-x");
    patX.setAttribute("width", "6");
    patX.setAttribute("height", "6");
    patX.setAttribute("patternUnits", "userSpaceOnUse");
    const lx = document.createElementNS(ns, "line");
    lx.setAttribute("x1", "0");
    lx.setAttribute("y1", "0");
    lx.setAttribute("x2", "6");
    lx.setAttribute("y2", "6");
    lx.setAttribute("stroke", "var(--led-red)");
    patX.appendChild(lx);
    defs.appendChild(patX);
    this.svg.appendChild(defs);

    const viewW = this._viewWidthNs();
    const major = 100;
    const minor = 10;
    for (let t = Math.floor(this.panNs / minor) * minor; t < this.panNs + viewW; t += minor) {
      const x = (t - this.panNs) * this.zoom;
      if (x < 0 || x > w) continue;
      const ln = document.createElementNS(ns, "line");
      ln.setAttribute("x1", x);
      ln.setAttribute("x2", x);
      ln.setAttribute("y1", 0);
      ln.setAttribute("y2", h);
      ln.setAttribute("stroke", t % major === 0 ? "var(--border)" : "var(--border-soft)");
      ln.setAttribute("stroke-width", t % major === 0 ? "1" : "0.5");
      ln.setAttribute("stroke-dasharray", t % major === 0 ? "" : "2,4");
      this.svg.appendChild(ln);
      if (t % major === 0 && t >= 0) {
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x + 2);
        tx.setAttribute("y", 12);
        tx.setAttribute("class", "wv-grid-label");
        tx.textContent = formatTime(t);
        this.svg.appendChild(tx);
      }
    }

    let nodeCount = 0;
    let y = 28;
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    const timeToX = (t) => this._timeToX(t);

    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || !sigs.length) return;
      y += GROUP_HDR;
      if (this.collapsed[gid]) return;
      sigs.forEach((sig) => {
        const rowY = y;
        const dim = this.selected && this.selected !== sig.name;
        const trs = downsample(sig.transitions, 2, timeToX);
        if (sig.width === 1) {
          nodeCount += this._drawScalar(sig, trs, rowY, dim);
        } else {
          nodeCount += this._drawBus(sig, trs, rowY, dim);
        }
        y += ROW_H + ROW_GAP;
      });
    });

    if (nodeCount > MAX_NODES) {
      this.banner.textContent =
        "Showing first 500 signal transitions per signal. Zoom in for full detail.";
      this.banner.classList.remove("hidden");
    } else {
      this.banner.classList.add("hidden");
    }

    if (this.markerNs != null) {
      const mx = (this.markerNs - this.panNs) * this.zoom;
      const ml = document.createElementNS(ns, "line");
      ml.setAttribute("class", "wv-marker-line");
      ml.setAttribute("x1", mx);
      ml.setAttribute("x2", mx);
      ml.setAttribute("y1", 0);
      ml.setAttribute("y2", h);
      this.svg.appendChild(ml);
    }

    const cl = document.createElementNS(ns, "line");
    cl.setAttribute("class", "wv-cursor-line");
    cl.setAttribute("y1", 0);
    cl.setAttribute("y2", h);
    this.svg.appendChild(cl);
  };

  WaveformViewer.prototype._drawScalar = function (sig, trs, rowY, dim) {
    const ns = "http://www.w3.org/2000/svg";
    const base = rowY + 20;
    const hi = rowY + 6;
    let nodes = 0;
    const g = document.createElementNS(ns, "g");
    if (dim) g.setAttribute("opacity", "0.5");
    let prevX = null;
    let prevHi = false;
    trs.forEach((tr, i) => {
      const x = this._timeToX(tr.time);
      const v = tr.value.toUpperCase();
      const isHi = v === "1";
      const isX = v === "X" || v === "Z";
      if (prevX !== null) {
        const path = document.createElementNS(ns, "path");
        let d = `M ${prevX} ${prevHi ? hi : base} H ${x}`;
        if (prevHi !== isHi && !isX) d += ` V ${isHi ? hi : base}`;
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", isX ? "var(--led-red)" : "var(--accent)");
        path.setAttribute("stroke-width", "1.5");
        if (isX) path.setAttribute("stroke-dasharray", "4,2");
        g.appendChild(path);
        nodes++;
      }
      if (isX) {
        const r = document.createElementNS(ns, "rect");
        r.setAttribute("x", x - 4);
        r.setAttribute("y", hi);
        r.setAttribute("width", 8);
        r.setAttribute("height", base - hi);
        r.setAttribute("fill", "url(#wv-hatch-x)");
        g.appendChild(r);
        nodes++;
      }
      prevX = x;
      prevHi = isHi;
      if (i === trs.length - 1) {
        const tail = document.createElementNS(ns, "line");
        tail.setAttribute("x1", x);
        tail.setAttribute("x2", this.waveWrap.clientWidth + 10);
        tail.setAttribute("y1", isHi ? hi : base);
        tail.setAttribute("y2", isHi ? hi : base);
        tail.setAttribute("stroke", "var(--accent)");
        tail.setAttribute("stroke-width", "1.5");
        g.appendChild(tail);
        nodes++;
      }
    });
    this.svg.appendChild(g);
    return nodes;
  };

  WaveformViewer.prototype._drawBus = function (sig, trs, rowY, dim) {
    const ns = "http://www.w3.org/2000/svg";
    const top = rowY + 6;
    const bot = rowY + 22;
    const mid = (top + bot) / 2;
    const rad = this.radix[sig.name] || "hex";
    const signed = !!this.signed[sig.name];
    let nodes = 0;
    const g = document.createElementNS(ns, "g");
    if (dim) g.setAttribute("opacity", "0.5");

    for (let i = 0; i < trs.length; i++) {
      const t0 = trs[i].time;
      const t1 = i + 1 < trs.length ? trs[i + 1].time : this.data.end_time;
      const x0 = this._timeToX(t0);
      const x1 = this._timeToX(t1);
      const w = x1 - x0;
      if (x1 < 0 || x0 > this.waveWrap.clientWidth) continue;
      const val = trs[i].value;
      const u = val.toUpperCase();
      const fill = document.createElementNS(ns, "rect");
      fill.setAttribute("x", x0);
      fill.setAttribute("y", top);
      fill.setAttribute("width", Math.max(w, 2));
      fill.setAttribute("height", bot - top);
      fill.setAttribute("fill", "var(--accent-soft)");
      fill.setAttribute("stroke", "none");
      g.appendChild(fill);

      const path = document.createElementNS(ns, "path");
      const d = `M ${x0} ${top} L ${x0 + 5} ${mid} L ${x0} ${bot} H ${x1 - 5} L ${x1} ${mid} L ${x1 - 5} ${top} H ${x0}`;
      path.setAttribute("d", d);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", u === "X" || u === "Z" ? "var(--led-amber)" : "var(--accent)");
      path.setAttribute("stroke-width", "1.5");
      g.appendChild(path);
      nodes += 2;

      if (w > 28) {
        const label = parseBin(val, sig.width, rad, signed);
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x0 + w / 2);
        tx.setAttribute("y", mid + 4);
        tx.setAttribute("text-anchor", "middle");
        tx.setAttribute("class", "wv-val-label");
        tx.textContent = u === "X" || u === "Z" ? u : label;
        g.appendChild(tx);
        nodes++;
      } else if (w > 8) {
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x0 + w / 2);
        tx.setAttribute("y", mid + 4);
        tx.setAttribute("text-anchor", "middle");
        tx.setAttribute("class", "wv-val-label");
        tx.textContent = "›";
        g.appendChild(tx);
        nodes++;
      }
    }
    this.svg.appendChild(g);
    return nodes;
  };

  WaveformViewer.prototype._renderMinimap = function () {
    const ns = "http://www.w3.org/2000/svg";
    const w = this.minimapSvg.clientWidth || this.container.clientWidth;
    const h = 40;
    while (this.minimapSvg.firstChild) this.minimapSvg.removeChild(this.minimapSvg.firstChild);
    this.minimapSvg.setAttribute("width", w);
    this.minimapSvg.setAttribute("height", h);
    const end = Math.max(this.endNs, 1);
    const bandH = 6;
    let by = 4;
    (this.data.signals || []).slice(0, 32).forEach((sig) => {
      const trs = sig.transitions;
      if (!trs.length) return;
      trs.forEach((tr, i) => {
        const t0 = toNs(tr.time, this.data);
        const t1 =
          i + 1 < trs.length ? toNs(trs[i + 1].time, this.data) : end;
        const rect = document.createElementNS(ns, "rect");
        rect.setAttribute("x", (t0 / end) * w);
        rect.setAttribute("y", by);
        rect.setAttribute("width", Math.max(((t1 - t0) / end) * w, 0.5));
        rect.setAttribute("height", bandH);
        rect.setAttribute("fill", "var(--accent)");
        rect.setAttribute("opacity", "0.6");
        this.minimapSvg.appendChild(rect);
      });
      by += bandH + 1;
    });
    const viewW = this._viewWidthNs();
    const vx = (this.panNs / end) * w;
    const vw = Math.min(w, (viewW / end) * w);
    const vp = document.createElementNS(ns, "rect");
    vp.setAttribute("class", "wv-minimap-viewport");
    vp.setAttribute("x", vx);
    vp.setAttribute("y", 0);
    vp.setAttribute("width", vw);
    vp.setAttribute("height", h);
    this.minimapSvg.appendChild(vp);
  };

  WaveformViewer.prototype.destroy = function () {
    this._listeners.forEach(([node, ev, fn]) => node.removeEventListener(ev, fn));
    this._listeners = [];
    this.container.innerHTML = "";
    this.container.classList.remove("waveform-viewer");
    document.querySelectorAll(".wv-ctx-menu").forEach((m) => m.remove());
  };

  window.WaveformViewer = {
    render: function (container, data, options) {
      if (container._wvInstance) {
        container._wvInstance.destroy();
      }
      if (data.error) {
        container.innerHTML = `<div class="wv-error">${data.error}</div>`;
        return null;
      }
      const inst = new WaveformViewer(container, data, options);
      container._wvInstance = inst;
      const ro = new ResizeObserver(() => inst._render());
      ro.observe(container);
      inst._resizeObs = ro;
      return inst;
    },
    destroy: function (container) {
      if (container._wvInstance) {
        container._wvInstance.destroy();
        delete container._wvInstance;
      }
      if (container._resizeObs) {
        container._resizeObs.disconnect();
        delete container._resizeObs;
      }
    },
  };
})();
