/**
 * VerifyRTL — Formal tab: hand-written properties checked with SymbiYosys.
 * Self-contained: reads App.rtlContent / #topModule, posts to /api/formal,
 * renders PROVEN/FALSIFIED per property with the counterexample waveform
 * (reusing WaveformViewer, the same renderer the Waveform tab uses).
 */
(function () {
  "use strict";

  let propRows = [];
  let rowSeq = 0;

  function $(sel) {
    return document.querySelector(sel);
  }

  function defaultProps() {
    return [{ id: ++rowSeq, description: "", expr: "" }];
  }

  function render() {
    const panel = $("#panelFormal");
    if (!panel) return;
    if (!propRows.length) propRows = defaultProps();

    panel.innerHTML = `
      <div class="panel">
        <div class="panel__header"><h2 class="panel__title">FORMAL PROPERTY CHECK</h2></div>
        <div class="panel__body">
          <p class="text-dim">Write one or more boolean rules about this design using its own port names
            (e.g. <code>light &lt;= 1</code>). Each is checked with SymbiYosys — PROVEN means true for all
            time on sequential designs (unbounded proof), not just the inputs tested by simulation.</p>
          <div id="formalPropRows"></div>
          <button type="button" class="btn btn-ghost" id="formalAddProp">+ Add property</button>
          <div style="margin-top:var(--s-4)">
            <button type="button" class="btn btn-primary" id="formalRunBtn">Run Formal Check</button>
            <span id="formalStatus" class="text-dim mono" style="margin-left:var(--s-3)"></span>
          </div>
        </div>
      </div>
      <div id="formalResults"></div>
    `;

    renderPropRows();
    $("#formalAddProp").addEventListener("click", () => {
      propRows.push({ id: ++rowSeq, description: "", expr: "" });
      renderPropRows();
    });
    $("#formalRunBtn").addEventListener("click", runFormalCheck);
  }

  function renderPropRows() {
    const host = $("#formalPropRows");
    if (!host) return;
    host.innerHTML = propRows
      .map(
        (r) => `
      <div class="formal-prop-row" data-id="${r.id}" style="display:flex; gap:var(--s-2); margin-bottom:var(--s-2); align-items:center;">
        <input type="text" class="input-mono" data-field="description"
          placeholder="plain English, e.g. light is never invalid"
          value="${App.escapeHtml(r.description)}" style="flex:1.4">
        <input type="text" class="input-mono mono" data-field="expr"
          placeholder="light <= 1"
          value="${App.escapeHtml(r.expr)}" style="flex:1">
        <button type="button" class="btn-ghost" data-remove="${r.id}" title="Remove">&times;</button>
      </div>`
      )
      .join("");

    host.querySelectorAll(".formal-prop-row").forEach((rowEl) => {
      const id = Number(rowEl.dataset.id);
      const row = propRows.find((r) => r.id === id);
      if (!row) return;
      rowEl.querySelectorAll("input").forEach((inp) => {
        inp.addEventListener("input", () => {
          row[inp.dataset.field] = inp.value;
        });
      });
    });

    host.querySelectorAll("[data-remove]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.dataset.remove);
        propRows = propRows.filter((r) => r.id !== id);
        if (!propRows.length) propRows = defaultProps();
        renderPropRows();
      });
    });
  }

  async function runFormalCheck() {
    const status = $("#formalStatus");
    const resultsEl = $("#formalResults");
    const rtl = App.rtlContent || "";
    if (!rtl.trim()) {
      status.textContent = "Load RTL on the Design step first.";
      return;
    }
    const props = propRows
      .filter((r) => r.expr.trim())
      .map((r, i) => ({
        name: `prop${i}`,
        description: r.description.trim(),
        expr: r.expr.trim(),
      }));
    if (!props.length) {
      status.textContent = "Enter at least one property expression.";
      return;
    }

    status.textContent = "Running SymbiYosys…";
    resultsEl.innerHTML = "";
    const topModuleEl = document.getElementById("topModule");

    const fd = new FormData();
    fd.append("rtl_text", rtl);
    fd.append("top_module", topModuleEl ? topModuleEl.value.trim() : "");
    fd.append("properties", JSON.stringify(props));

    let data;
    try {
      const res = await fetch("/api/formal", { method: "POST", body: fd });
      data = await res.json();
    } catch (err) {
      status.textContent = "Request failed: " + (err.message || err);
      return;
    }

    if (data.error) {
      status.textContent = "";
      resultsEl.innerHTML = `<div class="callout callout-error">
        <span class="callout__prefix">FORMAL UNAVAILABLE</span>
        <p class="callout-unverified__body">${App.escapeHtml(data.error)}</p>
      </div>`;
      return;
    }

    status.textContent = `${data.engine} v${data.engine_version}`;
    renderResults(data);
  }

  function renderResults(data) {
    const resultsEl = $("#formalResults");
    resultsEl.innerHTML = data.properties
      .map((p, i) => {
        const ok = p.success;
        const badgeCls = p.error ? "error" : ok ? "pass" : "fail";
        const ledCls = p.error ? "led-red" : ok ? "led-green" : "led-red";
        const verdict = p.error ? "ERROR" : p.verdict;
        return `
        <div class="panel" style="margin-top:var(--s-4)">
          <div class="panel__header">
            <span class="result-badge ${badgeCls}"><span class="led ${ledCls}"></span><span class="mono">${verdict}</span></span>
            <span style="margin-left:var(--s-3)">${App.escapeHtml(p.description || "(no description)")}</span>
          </div>
          <div class="panel__body">
            <pre class="code-block" style="max-height:80px">${App.escapeHtml(p.expr)}</pre>
            ${p.error ? `<p class="callout-unverified__body">${App.escapeHtml(p.error)}</p>` : ""}
            ${
              p.has_trace
                ? `<p class="text-dim">Falsified — counterexample trace below.</p>
                   <div id="formalWave${i}" class="wave-visual-panel active" style="min-height:200px"></div>`
                : ""
            }
          </div>
        </div>`;
      })
      .join("");

    data.properties.forEach((p, i) => {
      if (p.has_trace && p.waveform_json && window.WaveformViewer) {
        const container = document.getElementById(`formalWave${i}`);
        if (container) WaveformViewer.render(container, p.waveform_json);
      }
    });
  }

  window.Formal = { render };
})();
