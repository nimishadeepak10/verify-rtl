const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let rtlContent = "";
let rtlFileName = "";
let currentPreview = null;

const uploadZone = $("#uploadZone");
const fileInput = $("#rtlFile");
const rtlPaste = $("#rtlPaste");
const topModule = $("#topModule");
const analyzeBtn = $("#analyzeBtn");
const runBtn = $("#runBtn");
const sideContent = $("#sideContent");
const sideEmpty = $("#sideEmpty");
const resultsSection = $("#resultsSection");

function getLanguage() {
  const sel = document.querySelector('input[name="language"]:checked');
  return sel ? sel.value : "systemverilog";
}

function setLanguage(value) {
  $$(".lang-card").forEach((card) => {
    const input = card.querySelector('input[type="radio"]');
    const on = input.value === value;
    input.checked = on;
    card.classList.toggle("selected", on);
  });
}

$$(".lang-card").forEach((card) => {
  card.addEventListener("click", () => {
    const input = card.querySelector('input[type="radio"]');
    setLanguage(input.value);
    if (rtlContent) refreshPreview();
  });
});

// Upload zone
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

function loadFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    rtlContent = reader.result;
    rtlFileName = file.name;
    $("#fileName").textContent = file.name;
    rtlPaste.value = rtlContent;
    refreshPreview();
  };
  reader.readAsText(file);
}

rtlPaste.addEventListener("input", () => {
  rtlContent = rtlPaste.value;
  rtlFileName = "(pasted)";
  if (rtlContent.trim()) refreshPreview();
});

topModule.addEventListener("change", () => {
  if (rtlContent.trim()) refreshPreview();
});

// Input mode tabs
$$(".tabs-inline button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tabs-inline button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const mode = btn.dataset.mode;
    $("#uploadBlock").classList.toggle("hidden", mode !== "file");
    $("#pasteBlock").classList.toggle("hidden", mode !== "paste");
  });
});

let previewDebounce = null;
function refreshPreview() {
  clearTimeout(previewDebounce);
  previewDebounce = setTimeout(runAnalyze, 400);
}

function getBackend() {
  const sel = $("#backendSelect");
  return sel ? sel.value : "";
}

async function buildFormData() {
  const fd = new FormData();
  fd.append("language", getLanguage());
  fd.append("top_module", topModule.value.trim());
  fd.append("backend", getBackend());
  if (rtlFileName && rtlFileName !== "(pasted)" && fileInput.files[0]) {
    fd.append("rtl_file", fileInput.files[0]);
  } else {
    fd.append("rtl_text", rtlContent);
  }
  return fd;
}

async function loadBackends() {
  const select = $("#backendSelect");
  if (!select) return;
  try {
    const res = await fetch("/api/backends");
    const backends = await res.json();
    select.innerHTML = '<option value="">Auto-detect best available</option>';
    backends.forEach((b) => {
      const opt = document.createElement("option");
      opt.value = b.name;
      const dot = b.available ? "●" : "○";
      const ver = b.version ? ` (${b.version})` : "";
      const suffix = b.available ? "" : " — not installed";
      opt.textContent = `${dot} ${b.display_name}${ver}${suffix}`;
      opt.className = b.available ? "backend-ok" : "backend-missing";
      opt.disabled = !b.available;
      select.appendChild(opt);
    });
  } catch (err) {
    console.warn("Could not load backends:", err);
  }
}

async function runAnalyze() {
  if (!rtlContent.trim()) {
    sideEmpty.classList.remove("hidden");
    sideContent.classList.add("hidden");
    runBtn.disabled = true;
    return;
  }

  sideEmpty.innerHTML = '<span class="spinner"></span> Analyzing RTL...';
  sideEmpty.classList.remove("hidden");
  sideContent.classList.add("hidden");

  try {
    const fd = await buildFormData();
    const res = await fetch("/api/analyze", { method: "POST", body: fd });
    const data = await res.json();
    if (data.error) {
      sideEmpty.textContent = data.error;
      runBtn.disabled = true;
      return;
    }
    currentPreview = data;
    renderSidePanel(data);
    runBtn.disabled = !data.ready_to_run;
  } catch (err) {
    sideEmpty.textContent = "Analysis failed: " + err.message;
    runBtn.disabled = true;
  }
}

