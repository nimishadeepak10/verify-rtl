/**
 * VerifyRTL — results screen: summary, case table, tabs, downloads.
 */
(function () {
  "use strict";

  let activeFilter = "all";
  let expandedRow = null;
  let waveJsonCache = null;
  let waveWorkDirCache = null;

  function $(sel) {
    return document.querySelector(sel);
  }

  function parseSimLog(simLog) {
    const rows = [];
    if (!simLog) return rows;
    const lines = simLog.split("\n");
    let idx = 0;

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      // PASS t=5000 a=0 b=0 sum=0
      let m = trimmed.match(/^PASS\s+t=\d+\s+(.+)$/i);
      if (m) {
        const fields = parseFields(m[1]);
        rows.push(makeRow(idx++, "Simulation", "Vector check", fields, fields, "PASS", trimmed));
        continue;
      }

      // PASS a=0 b=0 sum=0 (legacy)
      m = trimmed.match(/^PASS\s+(.+)$/i);
      if (m && !trimmed.includes("PASS=")) {
        const fields = parseFields(m[1]);
        rows.push(makeRow(idx++, "Simulation", "Vector check", fields, fields, "PASS", trimmed));
        continue;
      }

      // FAIL t=5000 exp_sum=0 got_sum=3
      m = trimmed.match(/^FAIL\s+t=\d+\s+(.+)$/i);
      if (m) {
        const parts = parseFailFields(m[1]);
        rows.push(
          makeRow(idx++, "Simulation", "Mismatch", parts.inputs, parts.expected, "FAIL", trimmed, parts.got)
        );
        continue;
      }

      // FAIL test N ...
      m = trimmed.match(/^FAIL\s+test\s+(\d+)\s+exp=(\S+)\s+got=(\S+)/i);
      if (m) {
        rows.push(
          makeRow(
            idx++,
            "Simulation",
            `Test ${m[1]} mismatch`,
            {},
            { out: m[2] },
            "FAIL",
            trimmed,
            { out: m[3] }
          )
        );
      }
    }
    return rows;
  }

  function parseFields(s) {
    const out = {};
    const re = /(\w+)=(\S+)/g;
    let m;
    while ((m = re.exec(s))) out[m[1]] = m[2];
    return out;
  }

  function parseFailFields(s) {
    const inputs = {};
    const expected = {};
    const got = {};
    const re = /(exp|got)_(\w+)=(\S+)/g;
    let m;
    while ((m = re.exec(s))) {
      if (m[1] === "exp") expected[m[2]] = m[3];
      else got[m[2]] = m[3];
    }
    return { inputs, expected, got };
  }

  function makeRow(num, category, desc, inputs, expected, result, detail, gotOverride) {
    const got = gotOverride || (result === "PASS" ? expected : {});
    return {
      num: num + 1,
      category,
      description: desc,
      inputs,
      expected,
      got,
      result,
      detail,
      tags: inferTags(category, desc),
    };
  }

  function inferTags(category, desc) {
    const d = (desc || "").toLowerCase();
    const tags = [];
    if (d.includes("directed") || category.toLowerCase().includes("directed")) tags.push("directed");
    if (d.includes("corner")) tags.push("corner");
    if (d.includes("random")) tags.push("random");
    if (d.includes("negative")) tags.push("negative");
    if (!tags.length) tags.push("simulation");
    return tags;
  }

  function rowsFromVplan(plan, overallPass) {
    const rows = [];
    let idx = 0;
    (plan.categories || []).forEach((cat) => {
      if (cat.status !== "enabled") return;
      const addCases = (cases, catName) => {
        (cases || []).forEach((tc) => {
          rows.push({
            num: ++idx,
            category: catName,
            description: tc.description,
            inputs: tc.inputs || {},
            expected: tc.expected_outputs || {},
            got: overallPass ? tc.expected_outputs || {} : {},
            result: overallPass ? "PASS" : "—",
            detail: tc.rationale || "",
            tags: tc.tags || [cat.id],
          });
        });
      };
      addCases(cat.cases, cat.name);
      (cat.subcategories || []).forEach((sc) => {
        if (sc.status === "enabled") addCases(sc.cases, `${cat.name} / ${sc.name}`);
      });
    });
    return rows;
  }

  function formatFieldMap(map) {
    if (!map || !Object.keys(map).length) return "—";
    return Object.entries(map)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
  }

  function filterRows(rows) {
    if (activeFilter === "all") return rows;
    if (activeFilter === "passed") return rows.filter((r) => r.result === "PASS");
    if (activeFilter === "failed") return rows.filter((r) => r.result === "FAIL");
    return rows.filter((r) => (r.tags || []).some((t) => t.toLowerCase() === activeFilter));
  }

  function render(data) {
    const mount = $("#resultsMount");
    if (!mount) return;
    if (waveWorkDirCache !== data.work_dir) {
      waveJsonCache = null;
      waveWorkDirCache = null;
      const wv = $("#waveVisual");
      if (wv && window.WaveformViewer) WaveformViewer.destroy(wv);
    }

    const status = data.status || (data.success ? "pass" : "fail");
    const pass = status === "pass" || data.uvm_note;
    const simLog = (data.uvm_note ? data.uvm_note + "\n\n" : "") + (data.sim_log || "");

    let rows = parseSimLog(simLog);
    if (rows.length < 1 && App.currentVplan) {
      rows = rowsFromVplan(App.currentVplan, pass);
    }

    const passed = rows.filter((r) => r.result === "PASS").length;
    const failed = rows.filter((r) => r.result === "FAIL").length;
    const total = rows.length || (pass ? 16 : 0);

    const passCnt = simLog.match(/PASS=(\d+)/);
    const failCnt = simLog.match(/FAIL=(\d+)/);
    const pN = passCnt ? passCnt[1] : passed;
    const fN = failCnt ? failCnt[1] : failed;
    const tN = passCnt && failCnt ? String(+pN + +fN) : String(total);

    const backendParts = [];
    if (data.backend_used) backendParts.push(data.backend_used);
    if (data.backend_version) backendParts.push(`v${data.backend_version}`);
    if (data.simulator) backendParts.push(data.simulator);

    mount.innerHTML = `
      <div class="result-summary">
        <div class="result-badge ${pass ? "pass" : "fail"}">${pass ? "PASS" : status === "sim_missing" ? "NO SIM" : "FAIL"}</div>
        <div class="result-stats">${pN} passed · ${fN} failed · ${tN} total</div>
        <div class="result-meta">${App.escapeHtml(backendParts.join(" · ") || "—")}</div>
      </div>

      <div class="filter-chips" role="toolbar" aria-label="Filter results">
        ${["all", "passed", "failed", "directed", "corner", "negative", "random"]
          .map(
            (f) =>
              `<button type="button" class="chip${activeFilter === f ? " active" : ""}" data-filter="${f}">${f.charAt(0).toUpperCase() + f.slice(1)}</button>`
          )
          .join("")}
      </div>

      <div class="panel">
        <div class="panel__header"><h2 class="panel__title">TEST CASE RESULTS</h2></div>
        <div class="panel__body" style="padding:0;overflow-x:auto">
          <table class="data-table" id="resultsTable">
            <thead>
              <tr>
                <th>#</th><th>CATEGORY</th><th>DESCRIPTION</th><th>INPUTS</th>
                <th>EXPECTED</th><th>GOT</th><th>RESULT</th>
              </tr>
            </thead>
            <tbody id="resultsTableBody"></tbody>
          </table>
        </div>
      </div>

      <div class="result-tabs" role="tablist">
        <button type="button" class="active" data-panel="panelReport">Full report</button>
        <button type="button" data-panel="panelTb">Testbench</button>
        <button type="button" data-panel="panelLog">Sim log</button>
        <button type="button" data-panel="panelWave">Waveform</button>
      </div>
      <div id="panelReport" class="result-panel active"><pre class="code-block" id="reportPre"></pre></div>
      <div id="panelTb" class="result-panel"><pre class="code-block" id="tbPre"></pre></div>
      <div id="panelLog" class="result-panel"><pre class="code-block" id="logPre"></pre></div>
      <div id="panelWave" class="result-panel">
        <div class="wave-subtabs" role="tablist">
          <button type="button" class="active" data-wave-sub="visual">Visual</button>
          <button type="button" data-wave-sub="raw">Raw VCD Dump</button>
        </div>
        <div id="waveVisual" class="wave-visual-panel active"></div>
        <div id="waveRaw" class="wave-raw-panel"><pre class="code-block code-block--wave" id="wavePre"></pre></div>
      </div>

      <div class="download-row" id="downloadRow"></div>
    `;

    App._resultRows = rows;
    renderTableBody(rows);

    $("#reportPre").textContent = data.text_report || "";
    $("#tbPre").textContent = data.testbench || "";
    $("#logPre").textContent = simLog;
    $("#wavePre").textContent = data.waveform_text || "No waveform data.";

    const dl = $("#downloadRow");
    if (data.has_vcd && data.work_dir) {
      dl.innerHTML =
        `<a href="/api/download/vcd?work_dir=${encodeURIComponent(data.work_dir)}">↓ sim.vcd</a>` +
        `<a href="/api/download/report?work_dir=${encodeURIComponent(data.work_dir)}">↓ report.txt</a>` +
        `<button type="button" id="dlTb">↓ testbench.sv</button>`;
      $("#dlTb").addEventListener("click", () => downloadText(data.testbench || "", "testbench.sv"));
    } else {
      dl.innerHTML =
        `<button type="button" id="dlTb">↓ testbench.sv</button>`;
      const tbBtn = $("#dlTb");
      if (tbBtn) tbBtn.addEventListener("click", () => downloadText(data.testbench || "", "testbench.sv"));
    }

    document.querySelectorAll(".filter-chips .chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        activeFilter = chip.dataset.filter;
        document.querySelectorAll(".filter-chips .chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        renderTableBody(App._resultRows || []);
      });
    });

    document.querySelectorAll(".result-tabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".result-tabs button").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".result-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        document.getElementById(btn.dataset.panel).classList.add("active");
        if (btn.dataset.panel === "panelWave") {
          loadWaveformVisual(data);
        }
      });
    });

    bindWaveSubtabs(data);

    if (!pass && status !== "sim_missing") {
      document.querySelector('.result-tabs button[data-panel="panelLog"]')?.click();
    }

    App.stepState.results = "done";
    if (window.App && App.gotoStep) {
      /* already on results */
    }
  }

  function renderTableBody(allRows) {
    const tbody = $("#resultsTableBody");
    if (!tbody) return;
    const rows = filterRows(allRows);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-dim)">No matching test cases</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map((r) => {
        const failCls = r.result === "FAIL" ? " row-fail" : "";
        const resCls = r.result === "PASS" ? "result-pass" : r.result === "FAIL" ? "result-fail" : "";
        return (
          `<tr class="result-row${failCls}" data-row="${r.num}" tabindex="0">
          <td class="num">${r.num}</td>
          <td>${App.escapeHtml(r.category)}</td>
          <td>${App.escapeHtml(r.description)}</td>
          <td>${App.escapeHtml(formatFieldMap(r.inputs))}</td>
          <td>${App.escapeHtml(formatFieldMap(r.expected))}</td>
          <td>${App.escapeHtml(formatFieldMap(r.got))}</td>
          <td class="${resCls}">${r.result}</td>
        </tr>
        <tr class="row-detail" data-detail="${r.num}"><td colspan="7">${App.escapeHtml(r.detail || "")}</td></tr>`
        );
      })
      .join("");

    tbody.querySelectorAll(".result-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const n = tr.dataset.row;
        const det = tbody.querySelector(`tr[data-detail="${n}"]`);
        const wasOpen = det.classList.contains("open");
        tbody.querySelectorAll(".row-detail").forEach((d) => d.classList.remove("open"));
        if (!wasOpen) det.classList.add("open");
      });
    });
  }

  function downloadText(content, filename) {
    const blob = new Blob([content], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function bindWaveSubtabs(data) {
    document.querySelectorAll(".wave-subtabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".wave-subtabs button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const sub = btn.dataset.waveSub;
        $("#waveVisual").classList.toggle("active", sub === "visual");
        $("#waveRaw").classList.toggle("active", sub === "raw");
        if (sub === "visual") loadWaveformVisual(data);
      });
    });
  }

  async function loadWaveformVisual(data) {
    const container = $("#waveVisual");
    if (!container) return;
    if (!data.has_vcd || !data.work_dir) {
      container.innerHTML =
        '<div class="wv-error">No VCD waveform available for this run.</div>';
      switchToRawTab();
      return;
    }
    if (waveJsonCache && waveWorkDirCache === data.work_dir && container._wvInstance) {
      if (window.WaveformViewer) WaveformViewer.render(container, waveJsonCache);
      return;
    }
    container.innerHTML = '<div class="plan-loading"><span class="spinner"></span> Loading waveform…</div>';
    try {
      const res = await fetch(
        "/api/waveform/json?work_dir=" + encodeURIComponent(data.work_dir)
      );
      const json = await res.json();
      if (json.error) {
        container.innerHTML = `<div class="wv-error">${App.escapeHtml(json.error)}</div>`;
        switchToRawTab();
        return;
      }
      waveJsonCache = json;
      waveWorkDirCache = data.work_dir;
      if (window.WaveformViewer) {
        WaveformViewer.render(container, json);
      }
    } catch (err) {
      container.innerHTML = `<div class="wv-error">Failed to load waveform: ${App.escapeHtml(String(err))}</div>`;
      switchToRawTab();
    }
  }

  function switchToRawTab() {
    document.querySelector('.wave-subtabs button[data-wave-sub="raw"]')?.click();
  }

  window.Results = { render, loadWaveformVisual };
})();
