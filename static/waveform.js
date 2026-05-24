/**
 * VerifyRTL — in-browser SVG waveform viewer.
 */
(function () {
  "use strict";

  const ROW_H = 32;
  const GROUP_H = 26;
  const RULER_H = 32;
  const NAME_W = 280;
  const MIN_ZOOM = 0.05;
  const MAX_ZOOM = 500;

  const GROUP_ORDER = ["reference", "inputs", "outputs", "inouts", "testbench", "unknown", "results"];
  const GROUP_LABELS = {
    reference: "REFERENCE",
    inputs: "INPUTS",
    outputs: "OUTPUTS",
    inouts: "INOUTS",
    testbench: "TESTBENCH",
    unknown: "OTHER",
    results: "RESULTS",
  };

  const NO_BIT_EXPAND = /^(pass_cnt|fail_cnt)$/i;
  const CLOCK_NAMES = /^(clk|clock|ck)$/i;

  function displayGroup(sig) {
    if (sig.group === "reference") return "reference";
    if (NO_BIT_EXPAND.test(sig.name)) return "results";
    return sig.group || "unknown";
  }

  function isRefClk(sig) {
    return sig.name === "ref_clk" || sig.group === "reference";
  }

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
    if (ns >= 1) return ns.toFixed(3) + " ns";
    if (ns >= 0.001) return (ns * 1000).toFixed(2) + " ps";
    return (ns * 1e6).toFixed(1) + " fs";
  }

  function scalarBit(val) {
    const u = String(val).toUpperCase();
    if (u === "X" || u === "Z") return u;
    const bits = u.replace(/[^01]/g, "");
    if (!bits) return "0";
    return bits[bits.length - 1];
  }

  function ensureTransitionsAtZero(trs, width) {
    if (!trs || !trs.length) {
      return [{ time: 0, value: width > 1 ? "0".repeat(width) : "0" }];
    }
    if (trs[0].time === 0) return trs;
    return [{ time: 0, value: trs[0].value }, ...trs];
  }

  function formatCompactHex(n) {
    const v = n >>> 0;
    const hex = v.toString(16).toUpperCase();
    let minDigits = 1;
    if (v >= 100) minDigits = 3;
    else if (v >= 10) minDigits = 2;
    return "0x" + hex.padStart(minDigits, "0");
  }

  function isCounterSignal(sig) {
    return NO_BIT_EXPAND.test(sig.name);
  }

  function parseBin(val, width, radix, signed, compact) {
    const u = String(val).toUpperCase();
    if (u === "X" || u === "Z") return u;
    let bits = u.replace(/[^01]/g, "");
    if (!bits) bits = "0";
    bits = bits.padStart(width, "0").slice(-width);
    const n = parseInt(bits, 2) || 0;
    if (radix === "hex") {
      if (compact) return formatCompactHex(n);
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

  function extractBitFromBus(busVal, bitIndex, width) {
    const u = String(busVal).toUpperCase();
    if (u === "X" || u === "Z") return u;
    if (/^[XZ?]+$/.test(u)) return u[0];
    let bits = u.replace(/^B/i, "").replace(/[^01]/g, "");
    if (!bits) bits = "0";
    bits = bits.padStart(width, "0").slice(-width);
    const pos = width - 1 - bitIndex;
    return bits[pos] || "0";
  }

  /** Split multi-bit buses into a header row plus per-bit scalar rows (MSB first). */
  function expandSignalsToBits(signals) {
    const out = [];
    (signals || []).forEach((sig) => {
      const w = sig.width || 1;
      if (w <= 1 || NO_BIT_EXPAND.test(sig.name)) {
        out.push({ ...sig, width: w, rowKind: "scalar" });
        return;
      }
      out.push({
        name: sig.name,
        width: w,
        group: sig.group,
        transitions: sig.transitions || [],
        rowKind: "bus",
      });
      for (let b = w - 1; b >= 0; b--) {
        const bitTrans = (sig.transitions || []).map((tr) => ({
          time: tr.time,
          value: extractBitFromBus(tr.value, b, w),
        }));
        out.push({
          name: `${sig.name}(${b})`,
          width: 1,
          group: sig.group,
          transitions: bitTrans,
          rowKind: "bit",
          busName: sig.name,
          bitIndex: b,
        });
      }
    });
    return out;
  }

  function buildGroupedSignals(data, filter, collapsed) {
    const groups = {};
    const f = filter ? filter.toLowerCase() : "";
    (data.signals || []).forEach((sig) => {
      if (f) {
        const hay = [sig.name, sig.busName].filter(Boolean).join(" ").toLowerCase();
        if (!hay.includes(f)) return;
      }
      const g = displayGroup(sig);
      if (!groups[g]) groups[g] = [];
      groups[g].push(sig);
    });
    function signalSortRank(s) {
      const n = (s.busName || s.name).toLowerCase();
      if (s.rowKind === "bus") return 10;
      if (s.rowKind === "bit") return 20 + (999 - (s.bitIndex || 0));
      if (NO_BIT_EXPAND.test(s.name)) return n === "pass_cnt" ? 0 : 1;
      if (/^(rst_n|resetn|reset|rst)$/.test(n)) return 1;
      return 500;
    }
    function compareSignals(a, b) {
      const baseA = a.rowKind === "bus" ? a.name : a.busName || a.name;
      const baseB = b.rowKind === "bus" ? b.name : b.busName || b.name;
      if (baseA !== baseB) return baseA.localeCompare(baseB);
      const r = signalSortRank(a) - signalSortRank(b);
      return r !== 0 ? r : a.name.localeCompare(b.name);
    }
    GROUP_ORDER.forEach((g) => {
      if (groups[g]) groups[g].sort(compareSignals);
    });
    return { groups, collapsed };
  }

  function WaveformViewer(container, data) {
    this.container = container;
    this.data = Object.assign({}, data, {
      signals: expandSignalsToBits(data.signals || []),
    });
    this.endNs = Math.max(toNs(data.end_time || 0, data), 1);
    this.zoom = 1;
    this.panNs = 0;
    this.filter = "";
    this.collapsed = { reference: false };
    this.radix = {};
    this.signed = {};
    this.selected = null;
    this.markerNs = 0;
    this.cursorNs = 0;
    this.dragStartX = 0;
    this.dragStartPan = 0;
    this._listeners = [];

    this.colors = {
      accent: cssVar("--accent", "#f7b955"),
      reference: cssVar("--text-muted", "#8b949e"),
      inputs: cssVar("--led-blue", "#58a6ff"),
      outputs: cssVar("--led-green", "#3fb950"),
      inouts: cssVar("--led-amber", "#d29922"),
      testbench: cssVar("--text-muted", "#8b949e"),
      results: cssVar("--text-muted", "#8b949e"),
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
        <button type="button" class="wv-btn" data-act="fit" title="Fit all">⤢ FIT</button>
        <span class="wv-sep"></span>
        <span class="wv-time-label">Cursor: <span class="wv-cursor-time">0 ns</span></span>
        <span class="wv-sep"></span>
        <span class="wv-hint">Use + / − to zoom · Click waveform to set cursor</span>
        <label class="wv-filter">⌕ <input type="text" class="wv-filter-input" placeholder="filter signals" /></label>
      </div>
      <div class="wv-body">
        <div class="wv-axis-row">
          <div class="wv-names-corner">
            <span class="wv-ruler-label">TIME</span>
            <span class="wv-ruler-time">0 ns</span>
            <span class="wv-timescale-hint"></span>
          </div>
          <div class="wv-time-axis-scroll">
            <svg class="wv-axis-svg" xmlns="http://www.w3.org/2000/svg"></svg>
          </div>
        </div>
        <div class="wv-scroll-y">
          <div class="wv-track">
            <div class="wv-names"></div>
            <div class="wv-wave-h">
              <svg class="wv-svg" xmlns="http://www.w3.org/2000/svg"></svg>
            </div>
          </div>
        </div>
      </div>
      <div class="wv-minimap"><svg class="wv-minimap-svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
    `;

    this.vertScroll = el.querySelector(".wv-scroll-y");
    this.namesEl = el.querySelector(".wv-names");
    this.waveScroll = el.querySelector(".wv-wave-h");
    this.axisScroll = el.querySelector(".wv-time-axis-scroll");
    this.svg = el.querySelector(".wv-svg");
    this.axisSvg = el.querySelector(".wv-axis-svg");
    this.cursorTimeEl = el.querySelector(".wv-cursor-time");
    this.timescaleHintEl = el.querySelector(".wv-timescale-hint");
    this.filterInput = el.querySelector(".wv-filter-input");
    this.minimapSvg = el.querySelector(".wv-minimap-svg");

    this._bind();
    this._scheduleFitAndRender();
  };

  WaveformViewer.prototype._scheduleFitAndRender = function () {
    const self = this;
    let attempts = 0;
    const run = () => {
      attempts += 1;
      const w = self.waveScroll ? self.waveScroll.clientWidth : 0;
      if (w > 2 || attempts > 40) {
        if (self.zoom <= 0 || !Number.isFinite(self.zoom)) self.fit();
        self.cursorNs = 0;
        self.markerNs = 0;
        try {
          self._render();
        } catch (err) {
          self.container.innerHTML = `<div class="wv-error">Waveform render error: ${err.message}</div>`;
          console.error(err);
        }
        return;
      }
      requestAnimationFrame(run);
    };
    requestAnimationFrame(run);
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

    const blockWaveGestures = (e) => {
      e.preventDefault();
    };
    this.waveScroll.addEventListener("wheel", blockWaveGestures, { passive: false });
    this.waveScroll.addEventListener("touchmove", blockWaveGestures, { passive: false });
    this.waveScroll.addEventListener("gesturestart", blockWaveGestures);
    this.waveScroll.addEventListener("gesturechange", blockWaveGestures);
    this.waveScroll.addEventListener("gestureend", blockWaveGestures);

    const minimap = this.container.querySelector(".wv-minimap");
    if (minimap) {
      minimap.addEventListener("wheel", blockWaveGestures, { passive: false });
      minimap.addEventListener("touchmove", blockWaveGestures, { passive: false });
    }
    if (this.axisScroll) {
      this.axisScroll.addEventListener("wheel", blockWaveGestures, { passive: false });
    }

    this.waveScroll.addEventListener("scroll", () => {
      self.panNs = self.waveScroll.scrollLeft / self.zoom;
      if (self.axisScroll) self.axisScroll.scrollLeft = self.waveScroll.scrollLeft;
      self._renderGridAndCursor();
    });

    window.addEventListener("mousemove", (e) => {
      const rect = self.waveScroll.getBoundingClientRect();
      if (
        e.clientX >= rect.left &&
        e.clientX <= rect.right &&
        e.clientY >= rect.top &&
        e.clientY <= rect.bottom
      ) {
        const x = e.clientX - rect.left + self.waveScroll.scrollLeft;
        self.cursorNs = x / self.zoom;
        self._updateCursorDisplay();
        self._renderGridAndCursor();
      }
    });

    this.waveScroll.addEventListener("click", (e) => {
      const x = e.clientX - self.waveScroll.getBoundingClientRect().left + self.waveScroll.scrollLeft;
      self.markerNs = x / self.zoom;
      self.cursorNs = self.markerNs;
      self._updateCursorDisplay();
      self._render();
    });

  };

  WaveformViewer.prototype._contentWidth = function () {
    const z = this.zoom > 0 && Number.isFinite(this.zoom) ? this.zoom : MIN_ZOOM;
    const vw = this.waveScroll ? this.waveScroll.clientWidth : 0;
    return Math.max(this.endNs * z, vw || 400, 400);
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
    this._setScrollLeft(Math.max(0, newScroll));
    this.panNs = this.waveScroll.scrollLeft / this.zoom;
    this._render();
  };

  WaveformViewer.prototype._setScrollLeft = function (x) {
    this.waveScroll.scrollLeft = x;
    if (this.axisScroll) this.axisScroll.scrollLeft = x;
  };

  WaveformViewer.prototype.fit = function () {
    const w =
      (this.waveScroll && this.waveScroll.clientWidth) ||
      (this.container && this.container.clientWidth - NAME_W) ||
      600;
    if (w < 2) return;
    this.zoom = Math.max(MIN_ZOOM, w / Math.max(this.endNs, 1e-9));
    this._setScrollLeft(0);
    this.panNs = 0;
    this._render();
  };

  WaveformViewer.prototype._traceColor = function (sig) {
    const g = displayGroup(sig);
    return this.colors[g] || this.colors[sig.group] || this.colors.unknown;
  };

  WaveformViewer.prototype._buildLayout = function () {
    const { groups } = buildGroupedSignals(this.data, this.filter, this.collapsed);
    const rows = [];
    let y = 0;
    GROUP_ORDER.forEach((gid) => {
      const sigs = groups[gid];
      if (!sigs || !sigs.length) return;
      const count = sigs.filter((s) => s.rowKind !== "bit").length;
      rows.push({ type: "group", gid, y, h: GROUP_H, count });
      y += GROUP_H;
      if (!this.collapsed[gid] || gid === "reference") {
        sigs.forEach((sig) => {
          rows.push({ type: "signal", gid: displayGroup(sig), sig, y, h: ROW_H });
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

  WaveformViewer.prototype._formatSigValue = function (sig, ns) {
    const radKey = sig.busName || sig.name;
    const rad = this.radix[sig.name] || this.radix[radKey] || "hex";
    const compact = isCounterSignal(sig);
    return parseBin(
      this._valueAt(sig, ns),
      sig.width,
      rad,
      this.signed[sig.name] || this.signed[radKey],
      compact
    );
  };

  WaveformViewer.prototype._sigNameHtml = function (sig, col) {
    const w = sig.width || 1;
    const rng = w > 1 && sig.rowKind !== "bit" ? `[${w - 1}:0]` : "";
    const refNote = isRefClk(sig) ? '<span class="wv-ref-note">[10ns period · time marker]</span>' : "";
    return `<span class="wv-sig-name-col">
      <span class="wv-sig-name" style="color:${col}">${sig.name}</span>${rng ? `<span class="wv-sig-width">${rng}</span>` : ""}${refNote}
    </span>`;
  };

  WaveformViewer.prototype._updateCursorDisplay = function () {
    const ns = this.cursorNs != null ? this.cursorNs : 0;
    this.cursorTimeEl.textContent = formatTime(ns);
    const rulerTime = this.container.querySelector(".wv-ruler-time");
    if (rulerTime) rulerTime.textContent = formatTime(ns);
    if (!this.namesEl) return;
    const sigMap = {};
    (this.data.signals || []).forEach((s) => {
      sigMap[s.name] = s;
    });
    this.namesEl.querySelectorAll(".wv-sig-val").forEach((el) => {
      const sig = sigMap[el.dataset.sig];
      if (sig) el.textContent = this._formatSigValue(sig, ns);
    });
  };

  WaveformViewer.prototype._render = function () {
    if (this.zoom <= 0 || !Number.isFinite(this.zoom)) {
      const w = (this.waveScroll && this.waveScroll.clientWidth) || 400;
      this.zoom = Math.max(MIN_ZOOM, w / Math.max(this.endNs, 1e-9));
    }
    const { rows, totalH } = this._buildLayout();
    this._rows = rows;
    if (!rows.length) {
      const n = (this.data.signals || []).length;
      this.namesEl.innerHTML = `<div class="wv-error" style="padding:12px">No signals to display (${n} in data).</div>`;
      return;
    }
    const contentW = this._contentWidth();

    this._renderNames(rows, totalH);
    this._renderTimeAxis(contentW);
    this._renderSvg(rows, totalH, contentW);
    this._renderMinimap();
    this._updateCursorDisplay();
    if (this.waveScroll) this.panNs = this.waveScroll.scrollLeft / this.zoom;
  };

  WaveformViewer.prototype._renderNames = function (rows, totalH) {
    const self = this;
    const ns = this.cursorNs != null ? this.cursorNs : 0;
    let html = "";
    rows.forEach((row) => {
      if (row.type === "group") {
        const col = this.colors[row.gid] || this.colors.unknown;
        if (row.gid === "reference") {
          html += `<div class="wv-group-hdr wv-group-fixed" style="height:${row.h}px;border-left-color:${col}">
            <span style="color:${col}">${GROUP_LABELS.reference}</span>
          </div>`;
        } else {
          const collapsed = !!this.collapsed[row.gid];
          const arrow = collapsed ? "▶" : "▼";
          html += `<div class="wv-group-hdr" data-group="${row.gid}" style="height:${row.h}px;border-left-color:${col}">
            <span style="color:${col}">${arrow} ${GROUP_LABELS[row.gid]}${collapsed ? ` (${row.count})` : ""}</span>
          </div>`;
        }
      } else {
        const sig = row.sig;
        const sel = this.selected === sig.name ? " wv-sig-selected" : "";
        const dim = this.selected && this.selected !== sig.name ? " wv-sig-dim" : "";
        const col = this._traceColor(sig);
        const val = this._formatSigValue(sig, ns);
        const rowCls =
          sig.rowKind === "bus" ? " wv-sig-row wv-bus-row" : sig.rowKind === "bit" ? " wv-sig-row wv-bit-row" : " wv-sig-row";
        html += `<div class="${rowCls}${sel}${dim}" data-sig="${sig.name}" style="height:${row.h}px;border-left-color:${col}">
          ${this._sigNameHtml(sig, col)}
          <span class="wv-sig-val" data-sig="${sig.name}">${val}</span>
        </div>`;
      }
    });
    this.namesEl.innerHTML = html;
    this.namesEl.style.height = totalH + "px";
    const track = this.container.querySelector(".wv-track");
    if (track) track.style.height = totalH + "px";

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
    this._setScrollLeft(Math.max(0, t0 * this.zoom - 20));
    this._render();
  };

  WaveformViewer.prototype._timeToX = function (tRaw) {
    return toNs(tRaw, this.data) * this.zoom;
  };

  WaveformViewer.prototype._gridSteps = function () {
    const viewW = this._viewWidthNs();
    const major = niceStep(viewW);
    const minor = major / 5;
    return { major, minor };
  };

  WaveformViewer.prototype._clockPosedges = function () {
    const clk =
      (this.data.signals || []).find((s) => s.name === "ref_clk") ||
      (this.data.signals || []).find((s) => CLOCK_NAMES.test(s.name) && s.rowKind !== "bit");
    if (!clk) return [];
    const trs = ensureTransitionsAtZero(clk.transitions || [], 1);
    const edges = [];
    let prev = scalarBit(trs[0].value);
    for (let i = 1; i < trs.length; i++) {
      const v = scalarBit(trs[i].value);
      if (v === "1" && prev !== "1") edges.push(toNs(trs[i].time, this.data));
      prev = v;
    }
    return edges;
  };

  WaveformViewer.prototype._renderTimeAxis = function (contentW) {
    if (!this.axisSvg) return;
    const ns = "http://www.w3.org/2000/svg";
    const h = RULER_H;
    const w = contentW;
    this.axisSvg.setAttribute("width", w);
    this.axisSvg.setAttribute("height", h);

    if (this.timescaleHintEl) {
      const ts = (this.data.timescale || "1 ns").trim();
      const end = formatTime(this.endNs);
      this.timescaleHintEl.textContent = `${ts} · 0 → ${end}`;
    }

    this.axisSvg.innerHTML = "";
    const bg = document.createElementNS(ns, "rect");
    bg.setAttribute("width", w);
    bg.setAttribute("height", h);
    bg.setAttribute("fill", cssVar("--surface-3", "#161b22"));
    this.axisSvg.appendChild(bg);

    const baseline = h - 6;
    const baseLine = document.createElementNS(ns, "line");
    baseLine.setAttribute("x1", "0");
    baseLine.setAttribute("x2", w);
    baseLine.setAttribute("y1", baseline);
    baseLine.setAttribute("y2", baseline);
    baseLine.setAttribute("stroke", this.colors.grid);
    this.axisSvg.appendChild(baseLine);

    const { major, minor } = this._gridSteps();
    if (minor <= 0) return;
    const nSteps = Math.min(5000, Math.ceil((this.endNs + major) / minor));
    for (let i = 0; i <= nSteps; i++) {
      const t = i * minor;
      const x = t * this.zoom;
      if (x > w + 10) break;
      const isMajor = i % 5 === 0;
      const tick = document.createElementNS(ns, "line");
      tick.setAttribute("x1", x);
      tick.setAttribute("x2", x);
      tick.setAttribute("y1", isMajor ? 4 : baseline - 6);
      tick.setAttribute("y2", baseline);
      tick.setAttribute("stroke", isMajor ? this.colors.grid : this.colors.gridMinor);
      tick.setAttribute("stroke-width", isMajor ? "1" : "0.5");
      this.axisSvg.appendChild(tick);
      if (isMajor) {
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", x + 3);
        tx.setAttribute("y", 12);
        tx.setAttribute("fill", cssVar("--text-muted", "#8b949e"));
        tx.setAttribute("font-family", "JetBrains Mono, monospace");
        tx.setAttribute("font-size", "10");
        tx.textContent = formatTime(t);
        this.axisSvg.appendChild(tx);
      }
    }

    const edges = this._clockPosedges();
    edges.forEach((t) => {
      const x = t * this.zoom;
      if (x < 0 || x > w) return;
      const tri = document.createElementNS(ns, "path");
      tri.setAttribute("d", `M ${x} ${baseline} L ${x - 4} ${baseline + 5} L ${x + 4} ${baseline + 5} Z`);
      tri.setAttribute("fill", this.colors.clock);
      tri.setAttribute("opacity", "0.9");
      this.axisSvg.appendChild(tri);
    });

    this._axisCursor = document.createElementNS(ns, "line");
    this._axisCursor.setAttribute("y1", 0);
    this._axisCursor.setAttribute("y2", h);
    this._axisCursor.setAttribute("stroke", this.colors.accent);
    this._axisCursor.setAttribute("stroke-width", "2");
    this.axisSvg.appendChild(this._axisCursor);
    this._renderGridAndCursor();
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

    rows.forEach((row) => {
      if (row.type !== "group") return;
      const band = document.createElementNS(ns, "rect");
      band.setAttribute("x", "0");
      band.setAttribute("y", String(row.y));
      band.setAttribute("width", String(w));
      band.setAttribute("height", String(row.h));
      band.setAttribute("fill", this.colors.gridMinor);
      this.svg.appendChild(band);
    });

    const gGrid = document.createElementNS(ns, "g");
    const { major, minor } = this._gridSteps();
    if (minor <= 0) return;
    const nSteps = Math.min(5000, Math.ceil((this.endNs + major) / minor));
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
    }
    this.svg.appendChild(gGrid);

    rows.forEach((row) => {
      if (row.type !== "signal") return;
      const sig = row.sig;
      if (sig.rowKind === "bus") return;
      const color = this._traceColor(sig);
      const y0 = row.y;
      const trs = ensureTransitionsAtZero(sig.transitions || [], sig.width || 1);
      if (isRefClk(sig) && sig.width === 1) this._drawClock(sig, trs, y0, color, true);
      else if (CLOCK_NAMES.test(sig.name) && sig.width === 1) this._drawClock(sig, trs, y0, color, false);
      else if (sig.width === 1) this._drawScalar(sig, trs, y0, color);
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
    const cx = this.cursorNs * this.zoom;
    if (this._cursorLine) {
      this._cursorLine.setAttribute("x1", cx);
      this._cursorLine.setAttribute("x2", cx);
    }
    if (this._axisCursor) {
      this._axisCursor.setAttribute("x1", cx);
      this._axisCursor.setAttribute("x2", cx);
    }
  };

  WaveformViewer.prototype._drawClock = function (sig, trs, rowY, color, isRef) {
    if (isRef) color = this.colors.reference;
    const strokeW = isRef ? "1" : "2.5";
    const ns = "http://www.w3.org/2000/svg";
    const hi = rowY + 6;
    const lo = rowY + 26;
    const g = document.createElementNS(ns, "g");
    let px = null;
    let ph = false;
    trs.forEach((tr, i) => {
      const x = this._timeToX(tr.time);
      const bit = scalarBit(tr.value);
      const isHi = bit === "1";
      const isX = bit === "X" || bit === "Z";
      if (px !== null) {
        const path = document.createElementNS(ns, "path");
        const y1 = ph ? hi : lo;
        const y2 = isHi ? hi : lo;
        path.setAttribute("d", `M ${px} ${y1} H ${x} V ${y2}`);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", isX ? "#f85149" : color);
        path.setAttribute("stroke-width", strokeW);
        g.appendChild(path);
        if (!isRef) {
          const edge = document.createElementNS(ns, "line");
          edge.setAttribute("x1", x);
          edge.setAttribute("x2", x);
          edge.setAttribute("y1", hi);
          edge.setAttribute("y2", lo);
          edge.setAttribute("stroke", color);
          edge.setAttribute("stroke-width", "1.5");
          edge.setAttribute("opacity", "0.65");
          g.appendChild(edge);
        }
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
        tail.setAttribute("stroke-width", strokeW);
        g.appendChild(tail);
      }
    });
    const halfPeriods = [];
    for (let i = 1; i < trs.length; i++) {
      halfPeriods.push(this._timeToX(trs[i].time) - this._timeToX(trs[i - 1].time));
    }
    if (!isRef && halfPeriods.length >= 2) {
      const med = halfPeriods.slice().sort((a, b) => a - b)[Math.floor(halfPeriods.length / 2)];
      if (med > 28) {
        const tx = document.createElementNS(ns, "text");
        tx.setAttribute("x", this._timeToX(trs[1].time) + med * 0.35);
        tx.setAttribute("y", rowY + 14);
        tx.setAttribute("fill", color);
        tx.setAttribute("font-family", "JetBrains Mono, monospace");
        tx.setAttribute("font-size", "9");
        tx.setAttribute("opacity", "0.85");
        const tHalf = toNs(trs[1].time, this.data) - toNs(trs[0].time, this.data);
        tx.textContent = `T/2=${formatTime(tHalf)}`;
        g.appendChild(tx);
      }
    }
    this.svg.appendChild(g);
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
      const bit = scalarBit(tr.value);
      const isHi = bit === "1";
      const isX = bit === "X" || bit === "Z";
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
        const label = parseBin(val, sig.width, rad, this.signed[sig.name], isCounterSignal(sig));
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
      if (sig.rowKind === "bus") return;
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

  };

  WaveformViewer.prototype.destroy = function () {
    this.container.innerHTML = "";
    this.container.classList.remove("waveform-viewer");
  };

  window.WaveformViewer = {
    render: function (container, data) {
      if (container._wvInstance) container._wvInstance.destroy();
      if (!data || data.error) {
        container.innerHTML = `<div class="wv-error">${(data && data.error) || "No waveform data"}</div>`;
        return null;
      }
      if (!data.signals || !data.signals.length) {
        container.innerHTML =
          '<div class="wv-error">No signals in waveform data. Re-run verification after restarting the server.</div>';
        return null;
      }
      const inst = new WaveformViewer(container, data);
      container._wvInstance = inst;
      const ro = new ResizeObserver(() => {
        const inst = container._wvInstance;
        if (!inst) return;
        if (inst.zoom <= 0 && inst.waveScroll && inst.waveScroll.clientWidth > 2) inst.fit();
        else inst._render();
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
