/**
 * VerifyRTL — Formal tab: RTL + spec -> LLM-suggested assert/assume/cover
 * properties (classified, grounded in docs/formal_property_reference.md) ->
 * user review -> SVA conversion -> SymbiYosys proof.
 *
 * Self-contained: its own RTL/spec inputs (paste or file), independent of
 * the Design screen — this tab never requires running a simulation first.
 * Every stage is a separate, explicit user action; nothing auto-advances.
 */
(function () {
  "use strict";

  let dutRtl = "";
  let dutFileName = "";
  let dutTopModule = "";
  let rtlMode = "paste"; // "file" | "paste"

  let specText = "";
  let specFileName = "";
  let specMode = "paste";

  let suggestions = []; // {id, name, kind, pattern, description, signals, rationale, included}
  let propRows = [];
  let rowSeq = 0;

  function $(sel) {
    return document.querySelector(sel);
  }

  function defaultProps() {
    return [{ id: ++rowSeq, description: "", expr: "", kind: "assert" }];
  }

  function render() {
    const panel = $("#panelFormal");
    if (!panel) return;
    if (!propRows.length) propRows = defaultProps();
    if (!dutRtl && App.rtlContent) dutRtl = App.rtlContent; // convenience default, still independent/editable
    if (!dutTopModule) {
      const topEl = document.getElementById("topModule");
      if (topEl && topEl.value.trim()) dutTopModule = topEl.value.trim();
    }

    panel.innerHTML = `
      <div class="panel">
        <div class="panel__header"><h2 class="panel__title">DESIGN UNDER TEST</h2></div>
        <div class="panel__body">
          <p class="text-dim">Independent of the Design step — paste or upload RTL here to go
            straight to formal, no simulation required.</p>
          <div class="segmented" role="tablist" aria-label="RTL input mode" id="formalRtlTabs">
            <button type="button" class="segmented__btn ${rtlMode === "file" ? "active" : ""}" data-mode="file">FILE</button>
            <button type="button" class="segmented__btn ${rtlMode === "paste" ? "active" : ""}" data-mode="paste">PASTE</button>
          </div>
          <div id="formalRtlFileZone" style="${rtlMode === "file" ? "" : "display:none"}; margin-top:var(--s-3)">
            <input type="file" id="formalRtlFile" accept=".v,.sv">
            <span class="text-dim mono" id="formalRtlFileName" style="margin-left:var(--s-2)">${App.escapeHtml(dutFileName)}</span>
          </div>
          <div id="formalRtlPasteZone" style="${rtlMode === "paste" ? "" : "display:none"}; margin-top:var(--s-3)">
            <textarea id="formalRtlPaste" class="input-mono" rows="8" style="width:100%"
              placeholder="module my_dut ( ... );&#10;endmodule">${App.escapeHtml(dutRtl)}</textarea>
          </div>
          <label class="config-label" style="margin-top:var(--s-3); display:block">TOP MODULE</label>
          <input type="text" id="formalTopModule" class="input-mono" placeholder="auto-detect"
            value="${App.escapeHtml(dutTopModule)}" style="max-width:280px">
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">SPECIFICATION (OPTIONAL)</h2></div>
        <div class="panel__body">
          <p class="text-dim">A written requirement helps the suggestion engine, but isn't required —
            without one, properties are proposed from the RTL's structure and signal naming alone.</p>
          <div class="segmented" role="tablist" aria-label="Spec input mode" id="formalSpecTabs">
            <button type="button" class="segmented__btn ${specMode === "file" ? "active" : ""}" data-mode="file">FILE</button>
            <button type="button" class="segmented__btn ${specMode === "paste" ? "active" : ""}" data-mode="paste">PASTE</button>
          </div>
          <div id="formalSpecFileZone" style="${specMode === "file" ? "" : "display:none"}; margin-top:var(--s-3)">
            <input type="file" id="formalSpecFile" accept=".md,.txt">
            <span class="text-dim mono" id="formalSpecFileName" style="margin-left:var(--s-2)">${App.escapeHtml(specFileName)}</span>
          </div>
          <div id="formalSpecPasteZone" style="${specMode === "paste" ? "" : "display:none"}; margin-top:var(--s-3)">
            <textarea id="formalSpecPaste" class="input-mono" rows="5" style="width:100%"
              placeholder="e.g. output must not assert done before ack...">${App.escapeHtml(specText)}</textarea>
          </div>
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">SUGGEST PROPERTIES</h2></div>
        <div class="panel__body">
          <button type="button" class="btn btn-primary" id="formalSuggestBtn">Suggest Properties</button>
          <span id="formalSuggestStatus" class="text-dim mono" style="margin-left:var(--s-3)"></span>
          <div id="formalSuggestions" style="margin-top:var(--s-4)"></div>
        </div>
      </div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header"><h2 class="panel__title">FORMAL PROPERTY CHECK</h2></div>
        <div class="panel__body">
          <p class="text-dim">Review the list below (from suggestions above, or written by hand), then run.
            PROVEN/REACHED means true for all time on sequential designs (unbounded proof), not just the
            inputs a simulation happened to try.</p>
          <div id="formalPropRows"></div>
          <button type="button" class="btn btn-ghost" id="formalAddProp">+ Add property by hand</button>
          <div style="display:flex; gap:var(--s-3); align-items:center; flex-wrap:wrap; margin-top:var(--s-4)">
            <label class="text-dim mono" style="font-size:12px">Timeout (s)
              <input type="number" id="formalTimeout" class="input-mono" value="300" min="10" max="7200" style="width:80px; margin-left:6px">
            </label>
            <label class="text-dim mono" style="font-size:12px">Depth override (blank = auto)
              <input type="number" id="formalDepthOverride" class="input-mono" placeholder="auto" min="1" style="width:70px; margin-left:6px">
            </label>
            <button type="button" class="btn btn-primary" id="formalRunBtn">Run Formal Check</button>
            <span id="formalStatus" class="text-dim mono"></span>
          </div>
        </div>
      </div>
      <div id="formalResults"></div>

      <div class="panel" style="margin-top:var(--s-4)">
        <div class="panel__header">
          <h2 class="panel__title">RUN HISTORY</h2>
        </div>
        <div class="panel__body">
          <p class="text-dim">Every suggest/convert/run attempt, logged locally so it survives across
            sessions — not just this browser tab.</p>
          <button type="button" class="btn btn-ghost" id="formalHistoryRefresh">Refresh</button>
          <div id="formalHistory" style="margin-top:var(--s-3)"></div>
        </div>
      </div>
    `;

    bindDutInputs();
    bindSpecInputs();
    renderPropRows();

    $("#formalSuggestBtn").addEventListener("click", suggestProperties);
    $("#formalAddProp").addEventListener("click", () => {
      propRows.push({ id: ++rowSeq, description: "", expr: "", kind: "assert" });
      renderPropRows();
    });
    $("#formalRunBtn").addEventListener("click", runFormalCheck);
    $("#formalHistoryRefresh").addEventListener("click", loadHistory);
    loadHistory();
  }

  function historySummary(ev) {
    if (ev.kind === "suggest") {
      if (ev.error) return `suggest failed — ${ev.error}`;
      const kc = ev.kind_counts || {};
      const parts = Object.entries(kc).map(([k, n]) => `${n} ${k}`).join(", ");
      return `${ev.module}: proposed ${ev.num_proposed} (${parts})${ev.spec_provided ? " · with spec" : ""}`;
    }
    if (ev.kind === "convert") {
      return `${ev.module}: converted ${ev.num_properties}, ${ev.num_expressible} expressible`;
    }
    if (ev.kind === "run") {
      const vc = ev.verdict_counts || {};
      const parts = Object.entries(vc).map(([k, n]) => `${n} ${k}`).join(", ");
      const vac = ev.vacuity_warnings ? ` · ${ev.vacuity_warnings} possibly vacuous` : "";
      const ret = ev.retries ? ` · ${ev.retries} auto-retried` : "";
      return `${ev.module}: ${parts}${vac}${ret}`;
    }
    return JSON.stringify(ev);
  }

  async function loadHistory() {
    const host = $("#formalHistory");
    if (!host) return;
    host.innerHTML = '<span class="text-dim mono">Loading…</span>';
    let data;
    try {
      const res = await fetch("/api/formal/history?limit=30");
      data = await res.json();
    } catch (err) {
      host.innerHTML = `<span class="text-dim mono">Could not load history: ${App.escapeHtml(String(err.message || err))}</span>`;
      return;
    }
    const events = data.events || [];
    if (!events.length) {
      host.innerHTML = '<span class="text-dim mono">No attempts logged yet.</span>';
      return;
    }
    host.innerHTML = events
      .map((ev) => {
        const when = new Date(ev.ts * 1000).toLocaleString();
        return `<div style="display:flex; gap:var(--s-3); padding:6px 0; border-top:1px solid var(--border-soft, #21262d); font-size:12.5px;">
          <span class="text-dim mono" style="flex:none; width:150px">${App.escapeHtml(when)}</span>
          <span class="mono" style="flex:none; width:60px; text-transform:uppercase">${App.escapeHtml(ev.kind)}</span>
          <span>${App.escapeHtml(historySummary(ev))}</span>
        </div>`;
      })
      .join("");
  }

  function bindDutInputs() {
    $("#formalRtlTabs").querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        rtlMode = btn.dataset.mode;
        render();
      });
    });
    const fileInput = $("#formalRtlFile");
    if (fileInput) {
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files[0];
        if (!f) return;
        dutRtl = await f.text();
        dutFileName = f.name;
        $("#formalRtlFileName").textContent = dutFileName;
      });
    }
    const pasteEl = $("#formalRtlPaste");
    if (pasteEl) pasteEl.addEventListener("input", () => { dutRtl = pasteEl.value; });

    const topEl = $("#formalTopModule");
    if (topEl) topEl.addEventListener("input", () => { dutTopModule = topEl.value; });
  }

  function bindSpecInputs() {
    $("#formalSpecTabs").querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        specMode = btn.dataset.mode;
        render();
      });
    });
    const fileInput = $("#formalSpecFile");
    if (fileInput) {
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files[0];
        if (!f) return;
        specText = await f.text();
        specFileName = f.name;
        $("#formalSpecFileName").textContent = specFileName;
      });
    }
    const pasteEl = $("#formalSpecPaste");
    if (pasteEl) pasteEl.addEventListener("input", () => { specText = pasteEl.value; });
  }

  const KIND_META = {
    assert: { badge: "led-blue", label: "ASSERT" },
    assume: { badge: "led-amber", label: "ASSUME" },
    cover: { badge: "led-green", label: "COVER" },
  };

  async function suggestProperties() {
    const status = $("#formalSuggestStatus");
    const host = $("#formalSuggestions");
    if (!dutRtl.trim()) {
      status.textContent = "Add RTL above first.";
      return;
    }
    status.textContent = "Reading RTL" + (specText.trim() ? " + spec" : "") + ", proposing properties…";
    host.innerHTML = "";

    const fd = new FormData();
    fd.append("rtl_text", dutRtl);
    fd.append("spec_text", specText);
    fd.append("top_module", dutTopModule.trim());

    let data;
    try {
      const res = await fetch("/api/formal/suggest", { method: "POST", body: fd });
      data = await res.json();
    } catch (err) {
      status.textContent = "Request failed: " + (err.message || err);
      return;
    }
    if (data.error) {
      status.textContent = "";
      host.innerHTML = `<div class="callout callout-error">
        <span class="callout__prefix">SUGGESTION FAILED</span>
        <p class="callout-unverified__body">${App.escapeHtml(data.error)}</p>
      </div>`;
      loadHistory();
      return;
    }

    suggestions = (data.properties || []).map((p) => ({ ...p, id: ++rowSeq, included: true }));
    status.textContent = `${suggestions.length} proposed — review, edit, or remove, then add.`;
    renderSuggestions();
    loadHistory();
  }

  function renderSuggestions() {
    const host = $("#formalSuggestions");
    if (!host) return;
    if (!suggestions.length) {
      host.innerHTML = "";
      return;
    }
    host.innerHTML = `
      ${suggestions
        .map((s) => {
          const meta = KIND_META[s.kind] || KIND_META.assert;
          return `
        <div class="panel" data-sid="${s.id}" style="margin-bottom:var(--s-3)">
          <div class="panel__body">
            <div style="display:flex; gap:var(--s-3); align-items:flex-start;">
              <input type="checkbox" data-sfield="included" ${s.included ? "checked" : ""} style="margin-top:6px">
              <div style="flex:1">
                <div style="display:flex; gap:var(--s-2); align-items:center; margin-bottom:var(--s-2)">
                  <span class="result-badge"><span class="led ${meta.badge}"></span><span class="mono">${meta.label}</span></span>
                  <select data-sfield="kind" class="input-mono" style="width:auto">
                    <option value="assert" ${s.kind === "assert" ? "selected" : ""}>assert</option>
                    <option value="assume" ${s.kind === "assume" ? "selected" : ""}>assume</option>
                    <option value="cover" ${s.kind === "cover" ? "selected" : ""}>cover</option>
                  </select>
                  <span class="text-dim mono" style="font-size:11px">${App.escapeHtml(s.pattern || "")}</span>
                </div>
                <textarea data-sfield="description" class="input-mono" style="width:100%" rows="2">${App.escapeHtml(s.description)}</textarea>
                <p class="text-dim" style="font-size:12px; margin-top:var(--s-2)">
                  signals: <span class="mono">${App.escapeHtml((s.signals || []).join(", "))}</span><br>
                  ${App.escapeHtml(s.rationale || "")}
                  ${
                    s.kind === "assert"
                      ? s.paired_cover
                        ? `<br><span class="mono">pairs with cover: ${App.escapeHtml(s.paired_cover)}</span>`
                        : `<br><span class="mono" style="color:var(--led-amber,#d29922)">no paired cover — claimed unconditional</span>`
                      : ""
                  }
                </p>
              </div>
              <button type="button" class="btn-ghost" data-sremove="${s.id}" title="Remove">&times;</button>
            </div>
          </div>
        </div>`;
        })
        .join("")}
      <button type="button" class="btn btn-primary" id="formalAddSelected">Add selected to properties below</button>
    `;

    host.querySelectorAll("[data-sid]").forEach((el) => {
      const id = Number(el.dataset.sid);
      const s = suggestions.find((x) => x.id === id);
      if (!s) return;
      el.querySelectorAll("[data-sfield]").forEach((field) => {
        const evt = field.type === "checkbox" ? "change" : "input";
        field.addEventListener(evt, () => {
          const key = field.dataset.sfield;
          s[key] = field.type === "checkbox" ? field.checked : field.value;
        });
      });
    });
    host.querySelectorAll("[data-sremove]").forEach((btn) => {
      btn.addEventListener("click", () => {
        suggestions = suggestions.filter((s) => s.id !== Number(btn.dataset.sremove));
        renderSuggestions();
      });
    });
    $("#formalAddSelected").addEventListener("click", addSelectedSuggestions);
  }

  async function addSelectedSuggestions() {
    const status = $("#formalSuggestStatus");
    const addBtn = $("#formalAddSelected");
    const chosen = suggestions.filter((s) => s.included);
    if (!chosen.length) {
      status.textContent = "Nothing checked.";
      return;
    }
    // Prevent double-submit (e.g. an impatient second click) from converting
    // and adding the same selection twice — every field it touches gets
    // rebuilt from scratch below instead of appended to, for the same reason.
    if (addBtn.disabled) return;
    addBtn.disabled = true;
    status.textContent = "Converting selected properties to SVA…";
    clearNotExpressibleNotes();

    const fd = new FormData();
    fd.append("rtl_text", dutRtl);
    fd.append("top_module", dutTopModule.trim());
    fd.append(
      "properties",
      JSON.stringify(
        chosen.map((s) => ({
          name: s.name,
          kind: s.kind,
          description: s.description,
          rationale: s.rationale,
          paired_cover: s.paired_cover || "",
        }))
      )
    );

    let data;
    try {
      const res = await fetch("/api/formal/convert", { method: "POST", body: fd });
      data = await res.json();
    } catch (err) {
      status.textContent = "Request failed: " + (err.message || err);
      addBtn.disabled = false;
      return;
    }
    if (data.error) {
      status.textContent = data.error;
      addBtn.disabled = false;
      loadHistory();
      return;
    }

    let added = 0;
    const notExpressible = [];
    data.properties.forEach((c) => {
      if (c.expressible) {
        propRows.push({
          id: ++rowSeq,
          name: c.name,
          description: c.description,
          expr: c.expr,
          kind: c.kind,
          paired_cover: c.paired_cover || "",
        });
        added += 1;
      } else {
        notExpressible.push(c);
      }
    });
    // drop the default empty row once real rows exist
    propRows = propRows.filter((r) => r.description.trim() || r.expr.trim());
    if (!propRows.length) propRows = defaultProps();

    // Every property in this batch is now accounted for — either it's a row
    // below, or its explanation is in the "not yet supported" callout.
    // Remove them from the suggestion list so a second click on "add
    // selected" can't process the same ones again.
    const chosenIds = new Set(chosen.map((s) => s.id));
    suggestions = suggestions.filter((s) => !chosenIds.has(s.id));

    status.textContent = `Added ${added}${notExpressible.length ? `, ${notExpressible.length} not expressible yet (see note below)` : ""}.`;
    // renderSuggestions() rebuilds #formalSuggestions from scratch (and
    // clears it entirely once the list is empty) — it must run BEFORE the
    // notes are appended, or the notes get wiped out immediately.
    renderSuggestions();
    renderNotExpressibleNotes(notExpressible);
    renderPropRows();
    addBtn.disabled = false;
    loadHistory();
  }

  function clearNotExpressibleNotes() {
    const el = document.getElementById("formalNotExpressible");
    if (el) el.remove();
  }

  function renderNotExpressibleNotes(notExpressible) {
    clearNotExpressibleNotes();
    if (!notExpressible.length) return;
    const host = $("#formalSuggestions");
    const notes = notExpressible
      .map((c) => `<li><strong>${App.escapeHtml(c.description)}</strong> — ${App.escapeHtml(c.note)}</li>`)
      .join("");
    host.insertAdjacentHTML(
      "beforeend",
      `<div class="callout callout-warn" id="formalNotExpressible" style="margin-top:var(--s-3)">
        <span class="callout__prefix">NOT YET SUPPORTED</span>
        <ul style="margin-top:var(--s-2)">${notes}</ul>
      </div>`
    );
  }

  function renderPropRows() {
    const host = $("#formalPropRows");
    if (!host) return;
    host.innerHTML = propRows
      .map(
        (r) => `
      <div class="formal-prop-row" data-id="${r.id}" style="display:flex; gap:var(--s-2); margin-bottom:var(--s-2); align-items:center;">
        <select data-field="kind" class="input-mono" style="width:auto">
          <option value="assert" ${r.kind === "assert" ? "selected" : ""}>assert</option>
          <option value="assume" ${r.kind === "assume" ? "selected" : ""}>assume</option>
          <option value="cover" ${r.kind === "cover" ? "selected" : ""}>cover</option>
        </select>
        <input type="text" class="input-mono" data-field="description"
          placeholder="plain English, e.g. light is never invalid"
          title="${r.paired_cover ? `Pairs with cover: ${App.escapeHtml(r.paired_cover)}` : ""}"
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
      rowEl.querySelectorAll("[data-field]").forEach((inp) => {
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
    if (!dutRtl.trim()) {
      status.textContent = "Add RTL above first.";
      return;
    }
    const props = propRows
      .filter((r) => r.expr.trim())
      .map((r, i) => ({
        name: r.name || `prop${i}`,
        description: r.description.trim(),
        expr: r.expr.trim(),
        kind: r.kind || "assert",
        paired_cover: r.paired_cover || "",
      }));
    if (!props.length) {
      status.textContent = "Enter at least one property expression.";
      return;
    }

    status.textContent = "Running SymbiYosys…";
    resultsEl.innerHTML = "";

    const timeoutSec = parseInt($("#formalTimeout").value, 10) || 300;
    const depthOverride = parseInt($("#formalDepthOverride").value, 10) || 0;

    const fd = new FormData();
    fd.append("rtl_text", dutRtl);
    fd.append("top_module", dutTopModule.trim());
    fd.append("properties", JSON.stringify(props));
    fd.append("timeout_sec", String(timeoutSec));
    fd.append("depth_override", String(depthOverride));

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
      loadHistory();
      return;
    }

    status.textContent = `${data.engine} v${data.engine_version}`;
    renderResults(data);
    loadHistory();
  }

  function renderResults(data) {
    const resultsEl = $("#formalResults");
    const assumedHtml = (data.assumed_constraints || []).length
      ? `<div class="callout callout-info" style="margin-top:var(--s-4)">
          <span class="callout__prefix">ASSUMED (not independently checked)</span>
          <ul style="margin-top:var(--s-2)">
            ${data.assumed_constraints.map((a) => `<li class="mono">${App.escapeHtml(a.expr)} — ${App.escapeHtml(a.description)}</li>`).join("")}
          </ul>
        </div>`
      : "";

    resultsEl.innerHTML =
      assumedHtml +
      data.properties
        .map((p, i) => {
          const ok = p.success;
          const isError = p.error || p.verdict === "ERROR";
          const isInconclusive = ["TIMEOUT", "UNKNOWN", "CANCELLED"].includes(p.verdict);
          const badgeCls = isError ? "error" : isInconclusive ? "unverified" : ok ? "pass" : "fail";
          const ledCls = isError ? "led-red" : isInconclusive ? "led-amber" : ok ? "led-green" : "led-red";
          const verdict = p.error ? "ERROR" : p.verdict;
          return `
        <div class="panel" style="margin-top:var(--s-4)">
          <div class="panel__header">
            <span class="result-badge ${badgeCls}"><span class="led ${ledCls}"></span><span class="mono">${verdict}</span></span>
            <span class="text-dim mono" style="margin-left:var(--s-2); font-size:11px">${p.kind}</span>
            <span style="margin-left:var(--s-3)">${App.escapeHtml(p.description || "(no description)")}</span>
          </div>
          <div class="panel__body">
            <pre class="code-block" style="max-height:80px">${App.escapeHtml(p.expr)}</pre>
            ${p.error ? `<p class="callout-unverified__body">${App.escapeHtml(p.error)}</p>` : ""}
            ${
              p.retry_note
                ? `<div class="callout ${p.retried && verdict !== "ERROR" ? "callout-info" : "callout-warn"}" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">${p.retried ? "AUTO-RETRIED" : "RETRY DECLINED"}</span>
                     <p class="callout-unverified__body">${App.escapeHtml(p.retry_note)}</p>
                   </div>`
                : ""
            }
            ${
              verdict === "ERROR"
                ? `<div class="callout callout-error" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">TOOL ERROR</span>
                     <p class="callout-unverified__body">This did not compile against the real solver${p.retried ? ", even after an automatic fix attempt" : ""} — see the log for the raw error, not a proof result.</p>
                   </div>`
                : ""
            }
            ${
              verdict === "TIMEOUT"
                ? `<div class="callout callout-warn" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">TIMEOUT</span>
                     <p class="callout-unverified__body">The solver did not reach a verdict within the configured timeout — this is not a proof of anything either way. Try raising the timeout, or a different depth/engine, for designs this size.</p>
                   </div>`
                : verdict === "UNKNOWN"
                ? `<div class="callout callout-warn" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">UNKNOWN</span>
                     <p class="callout-unverified__body">The engine ran to completion but could not find a proof or a counterexample — a real, expected outcome for hard designs, not a tool error. Try a different depth or engine rather than trusting this as PROVEN or FALSIFIED.</p>
                   </div>`
                : verdict === "CANCELLED"
                ? `<div class="callout callout-warn" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">CANCELLED</span>
                     <p class="callout-unverified__body">This run was cancelled before producing a verdict.</p>
                   </div>`
                : ""
            }
            ${
              p.vacuity_warning
                ? `<div class="callout callout-warn" style="margin-top:var(--s-2)">
                     <span class="callout__prefix">POSSIBLY VACUOUS</span>
                     <p class="callout-unverified__body">${App.escapeHtml(p.vacuity_warning)}</p>
                   </div>`
                : ""
            }
            ${
              p.has_trace
                ? `<p class="text-dim">${p.kind === "cover" ? "Reached — trace below." : "Falsified — counterexample trace below."}</p>
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
