#!/usr/bin/env python3
"""CLI: verify RTL file and print text report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.analyzer import analyze_rtl  # noqa: E402
from rtl_verify.generators.base import TbLanguage  # noqa: E402
from rtl_verify.pipeline import run_verification  # noqa: E402
from rtl_verify.vplan_builder import build_vplan  # noqa: E402
from rtl_verify.vplan_format import format_vplan_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Automate RTL verification (TB + sim + waveform)")
    parser.add_argument("rtl", type=Path, help="Path to RTL .v/.sv file")
    parser.add_argument(
        "-l",
        "--language",
        choices=[x.value for x in TbLanguage],
        default="systemverilog",
    )
    parser.add_argument("-m", "--module", default=None, help="Top module name")
    parser.add_argument("-o", "--out-dir", type=Path, default=None, help="Output work directory")
    parser.add_argument(
        "-b",
        "--backend",
        default=None,
        help="Simulator backend id (icarus, vivado, reference); default auto-select",
    )
    parser.add_argument(
        "--vplan",
        action="store_true",
        help="Print structured verification plan (no simulation)",
    )
    parser.add_argument(
        "--vplan-json",
        action="store_true",
        help="Print verification plan as JSON",
    )
    args = parser.parse_args()

    rtl_source = args.rtl.read_text(encoding="utf-8")
    mod = analyze_rtl(rtl_source, top_module=args.module)

    if args.vplan or args.vplan_json:
        plan = build_vplan(
            rtl_source,
            mod,
            backend=args.backend,
            language=args.language,
        )
        if args.vplan_json:
            print(json.dumps(plan.to_dict(), indent=2))
        else:
            text = format_vplan_text(plan)
            try:
                sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
            print(text)
        return 0

    result = run_verification(
        rtl_source,
        language=TbLanguage(args.language),
        top_module=args.module,
        work_dir=args.out_dir,
        backend=args.backend,
    )
    report_path = result.work_dir / "report.txt"
    report_path.write_text(result.text_report, encoding="utf-8")
    print(result.text_report)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