function renderSidePanel(p) {
  sideEmpty.classList.add("hidden");
  sideContent.classList.remove("hidden");

  $("#sideModule").textContent = p.module_name;
  $("#sideFile").textContent = p.file_name || "—";
  $("#sideLines").textContent = p.rtl_lines ?? "—";
  const seqParts = [p.is_sequential ? "Sequential" : "Combinational"];
  if (p.clock_port) seqParts.push(`clk=${p.clock_port}`);
  if (p.reset_port) {
    seqParts.push(`rst=${p.reset_port} (${p.reset_active_low ? "active-low" : "active-high"})`);
  }
  $("#sideSeq").textContent = seqParts.join(" · ");
  const fsmBlock = document.getElementById("fsmBlock");
  if (p.state_reg && p.states && p.states.length) {
    fsmBlock.classList.remove("hidden");
    $("#sideFsm").textContent = `${p.state_reg}: ${p.states.join(", ")}`;
  } else {
    fsmBlock.classList.add("hidden");
  }
  $("#sideOp").textContent = p.inferred_operation_label;
  $("#sideLang").textContent = p.language_label;
  $("#sideSim").textContent = p.simulator;
  $("#sideTests").textContent = `${p.test_count} (${p.test_strategy})`;
  $("#sideCheck").textContent = p.self_checking ? "Self-checking" : "Monitor-only";

  const readyBadge = $("#sideReady");
  readyBadge.textContent = p.ready_to_run ? "Ready" : "Not ready";
  readyBadge.className = "badge " + (p.ready_to_run ? "badge-ok" : "badge-warn");

  const tbody = $("#portTableBody");
  tbody.innerHTML = "";
  (p.ports || []).forEach((port) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${port.name}</td>
      <td>${port.direction}</td>
      <td>${port.range}</td>
      <td>${port.width}</td>`;
    tbody.appendChild(tr);
  });

  const cl = $("#checklist");
  cl.innerHTML = "";
  (p.checklist || []).forEach((item) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="check-icon">${item.ok ? "✓" : "○"}</span> ${item.label}`;
    cl.appendChild(li);
  });

  const warnBox = $("#warningsBox");
  if (p.warnings && p.warnings.length) {
    warnBox.classList.remove("hidden");
    $("#warningsList").innerHTML = p.warnings.map((w) => `<li>${w}</li>`).join("");
  } else {
    warnBox.classList.add("hidden");
  }

  const mods = $("#modulesList");
  mods.textContent = (p.modules_in_file || []).join(", ") || p.module_name;
}

analyzeBtn.addEventListener("click", () => runAnalyze());

runBtn.addEventListener("click", async () => {
  if (!rtlContent.trim()) return;

  runBtn.disabled = true;
  analyzeBtn.disabled = true;
  resultsSection.classList.remove("hidden");
  $("#resultStatus").textContent = "Running...";
  $("#resultStatus").className = "badge badge-neutral";

  try {
    const fd = await buildFormData();
    const res = await fetch("/api/verify", { method: "POST", body: fd });
    const data = await res.json();

    const status = data.status || (data.success ? "pass" : "fail");
    const labels = {
      pass: "PASS",
      fail: "FAIL",
      sim_missing: "Simulator missing",
      tb_only: "TB only",
    };
    const classes = {
      pass: "badge-ok",
      fail: "badge-fail",
      sim_missing: "badge-warn",
      tb_only: "badge-warn",
    };
    $("#resultStatus").textContent = data.uvm_note ? "TB generated (UVM)" : (labels[status] || (data.success ? "PASS" : "FAIL"));
    $("#resultStatus").className = "badge " + (data.uvm_note ? "badge-warn" : (classes[status] || (data.success ? "badge-ok" : "badge-fail")));
    const note = document.getElementById("simulatorNote");
    if (note) {
      const parts = [];
      if (data.backend_used) parts.push(`Backend: ${data.backend_used}`);
      if (data.backend_version) parts.push(`v${data.backend_version}`);
      if (data.simulator) parts.push(data.simulator);
      note.textContent = parts.length ? parts.join(" · ") : "";
    }

    $("#reportPre").textContent = data.text_report || "";
    $("#tbPre").textContent = data.testbench || "";
    $("#logPre").textContent = (data.uvm_note ? data.uvm_note + "\n\n" : "") + (data.sim_log || "");
    $("#wavePre").textContent = data.waveform_text || "";

    const dl = $("#downloadRow");
    if (data.has_vcd && data.work_dir) {
      dl.classList.remove("hidden");
      $("#vcdLink").href = "/api/download/vcd?work_dir=" + encodeURIComponent(data.work_dir);
      $("#reportLink").href = "/api/download/report?work_dir=" + encodeURIComponent(data.work_dir);
    } else {
      dl.classList.add("hidden");
    }

    if (data.preview) renderSidePanel(data.preview);
  } catch (err) {
    $("#resultStatus").textContent = "Error";
    $("#resultStatus").className = "badge badge-fail";
    $("#logPre").textContent = String(err);
  } finally {
    runBtn.disabled = !(currentPreview && currentPreview.ready_to_run);
    analyzeBtn.disabled = false;
  }
});

$$(".result-tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".result-tabs button").forEach((b) => b.classList.remove("active"));
    $$(".result-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.panel).classList.add("active");
  });
});

// Load example
$("#loadExample").addEventListener("click", async () => {
  const res = await fetch("/static/example_adder.v");
  rtlContent = await res.text();
  rtlFileName = "adder_2bit.v";
  rtlPaste.value = rtlContent;
  $("#fileName").textContent = "example_adder.v (sample)";
  $$('.tabs-inline button[data-mode="paste"]').click();
  refreshPreview();
});

const backendSelect = $("#backendSelect");
if (backendSelect) {
  backendSelect.addEventListener("change", () => {
    if (rtlContent.trim()) refreshPreview();
  });
}

loadBackends();
setLanguage("systemverilog");
runBtn.disabled = true;
