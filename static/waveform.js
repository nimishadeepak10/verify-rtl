/**
 * VerifyRTL — in-browser SVG waveform viewer.
 */
(function () {
  "use strict";

  const ROW_H = 32;
  const GROUP_H = 26;
  const RULER_H = 32;
  const NAME_W = 220;
  const MIN_ZOOM = 0.05;
  const MAX_ZOOM = 500;

  const GROUP_ORDER = ["inputs", "outputs", "inouts", "testbench", "unknown"];
  const GROUP_LABELS = {
    inputs: "INPUTS",
    outputs: "OUTPUTS",
    inouts: "INOUTS",
    testbench: "TESTBENCH",
    unknown: "OTHER",
  };

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function toNs(t, data) {
    return t * (data.timescale_to_ns || 1);
  }

  function formatTime(ns) {
    if (!Number.isFinite(ns)) return "—";
    if (ns >= 1000) return (ns / 1000).toFixed(3) + " µs";
    return ns.toFixed(3) + " ns";
  }

  function parseBin(val, width, radix, signed) {
    const u = String(val).toUpperCase();
    if (u === "X" || u === "Z") return u;
    let bits = u.replace(/[^01]/g, "");
    if (!bits) bits = "0";
    bits = bits.padStart(width, "0").slice(-width);
    const n = parseInt(bits, 2) || 0;
    if (radix === "hex") {
      const hexW = Math.ceil(width / 4);
      return "0x" + n.toString(16).toUpperCase().padStart(hexW, "0");
    }
    if (radix === "dec") return String(n);
    if (radix === "bin") return "0b" + bits;
    if (signed && width > 0) {
      const mask = 1 << (width - 1);
      return String((n ^ mask) - mask);
    }
    return String(n);
  }

  function niceStep(spanNs) {
    const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];
    const target = spanNs / 8;
    for (const s of steps) if (s >= target) return s;
    return steps[steps.length - 1];
  }

  function buildGroupedSignals(data, filter, collapsed) {
    const groups = {};
    (data.signals || []).forEach((sig) => {
      if (filter && !sig.name.toLowerCase().includes(filter.toLowerCase())) return;
      const g = sig.group || "unknown";
      if (!groups[g]) groups[g] = [];
      groups[g].push(sig);
    });
    GROUP_ORDER.forEach((g) => {
      if (groups[g]) groups[g].sort((a, b) => a.name.localeCompare(b.name));
    });
    return { groups, collapsed };
  }

  function WaveformViewer(container, data) {
    this.container = container;
    this.data = data;
    this.endNs = Math.max(toNs(data.end_time || 0, data), 1);
    this.zoom = 1;
    this.panNs = 0;
    this.filter = "";
    this.collapsed = { testbench: false };
    this.radix = {};
    this.signed = {};
    this.selected = null;
    this.markerNs = 0;
    this.cursorNs = 0;
    this.dragging = false;
    this.dragStartX = 0;
    this.dragStartPan = 0;
    this._listeners = [];

    this.colors = {
      accent: cssVar("--accent", "#f7b955"),
      inputs: cssVar("--led-blue", "#58a6ff"),
      outputs: cssVar("--led-green", "#3fb950"),
      inouts: cssVar("--led-amber", "#d29922"),
      testbench: cssVar("--text-muted", "#8b949e"),
      unknown: cssVar("--text-dim", "#6e7681"),
      grid: cssVar("--border", "#30363d"),
      gridMinor: cssVar("--border-soft", "#21262d"),
      bgCell: "rgba(247, 185, 85, 0.15)",
    };

    this._init();
  }

  WaveformViewer.prototype._init = function () {
    const el = this.container;
    el.innerHTML = "";
    el.className = "waveform-viewer";
    el.tabIndex = 0;

    el.innerHTML = `
      <div class="wv-controls">
        <button type="button" class="wv-btn" data-act="zoom-out" title="Zoom out (−)">−</button>
        <button type="button" class="wv-btn" data-act="zoom-in" title="Zoom in (+)">+</button>
        <button type="button" class="wv-btn" data-act="fit" title="Fit all (0)">⤢ FIT</button>
        <span class="wv-sep"></span>
        <span class="wv-time-label">Cursor: <span class="wv-cursor-time">0 ns</span></span>
        <span class="wv-sep"></span>
        <span class="wv-hint">Wheel = zoom · Drag = pan · Click = place marker</span>
        <label class="wv-filter">⌕ <input type="text" class="wv-filter-input" placeholder="filter signals" /></label>
      </div>
      <div class="wv-values-panel">
        <div class="wv-values-title">VALUES AT CURSOR</div>
        <div class="wv-values-table-wrap"><table class="wv-values-table"><tbody class="wv-values-body"></tbody></table></div>
      </div>
      <div class="wv-body">
        <div class="wv-names"></div>
        <div class="wv-wave-scroll">
          <svg class="wv-svg" xmlns="http://www.w3.org/2000/svg"></svg>
        </div>
      </div>
      <div class="wv-minimap"><svg class="wv-minimap-svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
    `;

    this.namesEl = el.querySelector(".wv-names");
    this.waveScroll = el.querySelector(".wv-wave-scroll");
    this.svg = el.querySelector(".wv-svg");
    this.valuesBody = el.querySelector(".wv-values-body");
    this.cursorTimeEl = el.querySelector(".wv-cursor-time");
    this.filterInput = el.querySelector(".wv-filter-input");
    this.minimapSvg = el.querySelector(".wv-minimap-svg");

    this._bind();
    const self = this;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        self.fit();
        self.cursorNs = 0;
        self.markerNs = 0;
        self._render();
      });
    });
  };

  WaveformViewer.prototype._bind = function () {
    const self = this;

    this.container.querySelector('[data-act="zoom-in"]').onclick = () =>
      self.zoomAt(1.3, self._viewCenterNs());
    this.container.querySelector('[data-act="zoom-out"]').onclick = () =>
      self.zoomAt(1 / 1.3, self._viewCenterNs());
    this.container.querySelector('[data-act="fit"]').onclick = () => self.fit();

    this.filterInput.oninput = () => {
      self.filter = self.filterInput.value.trim();
      self._render();
    };

    this.waveScroll.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const f = e.ctrlKey ? 1.08 : 1.25;
        const z = e.deltaY < 0 ? f : 1 / f;
        const rect = self.waveScroll.getBoundingClientRect();
        const x = e.clientX - rect.left + self.waveScroll.scrollLeft;
        const anchorNs = x / self.zoom;
        self.zoomAt(z, anchorNs);
      },
      { passive: false }
    );

    let scrollSync = false;
    this.waveScroll.addEventListener("scroll", () => {
      self.panNs = self.waveScroll.scrollLeft / self.zoom;
      if (!scrollSync) {
        scrollSync = true;
        self.namesEl.scrollTop = self.waveScroll.scrollTop;
        scrollSync = false;
      }
      self._renderGridAndCursor();
    });
    this.namesEl.addEventListener("scroll", () => {
      if (!scrollSync) {
        scrollSync = true;
        self.waveScroll.scrollTop = self.namesEl.scrollTop;
        scrollSync = false;
      }
    });

    this.waveScroll.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      self.dragging = true;
      self.dragStartX = e.clientX;
      self.dragStartScroll = self.waveScroll.scrollLeft;
    });

    window.addEventListener("mousemove", (e) => {
      if (self.dragging) {
        const dx = self.dragStartX - e.clientX;
        self.waveScroll.scrollLeft = self.dragStartScroll + dx;
        return;
      }
      const rect = self.waveScroll.getBoundingClientRect();
      if (
        e.clientX >= rect.left &&
        e.clientX <= rect.right &&
        e.clientY >= rect.top &&
        e.clientY <= rect.bottom
      ) {
        const x = e.clientX - rect.left + self.waveScroll.scrollLeft;
        self.cursorNs = x / self.zoom;
        self._updateValuesPanel();
        self._renderGridAndCursor();
      }
    });

    window.addEventListener("mouseup", () => {
      self.dragging = false;
    });

    this.waveScroll.addEventListener("click", (e) => {
      const x = e.clientX - self.waveScroll.getBoundingClientRect().left + self.waveScroll.scrollLeft;
      self.markerNs = x / self.zoom;
      self.cursorNs = self.markerNs;
      self._updateValuesPanel();
      self._render();
    });

    this.container.addEventListener("keydown", (e) => {
      const vw = self._viewWidthNs();
      if (e.key === "+" || e.key === "=") self.zoomAt(1.25, self.panNs + vw / 2);
      if (e.key === "-") self.zoomAt(1 / 1.25, self.panNs + vw / 2);
      if (e.key === "0") self.fit();
      if (e.key === "ArrowLeft") self.waveScroll.scrollLeft -= 40;
      if (e.key === "ArrowRight") self.waveScroll.scrollLeft += 40;
      if (e.key === "Home") {
        self.waveScroll.scrollLeft = 0;
        self.cursorNs = 0;
      }
      if (e.key === "End") {
        self.waveScroll.scrollLeft = self._contentWidth() - self.waveScroll.clientWidth;
        self.cursorNs = self.endNs;
      }
      self._updateValuesPanel();
    });
  };

  WaveformViewer.prototype._contentWidth = function () {
    return Math.max(this.endNs * this.zoom, this.waveScroll.clientWidth || 400);
  };

  WaveformViewer.prototype._viewWidthNs = function () {
    return (this.waveScroll.clientWidth || 400) / this.zoom;
  };

  WaveformViewer.prototype._viewCenterNs = function () {
    return this.panNs + this._viewWidthNs() / 2;
  };

  WaveformViewer.prototype.zoomAt = function (factor, anchorNs) {
    const oldZoom = this.zoom;
    this.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, this.zoom * factor));
    const ratio = this.zoom / oldZoom;
    const newScroll = anchorNs * this.zoom - (anchorNs * oldZoom - this.waveScroll.scrollLeft) * ratio;
    this.waveScroll.scrollLeft = Math.max(0, newScroll);
    this.panNs = this.waveScroll.scrollLeft / this.zoom;
    this._render();
  };

  WaveformViewer.prototype.fit = function () {
    const w = this.waveScroll.clientWidth || 600;
    this.zoom = w / this.endNs;
    this.waveScroll.scrollLeft = 0;
    this.panNs = 0;
    this._render();
  };

  WaveformViewer.prototype._traceColor = function (sig) {
    return this.colors[sig.group] || this.colors.unknown;
  };

  WaveformViewer.prototype._buildLayout = function () {
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    const rows = [];
    let y = RULER_H;
    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || !sigs.length) return;
      rows.push({ type: "group", gid, y, h: GROUP_H, count: sigs.length });
      y += GROUP_H;
      if (!this.collapsed[gid]) {
        sigs.forEach((sig) => {
          rows.push({ type: "signal", gid, sig, y, h: ROW_H });
          y += ROW_H;
        });
      }
    });
    return { rows, totalH: Math.max(y, 160) };
  };

  WaveformViewer.prototype._valueAt = function (sig, ns) {
    const tr = sig.transitions || [];
    if (!tr.length) return "0";
    let val = tr[0].value;
    for (let i = 0; i < tr.length; i++) {
      if (toNs(tr[i].time, this.data) > ns) break;
      val = tr[i].value;
    }
    return val;
  };

  WaveformViewer.prototype._updateValuesPanel = function () {
    const ns = this.cursorNs != null ? this.cursorNs : 0;
    this.cursorTimeEl.textContent = formatTime(ns);
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    let html = "";
    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || !sigs.length || this.collapsed[gid]) return;
      const col = this.colors[gid] || this.colors.unknown;
      sigs.forEach((sig) => {
        const rad = this.radix[sig.name] || "hex";
        const disp = parseBin(this._valueAt(sig, ns), sig.width, rad, this.signed[sig.name]);
        html += `<tr>
          <td class="wv-val-sig" style="color:${col}">${sig.name}</td>
          <td class="wv-val-width">[${sig.width - 1}:0]</td>
          <td class="wv-val-v">${disp}</td>
          <td class="wv-val-g">${GROUP_LABELS[gid] || gid}</td>
        </tr>`;
      });
    });
    if (!html) html = '<tr><td colspan="4" class="wv-val-empty">No signals visible</td></tr>';
    this.valuesBody.innerHTML = html;
  };

  WaveformViewer.prototype._render = function () {
    const { rows, totalH } = this._buildLayout();
    this._rows = rows;
    const contentW = this._contentWidth();

    this._renderNames(rows, totalH);
    this._renderSvg(rows, totalH, contentW);
    this._renderMinimap();
    this._updateValuesPanel();
    this.panNs = this.waveScroll.scrollLeft / this.zoom;
  };

  WaveformViewer.prototype._renderNames = function (rows, totalH) {
    const self = this;
    let html = `<div class="wv-ruler-spacer">TIME →</div>`;
    rows.forEach((row) => {
      if (row.type === "group") {
        const collapsed = !!this.collapsed[row.gid];
        const arrow = collapsed ? "▶" : "▼";
        const col = this.colors[row.gid] || this.colors.unknown;
        html += `<div class="wv-group-hdr" data-group="${row.gid}" style="height:${row.h}px;border-left-color:${col}">
          <span style="color:${col}">${arrow} ${GROUP_LABELS[row.gid]}${collapsed ? ` (${row.count})` : ""}</span>
        </div>`;
      } else {
        const sig = row.sig;
        const sel = this.selected === sig.name ? " wv-sig-selected" : "";
        const dim = this.selected && this.selected !== sig.name ? " wv-sig-dim" : "";
        const col = this._traceColor(sig);
        const rng = sig.width > 1 ? `[${sig.width - 1}:0]` : "";
        html += `<div class="wv-sig-row${sel}${dim}" data-sig="${sig.name}" style="height:${row.h}px;border-left-color:${col}">
          <span class="wv-sig-name" style="color:${col}">${sig.name}</span>
          <span class="wv-sig-width">${rng}</span>
        </div>`;
      }
    });
    this.namesEl.innerHTML = html;
    this.namesEl.style.minHeight = totalH + "px";

    this.namesEl.querySelectorAll(".wv-group-hdr").forEach((hdr) => {
      hdr.onclick = () => {
        const g = hdr.dataset.group;
        self.collapsed[g] = !self.collapsed[g];
        self._render();
      };
    });
    this.namesEl.querySelectorAll(".wv-sig-row").forEach((row) => {
      row.onclick = () => {
        self.selected = self.selected === row.dataset.sig ? null : row.dataset.sig;
        self._render();
      };
      row.ondblclick = () => self._fitSignal(row.dataset.sig);
    });
  };

  WaveformViewer.prototype._fitSignal = function (name) {
    const sig = (this.data.signals || []).find((s) => s.name === name);
    if (!sig || !sig.transitions.length) return;
    const t0 = toNs(sig.transitions[0].time, this.data);
    const t1 = toNs(sig.transitions[sig.transitions.length - 1].time, this.data);
    const span = Math.max(t1 - t0, this.endNs * 0.05);
    this.zoom = (this.waveScroll.clientWidth || 400) / span;
    this.waveScroll.scrollLeft = Math.max(0, t0 * this.zoom - 20);
    this._render();
  };

  WaveformViewer.prototype._timeToX = function (tRaw) {
    return toNs(tRaw, this.data) * this.zoom;
  };

  WaveformViewer.prototype._renderSvg = function (rows, totalH, contentW) {
    const ns = "http://www.w3.org/2000/svg";
    const h = totalH;
    const w = contentW;
    this.svg.setAttribute("width", w);
    this.svg.setAttribute("height", h);
    this.svg.innerHTML = "";

    const defs = document.createElementNS(ns, "defs");
    const pat = document.createElementNS(ns, "pattern");
    pat.setAttribute("id", "wv-hatch-x");
    pat.setAttribute("width", "6");
    pat.setAttribute("height", "6");
    pat.setAttribute("patternUnits", "userSpaceOnUse");
    const ln = document.createElementNS(ns, "line");
    ln.setAttribute("x1", "0");
    ln.setAttribute("y1", "0");
    ln.setAttribute("x2", "6");
    ln.setAttribute("y2", "6");
    ln.setAttribute("stroke", "#f85149");
    pat.appendChild(ln);
    defs.appendChild(pat);
    this.svg.appendChild(defs);

    const gGrid = document.createElementNS(ns, "g");
    const pan = this.waveScroll.scrollLeft / this.zoom;
    const viewW = this._viewWidthNs();
    const major = niceStep(viewW);
    const minor = major / 5;
    const nSteps = Math.ceil((this.endNs + major) / minor);
    for (let i = 0; i <= nSteps; i++) {
      const t = i * minor;
      const x = t * this.zoom;
      if (x > w + 10) break;
      const isMajor = i % 5 === 0;
      const gl = document.createElementNS(ns, "line");
      gl.setAttribute("x1", x);
      gl.setAttribute("x2", x);
      gl.setAttribute("y1", 0);
      gl.setAttribute("y2", h);
      gl.setAttribute("stroke", isMajor ? this.colors.grid : this.colors.gridMinor);
      gl.setAttribute("stroke-width", isMajor ? "1" : "0.5");
      if (!isMajor) gl.setAttribute("stroke-dasharray", "2,4");
      gGrid.appendChild(gl);
      if (isMajor) {
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x + 4);
        tx.setAttribute("y", 20);
        tx.setAttribute("fill", "#8b949e");
        tx.setAttribute("font-family", "JetBrains Mono, monospace");
        tx.setAttribute("font-size", "10");
        tx.textContent = formatTime(t);
        gGrid.appendChild(tx);
      }
    }
    this.svg.appendChild(gGrid);

    rows.forEach((row) => {
      if (row.type !== "signal") return;
      const sig = row.sig;
      const color = this._traceColor(sig);
      const y0 = row.y;
      const trs = sig.transitions || [];
      if (sig.width === 1) this._drawScalar(sig, trs, y0, color);
      else this._drawBus(sig, trs, y0, color);
    });

    const mx = this.markerNs * this.zoom;
    const ml = document.createElementNS(ns, "line");
    ml.setAttribute("class", "wv-marker-line");
    ml.setAttribute("x1", mx);
    ml.setAttribute("x2", mx);
    ml.setAttribute("y1", 0);
    ml.setAttribute("y2", h);
    ml.setAttribute("stroke", "#58a6ff");
    ml.setAttribute("stroke-width", "2");
    ml.setAttribute("stroke-dasharray", "6,3");
    this.svg.appendChild(ml);

    this._cursorLine = document.createElementNS(ns, "line");
    this._cursorLine.setAttribute("class", "wv-cursor-line");
    this._cursorLine.setAttribute("y1", 0);
    this._cursorLine.setAttribute("y2", h);
    this._cursorLine.setAttribute("stroke", this.colors.accent);
    this._cursorLine.setAttribute("stroke-width", "2");
    this.svg.appendChild(this._cursorLine);

    this._renderGridAndCursor();
  };

  WaveformViewer.prototype._renderGridAndCursor = function () {
    if (!this._cursorLine) return;
    const cx = this.cursorNs * this.zoom;
    this._cursorLine.setAttribute("x1", cx);
    this._cursorLine.setAttribute("x2", cx);
  };

  WaveformViewer.prototype._drawScalar = function (sig, trs, rowY, color) {
    const ns = "http://www.w3.org/2000/svg";
    const hi = rowY + 8;
    const lo = rowY + 26;
    const g = document.createElementNS(ns, "g");
    let px = null;
    let ph = false;
    trs.forEach((tr, i) => {
      const x = this._timeToX(tr.time);
      const v = String(tr.value).toUpperCase();
      const isHi = v === "1";
      const isX = v === "X" || v === "Z";
      if (px !== null) {
        const path = document.createElementNS(ns, "path");
        const y1 = ph ? hi : lo;
        const y2 = isHi ? hi : lo;
        path.setAttribute("d", `M ${px} ${y1} H ${x} V ${y2}`);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", isX ? "#f85149" : color);
        path.setAttribute("stroke-width", "2");
        g.appendChild(path);
      }
      px = x;
      ph = isHi;
      if (i === trs.length - 1) {
        const tail = document.createElementNS(ns, "line");
        tail.setAttribute("x1", x);
        tail.setAttribute("x2", this._contentWidth());
        tail.setAttribute("y1", isHi ? hi : lo);
        tail.setAttribute("y2", isHi ? hi : lo);
        tail.setAttribute("stroke", color);
        tail.setAttribute("stroke-width", "2");
        g.appendChild(tail);
      }
    });
    this.svg.appendChild(g);
  };

  WaveformViewer.prototype._drawBus = function (sig, trs, rowY, color) {
    const ns = "http://www.w3.org/2000/svg";
    const top = rowY + 6;
    const bot = rowY + 26;
    const mid = (top + bot) / 2;
    const rad = this.radix[sig.name] || "hex";
    const g = document.createElementNS(ns, "g");

    for (let i = 0; i < trs.length; i++) {
      const x0 = this._timeToX(trs[i].time);
      const x1 = i + 1 < trs.length ? this._timeToX(trs[i + 1].time) : this.endNs * this.zoom;
      const bw = Math.max(x1 - x0, 3);
      const val = trs[i].value;
      const u = String(val).toUpperCase();

      const fill = document.createElementNS(ns, "rect");
      fill.setAttribute("x", x0);
      fill.setAttribute("y", top);
      fill.setAttribute("width", bw);
      fill.setAttribute("height", bot - top);
      fill.setAttribute("fill", color);
      fill.setAttribute("opacity", "0.2");
      g.appendChild(fill);

      const path = document.createElementNS(ns, "path");
      const d = `M ${x0} ${top} L ${x0 + 5} ${mid} L ${x0} ${bot} H ${x0 + bw - 5} L ${x0 + bw} ${mid} L ${x0 + bw - 5} ${top} Z`;
      path.setAttribute("d", d);
      path.setAttribute("fill", color);
      path.setAttribute("fill-opacity", "0.25");
      path.setAttribute("stroke", color);
      path.setAttribute("stroke-width", "1.5");
      g.appendChild(path);

      if (bw > 24) {
        const label = parseBin(val, sig.width, rad, this.signed[sig.name]);
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x0 + bw / 2);
        tx.setAttribute("y", mid + 4);
        tx.setAttribute("text-anchor", "middle");
        tx.setAttribute("fill", "#e6edf3");
        tx.setAttribute("font-family", "JetBrains Mono, monospace");
        tx.setAttribute("font-size", "11");
        tx.textContent = u === "X" || u === "Z" ? u : label;
        g.appendChild(tx);
      }
    }
    this.svg.appendChild(g);
  };

  WaveformViewer.prototype._renderMinimap = function () {
    const ns = "http://www.w3.org/2000/svg";
    const w = this.minimapSvg.clientWidth || this.container.clientWidth || 400;
    const h = 48;
    this.minimapSvg.setAttribute("width", w);
    this.minimapSvg.setAttribute("height", h);
    this.minimapSvg.innerHTML = "";
    const end = this.endNs;
    let by = 2;
    (this.data.signals || []).forEach((sig, si) => {
      const col = this._traceColor(sig);
      const trs = sig.transitions || [];
      trs.forEach((tr, i) => {
        const t0 = toNs(tr.time, this.data);
        const t1 = i + 1 < trs.length ? toNs(trs[i + 1].time, this.data) : end;
        const r = document.createElementNS(ns, "rect");
        r.setAttribute("x", (t0 / end) * w);
        r.setAttribute("y", by + (si % 4) * 2);
        r.setAttribute("width", Math.max(((t1 - t0) / end) * w, 1));
        r.setAttribute("height", 8);
        r.setAttribute("fill", col);
        r.setAttribute("opacity", "0.85");
        this.minimapSvg.appendChild(r);
      });
    });
    const viewW = this._viewWidthNs();
    const vx = (this.panNs / end) * w;
    const vw = Math.max(8, (viewW / end) * w);
    const vp = document.createElementNS(ns, "rect");
    vp.setAttribute("fill", "rgba(247,185,85,0.15)");
    vp.setAttribute("stroke", this.colors.accent);
    vp.setAttribute("stroke-width", "1.5");
    vp.setAttribute("x", vx);
    vp.setAttribute("y", 0);
    vp.setAttribute("width", vw);
    vp.setAttribute("height", h);
    this.minimapSvg.appendChild(vp);

    this.minimapSvg.onmousedown = (e) => {
      const rect = this.minimapSvg.getBoundingClientRect();
      const frac = (e.clientX - rect.left) / rect.width;
      this.waveScroll.scrollLeft = Math.max(0, frac * this._contentWidth() - this.waveScroll.clientWidth / 2);
      this._render();
    };
  };

  WaveformViewer.prototype.destroy = function () {
    this.container.innerHTML = "";
    this.container.classList.remove("waveform-viewer");
  };

  window.WaveformViewer = {
    render: function (container, data) {
      if (container._wvInstance) container._wvInstance.destroy();
      if (data.error) {
        container.innerHTML = `<div class="wv-error">${data.error}</div>`;
        return null;
      }
      const inst = new WaveformViewer(container, data);
      container._wvInstance = inst;
      const ro = new ResizeObserver(() => {
        if (container._wvInstance) container._wvInstance._render();
      });
      ro.observe(container);
      container._wvResizeObs = ro;
      return inst;
    },
    destroy: function (container) {
      if (container._wvResizeObs) {
        container._wvResizeObs.disconnect();
        delete container._wvResizeObs;
      }
      if (container._wvInstance) {
        container._wvInstance.destroy();
        delete container._wvInstance;
      }
    },
  };
})();
