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
)
from rtl_verify.property_suggester import suggest_properties  # noqa: E402
from rtl_verify.property_to_sva import convert_to_sva  # noqa: E402
from rtl_verify.llm_client import LLMNotConfigured  # noqa: E402

app = FastAPI(title="RTL Verify Automation", version="0.1.0")
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
    if not isinstance(props, list) or not props:
        return {"error": "properties must be a non-empty JSON list"}

    try:
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    except ValueError as e:
        return {"error": str(e)}

    base = Path(tempfile.mkdtemp(prefix="formal_api_"))
    ext = dut_source_extension(rtl_source, "systemverilog")
    rtl_path = base / f"dut{ext}"
    rtl_path.write_text(rtl_source, encoding="utf-8")

    assume_props = []
    assumed_constraints = []
    target_props = []
    for i, p in enumerate(props):
        name = str(p.get("name") or f"prop{i}")
        expr = str(p.get("expr") or "").strip()
        kind = str(p.get("kind") or "assert")
        entry = {"name": name, "description": str(p.get("description") or ""), "expr": expr, "kind": kind}
        if kind == "assume":
            if expr:
                assume_props.append((name, expr, "assume"))
            assumed_constraints.append(entry)
        else:
            target_props.append((i, entry))

    results = []
    for i, entry in target_props:
        name, expr, kind = entry["name"], entry["expr"], entry["kind"]
        if not expr:
            results.append({**entry, "success": False, "error": "empty expression"})
            continue
        try:
            wrapper_sv = generate_formal_wrapper(mod, assume_props + [(name, expr, kind)])
        except ValueError as e:
            results.append({**entry, "success": False, "error": str(e)})
            continue

        config = recommended_formal_config(mod, kind=kind)
        work = base / f"prop_{i}"
        wrapper_path = work / "wrapper.sv"
        work.mkdir(parents=True, exist_ok=True)
        wrapper_path.write_text(wrapper_sv, encoding="utf-8")

        result = engine.run(
            rtl_path, wrapper_path, work,
            top=f"{mod.name}_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
        )

        waveform_json_data = None
        if result.vcd_path is not None:
            waveform_json_data = vcd_to_json(result.vcd_path, module=mod)
            if "error" in waveform_json_data:
                waveform_json_data = None

        if kind == "cover":
            verdict = "REACHED" if result.success else "UNREACHED"
        else:
            verdict = "PROVEN" if result.success else "FALSIFIED"

        results.append({
            **entry,
            "success": result.success,
            "verdict": verdict,
            "config": config,
            "log": result.log[-4000:],
            "has_trace": result.vcd_path is not None,
            "waveform_json": waveform_json_data,
        })

    return {
        "module": mod.name,
        "engine": engine.display_name,
        "engine_version": engine.version(),
        "success": all(r.get("success") for r in results),
        "properties": results,
        "assumed_constraints": assumed_constraints,
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
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
    except ValueError as e:
        return {"error": str(e)}

    try:
        proposals = suggest_properties(mod, rtl_source, spec_text=spec)
    except LLMNotConfigured as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface any LLM/API failure to the UI, don't 500
        return {"error": f"Property suggestion failed: {e}"}

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
        mod = analyze_rtl(rtl_source, top_module=top_module.strip() or None)
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

    return {"module": mod.name, "properties": out}


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
