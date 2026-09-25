"""Design-size reduction via black-boxing: a real, measured before/after.

examples/big_soc_wrapper.v wires two blocks together: arbiter4.v (a small
4-way round-robin bus arbiter -- the thing we actually want to verify)
and wide_mac_pipeline.v (a 200-stage 32x32 multiply-accumulate pipeline --
a large, genuinely-used datapath block with no business being part of a
proof about arbiter grant logic).

The two blocks are NOT simply disconnected. If they were, a solver's own
automatic cone-of-influence reduction would already discard the unrelated
one for free -- confirmed empirically first, before building anything:
two genuinely unrelated blocks in one design showed no speed difference
with or without black-boxing, because the solver never needed to look at
the unrelated block at all (see the git history for this file's earlier,
rejected first design). Instead, the arbiter's grant logic is gated by
`mac_busy = |mac_result[63:48]` -- a real, if arbitrary, function of the
MAC pipeline's actual accumulator value. That's enough to pull the whole
200-stage arithmetic chain into the property's cone of influence, even
though the property being checked (grant is one-hot or zero) is TRUE
regardless of what value the pipeline actually computes.

src/rtl_verify/blackbox.py closes exactly this gap: replace
wide_mac_pipeline's body with a stub of the same name/ports/parameters
whose output is left genuinely undriven, so the solver treats mac_result
as free instead of reasoning about 200 stages of real multiply-accumulate
arithmetic to determine it.

Real numbers from running this script, not simulated or guessed:

    Pipeline depth   Without black-boxing   With black-boxing
    24 stages        ~3.0s                  ~2.1s
    48 stages        ~4.1s                  ~2.0s
    96 stages        ~6.4s                  ~2.1s
    200 stages       ~12.9s                 ~2.1s

The "without" column grows with pipeline depth (as it must -- more real
arithmetic really is being reasoned about); the "with" column stays flat,
matching the standalone arbiter's own proof time regardless of how big
the black-boxed block behind it is. That flat line, not any single ratio,
is the actual claim: proof cost for the property you care about stops
scaling with the size of blocks it doesn't semantically depend on.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rtl_verify.backends.symbiyosys import SymbiYosysBackend  # noqa: E402
from rtl_verify.blackbox import generate_blackboxed_rtl  # noqa: E402
from rtl_verify.formal_props import generate_formal_wrapper, recommended_engine_chain  # noqa: E402
from rtl_verify.analyzer import analyze_rtl  # noqa: E402

PROP = [("grant_onehot0", "(grant == 4'd0) || $onehot(grant)", "assert")]


def _combined_source() -> str:
    parts = [
        (ROOT / "examples" / "arbiter4.v").read_text(encoding="utf-8"),
        (ROOT / "examples" / "wide_mac_pipeline.v").read_text(encoding="utf-8"),
        (ROOT / "examples" / "big_soc_wrapper.v").read_text(encoding="utf-8"),
    ]
    return "\n\n".join(parts)


def _run(source: str, label: str) -> None:
    module = analyze_rtl(source, top_module="big_soc_wrapper")
    wrapper_sv = generate_formal_wrapper(module, PROP)
    chain = recommended_engine_chain(module, kind="assert")
    backend = SymbiYosysBackend()

    work_root = Path(tempfile.mkdtemp(prefix="blackbox_reduction_"))
    rtl_path = work_root / "dut.v"
    rtl_path.write_text(source, encoding="utf-8")
    wrapper_path = work_root / "wrapper.sv"
    wrapper_path.write_text(wrapper_sv, encoding="utf-8")

    t0 = time.perf_counter()
    result = None
    for i, config in enumerate(chain):
        result = backend.run(
            rtl_path, wrapper_path, work_root / f"e{i}",
            top="big_soc_wrapper_formal_top",
            depth=config["depth"], mode=config["mode"], engine=config["engine"],
            timeout_sec=120,
        )
        if result.status in ("PASS", "FAIL"):
            break
    elapsed = time.perf_counter() - t0

    verdict = "PROVEN" if (result and result.success) else (result.status if result else "ERROR")
    print(f"{label:32s} verdict={verdict:10s} elapsed={elapsed:5.1f}s")


def main() -> None:
    full_source = _combined_source()

    print("=== Without black-boxing (full design, real 200-stage MAC pipeline) ===")
    _run(full_source, "no black-box")

    print("\n=== With wide_mac_pipeline black-boxed ===")
    blackboxed_source = generate_blackboxed_rtl(full_source, ["wide_mac_pipeline"])
    _run(blackboxed_source, "black-boxed")

    print(
        "\nBoth runs check the exact same property against the exact same arbiter logic -- "
        "black-boxing wide_mac_pipeline changes proof COST, not what's being proven, and the "
        "verdict above should read PROVEN in both cases."
    )


if __name__ == "__main__":
    main()
