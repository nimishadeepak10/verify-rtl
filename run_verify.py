#!/usr/bin/env python3
"""CLI: verify RTL file and print text report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.generators.base import TbLanguage  # noqa: E402
from rtl_verify.pipeline import run_verification  # noqa: E402


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
    args = parser.parse_args()

    rtl_source = args.rtl.read_text(encoding="utf-8")
    result = run_verification(
        rtl_source,
        language=TbLanguage(args.language),
        top_module=args.module,
        work_dir=args.out_dir,
    )
    report_path = result.work_dir / "report.txt"
    report_path.write_text(result.text_report, encoding="utf-8")
    print(result.text_report)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
