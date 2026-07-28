/**
 * VerifyRTL — Traceability tab: spec -> atomic requirements -> mapped
 * against this design's verification-plan test cases and any already-
 * verified formal properties (pasted in) -> a traceability matrix that
 * honestly flags any requirement with no genuine match.
 *
 * Self-contained: its own RTL/spec inputs (paste or file), independent of
 * the Design screen — mirrors the Formal tab's pattern (Phase 2).
 */
(function () {
  "use strict";

  let dutRtl = "";
  let dutFileName = "";
  let dutTopModule = "";
  let rtlMode = "paste";

  let specText = "";
  let specFileName = "";
  let specMode = "paste";

  let propertiesText = "";

  let matrix = null; // last /api/traceability result

  function $(sel) {
    return document.querySelector(sel);
  }

  function render() {
    const panel = $("#panelTraceability");
    if (!panel) return;
    if (!dutRtl && App.rtlContent) dutRtl = App.rtlContent;
    if (!dutTopModule) {
      const topEl = document.getElementById("topModule");
      if (topEl && topEl.value.trim()) dutTopModule = topEl.value.trim();
    }

    panel.innerHTML = `
      <div class="panel">
        <div class="panel__header"><h2 class="panel__title">DESIGN UNDER TEST</h2></div>
        <div class="panel__body">
          <p class="text-dim">Independent of the Design step — paste or upload RTL here, no
            simulation required.</p>
          <div class="segmented" role="tablist" aria-label="RTL input mode" id="traceRtlTabs">
            <button type="button" class="segmented__btn ${rtlMode === "file" ? "active" : ""}" data-mode="file">FILE</button>
            <button type="button" class="segmented__btn ${rtlMode === "paste" ? "active" : ""}" data-mode="paste">PASTE</button>
          </div>
          <div id="traceRtlFileZone" style="${rtlMode === "file" ? "" : "display:none"}; margin-top:var(--s-3)">
            <input type="file" id="traceRtlFile" accept=".v,.sv">
            <span class="text-dim mono" id="traceRtlFileName" style="margin-left:var(--s-2)">${App.escapeHtml(dutFileName)}</span>
          </div>
          <div id="traceRtlPasteZone" style="${rtlMode === "paste" ? "" : "display:none"}; margin-top:var(--s-3)">
            <textarea id="traceRtlPaste" class="input-mono" rows="8" style="width:100%"
              placeholder="module my_dut ( ... );&#10;endmodule">${App.escapeHtml(dutRtl)}</textarea>
          </div>
          <label class="config-label" style="margin-top:var(--s-3); display:block">TOP MODULE</label>
          <input type="text" id="traceTopModule" class="input-mono" placeholder="auto-detect"
            value="${App.escapeHtml(dutTopModule)}" style="max-width:280px">
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">SPECIFICATION</h2></div>
        <div class="panel__body">
          <p class="text-dim">The requirements come from here — atomic, testable statements are
            extracted from this text.</p>
          <div class="segmented" role="tablist" aria-label="Spec input mode" id="traceSpecTabs">
            <button type="button" class="segmented__btn ${specMode === "file" ? "active" : ""}" data-mode="file">FILE</button>
            <button type="button" class="segmented__btn ${specMode === "paste" ? "active" : ""}" data-mode="paste">PASTE</button>
          </div>
          <div id="traceSpecFileZone" style="${specMode === "file" ? "" : "display:none"}; margin-top:var(--s-3)">
            <input type="file" id="traceSpecFile" accept=".md,.txt">
            <span class="text-dim mono" id="traceSpecFileName" style="margin-left:var(--s-2)">${App.escapeHtml(specFileName)}</span>
          </div>
          <div id="traceSpecPasteZone" style="${specMode === "paste" ? "" : "display:none"}; margin-top:var(--s-3)">
            <textarea id="traceSpecPaste" class="input-mono" rows="6" style="width:100%"
              placeholder="e.g. On reset, the FSM must enter the IDLE state within one cycle...">${App.escapeHtml(specText)}</textarea>
          </div>
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">VERIFIED PROPERTIES (OPTIONAL)</h2></div>
        <div class="panel__body">
          <p class="text-dim">One plain-English property description per line — copy these over from
            the Formal tab. Without them, requirements are only checked against the auto-generated
            verification-plan test cases.</p>
          <textarea id="tracePropsPaste" class="input-mono" rows="4" style="width:100%"
            placeholder="light is never an invalid encoding&#10;reset forces the FSM to S_RED">${App.escapeHtml(propertiesText)}</textarea>
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">TRACEABILITY MATRIX</h2></div>
        <div class="panel__body">
          <button type="button" class="btn btn-primary" id="traceBuildBtn">Build Traceability Matrix</button>
          <span id="traceStatus" class="text-dim mono" style="margin-left:var(--s-3)"></span>
          <div id="traceMatrix" style="margin-top:var(--s-4)"></div>
        </div>
      </div>
    `;

    bindDutInputs();
    bindSpecInputs();
    $("#tracePropsPaste").addEventListener("input", (e) => { propertiesText = e.target.value; });
    $("#traceBuildBtn").addEventListener("click", buildMatrix);
    renderMatrix();
  }

  function bindDutInputs() {
    $("#traceRtlTabs").querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        rtlMode = btn.dataset.mode;
        render();
      });
    });
    const fileInput = $("#traceRtlFile");
    if (fileInput) {
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files[0];
        if (!f) return;
        dutRtl = await f.text();
        dutFileName = f.name;
        $("#traceRtlFileName").textContent = dutFileName;
      });
    }
    const pasteEl = $("#traceRtlPaste");
    if (pasteEl) pasteEl.addEventListener("input", () => { dutRtl = pasteEl.value; });
    const topEl = $("#traceTopModule");
    if (topEl) topEl.addEventListener("input", () => { dutTopModule = topEl.value; });
  }

  function bindSpecInputs() {
    $("#traceSpecTabs").querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        specMode = btn.dataset.mode;
        render();
      });
    });
    const fileInput = $("#traceSpecFile");
    if (fileInput) {
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files[0];
        if (!f) return;
        specText = await f.text();
        specFileName = f.name;
        $("#traceSpecFileName").textContent = specFileName;
      });
    }
    const pasteEl = $("#traceSpecPaste");
    if (pasteEl) pasteEl.addEventListener("input", () => { specText = pasteEl.value; });
  }

  async function buildMatrix() {
    const status = $("#traceStatus");
    const btn = $("#traceBuildBtn");
    if (!dutRtl.trim()) {
      status.textContent = "Add RTL above first.";
      return;
    }
    if (!specText.trim()) {
      status.textContent = "Add a specification above first.";
      return;
    }
    if (btn.disabled) return;
    btn.disabled = true;
    status.textContent = "Extracting requirements and checking coverage…";
    $("#traceMatrix").innerHTML = "";

    const fd = new FormData();
    fd.append("rtl_text", dutRtl);
    fd.append("spec_text", specText);
    fd.append("top_module", dutTopModule.trim());
    fd.append("properties_text", propertiesText);

    let data;
    try {
      const res = await fetch("/api/traceability", { method: "POST", body: fd });
      data = await res.json();
    } catch (err) {
      status.textContent = "Request failed: " + (err.message || err);
      btn.disabled = false;
      return;
    }
    btn.disabled = false;

    if (data.error) {
      status.textContent = "";
      $("#traceMatrix").innerHTML = `<div class="callout callout-error">
        <span class="callout__prefix">TRACEABILITY FAILED</span>
        <p class="callout-unverified__body">${App.escapeHtml(data.error)}</p>
      </div>`;
      return;
    }

    matrix = data;
    status.textContent = `${data.total} requirements — ${data.gap_count} untested, checked against ${data.num_coverage_items} coverage items.`;
    renderMatrix();
  }

  function renderMatrix() {
    const host = $("#traceMatrix");
    if (!host) return;
    if (!matrix) {
      host.innerHTML = "";
      return;
    }
    const gapBadge = matrix.gap_count > 0
      ? `<span class="result-badge fail"><span class="led led-red"></span><span class="mono">${matrix.gap_count} GAP${matrix.gap_count === 1 ? "" : "S"}</span></span>`
      : `<span class="result-badge pass"><span class="led led-green"></span><span class="mono">ALL LINKED</span></span>`;

    const rows = (matrix.requirements || [])
      .map((r) => {
        const covered = !!r.covered;
        const badge = covered
          ? `<span class="result-badge pass"><span class="led led-green"></span><span class="mono">COVERED</span></span>`
          : `<span class="result-badge fail"><span class="led led-red"></span><span class="mono">UNTESTED</span></span>`;
        const linked = (r.linked_items || [])
          .map((it) => `<li class="mono">[${App.escapeHtml(it.source)}] ${App.escapeHtml(it.description)}</li>`)
          .join("");
        return `
        <div class="panel" style="margin-top:var(--s-3)">
          <div class="panel__header">
            ${badge}
            <span class="text-dim mono" style="margin-left:var(--s-2); font-size:11px">${App.escapeHtml(r.id)}</span>
            <span style="margin-left:var(--s-3)">${App.escapeHtml(r.text)}</span>
          </div>
          <div class="panel__body">
            ${linked ? `<ul style="margin:0; padding-left:18px">${linked}</ul>` : ""}
            ${!covered && r.rationale ? `<p class="text-dim">${App.escapeHtml(r.rationale)}</p>` : ""}
          </div>
        </div>`;
      })
      .join("");

    host.innerHTML = `
      <div style="display:flex; align-items:center; gap:var(--s-3); margin-bottom:var(--s-3)">
        ${gapBadge}
        <span class="text-dim mono">${matrix.total} requirements extracted</span>
      </div>
      ${rows || '<p class="text-dim">No requirements extracted from this spec.</p>'}
    `;
  }

  window.Traceability = { render };
})();
