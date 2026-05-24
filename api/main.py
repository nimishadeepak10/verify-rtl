"""HTTP API: upload RTL, get testbench + simulation results."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.backends.registry import backend_info_list  # noqa: E402
from rtl_verify.generators.base import TbLanguage  # noqa: E402
from rtl_verify.pipeline import run_verification  # noqa: E402
from rtl_verify.waveform import load_module_info, vcd_to_json  # noqa: E402
from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.preview import build_test_preview  # noqa: E402
from rtl_verify.vplan_builder import build_vplan  # noqa: E402

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

    return {
        "success": result.success,
        "status": getattr(result, "status", "pass" if result.success else "fail"),
        "simulator": getattr(result, "simulator", ""),
        "backend_used": getattr(result, "backend_used", ""),
        "backend_version": getattr(result, "backend_version", ""),
        "module": result.module.name,
        "inferred_op": result.module.inferred_op,
        "language": result.language.value,
        "testbench": result.testbench,
        "sim_log": result.sim_log,
        "text_report": result.text_report,
        "waveform_text": result.waveform_text,
        "uvm_note": result.uvm_note,
        "work_dir": str(result.work_dir),
        "has_vcd": result.vcd_path is not None,
        "preview": preview,
    }


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
