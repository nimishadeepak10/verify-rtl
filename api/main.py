"""HTTP API: upload RTL, get testbench + simulation results."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.backends.registry import backend_info_list, formal_backends  # noqa: E402
from rtl_verify.generators.base import TbLanguage  # noqa: E402
from rtl_verify.pipeline import run_verification  # noqa: E402
from rtl_verify.waveform import load_module_info, vcd_to_json  # noqa: E402
from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.preview import build_test_preview  # noqa: E402
from rtl_verify.vplan_builder import build_vplan  # noqa: E402
from rtl_verify.coverage import CoverageReport  # noqa: E402
from rtl_verify.rtl_features import dut_source_extension  # noqa: E402
from rtl_verify.formal_props import (  # noqa: E402
    generate_formal_wrapper,
    recommended_formal_config,
    recommended_engine_chain,
)
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva, convert_to_sva_retry  # noqa: E402
from rtl_verify.llm_client import LLMNotConfigured  # noqa: E402
from rtl_verify import formal_log  # noqa: E402
from rtl_verify import coverage_closure  # noqa: E402
from rtl_verify.coverage_closure import run_closure_loop  # noqa: E402
from rtl_verify.spec_traceability import build_traceability_matrix  # noqa: E402
from rtl_verify.failure_triage import answer_question  # noqa: E402
from rtl_verify.dut_probe import generate_probed_rtl  # noqa: E402
from rtl_verify.vacuity import run_vacuity_check  # noqa: E402
from rtl_verify.assumption_check import check_assumption_consistency  # noqa: E402
from rtl_verify.mutation_adequacy import run_mutation_adequacy  # noqa: E402
from rtl_verify.cross_check import cross_check_property  # noqa: E402
from rtl_verify import regression  # noqa: E402
from rtl_verify.regression import BaselineProperty  # noqa: E402
from rtl_verify.blackbox import generate_blackboxed_rtl, recommend_blackbox_candidates  # noqa: E402
from rtl_verify.cdc_check import analyze_cdc  # noqa: E402

app = FastAPI(title="RTL Verify Automation", version="0.1.0")


def _analyze_and_probe(rtl_source: str, top_module: str):
    """analyze_rtl() + generate_probed_rtl() as one step, for every formal
    endpoint (suggest/convert/check). Internal registers -- tags, valid
    bits, FSM state, anything with no port -- are otherwise invisible to a
    property, which is exactly the gap the direct_cache.v stress-test
    finding in the README documented (1 of 11 suggested properties was
    expressible). Instrumenting here, once, means property_suggester.py
    and property_to_sva.py need no changes at all: they already just list
    `module.ports` in their prompts, and the debug ports this adds are, by
    construction, ordinary ports by the time they see them.

    A design with no internal registers (most of the simple combinational
    examples) gets back its own rtl_source completely unchanged --
    generate_probed_rtl() is a no-op when there's nothing to expose, so
    this is safe to always call, not just for designs known to need it.
    """
    mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    probed_source, probed_mod, _ = generate_probed_rtl(rtl_source, mod)
    return probed_source, probed_mod
STATIC = ROOT / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    page = STATIC / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>RTL Verify</h1><p>static/index.html missing</p>")


@app.get("/api/backends")
async def list_backends():
    """List all registered simulator backends and availability."""
    return backend_info_list()


@app.post("/api/vplan")
async def vplan_endpoint(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    language: str = Form("systemverilog"),
    top_module: str = Form(""),
    backend: str = Form(""),
    enabled_categories: str = Form(""),
    enabled_subcategories: str = Form(""),
):
    """Build full verification plan with category-level toggles."""
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    try:
        cat_toggle = json.loads(enabled_categories) if enabled_categories.strip() else {}
        sub_toggle = json.loads(enabled_subcategories) if enabled_subcategories.strip() else {}
        if not isinstance(cat_toggle, dict) or not isinstance(sub_toggle, dict):
            return {"error": "enabled_categories and enabled_subcategories must be JSON objects"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in toggles: {e}"}

    try:
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
        plan = build_vplan(
            rtl_source,
            mod,
            enabled_categories={k: bool(v) for k, v in cat_toggle.items()},
            enabled_subcategories={k: bool(v) for k, v in sub_toggle.items()},
            backend=backend.strip() or None,
            language=language.lower(),
        )
        return plan.to_dict()
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/analyze")
async def analyze(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    language: str = Form("systemverilog"),
    top_module: str = Form(""),
    backend: str = Form(""),
):
    """Pre-test report: DUT info and planned verification before simulation."""
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}
    try:
        lang = TbLanguage(language.lower())
        preview = build_test_preview(
            rtl_source,
            language=lang,
            top_module=top_module.strip() or None,
            backend=backend.strip() or None,
        )
        preview["rtl_lines"] = len(rtl_source.splitlines())
        preview["file_name"] = rtl_file.filename if rtl_file else "(pasted)"
        return preview
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/verify")
async def verify(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    language: str = Form("systemverilog"),
    top_module: str = Form(""),
    backend: str = Form(""),
):
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text", "success": False}
    lang = TbLanguage(language.lower())
    backend_sel = backend.strip() or None
    result = run_verification(
        rtl_source,
        language=lang,
        top_module=top_module.strip() or None,
        backend=backend_sel,
    )
    preview = build_test_preview(
        rtl_source,
        language=lang,
        top_module=top_module.strip() or None,
        backend=backend_sel,
    )
    report_path = result.work_dir / "report.txt"
    report_path.write_text(result.text_report, encoding="utf-8")

    waveform_json_data = None
    if result.vcd_path is not None:
        mod_info = load_module_info(result.work_dir)
        waveform_json_data = vcd_to_json(result.vcd_path, module_info=mod_info)
        if "error" in waveform_json_data:
            waveform_json_data = None

    verdict = getattr(result, "verdict", "pass" if result.success else "fail")
    v_mode = getattr(result, "verification_mode", "monitor_only")
    v_expl = getattr(result, "verification_mode_explanation", "")
    overall_pass = verdict == "pass" or bool(result.uvm_note)
    errors = getattr(result, "errors", []) or []

    return {
        "success": result.success,
        "verdict": verdict,
        "verification_mode": v_mode,
        "verification_mode_explanation": v_expl,
        "status": getattr(result, "status", verdict),
        "simulator": getattr(result, "simulator", ""),
        "backend_used": getattr(result, "backend_used", ""),
        "backend_version": getattr(result, "backend_version", ""),
        "module": result.module.name,
        "inferred_op": result.module.inferred_op,
        "language": result.language.value,
        "detected_language": getattr(result, "detected_language", result.language.value),
        "synth_synthesizable": getattr(result, "synth_synthesizable", None),
        "synth_skipped": getattr(result, "synth_skipped", False),
        "synth_tool": getattr(result, "synth_tool", ""),
        "synth_log": getattr(result, "synth_log", ""),
        "testbench": result.testbench,
        "sim_log": result.sim_log,
        "text_report": result.text_report,
        "waveform_text": result.waveform_text,
        "uvm_note": result.uvm_note,
        "work_dir": result.work_dir.as_posix(),
        "has_vcd": result.vcd_path is not None,
        "waveform_json": waveform_json_data,
        "preview": preview,
        "errors": errors,
        "test_results": [],
        "coverage": result.coverage.to_dict() if result.coverage else None,
    }


@app.post("/api/formal")
async def formal_check(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    top_module: str = Form(""),
    properties: str = Form("[]"),
    timeout_sec: int = Form(300),
    depth_override: int = Form(0),
    cross_check: bool = Form(True),
    blackbox_modules: str = Form(""),
    auto_blackbox: bool = Form(False),
):
    """Check one or more hand-written boolean properties with SymbiYosys.

    `properties` is a JSON list of {"name", "description", "expr", "kind"}
    objects, each using the DUT's own port names, e.g.
    [{"name": "prop0", "description": "light is never invalid",
      "expr": "light <= 1", "kind": "assert"}].
    `kind` is "assert" (default), "assume", or "cover".

    assume-kind properties never get their own PASS/FAIL run (an assume
    alone isn't a checkable claim) — they're bundled as extra `assume()`
    constraints into every assert/cover property's own wrapper instead,
    and echoed back separately as "assumed_constraints" for transparency.
    cover-kind properties run under mode="cover" (see formal_props.py —
    confirmed by testing that mode="prove" silently swallows a cover
    instead of checking reachability), so their verdict label is
    REACHED/UNREACHED, not PROVEN/FALSIFIED.

    Whenever any assume-kind properties are supplied, `assumption_consistency`
    in the response reports whether they're jointly satisfiable at all — a
    real `cover(1)` under every assumption together, not a heuristic (see
    src/rtl_verify/assumption_check.py). An over-constrained assumption set
    makes every PROVEN/UNREACHED verdict in the same run suspect, for the
    same reason a vacuous guard makes one assert's own PROVEN suspect — this
    catches the set-wide version of that failure mode, with the specific
    conflicting assumption(s) isolated automatically rather than left for
    manual bisection.

    `timeout_sec` is the wall-clock budget handed to sby itself (not just
    an external kill) — default 300s, override for harder proofs (a
    multiplier, a cache tag array) that legitimately need more time.
    `depth_override` (0 = use the heuristic in recommended_formal_config)
    lets a user force a specific BMC/cover depth when the auto heuristic
    isn't enough for a given design.

    A verdict of ERROR means the tool/expression itself broke — worth
    fixing. TIMEOUT/UNKNOWN mean the solver genuinely could not decide
    the property in the time/resources given — this is a real, expected
    outcome for hard designs, not a bug in this pipeline, and must never
    be shown as if it were PROVEN or FALSIFIED (confirmed against sby's
    own source that PDR specifically can report UNKNOWN when it can't
    converge — a real, reachable case, not hypothetical).

    `cross_check` (default True): after a definitive PASS/FAIL, re-run the
    same property against an independent second engine (a genuinely
    different algorithm/solver from `recommended_engine_chain()` — PDR,
    k-induction/yices, k-induction/z3 are all independent implementations)
    and compare. Agreement is real evidence the verdict isn't specific to
    one tool's own bugs (this project found two, in one session, in
    yosys's own SystemVerilog frontend); a disagreement is surfaced as its
    own field, never silently resolved by trusting either side. Roughly
    doubles solver time per property — set False to skip for speed.

    `blackbox_modules` (default "", comma-separated module names): for
    design-size reduction on real, larger designs — a named submodule's
    entire body is replaced by a stub of the same name/ports/parameters
    whose outputs are left genuinely undriven, so the solver treats them
    as free rather than reasoning about that submodule's actual internal
    logic at all (see src/rtl_verify/blackbox.py for why this matters and
    what was tried and rejected first). Only black-box a submodule whose
    internal computation the property genuinely doesn't depend on — its
    OUTPUTS becoming free can change or break a property that does.

    `auto_blackbox` (default False, ignored if `blackbox_modules` is
    already set manually): only when a property comes back genuinely
    inconclusive (TIMEOUT/UNKNOWN/CANCELLED) after the full engine chain
    — never for a real PROVEN/FALSIFIED verdict — automatically retries
    it with black-boxing candidates from `recommend_blackbox_candidates()`
    (see blackbox.py; ranked by researched, cited criteria: large memory/
    register arrays first, wide multiply/divide next, plain size as a
    last resort), one candidate at a time, stopping at the first
    definitive verdict. A verdict reached this way carries its own
    `auto_blackbox` field naming exactly what was tried and abstracted —
    a PROVEN result here is real but WEAKER than a full-whitebox PROVEN
    (it assumes the abstracted module's real behavior doesn't matter to
    this property), and a FALSIFIED result may be a black-box artifact
    (the abstracted module's now-free outputs triggering behavior
    impossible in the real design) rather than a genuine bug — both
    explicitly caveated, never silently presented as equivalent to an
    unabstracted verdict.

    Independent of /api/verify — this never touches pipeline.py or the
    simulator backends, only the formal backend from Phase 1.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    formal_engines = formal_backends()
    if not formal_engines:
        return {
            "error": (
                "No formal backend available. Install the OSS CAD Suite "
                "(SymbiYosys + Yosys): https://github.com/YosysHQ/oss-cad-suite-build"
            )
        }
    engine = formal_engines[0]

    try:
        props = json.loads(properties) if properties.strip() else []
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in properties: {e}"}
    if not isinstance(props, list):
        return {"error": "properties must be a JSON list"}

    try:
        probed_source, mod = _analyze_and_probe(rtl_source, top_module)
    except ValueError as e:
        return {"error": str(e)}

    blackbox_names = [n.strip() for n in blackbox_modules.split(",") if n.strip()]
    if blackbox_names:
        try:
            probed_source = generate_blackboxed_rtl(probed_source, blackbox_names)
        except ValueError as e:
            return {"error": f"Could not black-box {blackbox_names}: {e}"}

    base = Path(tempfile.mkdtemp(prefix="formal_api_"))
    ext = dut_source_extension(rtl_source, "systemverilog")
    rtl_path = base / f"dut{ext}"
    # Write the INSTRUMENTED (and, if requested, black-boxed) copy, not the
    # original rtl_source -- mod.ports (and therefore the wrapper generated
    # below) already expects the debug ports _analyze_and_probe() added,
    # the file on disk has to match, or the wrapper's instantiation would
    # fail to find those ports at all. The user's own original RTL is
    # never written to or modified anywhere. Black-boxing only replaces a
    # named SUBMODULE's own body with an undriven-output stub -- mod
    # (describing the TOP module's own ports) is unaffected by it, so no
    # separate re-analysis is needed here.
    rtl_path.write_text(probed_source, encoding="utf-8")

    assume_props = []
    assumed_constraints = []
    target_props = []
    for i, p in enumerate(props):
        name = str(p.get("name") or f"prop{i}")
        expr = str(p.get("expr") or "").strip()
        kind = str(p.get("kind") or "assert")
        entry = {
            "name": name,
            "description": str(p.get("description") or ""),
            "expr": expr,
            "kind": kind,
            "paired_cover": str(p.get("paired_cover") or ""),
            "rationale": str(p.get("rationale") or ""),
        }
        if kind == "assume":
            if expr:
                assume_props.append((name, expr, "assume"))
            assumed_constraints.append(entry)
        else:
            target_props.append((i, entry))

    # Assumption consistency ("over-constraint") check: is there any
    # legal input scenario at all once every supplied assumption is
    # applied together? Runs ONCE per call (assumptions are shared
    # across every property in this run, not per-property), before any
    # property is actually checked -- an over-constrained assumption set
    # makes every PROVEN/UNREACHED verdict below suspect for the same
    # reason a single vacuous guard makes one assert's PROVEN suspect
    # (see assumption_check.py's own module docstring for the researched
    # citation trail). A real solver call, not a heuristic; skipped
    # entirely (no extra solver time spent) when no assumptions were
    # submitted.
    assumption_consistency = check_assumption_consistency(
        mod, rtl_path, engine, assume_props,
        timeout_sec=max(60, timeout_sec // max(1, len(target_props) or 1)),
        depth_override=depth_override,
        work_root=base / "assumption_consistency",
    )

    # Regression detection: has this module's RTL changed since the last
    # time it was checked? If so, automatically re-run every property this
    # project has ever recorded a verdict for -- not just what THIS call
    # submitted -- so editing one part of a design surfaces a break in a
    # property nobody thought to re-test. Hashed on the ORIGINAL rtl_source
    # (before dut_probe.py's instrumentation), so this tracks the user's
    # actual RTL, not incidental changes to the instrumentation logic.
    rtl_hash = regression.compute_rtl_hash(rtl_source)
    baseline = regression.load_baseline(mod.name)
    already_named = {entry["name"] for _, entry in target_props}
    auto_added_names: set[str] = set()
    if baseline is not None and baseline.rtl_hash != rtl_hash:
        for bp in baseline.properties:
            if bp.name in already_named:
                continue  # this call already resubmitted it -- don't run it twice
            next_i = len(props) + len(auto_added_names)
            target_props.append((next_i, {
                "name": bp.name, "description": "[auto re-run: regression check]",
                "expr": bp.expr, "kind": bp.kind, "paired_cover": "", "rationale": "",
            }))
            auto_added_names.add(bp.name)

    if not target_props:
        if baseline is None:
            return {"error": "properties must be a non-empty JSON list (no prior baseline exists for this module to fall back on)"}
        return {
            "module": mod.name,
            "success": True,
            "properties": [],
            "assumed_constraints": assumed_constraints,
            "regression_report": {
                "baseline_existed": True, "rtl_changed": False,
                "checked_count": 0, "regressions": [], "fixed": [], "unchanged_count": 0,
            },
            "note": "RTL unchanged since the last recorded baseline and no new properties submitted — nothing to check.",
        }

    # Only ERROR (a genuine tool/compile problem — bad syntax, missing
    # signal) is worth an LLM-driven expression fix. TIMEOUT/UNKNOWN mean
    # the solver ran without incident but couldn't reach a verdict in the
    # time/resources it had — rewriting the expression's syntax can't fix
    # that, so retrying would just burn another run for nothing.
    _ERROR_STATUSES = {"ERROR", None}
    _INCONCLUSIVE_STATUSES = {"TIMEOUT", "UNKNOWN", "CANCELLED"}
    MAX_RETRIES = 1

    def _run_chain(work: Path, expr_to_run: str, name: str, kind: str, run_rtl_path: Path = rtl_path):
        """Walk recommended_engine_chain(), stopping at the first PASS/FAIL.

        Every non-definitive status (ERROR/TIMEOUT/UNKNOWN/CANCELLED) is
        treated as a reason to try the next engine, not just the "expected"
        inconclusive ones — a solver-specific ERROR on attempt 1 is often
        just that, solver-specific, and a different engine may run it fine.
        Only once the whole chain is exhausted does the final attempt's
        status get used to decide anything (e.g. whether an LLM syntax-fix
        retry is worth it).

        timeout_sec is split evenly across the chain's rungs rather than
        given in full to each attempt — otherwise the documented "wall-clock
        budget handed to sby" would silently balloon to N times what the
        caller asked for. Each rung still gets at least 30s.

        `run_rtl_path` defaults to the manually-black-boxed (or plain)
        `rtl_path` written once above, but the auto-black-box escalation
        path below passes a DIFFERENT, per-candidate black-boxed copy —
        `mod` (the top module's own ports) is unaffected either way, since
        black-boxing only ever replaces a named SUBMODULE's body.
        """
        wrapper_sv = generate_formal_wrapper(mod, assume_props + [(name, expr_to_run, kind)])
        chain = recommended_engine_chain(mod, kind=kind, depth_override=depth_override)
        per_attempt_timeout = max(30, timeout_sec // len(chain))
        work.mkdir(parents=True, exist_ok=True)
        wrapper_path = work / "wrapper.sv"
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")

        attempts = []
        result = None
        config = chain[0]
        for i, config in enumerate(chain):
            attempt_dir = work / f"engine_{i}"
            result = engine.run(
                run_rtl_path, wrapper_path, attempt_dir,
                top=f"{mod.name}_formal_top",
                depth=config["depth"], mode=config["mode"], engine=config["engine"],
                timeout_sec=per_attempt_timeout,
            )
            attempts.append({"label": config["label"], "mode": config["mode"],
                              "engine": config["engine"], "status": result.status})
            if result.status in ("PASS", "FAIL"):
                break
        return result, config, attempts

    results = []
    for i, entry in target_props:
        name, expr, kind = entry["name"], entry["expr"], entry["kind"]
        if not expr:
            results.append({**entry, "success": False, "error": "empty expression"})
            continue

        current_expr = expr
        attempt = 0
        retry_note = None
        all_attempts = []
        try:
            result, config, chain_attempts = _run_chain(base / f"prop_{i}", current_expr, name, kind)
            all_attempts.extend(chain_attempts)
        except ValueError as e:
            results.append({**entry, "success": False, "error": str(e)})
            continue

        # A tool-level ERROR (bad syntax, not a real proof result) is worth
        # one automatic fix attempt — feed the concrete error back to the
        # same conversion model and ask it to correct the expression,
        # rather than surfacing a raw compiler error to the user for
        # something the tool could plausibly self-correct. TIMEOUT/UNKNOWN
        # are deliberately excluded — no syntax fix resolves "the solver
        # didn't finish in time" or "the engine gave up," so retrying would
        # just spend another full run for nothing.
        while result.status in _ERROR_STATUSES and attempt < MAX_RETRIES:
            attempt += 1
            try:
                fix = convert_to_sva_retry(
                    mod, kind, entry["description"], entry.get("rationale", ""),
                    current_expr, result.log,
                )
            except LLMNotConfigured:
                break
            except Exception:  # noqa: BLE001 — a failed retry just stops retrying
                break
            if not fix.get("expressible") or not fix.get("expr", "").strip():
                retry_note = fix.get("note") or "Automatic fix attempt declined to retry."
                break
            new_expr = fix["expr"].strip()
            if new_expr == current_expr:
                retry_note = "Automatic fix attempt returned the same expression — stopping."
                break
            current_expr = new_expr
            try:
                result, config, chain_attempts = _run_chain(
                    base / f"prop_{i}_retry{attempt}", current_expr, name, kind
                )
                all_attempts.extend(chain_attempts)
            except ValueError as e:
                retry_note = f"Retry {attempt} failed to build a wrapper: {e}"
                break
            retry_note = f"Auto-fixed after a tool error — retried expression: {current_expr}"

        waveform_json_data = None
        if result.vcd_path is not None:
            waveform_json_data = vcd_to_json(result.vcd_path, module=mod)
            if "error" in waveform_json_data:
                waveform_json_data = None

        # ERROR/TIMEOUT/UNKNOWN/CANCELLED must never collapse into
        # PROVEN/FALSIFIED/REACHED/UNREACHED — those are legitimate proof
        # results; these mean the solver didn't produce one at all, for
        # three different reasons a user needs to tell apart (a bug in the
        # expression vs. ran out of time vs. the engine itself gave up).
        if result.status in _ERROR_STATUSES:
            verdict = "ERROR"
        elif result.status in _INCONCLUSIVE_STATUSES:
            verdict = result.status
        elif kind == "cover":
            verdict = "REACHED" if result.success else "UNREACHED"
        else:
            verdict = "PROVEN" if result.success else "FALSIFIED"

        # Auto-black-box escalation: only for a genuinely inconclusive
        # verdict, only when the caller opted in, and only when they
        # haven't already picked their own black-box set manually (mixing
        # the two would make it unclear which module caused which effect).
        # Never fires for ERROR (a syntax fix, not an abstraction, is the
        # right response) or for a real PROVEN/FALSIFIED/REACHED/UNREACHED
        # — this can only ever act on "the solver didn't decide," never
        # override a decision the solver actually reached.
        auto_blackbox_info = None
        if verdict in _INCONCLUSIVE_STATUSES and auto_blackbox and not blackbox_names:
            candidates = recommend_blackbox_candidates(mod, rtl_source)
            tried = []
            for cand in candidates[:3]:
                try:
                    bb_source = generate_blackboxed_rtl(probed_source, [cand.module_name])
                except ValueError as e:
                    tried.append({"module": cand.module_name, "reason": cand.reason,
                                  "status": "SKIPPED", "detail": str(e)})
                    continue
                bb_dir = base / f"prop_{i}_autobb_{cand.module_name}"
                bb_dir.mkdir(parents=True, exist_ok=True)
                bb_rtl_path = bb_dir / rtl_path.name
                bb_rtl_path.write_text(bb_source, encoding="utf-8")
                bb_result, bb_config, bb_attempts = _run_chain(
                    bb_dir / "run", current_expr, name, kind, run_rtl_path=bb_rtl_path,
                )
                tried.append({
                    "module": cand.module_name, "reason": cand.reason, "detail": cand.detail,
                    "status": bb_result.status,
                })
                if bb_result.status in ("PASS", "FAIL"):
                    result, config = bb_result, bb_config
                    all_attempts.extend(bb_attempts)
                    if kind == "cover":
                        verdict = "REACHED" if result.success else "UNREACHED"
                    else:
                        verdict = "PROVEN" if result.success else "FALSIFIED"
                    waveform_json_data = None
                    if result.vcd_path is not None:
                        waveform_json_data = vcd_to_json(result.vcd_path, module=mod)
                        if "error" in waveform_json_data:
                            waveform_json_data = None
                    auto_blackbox_info = {
                        "resolved": True, "blackboxed_module": cand.module_name,
                        "reason": cand.reason, "candidates_tried": tried,
                        "caveat": (
                            f"This {verdict} verdict was only reached after black-boxing "
                            f"'{cand.module_name}' (its outputs are treated as free/"
                            "unconstrained, not its real logic). If PROVEN: real, but weaker "
                            "than a full-whitebox proof — it assumes this property doesn't "
                            "actually depend on what that module computes. If FALSIFIED: the "
                            "counterexample may be a black-box artifact — the abstracted "
                            "module's now-free outputs triggering behavior impossible in the "
                            "real design — verify the trace against that module's real logic "
                            "before treating this as a confirmed bug."
                        ),
                    }
                    break
            if auto_blackbox_info is None:
                auto_blackbox_info = {
                    "resolved": False, "blackboxed_module": None, "reason": None,
                    "candidates_tried": tried,
                    "caveat": (
                        "Auto-black-box escalation was attempted but no candidate resolved "
                        "this to a definitive verdict — the original inconclusive result "
                        "stands." if tried else
                        "Auto-black-box escalation found no directly-instantiated, locally-"
                        "defined submodule to try — the original inconclusive result stands."
                    ),
                }

        entry_out = {
            **entry,
            "expr": current_expr,
            "success": result.success,
            "verdict": verdict,
            "config": config,
            "engine_label": config.get("label", config.get("engine", "")),
            "attempts": all_attempts,
            "log": result.log[-4000:],
            "has_trace": result.vcd_path is not None,
            "waveform_json": waveform_json_data,
            "retried": attempt > 0,
            "retry_note": retry_note,
            "auto_blackbox": auto_blackbox_info,
        }

        # Independent second-engine cross-check: only meaningful once a
        # definitive verdict exists (never for ERROR/TIMEOUT/UNKNOWN, which
        # already say the primary run itself didn't produce an answer), and
        # never after an auto-black-box escalation resolved it — that
        # verdict belongs to a DIFFERENT design (one submodule stubbed out)
        # than the plain `rtl_path` this cross-check would re-run against,
        # so comparing them wouldn't mean what cross_check's own contract
        # assumes (the same design, a second engine). Skipped explicitly,
        # not silently, with a note explaining why.
        if auto_blackbox_info is not None and auto_blackbox_info["resolved"]:
            entry_out["cross_check"] = {
                "performed": False, "engine_label": None, "status": None, "agrees": None,
                "note": "Skipped: this verdict came from an auto-black-boxed design, not the "
                        "plain RTL — cross-checking against a different design wouldn't be a "
                        "valid second opinion on the same result.",
            }
        elif cross_check and result.status in ("PASS", "FAIL"):
            cc = cross_check_property(
                mod, rtl_path, engine, name, current_expr, kind,
                primary_success=result.success,
                primary_engine_label=entry_out["engine_label"],
                timeout_sec=max(60, timeout_sec // max(1, len(target_props))),
                depth_override=depth_override,
                work_root=base / f"prop_{i}_crosscheck",
            )
            entry_out["cross_check"] = {
                "performed": cc.performed,
                "engine_label": cc.engine_label,
                "status": cc.status,
                "agrees": cc.agrees,
                "note": cc.note,
            }

        results.append(entry_out)

    # Vacuity confidence: an assert can be PROVEN only because its own
    # triggering condition never occurs, which "proves" nothing useful.
    # Every PROVEN assert gets a "confidence" verdict here — not just the
    # LLM-suggested ones that happen to carry a hand-written paired_cover
    # (property_suggester.py). Two tiers, cheapest first:
    #  1. If a paired_cover was supplied AND already REACHED in this same
    #     run, that's real evidence — use it, no extra solver call needed.
    #  2. Otherwise, fall back to vacuity.py's automatic check: extract the
    #     guard from the property's own !(guard) || (...) shape (this
    #     project's standard form — see formal_props.py's helpers and
    #     property_to_sva.py's own "write if-then as !A || B" convention)
    #     and run a REAL cover(guard) through the solver. Every PROVEN
    #     assert gets a definite confidence field either way: NON_VACUOUS,
    #     VACUOUS, UNKNOWN (solver couldn't decide), or NOT_APPLICABLE
    #     (the property isn't in the recognized shape) — never silently
    #     skipped.
    verdict_by_name = {r["name"]: r for r in results}
    for i, r in enumerate(results):
        if r["kind"] != "assert" or r.get("verdict") != "PROVEN":
            continue

        cover = verdict_by_name.get(r.get("paired_cover")) if r.get("paired_cover") else None
        if cover is not None and cover.get("kind") == "cover" and cover.get("verdict") == "REACHED":
            r["confidence"] = {
                "checked": True,
                "method": "paired_cover",
                "guard_expr": None,
                "status": "NON_VACUOUS",
                "note": f"Confirmed via paired cover \"{r['paired_cover']}\", which REACHED.",
            }
            continue

        if cover is not None and cover.get("kind") == "cover" and cover.get("verdict") != "REACHED":
            r["vacuity_warning"] = (
                f"Paired cover \"{r['paired_cover']}\" is {cover.get('verdict', 'not resolved')} "
                "(not REACHED) — falling back to the automatic guard-reachability check."
            )

        try:
            vac = run_vacuity_check(
                mod, rtl_path, engine, r["name"], r["expr"],
                timeout_sec=max(60, timeout_sec // max(1, len(target_props))),
                depth_override=depth_override,
                work_root=base / f"prop_{i}_vacuity",
            )
            r["confidence"] = {
                "checked": vac.checked,
                "method": vac.method,
                "guard_expr": vac.guard_expr,
                "status": vac.status,
                "note": vac.note,
            }
        except Exception as e:  # noqa: BLE001 — a failed vacuity check must not break the main result
            r["confidence"] = {
                "checked": False, "method": None, "guard_expr": None,
                "status": "ERROR", "note": f"Vacuity check itself failed: {e}",
            }

    # Regression report: diff every baseline property that got (re)run this
    # call against its last recorded verdict, then persist the new
    # baseline -- last-known verdicts for properties untouched this call
    # carry forward unchanged, everything actually run this call gets its
    # fresh verdict recorded.
    rerun_results = {r["name"]: r.get("verdict", "ERROR") for r in results}
    reg_report = regression.diff_against_baseline(baseline, rtl_hash, rerun_results)
    merged: dict[str, BaselineProperty] = {
        p.name: p for p in (baseline.properties if baseline else [])
    }
    for r in results:
        merged[r["name"]] = BaselineProperty(
            name=r["name"], expr=r.get("expr", ""), kind=r["kind"],
            verdict=r.get("verdict", "ERROR"),
        )
    regression.save_baseline(mod.name, rtl_hash, list(merged.values()))

    def _entry_dict(e) -> dict:
        return {"name": e.name, "expr": e.expr, "kind": e.kind,
                "old_verdict": e.old_verdict, "new_verdict": e.new_verdict}

    regression_report = {
        "baseline_existed": reg_report.baseline_existed,
        "rtl_changed": reg_report.rtl_changed,
        "checked_count": len(reg_report.checked),
        "regressions": [_entry_dict(e) for e in reg_report.regressions],
        "fixed": [_entry_dict(e) for e in reg_report.fixed],
        "unchanged_count": len(reg_report.unchanged),
    }

    verdict_counts: dict[str, int] = {}
    for r in results:
        verdict_counts[r.get("verdict", "ERROR")] = verdict_counts.get(r.get("verdict", "ERROR"), 0) + 1
    formal_log.log_event("run", {
        "module": mod.name,
        "engine": engine.display_name,
        "num_properties": len(results),
        "num_assumed": len(assumed_constraints),
        "verdict_counts": verdict_counts,
        "vacuity_warnings": sum(1 for r in results if r.get("vacuity_warning")),
        "vacuous_properties": sum(1 for r in results if r.get("confidence", {}).get("status") == "VACUOUS"),
        "assumption_over_constrained": assumption_consistency.status == "OVER_CONSTRAINED",
        "cross_checks_performed": sum(1 for r in results if r.get("cross_check", {}).get("performed")),
        "cross_check_disagreements": sum(1 for r in results if r.get("cross_check", {}).get("agrees") is False),
        "retries": sum(1 for r in results if r.get("retried")),
        "regressions_found": len(reg_report.regressions),
        "success": all(r.get("success") for r in results),
    })

    return {
        "module": mod.name,
        "engine": engine.display_name,
        "engine_version": engine.version(),
        "success": all(r.get("success") for r in results),
        "properties": results,
        "assumed_constraints": assumed_constraints,
        "assumption_consistency": {
            "checked": assumption_consistency.checked,
            "status": assumption_consistency.status,
            "note": assumption_consistency.note,
            "minimal_conflicting_set": assumption_consistency.minimal_conflicting_set,
        },
        "work_dir": base.as_posix(),
        "regression_report": regression_report,
    }


@app.post("/api/formal/mutation_adequacy")
async def formal_mutation_adequacy(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    top_module: str = Form(""),
    properties: str = Form("[]"),
    timeout_sec: int = Form(300),
    depth_override: int = Form(0),
    max_mutants: int = Form(20),
):
    """"How many properties is enough, and are they actually any good?" --
    a real, measured answer via RTL mutation testing (researched from
    YosysHQ's own published MCY methodology; see src/rtl_verify/mutate.py
    and mutation_adequacy.py for the full citation trail and the
    deliberate simplifications versus the full MCY tool).

    `properties` is the same shape as `/api/formal`'s (assert-kind only —
    assume/cover entries are ignored here). Each is FIRST re-confirmed
    PROVEN on the real, unmutated design via the same engine/solver
    fallback chain `/api/formal` uses — scoring mutation coverage against
    a property that doesn't even hold on the real design is meaningless,
    so anything that isn't PROVEN here is excluded from scoring, with the
    reason reported in `baseline`, not silently dropped.

    Only the confirmed-PROVEN subset is then scored against up to
    `max_mutants` single-operator mutations (relational/logical/bitwise/
    arithmetic) of the target module's own body — see mutate.py for
    exactly which operators and why `<=`/`>=` are deliberately excluded
    as mutation sources (Verilog's nonblocking-assignment ambiguity).
    Each mutant is tried against only the FASTEST engine in
    `recommended_engine_chain()`, not the full fallback chain every other
    property check in this project uses — a deliberate cost bound, since
    mutation testing already multiplies solver runs by the mutant count.

    A `NOT_CAUGHT` mutant may be a real, honest gap in the property set,
    or it may be a behaviorally-equivalent change this endpoint has no
    netlist-level equivalence-checking layer to rule out (unlike the
    real MCY tool) — every result says so explicitly rather than
    implying more precision than it has.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    formal_engines = formal_backends()
    if not formal_engines:
        return {"error": "No formal backend available. Install the OSS CAD Suite."}
    engine = formal_engines[0]

    try:
        props = json.loads(properties) if properties.strip() else []
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in properties: {e}"}
    if not isinstance(props, list):
        return {"error": "properties must be a JSON list"}

    try:
        probed_source, mod = _analyze_and_probe(rtl_source, top_module)
    except ValueError as e:
        return {"error": str(e)}

    base = Path(tempfile.mkdtemp(prefix="mutation_adequacy_api_"))
    ext = dut_source_extension(rtl_source, "systemverilog")
    rtl_path = base / f"dut{ext}"
    rtl_path.write_text(probed_source, encoding="utf-8")

    assume_props = []
    candidate_asserts = []
    for i, p in enumerate(props):
        name = str(p.get("name") or f"prop{i}")
        expr = str(p.get("expr") or "").strip()
        kind = str(p.get("kind") or "assert")
        if not expr:
            continue
        if kind == "assume":
            assume_props.append((name, expr, "assume"))
        elif kind == "assert":
            candidate_asserts.append((name, expr))

    if not candidate_asserts:
        return {"error": "properties must include at least one assert-kind property with a non-empty expr"}

    chain = recommended_engine_chain(mod, kind="assert", depth_override=depth_override)
    per_attempt_timeout = max(30, timeout_sec // max(1, len(candidate_asserts)) // len(chain))

    baseline = []
    proven_properties: list[tuple[str, str, str]] = []
    for name, expr in candidate_asserts:
        try:
            wrapper_sv = generate_formal_wrapper(mod, assume_props + [(name, expr, "assert")])
        except ValueError as e:
            baseline.append({"name": name, "verdict": "ERROR", "note": str(e)})
            continue
        work = base / f"baseline_{name}"
        result = None
        for i, config in enumerate(chain):
            attempt_dir = work / f"engine_{i}"
            wrapper_path = attempt_dir / "wrapper.sv"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            wrapper_path.write_text(wrapper_sv, encoding="utf-8")
            result = engine.run(
                rtl_path, wrapper_path, attempt_dir,
                top=f"{mod.name}_formal_top",
                depth=config["depth"], mode=config["mode"], engine=config["engine"],
                timeout_sec=per_attempt_timeout,
            )
            if result.status in ("PASS", "FAIL"):
                break
        if result is not None and result.status == "PASS":
            baseline.append({"name": name, "verdict": "PROVEN"})
            proven_properties.append((name, expr, "assert"))
        else:
            status = result.status if result is not None else "ERROR"
            baseline.append({
                "name": name, "verdict": status,
                "note": "Excluded from mutation scoring -- only properties confirmed PROVEN on "
                        "the real, unmutated design are meaningful to score mutation coverage "
                        "against.",
            })

    report = run_mutation_adequacy(
        mod, probed_source, proven_properties, assume_props, engine,
        max_mutants=max_mutants, per_attempt_timeout_sec=max(15, per_attempt_timeout),
        depth_override=depth_override, work_root=base / "mutants",
    )

    return {
        "module": mod.name,
        "baseline": baseline,
        "adequacy": {
            "checked": report.checked,
            "total_mutants": report.total_mutants,
            "caught": report.caught,
            "not_caught": report.not_caught,
            "inconclusive": report.inconclusive,
            "kill_rate": report.kill_rate,
            "note": report.note,
            "mutants": [
                {
                    "id": m.mutant_id, "operator": m.operator, "location": m.location,
                    "verdict": m.verdict, "caught_by": m.caught_by, "detail": m.detail,
                }
                for m in report.mutants
            ],
        },
        "work_dir": base.as_posix(),
    }


@app.post("/api/formal/suggest")
async def formal_suggest(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    spec_file: UploadFile | None = File(None),
    spec_text: str = Form(""),
    top_module: str = Form(""),
):
    """LLM-assisted property suggestion — the actual Phase 2 gap: reads
    RTL (+ optional spec) and proposes properties in plain English, each
    classified as assert/assume/cover, grounded in
    docs/formal_property_reference.md. Nothing here compiles to SVA or
    runs through the solver — that's /api/formal/convert and /api/formal.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    if spec_file and spec_file.filename:
        spec = (await spec_file.read()).decode("utf-8", errors="replace")
    else:
        spec = spec_text

    try:
        probed_source, mod = _analyze_and_probe(rtl_source, top_module)
    except ValueError as e:
        return {"error": str(e)}

    try:
        # Show the LLM the instrumented source, not the original -- the
        # `assign __dbg_x = x;` lines it adds are self-explanatory context
        # for what the extra ports mean, without needing separate prose.
        proposals = suggest_properties(mod, probed_source, spec_text=spec)
    except LLMNotConfigured as e:
        formal_log.log_event("suggest", {"module": mod.name, "error": str(e)})
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface any LLM/API failure to the UI, don't 500
        formal_log.log_event("suggest", {"module": mod.name, "error": str(e)})
        return {"error": f"Property suggestion failed: {e}"}

    kind_counts: dict[str, int] = {}
    for p in proposals:
        kind_counts[p.get("kind", "?")] = kind_counts.get(p.get("kind", "?"), 0) + 1
    formal_log.log_event("suggest", {
        "module": mod.name,
        "rtl_lines": len(rtl_source.splitlines()),
        "spec_provided": bool(spec.strip()),
        "num_proposed": len(proposals),
        "kind_counts": kind_counts,
    })

    return {"module": mod.name, "properties": proposals}


@app.post("/api/formal/convert")
async def formal_convert(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    top_module: str = Form(""),
    properties: str = Form("[]"),
):
    """Convert approved plain-English properties to SVA — the deliberately
    simpler half of Phase 2. Still returns expressible=false rather than a
    guessed expression when a property genuinely needs syntax this tool
    doesn't support yet (see property_to_sva.py).

    `properties` is a JSON list of {"name", "kind", "description", "rationale"}.
    Returns the same list with "expressible", "expr", "note" added to each.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    try:
        props = json.loads(properties) if properties.strip() else []
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in properties: {e}"}
    if not isinstance(props, list) or not props:
        return {"error": "properties must be a non-empty JSON list"}

    try:
        _probed_source, mod = _analyze_and_probe(rtl_source, top_module)
    except ValueError as e:
        return {"error": str(e)}

    out = []
    for p in props:
        kind = str(p.get("kind") or "assert")
        description = str(p.get("description") or "")
        rationale = str(p.get("rationale") or "")
        try:
            conv = convert_to_sva(mod, kind, description, rationale)
        except LLMNotConfigured as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            conv = {"expressible": False, "expr": "", "note": f"Conversion failed: {e}"}
        out.append({**p, **conv})

    formal_log.log_event("convert", {
        "module": mod.name,
        "num_properties": len(out),
        "num_expressible": sum(1 for c in out if c.get("expressible")),
    })

    return {"module": mod.name, "properties": out}


@app.get("/api/formal/history")
async def formal_history(limit: int = 30):
    """Recent formal-tab attempts (suggest/convert/run), most recent first.

    Persisted to logs/formal_runs.jsonl (see formal_log.py) so this survives
    across sessions, not just the current browser tab.
    """
    return {"events": formal_log.read_recent(limit=limit)}


@app.get("/api/waveform/json")
async def waveform_json(work_dir: str):
    """Return structured waveform data for visual rendering."""
    base = Path(work_dir)
    vcd = base / "sim.vcd"
    if not vcd.is_file():
        return {"error": "No waveform available"}
    mod_info = load_module_info(base)
    return vcd_to_json(vcd, module_info=mod_info)


@app.get("/api/download/vcd")
async def download_vcd(work_dir: str):
    path = Path(work_dir) / "sim.vcd"
    if not path.is_file():
        return PlainTextResponse("VCD not found", status_code=404)
    return FileResponse(path, filename="sim.vcd", media_type="application/octet-stream")


@app.get("/api/download/report")
async def download_report(work_dir: str):
    path = Path(work_dir) / "report.txt"
    if not path.is_file():
        return PlainTextResponse("Report not found", status_code=404)
    return FileResponse(path, filename="report.txt", media_type="text/plain")


@app.get("/api/coverage")
async def coverage(work_dir: str):
    """Return coverage report JSON from a completed run."""
    path = Path(work_dir) / "coverage.json"
    if not path.is_file():
        return {"error": "coverage.json not found — run verification first"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"Failed to read coverage.json: {e}"}


@app.post("/api/coverage/close")
async def coverage_close(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    top_module: str = Form(""),
    backend: str = Form(""),
    max_iterations: int = Form(5),
    target_percent: float = Form(95.0),
):
    """Phase 3: agentic coverage-closure loop. Reads coverage gaps left by
    the previous round (none on round 1), asks an LLM for new directed
    input vectors targeting them, regenerates a directed testbench with
    every vector proposed so far, reruns the simulator, and recomputes
    coverage — repeating until the target is reached, two rounds pass with
    negligible improvement, or max_iterations is hit.

    Independent of /api/verify — uses its own minimal directed-stimulus
    testbench generator (coverage_closure.py) rather than the default
    pipeline's formulaic stimulus.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    try:
        result = run_closure_loop(
            rtl_source,
            top_module=top_module.strip() or None,
            backend_name=backend.strip() or None,
            max_iterations=max(1, min(20, max_iterations)),
            target_percent=target_percent,
        )
    except ValueError as e:
        return {"error": str(e)}
    return result


@app.get("/api/coverage/close/history")
async def coverage_close_history(limit: int = 30):
    """Recent coverage-closure rounds, most recent first — persisted to
    logs/coverage_closure.jsonl so it survives across sessions.
    """
    return {"events": coverage_closure.read_recent(limit=limit)}


@app.post("/api/traceability")
async def traceability(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    spec_file: UploadFile | None = File(None),
    spec_text: str = Form(""),
    top_module: str = Form(""),
    properties_text: str = Form(""),
):
    """Phase 4: spec -> verification-plan traceability. Extracts atomic
    requirements from the spec, then checks each against this design's
    test-plan cases and any already-verified formal properties (pasted in
    plain English, one per line) — flagging any requirement with no
    genuine match instead of a false "covered".
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    if spec_file and spec_file.filename:
        spec = (await spec_file.read()).decode("utf-8", errors="replace")
    else:
        spec = spec_text
    if not spec.strip():
        return {"error": "Provide spec_file or spec_text"}

    try:
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    except ValueError as e:
        return {"error": str(e)}

    plan = None
    try:
        plan = build_vplan(rtl_source, mod)
    except Exception:  # noqa: BLE001 — traceability against properties alone still works
        plan = None

    try:
        result = build_traceability_matrix(spec, rtl_source, mod, plan, properties_text=properties_text)
    except LLMNotConfigured as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface any LLM failure, don't 500
        return {"error": f"Traceability build failed: {e}"}
    return result


@app.post("/api/cdc")
async def cdc_check(
    rtl_file: UploadFile | None = File(None),
    rtl_text: str = Form(""),
    top_module: str = Form(""),
):
    """Clock-domain-crossing (CDC) and reset-domain-crossing (RDC) check --
    a static structural scan, not a formal proof (see cdc_check.py's own
    docstring for exactly what that does and doesn't claim). Runs no
    solver, so this is fast and needs no formal backend.

    Also traces one level of instantiation: a crossing synchronized by
    INSTANTIATING a reusable synchronizer submodule (the idiomatic way
    real engineers write this, not hand-inlined flip-flops) is resolved
    if that submodule is defined in the same submitted RTL text, with
    the capture depth traced inside the submodule's own body.

    Returns: `domains` (clock signal -> register count), `crossings`
    (every signal referenced across a clock-domain boundary, with a
    best-effort synchronizer-depth verdict: NON_VACUOUS-style
    UNSYNCHRONIZED / WEAK / LIKELY_OK), and `reset_signals` (every
    async-reset trigger, classified as a primary input, a registered/
    synchronized signal, or risky combinational logic). A single-clock
    design correctly reports zero crossings — that's not a limitation,
    it's the honest answer when there's nothing to cross.
    """
    if rtl_file and rtl_file.filename:
        rtl_source = (await rtl_file.read()).decode("utf-8", errors="replace")
    elif rtl_text.strip():
        rtl_source = rtl_text
    else:
        return {"error": "Provide rtl_file or rtl_text"}

    try:
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    except ValueError as e:
        return {"error": str(e)}

    report = analyze_cdc(mod, rtl_source)
    return {
        "module": mod.name,
        "domains": {
            name: {"clock_signal": dom.clock_signal, "register_count": len(dom.registers)}
            for name, dom in report.domains.items()
        },
        "crossings": [
            {
                "signal": c.signal, "source_domain": c.source_domain, "dest_domain": c.dest_domain,
                "width": c.width, "sync_depth": c.sync_depth, "verdict": c.verdict, "note": c.note,
            }
            for c in report.crossings
        ],
        "reset_signals": [
            {"name": r.name, "kind": r.kind, "verdict": r.verdict, "note": r.note}
            for r in report.reset_signals
        ],
        "unsynchronized_count": len(report.unsynchronized_crossings),
    }


@app.post("/api/chat")
async def chat(
    work_dir: str = Form(...),
    question: str = Form(...),
    history: str = Form("[]"),
):
    """Phase 5: failure triage / waveform chat. Scoped to one run's
    work_dir — packages that run's RTL, report, and waveform transitions
    as context and answers a question about it, citing specific RTL
    lines and/or signal/timestamp pairs rather than answering vaguely.

    `history` is a JSON list of prior {"question","answer"} pairs from
    this same run's chat, for follow-up questions.
    """
    base = Path(work_dir)
    if not base.is_dir():
        return {"error": "work_dir not found — this run's files may have been cleaned up"}
    if not question.strip():
        return {"error": "Ask a question"}

    try:
        turns = json.loads(history) if history.strip() else []
    except json.JSONDecodeError:
        turns = []
    if not isinstance(turns, list):
        turns = []

    try:
        answer = answer_question(base, question, history=turns)
    except LLMNotConfigured as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface any LLM failure, don't 500
        return {"error": f"Chat failed: {e}"}
    return {"answer": answer}
