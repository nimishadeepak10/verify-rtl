/**
 * VerifyRTL — verification plan fetch, render, category toggles.
 */
(function () {
  "use strict";

  const CATEGORY_NUMS = ["①", "②", "③", "④", "⑤", "⑥"];
  let toggleDebounce = null;
  let loadToken = 0;

  function $(sel) {
    return document.querySelector(sel);
  }

  function formatPorts(summary) {
    if (!summary) return "—";
    const ins = summary.inputs ?? 0;
    const outs = summary.outputs ?? 0;
    return `${ins} in · ${outs} out`;
  }

  function formatInputs(inputs) {
    if (!inputs) return "—";
    return Object.entries(inputs)
      .map(([k, v]) => `<span class="port-val">${k}</span><span class="port-eq">=</span><span class="num">${v}</span>`)
      .join(" ");
  }

  function formatExpected(exp) {
    if (!exp) return "—";
    return Object.entries(exp)
      .map(([k, v]) => `<span class="port-val">${k}</span><span class="port-eq">=</span><span class="num">${v}</span>`)
      .join(" ");
  }

  function caseCount(cat) {
    if (cat.status !== "enabled") return 0;
    let n = (cat.cases || []).length;
    (cat.subcategories || []).forEach((sc) => {
      if (sc.status === "enabled") n += (sc.cases || []).length;
    });
    return n;
  }

  function renderCaseTable(cases) {
    if (!cases || !cases.length) {
      return '<p class="mono" style="color:var(--text-dim);font-size:12px">No test cases in this category.</p>';
    }
    const rows = cases
      .map(
        (tc) =>
          `<tr>
        <td>${App.escapeHtml(tc.id)}</td>
        <td>${App.escapeHtml(tc.description)}</td>
        <td>${formatInputs(tc.inputs)}</td>
        <td>${formatExpected(tc.expected_outputs)}</td>
        <td>${(tc.tags || []).map((t) => App.escapeHtml(t)).join(", ")}</td>
      </tr>`
      )
      .join("");
    return (
      '<table class="data-table"><thead><tr>' +
      "<th>ID</th><th>DESCRIPTION</th><th>INPUTS</th><th>EXPECTED</th><th>TAGS</th>" +
      `</tr></thead><tbody>${rows}</tbody></table>`
    );
  }

  function renderSubcategories(subs, catId) {
    if (!subs || !subs.length) return "";
    return subs
      .map((sc, si) => {
        const na = sc.status === "n/a";
        const enabled = sc.status === "enabled";
        const count = enabled ? (sc.cases || []).length : 0;
        return (
          `<div class="subcat-panel" data-subcat="${App.escapeHtml(sc.id)}">
        <div class="subcat-panel__head">
          <span class="subcat-panel__name">${App.escapeHtml(sc.name)}</span>
          <div class="cat-panel__meta">
            <span class="cat-count">${count} cases</span>
            <button type="button" class="toggle-pill${enabled ? " on" : ""}${na ? " disabled" : ""}"
              data-cat="${App.escapeHtml(catId)}" data-sub="${App.escapeHtml(sc.id)}"
              aria-pressed="${enabled}" ${na ? "disabled" : ""}></button>
          </div>
        </div>
        <p class="cat-panel__rationale">${App.escapeHtml(sc.rationale || "")}</p>
        ${na && sc.not_applicable_reason ? `<div class="callout callout-warn"><span class="callout__prefix">[N/A]</span>${App.escapeHtml(sc.not_applicable_reason)}</div>` : ""}
        ${renderCaseTable(sc.cases)}
      </div>`
        );
      })
      .join("");
  }

  function renderCategory(cat, index) {
    const num = CATEGORY_NUMS[index] || `${index + 1}.`;
    const na = cat.status === "n/a";
    const enabled = cat.status === "enabled";
    const count = caseCount(cat);
    const toggleClass = `toggle-pill${enabled ? " on" : ""}${na ? " disabled" : ""}`;

    return (
      `<article class="cat-panel" data-category="${App.escapeHtml(cat.id)}">
      <header class="cat-panel__head">
        <span class="cat-panel__prefix">${num}</span>
        <div class="cat-panel__main">
          <div class="cat-panel__title-row">
            <span class="cat-panel__name">${App.escapeHtml(cat.name).toUpperCase()}</span>
            <div class="cat-panel__meta">
              <span class="led ${enabled ? "led-green" : na ? "led-dim" : "led-dim"}"></span>
              <span class="cat-count">${count} cases</span>
              ${na ? '<span class="pill-na">N/A</span>' : ""}
              <button type="button" class="${toggleClass}" data-cat="${App.escapeHtml(cat.id)}"
                aria-pressed="${enabled}" ${na ? "disabled" : ""}></button>
            </div>
          </div>
          <p class="cat-panel__rationale">${App.escapeHtml(cat.rationale || "")}</p>
        </div>
      </header>
      <div class="cat-panel__body">
        ${na && cat.not_applicable_reason ? `<div class="callout callout-warn"><span class="callout__prefix">[N/A]</span>${App.escapeHtml(cat.not_applicable_reason)}</div>` : ""}
        <button type="button" class="expand-btn" aria-expanded="false" data-expand="${App.escapeHtml(cat.id)}">
          ▶ Expand to view test cases
        </button>
        <div class="cat-expand" id="expand-${App.escapeHtml(cat.id)}">
          ${renderSubcategories(cat.subcategories, cat.id)}
          ${renderCaseTable(cat.cases)}
        </div>
      </div>
    </article>`
    );
  }

  function renderPlan(plan) {
    const p = App.currentPreview || {};
    const ready = p.ready_to_run;
    const fileName = p.file_name || App.rtlFileName || "—";
    const lines = p.rtl_lines || App.rtlLines || "—";

    let html = `
      <header class="plan-header">
        <h1 class="plan-header__title">${App.escapeHtml(plan.dut_name)}</h1>
        <p class="plan-header__sub">${App.escapeHtml(plan.design_type)} · ${formatPorts(plan.port_summary)} · ${plan.total_planned_cases} test cases</p>
        <span class="status-pill"><span class="led ${ready ? "led-green" : "led-amber"}"></span> ${ready ? "READY" : "NOT READY"}</span>
      </header>

      <section class="plan-section">
        <h3 class="plan-section__title">DUT OVERVIEW</h3>
        <table class="dut-table">
          <tr><td>Module</td><td>${App.escapeHtml(plan.dut_name)}</td></tr>
          <tr><td>Type</td><td>${App.escapeHtml(plan.design_type)}</td></tr>
          <tr><td>Function</td><td>${App.escapeHtml(plan.dut_summary || "—")}</td></tr>
          <tr><td>Inputs</td><td>${plan.port_summary?.inputs ?? "—"}</td></tr>
          <tr><td>Outputs</td><td>${plan.port_summary?.outputs ?? "—"}</td></tr>
          <tr><td>Source file</td><td>${App.escapeHtml(fileName)}</td></tr>
          <tr><td>Lines</td><td>${lines}</td></tr>
        </table>
      </section>

      <section class="plan-section">
        <h3 class="plan-section__title">VERIFICATION STRATEGY</h3>
        <p class="plan-prose">${App.escapeHtml(plan.methodology || "")}</p>
        <p class="plan-prose" style="margin-top:var(--s-3)"><strong>Reference model:</strong> ${App.escapeHtml(plan.reference_model || "")}</p>
      </section>

      <section class="plan-section">
        <h3 class="plan-section__title">TEST CATEGORIES</h3>
        ${(plan.categories || []).map((c, i) => renderCategory(c, i)).join("")}
      </section>

      <section class="plan-section">
        <h3 class="plan-section__title">COVERAGE GOALS</h3>
        <ul class="criteria-list">
          ${(plan.coverage_goals || [])
            .map(
              (g) =>
                `<li><span class="goal-item__name">${App.escapeHtml(g.name).toUpperCase()}</span> — ${App.escapeHtml(g.rationale)} <span class="pct-badge">≥ ${g.target_percent}%</span></li>`
            )
            .join("")}
        </ul>
      </section>

      <section class="plan-section">
        <h3 class="plan-section__title">PASS / FAIL CRITERIA</h3>
        <ol class="criteria-list">
          ${(plan.pass_criteria || []).map((c) => `<li>${App.escapeHtml(c)}</li>`).join("")}
        </ol>
      </section>

      <section class="plan-section">
        <h3 class="plan-section__title">NOTES</h3>
        <div class="notes-list">
          ${(plan.notes || [])
            .map((n) => {
              const sev = (n.severity || "info").toLowerCase();
              const cls = sev === "warn" ? "callout-warn" : sev === "error" ? "callout-error" : "callout-info";
              const prefix = sev === "warn" ? "[WARN]" : sev === "error" ? "[ERROR]" : "[INFO]";
              return `<div class="callout ${cls}"><span class="callout__prefix">${prefix}</span>${App.escapeHtml(n.message)}</div>`;
            })
            .join("")}
        </div>
      </section>
    `;

    $("#planContent").innerHTML = html;
    $("#planContent").classList.remove("hidden");
    $("#planEmpty").classList.add("hidden");
    $("#planFooter").classList.remove("hidden");
    if (window.App.updateRunButton) App.updateRunButton();
    else $("#runFromPlan").disabled = !(App.currentPreview && App.currentPreview.ready_to_run);

    bindPlanEvents();
  }

  function bindPlanEvents() {
    document.querySelectorAll(".expand-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.expand;
        const panel = document.getElementById(`expand-${id}`);
        const open = panel.classList.toggle("open");
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.textContent = open ? "▼ Collapse test cases" : "▶ Expand to view test cases";
      });
    });

    document.querySelectorAll(".toggle-pill:not(.disabled)").forEach((tog) => {
      tog.addEventListener("click", () => onToggle(tog));
      tog.addEventListener("keydown", (e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          onToggle(tog);
        }
      });
    });
  }

  function onToggle(tog) {
    const catId = tog.dataset.cat;
    const subId = tog.dataset.sub;
    const nextOn = !tog.classList.contains("on");
    if (subId) {
      App.vplanToggles.subcategories[subId] = nextOn;
    } else {
      App.vplanToggles.categories[catId] = nextOn;
    }
    tog.classList.add("loading");
    clearTimeout(toggleDebounce);
    toggleDebounce = setTimeout(() => refetchVplan(tog), 250);
  }

  function syncTogglesFromPlan(plan) {
    App.vplanToggles.categories = {};
    App.vplanToggles.subcategories = {};
    (plan.categories || []).forEach((c) => {
      App.vplanToggles.categories[c.id] = c.status === "enabled";
      (c.subcategories || []).forEach((sc) => {
        App.vplanToggles.subcategories[sc.id] = sc.status === "enabled";
      });
    });
  }

  async function refetchVplan(triggerBtn) {
    try {
      const fd = await App.buildFormData();
      fd.append("enabled_categories", JSON.stringify(App.vplanToggles.categories));
      fd.append("enabled_subcategories", JSON.stringify(App.vplanToggles.subcategories));
      const res = await fetch("/api/vplan", { method: "POST", body: fd });
      const data = await res.json();
      if (data.error) {
        console.warn(data.error);
        return;
      }
      App.currentVplan = data;
      syncTogglesFromPlan(data);
      renderPlan(data);
      App.updateNavContext();
    } catch (err) {
      console.warn("Vplan refresh failed:", err);
    } finally {
      if (triggerBtn) triggerBtn.classList.remove("loading");
    }
  }

  async function load() {
    if (!App.rtlContent.trim()) {
      $("#planEmpty").classList.remove("hidden");
      $("#planContent").classList.add("hidden");
      $("#planFooter").classList.add("hidden");
      return;
    }

    const token = ++loadToken;
    $("#planLoading").classList.remove("hidden");
    $("#planEmpty").classList.add("hidden");

    try {
      const fd = await App.buildFormData();
      fd.append("enabled_categories", JSON.stringify(App.vplanToggles.categories));
      fd.append("enabled_subcategories", JSON.stringify(App.vplanToggles.subcategories));
      const res = await fetch("/api/vplan", { method: "POST", body: fd });
      const data = await res.json();
      if (token !== loadToken) return;
      if (data.error) {
        $("#planEmpty").classList.remove("hidden");
        $("#planEmpty").innerHTML = `<p>${App.escapeHtml(data.error)}</p>`;
        return;
      }
      App.currentVplan = data;
      syncTogglesFromPlan(data);
      renderPlan(data);
      App.updateNavContext();
      App.updateTopCrumb();
    } catch (err) {
      if (token === loadToken) {
        $("#planEmpty").classList.remove("hidden");
        $("#planEmpty").textContent = "Failed to load plan: " + err.message;
      }
    } finally {
      $("#planLoading").classList.add("hidden");
    }
  }

  function invalidate() {
    App.currentVplan = null;
  }

  function renderCoverage() {
    const mount = $("#coverageGoals");
    if (!mount) return;

    const result = App.currentResult;
    const cov = result && result.coverage ? result.coverage : null;

    if (!result || !result.work_dir) {
      mount.innerHTML =
        '<div class="empty-state"><h2 class="empty-state__title">No coverage yet</h2><p class="empty-state__lead">Run verification on the Run screen to see coverage results.</p><button type="button" class="btn btn-primary" data-goto="run">Go to Run →</button></div>';
      mount.querySelector('button[data-goto="run"]')?.addEventListener("click", () => App.gotoStep && App.gotoStep("run"));
      return;
    }

    const ledClass = (p, na) => {
      if (na) return "led-dim";
      if (p >= 90) return "led-green";
      if (p >= 50) return "led-amber";
      return "led-red";
    };

    const bar = (pct) => {
      const v = Math.max(0, Math.min(100, Number(pct) || 0));
      return `<div class="cov-bar"><div class="cov-bar__fill" style="width:${v}%"></div><div class="cov-bar__label">${v.toFixed(1)}%</div></div>`;
    };

    const fmtLoc = (lineNo) => `dut.v:${lineNo}`;

    if (!cov) {
      mount.innerHTML =
        '<p class="coverage-intro">Coverage data not found for this run. Re-run verification (coverage is computed post-simulation).</p>';
      return;
    }

    const overall = Number(cov.overall_percent || 0);
    const meta = cov.meta || {};
    const dut = meta.dut || result.module || "design";
    const tests = (result.sim_log || "").match(/PASS=(\d+)/i);
    const passN = tests ? tests[1] : "—";

    const stmt = cov.statement || {};
    const br = cov.branch || {};
    const tg = cov.toggle || {};
    const fsm = cov.fsm || {};

    const fsmNA = !!fsm.not_applicable || (fsm.total_transitions === 0 && fsm.total_states === 0);

    mount.innerHTML = `
      <div class="cov-card">
        <div class="cov-head">
          <div class="cov-title mono">CODE COVERAGE</div>
          <div class="cov-overall">
            <span class="led ${ledClass(overall,false)}"></span>
            <span class="mono cov-overall__label">OVERALL</span>
            <span class="mono cov-overall__pct">${overall.toFixed(1)}%</span>
          </div>
        </div>
        ${bar(overall)}
        <div class="cov-meta">${App.escapeHtml(`${dut} · ${passN} test cases · ${result.work_dir}`)}</div>

        <div class="cov-section" data-section="stmt">
          <button type="button" class="cov-section__head">
            <span class="led ${ledClass(Number(stmt.percent||0),false)}"></span>
            <span class="mono">STATEMENT COVERAGE</span>
            <span class="mono cov-section__pct">${Number(stmt.percent||0).toFixed(1)}%</span>
          </button>
          <div class="cov-section__body">
            <div class="cov-sub mono">${stmt.hit || 0} / ${stmt.total || 0} statements executed</div>
            ${bar(Number(stmt.percent||0))}
            <div class="cov-subhead mono">UNCOVERED STATEMENTS (${(stmt.uncovered_lines||[]).length})</div>
            <div class="cov-list">
              ${(stmt.uncovered_lines || []).slice(0, 200).map((ln) => `<button type="button" class="cov-link" data-jump="${ln}">${fmtLoc(ln)}</button>`).join("") || '<div class="cov-dim">—</div>'}
            </div>
          </div>
        </div>

        <div class="cov-section" data-section="branch">
          <button type="button" class="cov-section__head">
            <span class="led ${ledClass(Number(br.percent||0),false)}"></span>
            <span class="mono">BRANCH COVERAGE</span>
            <span class="mono cov-section__pct">${Number(br.percent||0).toFixed(1)}%</span>
          </button>
          <div class="cov-section__body">
            <div class="cov-sub mono">${br.hit || 0} / ${br.total || 0} branches taken</div>
            ${bar(Number(br.percent||0))}
            <div class="cov-branch-list">
              ${(br.branches || []).slice(0, 500).map((b) => {
                const hit = !!b.hit;
                const mark = hit ? "✓" : "✗";
                const cls = hit ? "cov-ok" : "cov-miss";
                const ln = b.line_no || "";
                return `<div class="cov-branch ${cls}">
                  <div class="cov-branch__mark mono">${mark}</div>
                  <div class="cov-branch__label mono">${App.escapeHtml(String(b.label||""))}</div>
                  <div class="cov-branch__count mono">${hit ? `hit ${b.count||0}×` : "not taken"}</div>
                  <button type="button" class="cov-branch__jump" data-jump="${ln}">↗</button>
                </div>`;
              }).join("") || '<div class="cov-dim">—</div>'}
            </div>
          </div>
        </div>

        <div class="cov-section" data-section="toggle">
          <button type="button" class="cov-section__head">
            <span class="led ${ledClass(Number(tg.percent||0),false)}"></span>
            <span class="mono">TOGGLE COVERAGE</span>
            <span class="mono cov-section__pct">${Number(tg.percent||0).toFixed(1)}%</span>
          </button>
          <div class="cov-section__body">
            <div class="cov-sub mono">${tg.bits_toggled_both || 0} / ${tg.total_bits || 0} bits toggled both directions</div>
            ${bar(Number(tg.percent||0))}
            <div class="cov-subhead mono">BITS PER SIGNAL</div>
            <div class="cov-toggle-legend mono">
              <span><span class="cov-bit cov-bit--both"></span> both</span>
              <span><span class="cov-bit cov-bit--up"></span> up only</span>
              <span><span class="cov-bit cov-bit--down"></span> down only</span>
              <span><span class="cov-bit cov-bit--never"></span> never</span>
            </div>
            <div class="cov-toggle-list">
              ${Object.entries(tg.per_signal || {}).slice(0, 200).map(([name, meta]) => {
                const w = meta.width || 1;
                const both = meta.bits_both || [];
                const up = meta.bits_up || [];
                const down = meta.bits_down || [];
                const bits = [];
                for (let i = w - 1; i >= 0; i--) {
                  const cls = both[i] ? "both" : up[i] && !down[i] ? "up" : down[i] && !up[i] ? "down" : "never";
                  bits.push(`<span class="cov-bit cov-bit--${cls}" title="${name}[${i}]"></span>`);
                }
                return `<div class="cov-toggle-row" data-sig="${App.escapeHtml(name)}">
                  <button type="button" class="cov-sig mono" data-signal="${App.escapeHtml(name)}">${App.escapeHtml(name)}</button>
                  <span class="cov-sigw mono">[${w-1}:0]</span>
                  <div class="cov-bits">${bits.join("")}</div>
                </div>`;
              }).join("") || '<div class="cov-dim">—</div>'}
            </div>
          </div>
        </div>

        <div class="cov-section" data-section="fsm">
          <button type="button" class="cov-section__head">
            <span class="led ${ledClass(Number(fsm.state_percent||0),fsmNA)}"></span>
            <span class="mono">FSM COVERAGE</span>
            <span class="mono cov-section__pct">${fsmNA ? "N/A" : `${Number(fsm.state_percent||0).toFixed(1)}%`}</span>
          </button>
          <div class="cov-section__body">
            ${fsmNA ? `<div class="cov-dim">${App.escapeHtml(fsm.na_reason || "N/A")}</div>` : `
              <div class="cov-sub mono">${fsm.states_visited || 0} / ${fsm.total_states || 0} states visited</div>
              ${bar(Number(fsm.state_percent||0))}
              ${(fsm.total_transitions || 0) ? `<div class="cov-sub mono">${fsm.transitions_taken || 0} / ${fsm.total_transitions || 0} transitions taken</div>${bar(Number(fsm.transition_percent||0))}` : ""}
            `}
          </div>
        </div>
      </div>
    `;

    // collapsible behavior
    mount.querySelectorAll(".cov-section__head").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sec = btn.closest(".cov-section");
        sec.classList.toggle("open");
      });
      btn.closest(".cov-section")?.classList.add("open");
    });

    // jump handlers
    mount.querySelectorAll("[data-jump]").forEach((el) => {
      el.addEventListener("click", () => {
        const ln = parseInt(el.dataset.jump, 10);
        if (Number.isFinite(ln) && App.jumpToLine) App.jumpToLine(ln);
      });
    });

    // toggle signal click -> try highlight in waveform
    mount.querySelectorAll("[data-signal]").forEach((el) => {
      el.addEventListener("click", () => {
        const sig = el.dataset.signal;
        if (sig && App.highlightWaveSignal) App.highlightWaveSignal(sig);
      });
    });
  }

  window.VPlan = {
    load,
    invalidate,
    renderCoverage,
    refetchVplan,
  };
})();
