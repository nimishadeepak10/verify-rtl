/**
 * VerifyRTL — app shell, routing, design screen, API orchestration.
 */
(function () {
  "use strict";

  const STEPS = [
    { id: "design", num: "01", label: "Design" },
    { id: "plan", num: "02", label: "Plan" },
    { id: "run", num: "03", label: "Run" },
    { id: "results", num: "04", label: "Results" },
    { id: "coverage", num: "05", label: "Coverage" },
  ];

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  window.App = {
    rtlContent: "",
    rtlFileName: "",
    rtlLines: 0,
    currentPreview: null,
    currentVplan: null,
    currentResult: null,
    backends: [],
    currentStep: "design",
    stepState: {
      design: "current",
      plan: "dim",
      run: "dim",
      results: "dim",
      coverage: "dim",
    },
    vplanToggles: { categories: {}, subcategories: {} },
    previewDebounce: null,
  };

  // Exposed navigation helpers (used by Coverage screen interactions).
  App.gotoStep = gotoStep;

  App.jumpToLine = function jumpToLine(lineNo) {
    const ln = Math.max(1, Number(lineNo) || 1);
    gotoStep("design");
    // ensure paste mode visible
    const pasteBtn = document.querySelector('.segmented__btn[data-mode="paste"]');
    if (pasteBtn) pasteBtn.click();
    if (!rtlPaste) return;
    const lines = (rtlPaste.value || "").split("\n");
    let pos = 0;
    for (let i = 0; i < Math.min(lines.length, ln - 1); i++) pos += lines[i].length + 1;
    rtlPaste.focus();
    rtlPaste.setSelectionRange(pos, pos + (lines[ln - 1] ? lines[ln - 1].length : 0));
    // scroll roughly
    const lh = 18;
    rtlPaste.scrollTop = Math.max(0, (ln - 3) * lh);
  };

  App.highlightWaveSignal = function highlightWaveSignal(signalName) {
    // Best-effort: jump to Results → Waveform and rely on viewer's search/highlight if present.
    if (!App.currentResult) return;
    gotoStep("results");
    try {
      document.querySelector('.result-tabs button[data-panel="panelWave"]')?.click();
      // viewer may implement an optional highlight API
      if (window.WaveformViewer && WaveformViewer.highlightSignal) {
        WaveformViewer.highlightSignal(signalName);
      }
    } catch (e) {
      /* ignore */
    }
  };

  const uploadZone = $("#uploadZone");
  const fileInput = $("#rtlFile");
  const rtlPaste = $("#rtlPaste");
  const pasteGutter = $("#pasteGutter");
  const topModule = $("#topModule");
  const backendSelect = $("#backendSelect");

  function getLanguage() {
    const sel = document.querySelector('input[name="language"]:checked');
    return sel ? sel.value : "systemverilog";
  }

  function setLanguage(value) {
    $$(".lang-opt").forEach((opt) => {
      const input = opt.querySelector('input[type="radio"]');
      const on = input && input.value === value;
      if (input) input.checked = on;
      opt.classList.toggle("selected", on);
    });
  }

  function getBackend() {
    return backendSelect ? backendSelect.value : "";
  }

  async function buildFormData() {
    const fd = new FormData();
    fd.append("language", getLanguage());
    fd.append("top_module", topModule.value.trim());
    fd.append("backend", getBackend());
    if (App.rtlFileName && App.rtlFileName !== "(pasted)" && fileInput.files[0]) {
      fd.append("rtl_file", fileInput.files[0]);
    } else {
      fd.append("rtl_text", App.rtlContent);
    }
    return fd;
  }

  function updateSourceInfo() {
    const el = $("#sourceInfo");
    if (!el) return;
    if (!App.rtlContent.trim()) {
      el.textContent = "";
      return;
    }
    const name = App.rtlFileName || "(pasted)";
    const lines = App.rtlLines || App.rtlContent.split("\n").length;
    el.textContent = `${name} · ${lines} lines`;
  }

  function updatePasteGutter() {
    if (!rtlPaste || !pasteGutter) return;
    const lines = Math.max(1, (rtlPaste.value || "").split("\n").length);
    let g = "";
    for (let i = 1; i <= lines; i++) g += i + "\n";
    pasteGutter.textContent = g;
  }

  function updateTopCrumb() {
    const el = $("#topCrumb");
    if (!el) return;
    const p = App.currentPreview;
    if (!p || !App.rtlContent.trim()) {
      el.innerHTML = '<span class="crumb-dim">no design loaded</span>';
      return;
    }
    const mod = p.module_name || "—";
    const type = p.is_sequential ? "Sequential" : "Combinational";
    let status = "Ready";
    let led = "led-green";
    if (!p.ready_to_run) {
      status = "Not ready";
      led = "led-amber";
    }
    if (App.currentStep === "run") {
      status = "Running";
      led = "led-amber";
    }
    el.innerHTML =
      `<span class="crumb-accent">${escapeHtml(mod)}</span>` +
      '<span class="crumb-sep">·</span>' +
      `<span>${type}</span>` +
      '<span class="crumb-sep">·</span>' +
      `<span class="led ${led}" style="display:inline-block;vertical-align:middle;margin-right:4px"></span>` +
      `<span>${status}</span>`;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function showToast(message, type) {
    const host = $("#toastHost");
    if (!host) return;
    const t = document.createElement("div");
    t.className = "toast toast--" + (type || "info");
    t.textContent = message;
    host.appendChild(t);
    setTimeout(() => {
      t.style.opacity = "0";
      t.style.transition = "opacity 0.2s";
      setTimeout(() => t.remove(), 220);
    }, 4200);
  }

  function setActionLoading(loading) {
    const analyze = $("#analyzeBtn");
    const run = $("#runFromPlan");
    if (analyze) {
      analyze.classList.toggle("is-loading", loading === "analyze");
      analyze.disabled = loading === "analyze";
    }
    if (run) {
      run.classList.toggle("is-loading", loading === "run");
      if (loading === "run") run.disabled = true;
    }
  }

  function updateRunButton() {
    const run = $("#runFromPlan");
    if (!run) return;
    const ready = App.currentPreview && App.currentPreview.ready_to_run;
    run.disabled = !ready || run.classList.contains("is-loading");
    run.title = ready
      ? "Generate TB, simulate, and view results"
      : "Analyze your design first to enable verification";
  }

  function setStepState(stepId, state) {
    App.stepState[stepId] = state;
    renderNav();
  }

  function renderNav() {
    const mkLink = (step) => {
      const st = App.stepState[step.id] || "dim";
      let ledClass = "led-dim";
      if (st === "done") ledClass = "led-green";
      if (st === "current") ledClass = "led-amber";
      const active = App.currentStep === step.id ? " active" : "";
      return (
        `<li><button type="button" class="step-nav__link${active}" data-step="${step.id}" ` +
        `aria-current="${App.currentStep === step.id ? "step" : "false"}">` +
        `<span class="step-nav__num">${step.num}</span>` +
        `<span class="step-nav__label">${step.label}</span>` +
        `<span class="led ${ledClass}"></span></button></li>`
      );
    };
    const html = STEPS.map(mkLink).join("");
    const nav = $("#stepNav");
    const mobile = $("#navTabsMobile");
    if (nav) nav.innerHTML = html;
    if (mobile) mobile.innerHTML = html;
    $$(".step-nav__link").forEach((btn) => {
      btn.addEventListener("click", () => gotoStep(btn.dataset.step));
    });
  }

  function gotoStep(stepId) {
    if (!STEPS.some((s) => s.id === stepId)) return;
    App.currentStep = stepId;
    $$(".screen").forEach((sc) => {
      sc.classList.toggle("screen-active", sc.dataset.step === stepId);
    });
    Object.keys(App.stepState).forEach((k) => {
      if (k !== stepId && App.stepState[k] === "current") {
        App.stepState[k] = "done";
      }
    });
    App.stepState[stepId] = "current";
    if (App.rtlContent.trim() && stepId !== "design") App.stepState.design = "done";
    if (App.currentVplan && ["run", "results", "coverage"].includes(stepId)) App.stepState.plan = "done";
    if (App.currentResult) App.stepState.results = "done";
    renderNav();
    updateTopCrumb();

    if (stepId === "plan" && App.rtlContent.trim()) {
      if (window.VPlan && VPlan.load) VPlan.load();
    }
    if (stepId === "coverage" && window.VPlan && VPlan.renderCoverage) {
      VPlan.renderCoverage();
    }
    if (stepId === "results" && App.currentResult && window.Results && Results.render) {
      Results.render(App.currentResult);
    }
  }

  function updateNavContext() {
    const nameEl = $("#ctxBackendName");
    const verEl = $("#ctxBackendVer");
    const ledEl = $("#ctxBackendLed");
    const countEl = $("#ctxTestCount");
    const sel = getBackend();
    let b = App.backends.find((x) => x.name === sel);
    if (!b && sel === "") {
      b = App.backends.find((x) => x.available);
    }
    if (b) {
      nameEl.textContent = b.display_name || b.name;
      verEl.textContent = b.version ? `v${b.version}` : b.available ? "" : "not installed";
      ledEl.className = "led " + (b.available ? "led-green" : "led-red");
    } else {
      nameEl.textContent = "—";
      verEl.textContent = "";
      ledEl.className = "led led-dim";
    }
    const n = App.currentVplan ? App.currentVplan.total_planned_cases : App.currentPreview ? App.currentPreview.test_count : "—";
    countEl.textContent = n === "—" ? "—" : String(n);
  }

  async function loadBackends() {
    if (!backendSelect) return;
    try {
      const res = await fetch("/api/backends");
      App.backends = await res.json();
      backendSelect.innerHTML = '<option value="">Auto-detect best available</option>';
      App.backends.forEach((b) => {
        const opt = document.createElement("option");
        opt.value = b.name;
        const ver = b.version ? ` (${b.version})` : "";
        const suffix = b.available ? "" : " — not installed";
        opt.textContent = `${b.available ? "●" : "○"} ${b.display_name}${ver}${suffix}`;
        opt.className = b.available ? "backend-ok" : "backend-missing";
        opt.disabled = !b.available;
        backendSelect.appendChild(opt);
      });
      updateNavContext();
    } catch (err) {
      console.warn("Could not load backends:", err);
    }
  }

  function refreshPreview() {
    clearTimeout(App.previewDebounce);
    App.previewDebounce = setTimeout(runAnalyze, 400);
  }

  async function runAnalyze() {
    if (!App.rtlContent.trim()) {
      App.currentPreview = null;
      updateTopCrumb();
      updateNavContext();
      updateRunButton();
      showToast("Load RTL first — drop a file, paste code, or use Load example.", "error");
      return;
    }

    setActionLoading("analyze");
    try {
      const fd = await buildFormData();
      const res = await fetch("/api/analyze", { method: "POST", body: fd });
      const data = await res.json();
      if (data.error) {
        App.currentPreview = null;
        updateTopCrumb();
        updateRunButton();
        showToast(data.error, "error");
        return;
      }
      App.currentPreview = data;
      App.rtlLines = data.rtl_lines || App.rtlContent.split("\n").length;
      updateSourceInfo();
      updateTopCrumb();
      updateNavContext();
      setStepState("design", "done");
      setStepState("plan", "current");
      updateRunButton();

      App.currentVplan = null;
      if (window.VPlan && VPlan.invalidate) VPlan.invalidate();
      showToast(
        `Analyzed ${data.module_name || "design"} — ${data.is_sequential ? "sequential" : "combinational"}`,
        "success"
      );
    } catch (err) {
      showToast("Analysis failed: " + String(err), "error");
    } finally {
      setActionLoading(null);
    }
  }

  async function runVerify() {
    if (!App.rtlContent.trim()) {
      showToast("Load RTL before running verification.", "error");
      return;
    }
    if (!App.currentPreview || !App.currentPreview.ready_to_run) {
      showToast("Run Analyze first — your design is not ready yet.", "error");
      gotoStep("plan");
      return;
    }

    setActionLoading("run");
    gotoStep("run");
    setRunStep("tb", "active");
    $("#runSpinner").classList.remove("hidden");
    $("#runError").classList.add("hidden");
    $("#runLog").textContent = "";
    const backendLabel = $("#runBackendLabel");
    const b = App.backends.find((x) => x.name === getBackend()) || App.backends.find((x) => x.available);
    if (backendLabel) backendLabel.textContent = b ? b.display_name : "simulator";

    try {
      const fd = await buildFormData();
      setRunStep("compile", "active");
      const res = await fetch("/api/verify", { method: "POST", body: fd });
      const data = await res.json();

      setRunStep("tb", "done");
      setRunStep("compile", "done");
      setRunStep("sim", "done");
      setRunStep("wave", "done");
      setRunStep("cov", "done");
      $("#runSpinner").classList.add("hidden");

      if (data.error) {
        showRunError(data.error);
        showToast(data.error, "error");
        return;
      }

      App.currentResult = data;
      $("#runLog").textContent = (data.uvm_note ? data.uvm_note + "\n\n" : "") + (data.sim_log || "");

      if (data.preview) {
        App.currentPreview = data.preview;
        updateTopCrumb();
        updateRunButton();
      }

      const status = data.status || (data.success ? "pass" : "fail");
      if (status === "pass" || data.uvm_note) {
        setStepState("run", "done");
        setStepState("results", "current");
        if (window.Results && Results.render) Results.render(data);
        gotoStep("results");
        showToast(data.uvm_note ? "UVM skeleton generated" : "Verification passed", "success");
      } else {
        const msg =
          status === "sim_missing"
            ? "Simulator not available. Install Icarus Verilog or choose Auto-detect."
            : "Verification failed — see log and Results.";
        showRunError(msg);
        showToast(msg, "error");
        if (window.Results && Results.render) Results.render(data);
        gotoStep("results");
        setStepState("results", "current");
      }
    } catch (err) {
      $("#runSpinner").classList.add("hidden");
      showRunError(String(err));
      showToast(String(err), "error");
    } finally {
      setActionLoading(null);
      updateRunButton();
    }
  }

  function setRunStep(id, state) {
    const li = $(`#runSteps li[data-step="${id}"]`);
    if (!li) return;
    li.classList.remove("active", "done");
    if (state === "active") li.classList.add("active");
    if (state === "done") li.classList.add("done");
    const led = li.querySelector(".led");
    if (led) {
      led.className = "led " + (state === "done" ? "led-green" : state === "active" ? "led-amber" : "led-dim");
    }
  }

  function showRunError(msg) {
    const el = $("#runError");
    el.classList.remove("hidden");
    el.className = "callout callout-warn";
    el.innerHTML = `<span class="callout__prefix">[WARN]</span>${escapeHtml(msg)}`;
  }

  function loadFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
      App.rtlContent = reader.result;
      App.rtlFileName = file.name;
      rtlPaste.value = App.rtlContent;
      App.rtlLines = App.rtlContent.split("\n").length;
      updatePasteGutter();
      updateSourceInfo();
      refreshPreview();
    };
    reader.readAsText(file);
  }

  function initDesign() {
    uploadZone.addEventListener("click", () => fileInput.click());
    uploadZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      uploadZone.classList.add("dragover");
    });
    uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
    uploadZone.addEventListener("drop", (e) => {
      e.preventDefault();
      uploadZone.classList.remove("dragover");
      const file = e.dataTransfer.files[0];
      if (file) loadFile(file);
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) loadFile(fileInput.files[0]);
    });

    rtlPaste.addEventListener("input", () => {
      App.rtlContent = rtlPaste.value;
      App.rtlFileName = "(pasted)";
      App.rtlLines = App.rtlContent.split("\n").length;
      updatePasteGutter();
      updateSourceInfo();
      if (App.rtlContent.trim()) refreshPreview();
    });

    topModule.addEventListener("change", () => {
      if (App.rtlContent.trim()) refreshPreview();
    });

    $$(".segmented__btn[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$(".segmented__btn[data-mode]").forEach((b) => {
          b.classList.remove("active");
          b.setAttribute("aria-selected", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-selected", "true");
        const mode = btn.dataset.mode;
        $("#uploadBlock").classList.toggle("hidden", mode !== "file");
        $("#pasteBlock").classList.toggle("hidden", mode !== "paste");
      });
    });

    $$(".lang-opt").forEach((opt) => {
      opt.addEventListener("click", () => {
        const input = opt.querySelector('input[type="radio"]');
        if (input) setLanguage(input.value);
        if (App.rtlContent.trim()) refreshPreview();
      });
    });

    if (backendSelect) {
      backendSelect.addEventListener("change", () => {
        updateNavContext();
        if (App.rtlContent.trim()) refreshPreview();
      });
    }

    $("#loadExample").addEventListener("click", async () => {
      const res = await fetch("/static/example_adder.v");
      App.rtlContent = await res.text();
      App.rtlFileName = "adder_2bit.v";
      App.rtlLines = App.rtlContent.split("\n").length;
      rtlPaste.value = App.rtlContent;
      $$('.segmented__btn[data-mode="paste"]').click();
      updatePasteGutter();
      updateSourceInfo();
      refreshPreview();
      showToast("Loaded adder_2bit.v example", "info");
    });

    $("#analyzeBtn").addEventListener("click", async () => {
      await runAnalyze();
      if (App.currentPreview && !App.currentPreview.error) {
        gotoStep("plan");
        if (window.VPlan && VPlan.load) await VPlan.load();
      }
    });

    $("#runFromPlan").addEventListener("click", () => runVerify());

    $$("[data-goto]").forEach((btn) => {
      btn.addEventListener("click", () => gotoStep(btn.dataset.goto));
    });
  }

  window.App.gotoStep = gotoStep;
  window.App.runAnalyze = runAnalyze;
  window.App.runVerify = runVerify;
  window.App.buildFormData = buildFormData;
  window.App.getLanguage = getLanguage;
  window.App.getBackend = getBackend;
  window.App.escapeHtml = escapeHtml;
  window.App.updateNavContext = updateNavContext;
  window.App.updateTopCrumb = updateTopCrumb;
  window.App.showToast = showToast;
  window.App.updateRunButton = updateRunButton;

  initDesign();
  renderNav();
  loadBackends();
  setLanguage("systemverilog");
  updatePasteGutter();
  gotoStep("design");
})();
