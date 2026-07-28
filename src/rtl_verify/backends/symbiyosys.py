"""SymbiYosys (sby) formal backend — model checking via Yosys.

Unlike IcarusBackend/VivadoBackend, this does not run a self-checking
testbench; it proves (or falsifies) SVA properties bound to the DUT.
Two engines are supported via the ``mode``/``engine`` args, and they give
genuinely different guarantees, not just different speed:

- ``mode="bmc", engine="smtbmc"`` (the default): bounded — proves no
  violation exists within ``depth`` steps. A PASS here does NOT mean the
  property holds forever, only that nothing broke within the checked
  window. Good for fast bug-hunting.
- ``mode="prove", engine="abc pdr"``: unbounded (PDR/property-directed
  reachability) — proves the property for all time, or finds a genuine
  counterexample regardless of how far away it is. ``depth`` is ignored
  in this mode (PDR doesn't unroll to a fixed step count).
- ``mode="cover", engine="smtbmc"``: for ``cover`` (not ``assert``)
  statements — checks reachability, not correctness. ``success=True``
  means the cover WAS reached (confirmed by running it: PDR silently
  folds a ``cover`` into its proof as a constraint instead of actually
  checking reachability — ``mode="cover"`` is required for a cover
  statement to mean anything). An unreached cover returns
  ``success=False`` with no trace, since there's nothing to show.

``run()`` keeps the same shape as the simulator backends so the registry
can treat it uniformly, with two differences the caller must know about:

- ``tb_path`` is not a driving testbench — it's a file containing the
  ``assert property`` statements (typically guarded by ``` `ifdef FORMAL ```),
  either standalone or bound to the DUT via ``bind``. Pass the same path
  as ``rtl_path`` if the properties already live inside the RTL file.
- ``top`` must be the DUT module name (or the module the properties are
  bound to), not the simulation testbench module.

``BackendResult.success`` is True only when SymbiYosys reports PASS (the
properties held for the configured depth) — never on error/timeout/unknown.
``BackendResult.vcd_path`` is the counterexample trace on FAIL, and None on
PASS (formal proofs don't produce a trace when nothing failed), which maps
directly onto the existing waveform viewer.

``BackendResult.status`` carries sby's own status word verbatim — confirmed
by reading sby's actual source (share/yosys/python3/sby_core.py), the full
vocabulary is ``PASS``, ``FAIL``, ``ERROR``, ``TIMEOUT``, ``UNKNOWN``, and
``CANCELLED``. The last three are easy to conflate but mean different
things and callers should not collapse them into one "failed" bucket:
``ERROR`` is a genuine tool/compile problem (worth retrying with a fixed
expression); ``TIMEOUT`` means the configured wall-clock budget ran out
before the solver decided anything; ``UNKNOWN`` means the engine itself
gave up without a proof or a counterexample (confirmed in
sby_engine_abc.py — this is a real, reachable outcome for PDR specifically,
not a hypothetical). Neither TIMEOUT nor UNKNOWN is a syntax problem an LLM
retry could fix, and neither is a legitimate FALSIFIED/UNREACHED verdict —
they mean "we don't know," which is a different, honest thing to tell the
user than "this is false."
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .base import BackendResult, SimulatorBackend

_STATUS_FILENAME = "status"
_LOGFILE = "logfile.txt"


def _find_trace(job_path: Path) -> Path | None:
    """Locate the counterexample/cover trace VCD, whichever naming this mode used.

    bmc/prove name it engine_0/trace.vcd on falsification. cover mode instead
    numbers it engine_0/trace0.vcd (it can reach several distinct cover
    points), and produces no trace at all when nothing was reached.
    """
    plain = job_path / "engine_0" / "trace.vcd"
    if plain.is_file():
        return plain
    numbered = sorted((job_path / "engine_0").glob("trace*.vcd"))
    return numbered[0] if numbered else None


def find_sby() -> str | None:
    for name in ("sby", "sby.exe"):
        path = shutil.which(name)
        if path:
            return path
    candidates: list[Path] = []
    for base in (
        Path(r"C:\oss-cad-suite\bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        candidates.append(base / "sby.exe")
        candidates.append(base / "sby")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_yosys(sby_path: str | None = None) -> str | None:
    for name in ("yosys", "yosys.exe"):
        path = shutil.which(name)
        if path:
            return path
    if sby_path:
        for name in ("yosys.exe", "yosys"):
            candidate = Path(sby_path).parent / name
            if candidate.is_file():
                return str(candidate)
    return None


class SymbiYosysBackend(SimulatorBackend):
    name = "symbiyosys"
    display_name = "SymbiYosys (formal)"
    supports_uvm = False
    supports_systemverilog = True
    supports_formal = True

    def is_available(self) -> bool:
        sby = find_sby()
        return sby is not None and find_yosys(sby) is not None

    def version(self) -> Optional[str]:
        sby = find_sby()
        if not sby:
            return None
        try:
            r = subprocess.run(
                [sby, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            line = (r.stdout or r.stderr or "").strip().splitlines()[0]
            m = re.search(r"v?([\d.]+)", line, re.IGNORECASE)
            return m.group(1) if m else line
        except (subprocess.TimeoutExpired, OSError, IndexError):
            return None

    def run(
        self,
        rtl_path: Path,
        tb_path: Path,
        work_dir: Path,
        top: str = "tb",
        depth: int = 20,
        mode: str = "bmc",
        engine: str = "smtbmc",
        timeout_sec: int = 300,
    ) -> BackendResult:
        t0 = time.perf_counter()
        work_dir.mkdir(parents=True, exist_ok=True)
        work_abs = work_dir.resolve()

        sby = find_sby()
        if not sby:
            return BackendResult(
                success=False,
                log=(
                    "SymbiYosys (sby) not found.\n"
                    "Install the OSS CAD Suite: https://github.com/YosysHQ/oss-cad-suite-build\n"
                    "Typical Windows path: C:\\oss-cad-suite\\bin"
                ),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
                status="ERROR",
            )
        yosys = find_yosys(sby)
        if not yosys:
            return BackendResult(
                success=False,
                log="sby found but yosys is not — both must be on PATH (they ship together in oss-cad-suite/bin).",
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
                status="ERROR",
            )

        rtl_abs = rtl_path.resolve()
        tb_abs = tb_path.resolve()
        files = [rtl_abs] if tb_abs == rtl_abs else [rtl_abs, tb_abs]
        read_files = " ".join(f'"{f}"' for f in files)

        # "prove" mode (PDR/induction engines) searches for an unbounded proof
        # or counterexample rather than unrolling to a fixed step count, so a
        # depth option isn't meaningful there — omit it rather than pass a
        # number that would silently be ignored.
        options_lines = [f"mode {mode}"]
        if mode != "prove":
            options_lines.append(f"depth {depth}")
        # sby's own [options] "timeout" (confirmed in sby_core.py: parsed via
        # handle_int_option("timeout", None)) lets it stop gracefully and
        # write a proper "TIMEOUT" status + partial log — versus killing the
        # whole process from outside, which can leave no status file at all.
        # The outer subprocess timeout below stays as a safety net only, in
        # case sby itself doesn't respect its own limit.
        options_lines.append(f"timeout {timeout_sec}")

        sby_config = (
            f"[options]\n" + "\n".join(options_lines) + "\n"
            f"\n"
            f"[engines]\n"
            f"{engine}\n"
            f"\n"
            f"[script]\n"
            f"read -formal {read_files}\n"
            f"prep -top {top}\n"
            f"\n"
            f"[files]\n" + "".join(f"{f}\n" for f in files)
        )
        config_path = work_abs / "formal.sby"
        config_path.write_text(sby_config, encoding="utf-8")

        job_dir = "formal_run"
        job_path = work_abs / job_dir
        run_cmd = [sby, "-f", "-d", job_dir, str(config_path)]

        env = os.environ.copy()
        bindir = str(Path(sby).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")

        log_lines: list[str] = ["=== SYMBIYOSYS ===", " ".join(run_cmd)]
        try:
            r = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
                cwd=str(work_abs),
                env=env,
                timeout=timeout_sec + 30,
            )
            log_lines.append(r.stdout or "")
            log_lines.append(r.stderr or "")
        except subprocess.TimeoutExpired:
            # Only reachable if sby ignored its own [options] timeout above
            # (e.g. hung inside a subprocess it doesn't poll) — the normal
            # path is sby noticing its own timeout first and writing status
            # "TIMEOUT" itself, which the status-file read below picks up.
            log_lines.append("SymbiYosys timed out (outer safety-net kill — sby did not exit on its own timeout).")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
                status="TIMEOUT",
            )
        except FileNotFoundError as e:
            log_lines.append(f"sby executable missing: {e}")
            return BackendResult(
                success=False,
                log="\n".join(log_lines),
                vcd_path=None,
                work_dir=work_abs,
                duration_sec=time.perf_counter() - t0,
                status="ERROR",
            )

        status_file = job_path / _STATUS_FILENAME
        status_word = "ERROR"
        if status_file.is_file():
            first_line = status_file.read_text(encoding="utf-8", errors="replace").strip()
            status_word = (first_line.split() or ["ERROR"])[0]

        logfile = job_path / _LOGFILE
        if logfile.is_file():
            log_lines.append("=== " + _LOGFILE + " ===")
            log_lines.append(logfile.read_text(encoding="utf-8", errors="replace"))

        trace = _find_trace(job_path)
        return BackendResult(
            success=status_word == "PASS",
            log="\n".join(log_lines),
            vcd_path=trace,
            work_dir=work_abs,
            duration_sec=time.perf_counter() - t0,
            status=status_word,
        )
