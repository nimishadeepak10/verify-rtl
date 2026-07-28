/**

 * VerifyRTL — results screen: summary, case table, tabs, waveform loader.

 */

(function () {

  "use strict";



  let activeFilter = "all";

  let waveJsonCache = null;

  let waveWorkDirCache = null;

  let chatHistory = [];

  let chatWorkDir = null;



  function $(sel) {

    return document.querySelector(sel);

  }



  function parseFields(s) {

    const out = {};

    const re = /(\w+)=(\S+)/g;

    let m;

    while ((m = re.exec(s))) out[m[1]] = m[2];

    return out;

  }



  function parseFailFields(s) {

    const expected = {};

    const got = {};

    const re = /(exp|got)_(\w+)=(\S+)/g;

    let m;

    while ((m = re.exec(s))) {

      if (m[1] === "exp") expected[m[2]] = m[3];

      else got[m[2]] = m[3];

    }

    return { inputs: {}, expected, got };

  }



  function makeRow(num, category, desc, inputs, expected, result, detail, gotOverride, tags) {

    const got = gotOverride || (result === "PASS" ? expected : {});

    return {

      num,

      category,

      description: desc,

      inputs: inputs || {},

      expected: expected || {},

      got: got || {},

      result,

      detail,

      tags: tags || ["simulation"],

    };

  }



  function parsePipeFields(section) {
    const out = {};
    if (!section) return out;
    section.split("|").forEach((part) => {
      const p = part.trim();
      const eq = p.indexOf("=");
      if (eq > 0) out[p.slice(0, eq).trim()] = p.slice(eq + 1).trim();
    });
    return out;
  }

  function parseTestLine(trimmed) {
    if (!/^TEST,/i.test(trimmed)) return null;
    const parts = trimmed.split(",");
    if (parts.length < 5) return null;
    const testIdx = parseInt(parts[1], 10);
    if (!Number.isFinite(testIdx)) return null;
    let inputs = {};
    let expected = {};
    let got = {};
    let result = "OBSERVED";
    let i = 2;
    while (i < parts.length) {
      const tag = (parts[i] || "").trim().toUpperCase();
      if (tag === "IN" && i + 1 < parts.length) {
        inputs = parsePipeFields(parts[i + 1]);
        i += 2;
        continue;
      }
      if ((tag === "EXP" || tag === "EXPECTED") && i + 1 < parts.length) {
        expected = parsePipeFields(parts[i + 1]);
        i += 2;
        continue;
      }
      if (tag === "OUT" && i + 1 < parts.length) {
        got = parsePipeFields(parts[i + 1]);
        i += 2;
        continue;
      }
      if (tag === "RESULT" && i + 1 < parts.length) {
        const r = parts[i + 1].trim().toUpperCase();
        result = r === "OBS" ? "OBSERVED" : r;
        i += 2;
        continue;
      }
      i += 1;
    }
    return { testIdx, inputs, expected, got, result, detail: trimmed };
  }

  function parseSimLog(simLog, overallPass) {

    const rows = [];

    if (!simLog) return rows;

    const stimResult = overallPass ? "PASS" : "RUN";

    const lines = simLog.split("\n");

    let idx = 0;



    for (const line of lines) {

      const trimmed = line.trim();

      if (!trimmed || trimmed.startsWith("===")) continue;

      const test = parseTestLine(trimmed);
      if (test) {
        rows.push(
          makeRow(
            ++idx,
            "Simulation",
            "Vector check",
            test.inputs,
            test.expected,
            test.result,
            test.detail,
            test.got,
            ["simulation"]
          )
        );
        continue;
      }



      let m = trimmed.match(/(?:^|\s)PASS\s+t=\d+\s+(.+)$/i);

      if (m && !trimmed.includes("PASS=")) {

        const fields = parseFields(m[1]);

        rows.push(

          makeRow(++idx, "Simulation", "Vector check", fields, fields, "PASS", trimmed, fields)

        );

        continue;

      }



      m = trimmed.match(/(?:^|\s)FAIL\s+t=\d+\s+(.+)$/i);

      if (m) {

        const parts = parseFailFields(m[1]);

        rows.push(

          makeRow(

            ++idx,

            "Simulation",

            "Mismatch",

            parts.inputs,

            parts.expected,

            "FAIL",

            trimmed,

            parts.got

          )

        );

        continue;

      }



      m = trimmed.match(/^STIM\s+t=\d+\s+(.+)$/i);

      if (m) {

        const fields = parseFields(m[1]);

        rows.push(

          makeRow(

            ++idx,

            "Simulation",

            "Stimulus applied",

            fields,

            {},

            stimResult,

            trimmed,

            fields,

            ["stimulus"]

          )

        );

        continue;

      }



      m = trimmed.match(/^SEQ\s+t=\d+\s+(.+)$/i);

      if (m) {

        const rest = m[1].trim();

        if (/^test=\d+/i.test(rest)) {

          rows.push(

            makeRow(++idx, "Simulation", "Sequential step", {}, {}, stimResult, trimmed, {}, [

              "sequential",

            ])

          );

        } else {

          const fields = parseFields(rest);

          rows.push(

            makeRow(

              ++idx,

              "Simulation",

              "Sequential step",

              fields,

              {},

              stimResult,

              trimmed,

              fields,

              ["sequential"]

            )

          );

        }

      }

    }

    return rows;

  }



  function rowsFromVplan(plan, overallPass) {

    const rows = [];

    let idx = 0;

    (plan.categories || []).forEach((cat) => {

      if (cat.status !== "enabled") return;

      const addCases = (cases, catName, tagId) => {

        (cases || []).forEach((tc) => {

          const exp = tc.expected_outputs || {};

          rows.push({

            num: ++idx,

            category: catName,

            description: tc.description,

            inputs: tc.inputs || {},

            expected: exp,

            got: overallPass ? exp : {},

            result: overallPass ? "PASS" : "—",

            detail: tc.rationale || "",

            tags: tc.tags || [tagId || cat.id],

          });

        });

      };

      addCases(cat.cases, cat.name, cat.id);

      (cat.subcategories || []).forEach((sc) => {

        if (sc.status === "enabled") addCases(sc.cases, `${cat.name} / ${sc.name}`, sc.id);

      });

    });

    return rows;

  }



  function summaryTestCount(simLog) {

    const m = (simLog || "").match(/PASS=(\d+)/i);

    return m ? parseInt(m[1], 10) : 0;

  }



  function resolveResultRows(data, pass) {

    if (data.test_results && data.test_results.length) {

      return data.test_results.map((r, i) => ({

        num: r.num != null ? r.num : i + 1,

        category: r.category || "Simulation",

        description: r.description || "",

        inputs: r.inputs || {},

        expected: r.expected || {},

        got: r.got || {},

        result: r.result || "—",

        detail: r.detail || "",

        tags: r.tags || ["simulation"],

      }));

    }



    const simLog = (data.uvm_note ? data.uvm_note + "\n\n" : "") + (data.sim_log || "");

    let rows = parseSimLog(simLog, pass);

    const expectedN = summaryTestCount(simLog);



    if (rows.length < 1 && App.currentVplan) {

      rows = rowsFromVplan(App.currentVplan, pass);

    } else if (expectedN > 0 && rows.length < expectedN && App.currentVplan) {

      const vplanRows = rowsFromVplan(App.currentVplan, pass);

      if (vplanRows.length >= expectedN) rows = vplanRows;

    }



    return rows;

  }



  function formatFieldMap(map, verdict) {

    if (!map || !Object.keys(map).length) {
      if (verdict === "unverified") return "no model";
      return "—";
    }
    if (map._note === "no model") return "no model";

    return Object.entries(map)

      .filter(([k]) => k !== "_note")

      .map(([k, v]) => `${k}=${v}`)

      .join(" ");

  }



  function filterRows(rows, unverified) {

    if (activeFilter === "all") return rows;

    if (activeFilter === "passed" || activeFilter === "observed") {
      if (unverified) return rows.filter((r) => r.result === "OBSERVED");
      return rows.filter((r) => r.result === "PASS");
    }

    if (activeFilter === "failed") return rows.filter((r) => r.result === "FAIL");

    return rows.filter((r) =>

      (r.tags || []).some((t) => String(t).toLowerCase() === activeFilter)

    );

  }



  function render(data) {

    const mount = $("#resultsMount");

    if (!mount) return;



    activeFilter = "all";



    if (waveWorkDirCache !== data.work_dir) {

      waveJsonCache = null;

      waveWorkDirCache = null;

      const wv = $("#waveVisual");

      if (wv && window.WaveformViewer) WaveformViewer.destroy(wv);

    }



    const verdict = data.verdict || (data.success ? "pass" : data.status || "fail");
    const vMode = data.verification_mode || "";
    const vExpl = data.verification_mode_explanation || "";

    const pass = verdict === "pass" || data.uvm_note;
    const simLog = (data.uvm_note ? data.uvm_note + "\n\n" : "") + (data.sim_log || "");
    const errors = data.errors || [];

    const isWaveformPass = verdict === "pass" && vMode === "waveform";
    const isNotSynth = verdict === "not_synthesizable" || data.status === "not_synthesizable";
    const isError = verdict === "error" || data.status === "sim_missing" || data.status === "compile_failed";

    const statsHtml = isWaveformPass
      ? `<div class="result-stats result-stats--muted">Simulation completed — inspect waveforms</div>`
      : isNotSynth
        ? `<div class="result-stats result-stats--muted">Synthesis failed — simulation not run</div>`
        : isError
          ? `<div class="result-stats result-stats--muted">${errors.length ? errors.length + " error(s)" : "Run failed"} — see log below</div>`
          : `<div class="result-stats result-stats--muted">See simulation log</div>`;

    const synthMeta = [];
    if (data.synth_synthesizable === true) synthMeta.push("synthesizable");
    else if (data.synth_synthesizable === false) synthMeta.push("not synthesizable");
    else if (data.synth_skipped) synthMeta.push("synth check skipped");
    if (data.detected_language) synthMeta.push(data.detected_language);



    const backendParts = [];

    if (data.backend_used) backendParts.push(data.backend_used);

    if (data.backend_version) backendParts.push(`v${data.backend_version}`);

    if (data.simulator) backendParts.push(data.simulator);



    mount.innerHTML = `

      <header class="screen-header">

        <h1 class="screen-title">Results</h1>

        <p class="screen-lead">Summary below — open the <strong>Waveform</strong> tab for timing, or download the testbench and VCD.</p>

      </header>

      <div class="result-summary">

        <div class="result-badge result-badge--${verdict === "pass" ? "pass" : isNotSynth ? "error" : verdict === "unverified" ? "unverified" : verdict === "error" || data.status === "sim_missing" ? "error" : "fail"}">
          <span class="led ${verdict === "pass" ? "led-green" : isNotSynth ? "led-red" : verdict === "unverified" ? "led-amber" : "led-red"}"></span>
          <span class="mono">${verdict === "pass" ? (isWaveformPass ? "SIM OK" : "PASS") : isNotSynth ? "NOT SYNTH" : verdict === "unverified" ? "UNVERIFIED" : verdict === "error" || data.status === "sim_missing" ? (data.status === "sim_missing" ? "NO SIM" : "ERROR") : "FAIL"}</span>
        </div>

        ${statsHtml}

        <div class="result-meta">${App.escapeHtml([...backendParts, ...synthMeta].join(" · ") || "—")}</div>

      </div>

      ${isNotSynth ? `<div class="callout callout-warn callout-unverified">
        <span class="callout__prefix">NOT SYNTHESIZABLE</span> — Vivado synthesis failed
        <p class="callout-unverified__body">${App.escapeHtml(vExpl || "Fix synthesis errors in the Full report.")}</p>
        ${errors.length ? `<ul class="error-list">${errors.map((e) => `<li class="mono">${App.escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      </div>` : ""}

      ${isError && !isNotSynth ? `<div class="callout callout-warn callout-unverified">
        <span class="callout__prefix">SIMULATION ERROR</span> — compile or run failed
        <p class="callout-unverified__body">${App.escapeHtml(vExpl || "See Sim log tab for details.")}</p>
        ${errors.length ? `<ul class="error-list">${errors.map((e) => `<li class="mono">${App.escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      </div>` : ""}

      ${isWaveformPass ? `<div class="callout callout-warn callout-unverified" style="border-left-color: var(--led-green)">
        <span class="callout__prefix">WAVEFORM VERIFICATION</span> — synthesis ${data.synth_skipped ? "check skipped" : "passed"}, simulation completed
        <p class="callout-unverified__body">${App.escapeHtml(vExpl)}</p>
        <p class="callout-unverified__hint">Open the <strong>Waveform</strong> tab to confirm RTL behavior.</p>
      </div>` : ""}

      <div class="panel">
        <div class="panel__header"><h2 class="panel__title">SIMULATION STATUS</h2></div>
        <div class="panel__body">
          <p>${data.has_vcd ? "VCD waveform captured — use the <strong>Waveform</strong> tab." : "No waveform file was produced for this run."}</p>
          <p class="text-dim">Testbench applies timed input stimulus only (no per-test PASS/FAIL). RTL correctness is judged from waveforms and tool logs.</p>
        </div>
      </div>



      <div class="result-tabs" role="tablist">

        <button type="button" class="active" data-panel="panelReport">Full report</button>

        <button type="button" data-panel="panelTb">Testbench</button>

        <button type="button" data-panel="panelLog">Sim log</button>

        <button type="button" data-panel="panelWave">Waveform</button>

        <button type="button" data-panel="panelChat">Chat</button>

        <button type="button" id="resultsFormalLink">Formal →</button>

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

      <div id="panelChat" class="result-panel">

        <p class="text-dim">Ask about this run — answers cite the specific RTL line or waveform
          signal/timestamp, scoped to this run only.</p>

        <div id="chatMessages" style="margin:var(--s-3) 0; max-height:400px; overflow-y:auto"></div>

        <div style="display:flex; gap:var(--s-2)">

          <input type="text" id="chatInput" class="input-mono" style="flex:1"

            placeholder="e.g. Why did sum never toggle bit 2?">

          <button type="button" class="btn btn-primary" id="chatSendBtn">Send</button>

        </div>

        <span id="chatStatus" class="text-dim mono" style="font-size:12px"></span>

      </div>



      <div class="download-row" id="downloadRow"></div>

    `;



    App._lastVerifyData = data;

    App._resultVerdict = verdict;

    $("#reportPre").textContent = data.text_report || "";

    $("#tbPre").textContent = data.testbench || "";

    $("#logPre").textContent = simLog;

    $("#wavePre").textContent = data.waveform_text || "No waveform data.";



    if (data.waveform_json && data.waveform_json.signals && data.waveform_json.signals.length) {

      waveJsonCache = data.waveform_json;

      waveWorkDirCache = data.work_dir;

    }



    const dl = $("#downloadRow");

    if (data.has_vcd && data.work_dir) {

      dl.innerHTML =

        `<a href="/api/download/vcd?work_dir=${encodeURIComponent(data.work_dir)}">↓ sim.vcd</a>` +

        `<a href="/api/download/report?work_dir=${encodeURIComponent(data.work_dir)}">↓ report.txt</a>` +

        `<button type="button" id="dlTb">↓ testbench.sv</button>`;

      $("#dlTb").addEventListener("click", () => downloadText(data.testbench || "", "testbench.sv"));

    } else {

      dl.innerHTML = `<button type="button" id="dlTb">↓ testbench.sv</button>`;

      $("#dlTb").addEventListener("click", () => downloadText(data.testbench || "", "testbench.sv"));

    }



    document.querySelectorAll(".result-tabs button[data-panel]").forEach((btn) => {

      btn.addEventListener("click", () => {

        document.querySelectorAll(".result-tabs button[data-panel]").forEach((b) => b.classList.remove("active"));

        document.querySelectorAll(".result-panel").forEach((p) => p.classList.remove("active"));

        btn.classList.add("active");

        document.getElementById(btn.dataset.panel).classList.add("active");

        if (btn.dataset.panel === "panelWave") {

          loadWaveformVisual(data);

        }

      });

    });

    const formalLink = document.getElementById("resultsFormalLink");

    if (formalLink) {

      formalLink.addEventListener("click", () => App.gotoStep("formal"));

    }



    bindWaveSubtabs(data);

    bindChat(data);



    if (!pass && (verdict === "error" || data.status === "sim_missing")) {

      document.querySelector('.result-tabs button[data-panel="panelLog"]')?.click();

    } else if (pass && data.has_vcd) {

      document.querySelector('.result-tabs button[data-panel="panelWave"]')?.click();

    }

  }



  function renderTableBody(allRows) {

    const tbody = $("#resultsTableBody");

    if (!tbody) return;

    const rows = filterRows(allRows, App._resultUnverified);

    if (!rows.length) {

      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-dim)">No matching test cases — try the <strong>All</strong> filter</td></tr>`;

      return;

    }

    tbody.innerHTML = rows

      .map((r) => {

        const failCls = r.result === "FAIL" ? " row-fail" : "";

        const resCls =
          r.result === "PASS"
            ? "result-pass"
            : r.result === "FAIL"
              ? "result-fail"
              : r.result === "OBSERVED"
                ? "result-observed"
                : "";

        const v = App._resultVerdict || "";

        return (

          `<tr class="result-row${failCls}" data-row="${r.num}" tabindex="0">

          <td class="num">${r.num}</td>

          <td>${App.escapeHtml(r.category)}</td>

          <td>${App.escapeHtml(r.description)}</td>

          <td>${App.escapeHtml(formatFieldMap(r.inputs, v))}</td>

          <td>${App.escapeHtml(formatFieldMap(r.expected, v))}</td>

          <td>${App.escapeHtml(formatFieldMap(r.got, v))}</td>

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



  function renderChatMessages() {

    const host = $("#chatMessages");

    if (!host) return;

    if (!chatHistory.length) {

      host.innerHTML = '<span class="text-dim mono">No questions yet.</span>';

      return;

    }

    host.innerHTML = chatHistory

      .map(

        (turn) => `

      <div style="margin-bottom:var(--s-3)">

        <div class="mono" style="font-weight:600">Q: ${App.escapeHtml(turn.question)}</div>

        <div style="margin-top:4px; white-space:pre-wrap">${App.escapeHtml(turn.answer)}</div>

      </div>`

      )

      .join("");

    host.scrollTop = host.scrollHeight;

  }



  function bindChat(data) {

    chatHistory = [];

    chatWorkDir = data.work_dir || null;

    renderChatMessages();

    const sendBtn = $("#chatSendBtn");

    const input = $("#chatInput");

    const status = $("#chatStatus");

    if (!sendBtn || !input) return;



    const send = async () => {

      const question = input.value.trim();

      if (!question) return;

      if (!chatWorkDir) {

        status.textContent = "No waveform/report available for this run to ask about.";

        return;

      }

      sendBtn.disabled = true;

      status.textContent = "Thinking…";

      input.value = "";



      const fd = new FormData();

      fd.append("work_dir", chatWorkDir);

      fd.append("question", question);

      fd.append("history", JSON.stringify(chatHistory));



      let resp;

      try {

        const res = await fetch("/api/chat", { method: "POST", body: fd });

        resp = await res.json();

      } catch (err) {

        status.textContent = "Request failed: " + (err.message || err);

        sendBtn.disabled = false;

        return;

      }

      sendBtn.disabled = false;

      status.textContent = "";



      if (resp.error) {

        chatHistory.push({ question, answer: "Error: " + resp.error });

      } else {

        chatHistory.push({ question, answer: resp.answer });

      }

      renderChatMessages();

    };



    sendBtn.addEventListener("click", send);

    input.addEventListener("keydown", (e) => {

      if (e.key === "Enter") send();

    });

  }



  function showWaveError(container, message) {

    if (window.WaveformViewer) WaveformViewer.destroy(container);

    container.innerHTML = `<div class="wv-error">${App.escapeHtml(message)}</div>`;

  }



  async function fetchWaveformJson(workDir) {

    const res = await fetch("/api/waveform/json?work_dir=" + encodeURIComponent(workDir));

    const json = await res.json();

    if (!res.ok) {

      throw new Error(json.detail || json.error || `HTTP ${res.status} — restart the server to load Phase A.8`);

    }

    if (json.error) {

      throw new Error(json.error);

    }

    if (!json.signals || !json.signals.length) {

      throw new Error("Waveform JSON has no signals — re-run verification.");

    }

    return json;

  }



  async function loadWaveformVisual(data) {

    const container = $("#waveVisual");

    if (!container) return;



    if (!data.has_vcd) {

      showWaveError(container, "No VCD waveform available for this run.");

      switchToRawTab();

      return;

    }



    const workDir = data.work_dir;



    if (waveJsonCache && waveWorkDirCache === workDir && waveJsonCache.signals && waveJsonCache.signals.length) {

      if (window.WaveformViewer) WaveformViewer.render(container, waveJsonCache);

      return;

    }



    if (data.waveform_json && data.waveform_json.signals && data.waveform_json.signals.length) {

      waveJsonCache = data.waveform_json;

      waveWorkDirCache = workDir;

      if (window.WaveformViewer) WaveformViewer.render(container, waveJsonCache);

      return;

    }



    container.innerHTML = '<div class="plan-loading"><span class="spinner"></span> Loading waveform…</div>';



    try {

      const json = await fetchWaveformJson(workDir);

      waveJsonCache = json;

      waveWorkDirCache = workDir;

      if (window.WaveformViewer) {

        WaveformViewer.render(container, json);

      }

    } catch (err) {

      showWaveError(

        container,

        String(err.message || err) +

          " Tip: stop old servers and run: python -m uvicorn api.main:app --host 127.0.0.1 --port 8005"

      );

      switchToRawTab();

    }

  }



  function switchToRawTab() {

    document.querySelector('.wave-subtabs button[data-wave-sub="raw"]')?.click();

  }



  window.Results = { render, loadWaveformVisual };

})();


