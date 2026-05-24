"""Build structured verification plans from RTL analysis."""

from __future__ import annotations

import re
from itertools import product
from typing import Dict, List, Optional, Tuple

from .analyzer import Port, PortDirection, RtlModule, _strip_comments
from .backends.registry import ALL_BACKENDS, auto_select, get_backend
from .combinational_model import can_evaluate_combinational, expected_outputs
from .vplan import (
    CategoryStatus,
    CoverageGoal,
    PlanNote,
    Severity,
    TestCase,
    TestCategory,
    TestSubcategory,
    VerificationPlan,
)

_HANDSHAKE_NAMES = frozenset(
    {"valid", "ready", "req", "ack", "enable", "tvalid", "tready", "valid_i", "valid_o"}
)


_PLAN_RTL: str = ""


def build_vplan(
    rtl: str,
    module: RtlModule,
    enabled_categories: Optional[Dict[str, bool]] = None,
    enabled_subcategories: Optional[Dict[str, bool]] = None,
    backend: Optional[str] = None,
    language: str = "systemverilog",
) -> VerificationPlan:
    """Build full verification plan with category-level toggles."""
    global _PLAN_RTL
    _PLAN_RTL = rtl
    enabled_categories = enabled_categories or {}
    enabled_subcategories = enabled_subcategories or {}
    notes: List[PlanNote] = []

    data_ins = module.data_inputs if module.is_sequential else module.inputs
    has_io = bool(data_ins) and bool(module.outputs)
    input_bits = sum(p.width for p in data_ins)
    input_space = 1 << input_bits if input_bits else 0
    self_check = (
        not module.is_sequential
        and has_io
        and can_evaluate_combinational(rtl, module)
    )

    categories: List[TestCategory] = []

    if has_io:
        categories.append(_build_directed(module, data_ins, self_check))
        categories.append(_build_corner(module, data_ins, self_check))
        categories.append(
            _build_negative(rtl, module, data_ins, backend, language, self_check)
        )
        categories.append(_build_random(module, data_ins, input_space, self_check))
        categories.append(
            _build_exhaustive(module, data_ins, input_space, self_check)
        )
    else:
        categories.append(
            TestCategory(
                id="directed",
                name="Directed",
                rationale="Directed tests require at least one input and one output port.",
                status=CategoryStatus.NOT_APPLICABLE,
                not_applicable_reason="DUT has no stimulus/monitor port pair.",
            )
        )

    _apply_toggles(categories, enabled_categories, enabled_subcategories, notes)

    methodology = _derive_methodology(categories)
    reference_model = _reference_model_text(module, self_check)
    coverage_goals = _coverage_goals(module, categories)
    pass_criteria = _pass_criteria(module, rtl, categories, self_check)
    plan_notes = _plan_notes(module, categories, self_check, backend, language)
    notes.extend(plan_notes)

    total = sum(c.enabled_case_count for c in categories)

    return VerificationPlan(
        dut_name=module.name,
        dut_summary=_dut_summary(module),
        design_type=_design_type(module),
        port_summary=_port_summary(module),
        methodology=methodology,
        reference_model=reference_model,
        categories=categories,
        coverage_goals=coverage_goals,
        pass_criteria=pass_criteria,
        notes=notes,
        total_planned_cases=total,
    )


def _port_summary(module: RtlModule) -> Dict[str, int]:
    return {
        "inputs": len(module.inputs),
        "outputs": len(module.outputs),
        "data_inputs": len(module.data_inputs),
        "total_ports": len(module.ports),
    }


def _design_type(module: RtlModule) -> str:
    if module.is_sequential:
        if module.state_reg:
            return "Sequential (FSM)"
        return "Sequential"
    return "Combinational"


def _dut_summary(module: RtlModule) -> str:
    if module.inferred_op == "add":
        return "Inferred 2-input adder (a + b)"
    if module.inferred_op == "and":
        return "Inferred 2-input bitwise AND"
    if module.inferred_op == "xor":
        return "Inferred 2-input bitwise XOR"
    if module.is_sequential and module.state_reg:
        return f"Sequential design with FSM register '{module.state_reg}'"
    if module.is_sequential:
        return "Clocked sequential logic"
    return "Combinational logic (function not fully inferred)"


def _max_val(port: Port) -> int:
    return (1 << port.width) - 1


def _fmt(v: int, width: int) -> str:
    if width <= 4:
        return str(v)
    return f"0x{v:X}"


def _expected(
    module: RtlModule,
    inputs: Dict[str, int],
    rtl: str = "",
) -> Optional[Dict[str, str]]:
    if not module.outputs:
        return None
    if rtl and not module.is_sequential and can_evaluate_combinational(rtl, module):
        try:
            golden = expected_outputs(rtl, module, inputs)
            return {
                name: _fmt(val, next(p.width for p in module.outputs if p.name == name))
                for name, val in golden.items()
            }
        except Exception:
            return None
    if module.inferred_op not in ("add", "and", "xor"):
        return None
    ins = module.data_inputs if module.is_sequential else module.inputs
    if len(ins) < 2:
        return None
    a = inputs.get(ins[0].name, 0)
    b = inputs.get(ins[1].name, 0)
    out = module.outputs[0]
    mask = (1 << out.width) - 1
    if module.inferred_op == "add":
        got = (a + b) & mask
    elif module.inferred_op == "and":
        got = a & b
    else:
        got = a ^ b
    return {out.name: _fmt(got, out.width)}


def _case(
    cid: str,
    desc: str,
    inputs: Dict[str, int],
    module: RtlModule,
    rationale: str,
    tags: Optional[List[str]] = None,
    self_check: bool = True,
) -> TestCase:
    ins = {k: _fmt(v, next(p.width for p in module.ports if p.name == k)) for k, v in inputs.items()}
    exp = _expected(module, inputs, _PLAN_RTL) if self_check else None
    return TestCase(
        id=cid,
        description=desc,
        inputs=ins,
        expected_outputs=exp,
        rationale=rationale,
        tags=tags or [],
    )


def _build_directed(
    module: RtlModule, data_ins: List[Port], self_check: bool
) -> TestCategory:
    cases: List[TestCase] = []
    if module.inferred_op == "add" and len(data_ins) >= 2:
        a, b = data_ins[0], data_ins[1]
        ma, mb = _max_val(a), _max_val(b)
        vectors = [
            (0, 0, "Zero + zero baseline"),
            (1, 0, "One + zero identity check"),
            (ma, mb, "Max + max — tests carry into wider output"),
            (1, ma - 1 if ma > 1 else 1, "Asymmetric small + large"),
            (2, 3, "Typical mid-range values") if ma >= 3 else (1, 2, "Typical mid-range values"),
        ]
        for i, (va, vb, desc) in enumerate(vectors[:8], start=1):
            cases.append(
                _case(
                    f"directed_{i:03d}",
                    desc,
                    {a.name: va, b.name: vb},
                    module,
                    "Directed scenarios target known arithmetic behaviors and overflow edges.",
                    ["directed"],
                    self_check,
                )
            )
    elif module.inferred_op == "and" and len(data_ins) >= 2:
        a, b = data_ins[0], data_ins[1]
        ma, mb = _max_val(a), _max_val(b)
        for i, (va, vb, desc, rat) in enumerate(
            [
                (0, 0, "All zeros", "AND with zero inputs"),
                (ma, ma, "All ones mask", "Full-width ones pattern"),
                (ma, 0, "Mask with zeros", "One operand all-ones"),
                (0x5 & ma, (~0x5) & mb, "Pattern + inverse", "Bitwise complement interaction"),
            ],
            start=1,
        ):
            cases.append(
                _case(f"directed_{i:03d}", desc, {a.name: va, b.name: vb}, module, rat, ["directed"], self_check)
            )
    elif module.inferred_op == "xor" and len(data_ins) >= 2:
        a, b = data_ins[0], data_ins[1]
        ma = _max_val(a)
        cases.extend(
            [
                _case(f"directed_{1:03d}", "Identity x ^ 0", {a.name: 3, b.name: 0}, module, "XOR with zero", ["directed"], self_check),
                _case(f"directed_{2:03d}", "Self-cancel x ^ x", {a.name: ma, b.name: ma}, module, "XOR cancels to zero", ["directed"], self_check),
                _case(f"directed_{3:03d}", "Toggle bits", {a.name: 1, b.name: 2}, module, "Single-bit difference", ["directed"], self_check),
            ]
        )
    else:
        cases.append(
            _case(
                "directed_001",
                "Sanity stimulus",
                {p.name: 0 for p in data_ins},
                module,
                "Monitor-only sanity check when no golden model is inferred.",
                ["sanity"],
                False,
            )
        )

    return TestCategory(
        id="directed",
        name="Directed",
        rationale=(
            "Directed tests exercise known-critical scenarios chosen from the inferred "
            "function. They catch obvious functional bugs quickly before broader sweeps."
        ),
        status=CategoryStatus.ENABLED,
        cases=cases,
    )


def _build_corner(
    module: RtlModule, data_ins: List[Port], self_check: bool
) -> TestCategory:
    subcats: List[TestSubcategory] = []

    # all_zeros
    subcats.append(
        TestSubcategory(
            id="all_zeros",
            name="All zeros",
            rationale="All inputs at minimum detects stuck-at and gating errors.",
            status=CategoryStatus.ENABLED,
            cases=[
                _case(
                    "corner_az_001",
                    "All inputs zero",
                    {p.name: 0 for p in data_ins},
                    module,
                    "Baseline minimum corner.",
                    ["corner", "all_zeros"],
                    self_check,
                )
            ],
        )
    )

    # all_ones
    az_cases = []
    for i, p in enumerate(data_ins):
        ins = {q.name: _max_val(q) if q.name == p.name else 0 for q in data_ins}
        az_cases.append(
            _case(
                f"corner_ao_{i+1:03d}",
                f"Input {p.name} at max",
                ins,
                module,
                "Per-port maximum value corner.",
                ["corner", "all_ones"],
                self_check,
            )
        )
    if len(data_ins) > 1:
        az_cases.append(
            _case(
                "corner_ao_all",
                "All inputs at max",
                {p.name: _max_val(p) for p in data_ins},
                module,
                "Simultaneous max on all inputs.",
                ["corner", "all_ones"],
                self_check,
            )
        )
    subcats.append(
        TestSubcategory(
            id="all_ones",
            name="All ones",
            rationale="Maximum values stress carry chains and width boundaries.",
            status=CategoryStatus.ENABLED,
            cases=az_cases,
        )
    )

    # walking_one
    wo_cases: List[TestCase] = []
    for p in data_ins:
        for bit in range(p.width):
            val = 1 << bit
            ins = {q.name: val if q.name == p.name else 0 for q in data_ins}
            wo_cases.append(
                _case(
                    f"corner_w1_{p.name}_{bit}",
                    f"Walking-one on {p.name}[{bit}]",
                    ins,
                    module,
                    "Single-hot patterns isolate bit-position bugs.",
                    ["corner", "walking_one"],
                    self_check,
                )
            )
    subcats.append(
        TestSubcategory(
            id="walking_one",
            name="Walking one",
            rationale="One-hot patterns verify each input bit is independently observable.",
            status=CategoryStatus.ENABLED,
            cases=wo_cases,
        )
    )

    # walking_zero
    wz_cases: List[TestCase] = []
    for p in data_ins:
        maxv = _max_val(p)
        for bit in range(p.width):
            val = maxv ^ (1 << bit)
            ins = {q.name: val if q.name == p.name else maxv for q in data_ins}
            wz_cases.append(
                _case(
                    f"corner_w0_{p.name}_{bit}",
                    f"Walking-zero on {p.name}[{bit}]",
                    ins,
                    module,
                    "Single zero in a field of ones tests inverse sensitivity.",
                    ["corner", "walking_zero"],
                    self_check,
                )
            )
    subcats.append(
        TestSubcategory(
            id="walking_zero",
            name="Walking zero",
            rationale="Inverse walking patterns complement walking-one coverage.",
            status=CategoryStatus.ENABLED,
            cases=wz_cases,
        )
    )

    # alternating
    alt_cases: List[TestCase] = []
    for p in data_ins:
        if p.width >= 2:
            pat_a = int("01" * ((p.width + 1) // 2), 2) & _max_val(p)
            pat_b = int("10" * ((p.width + 1) // 2), 2) & _max_val(p)
            for tag, val in [("555", pat_a), ("AAA", pat_b)]:
                ins = {q.name: val if q.name == p.name else 0 for q in data_ins}
                alt_cases.append(
                    _case(
                        f"corner_alt_{p.name}_{tag}",
                        f"Alternating pattern on {p.name}",
                        ins,
                        module,
                        "Checkerboard patterns toggle adjacent bits.",
                        ["corner", "alternating"],
                        self_check,
                    )
                )
    subcats.append(
        TestSubcategory(
            id="alternating",
            name="Alternating",
            rationale="0x5555/0xAAAA patterns exercise alternating bit toggles.",
            status=CategoryStatus.ENABLED,
            cases=alt_cases if alt_cases else [
                _case("corner_alt_skip", "N/A for 1-bit ports", {data_ins[0].name: 0}, module, "Skipped", [], False)
            ],
        )
    )

    return TestCategory(
        id="corner",
        name="Corner Cases",
        rationale=(
            "Corner cases catch boundary bugs — off-by-one, sign extension errors, "
            "and bit-position confusion that directed tests may miss."
        ),
        status=CategoryStatus.ENABLED,
        subcategories=subcats,
    )


def _only_two_state_backends(backend: Optional[str], language: str) -> bool:
    if backend:
        return backend.strip().lower() == "reference"
    chosen = auto_select(language)
    if chosen and chosen.name != "reference":
        return False
    avail = [b for b in ALL_BACKENDS if b.is_available()]
    if not avail:
        return True
    four_state = {"icarus"}
    return all(b.name not in four_state for b in avail)


def _has_handshake_ports(module: RtlModule) -> bool:
    for p in module.ports:
        if p.name.lower() in _HANDSHAKE_NAMES:
            return True
        nl = p.name.lower()
        if any(h in nl for h in ("valid", "ready", "req", "ack")):
            return True
    return False


def _has_encoded_input_constraint(rtl: str, module: RtlModule) -> bool:
    clean = _strip_comments(rtl)
    if re.search(r"//\s*valid\s*:\s*\d", clean, re.IGNORECASE):
        return True
    for p in module.data_inputs:
        if re.search(rf"case\s*\(\s*{re.escape(p.name)}\s*\)", clean):
            return True
    return False


def _overflow_possible(module: RtlModule) -> Tuple[bool, str]:
    if module.inferred_op != "add" or len(module.inputs) < 2 or not module.outputs:
        return False, "Not an adder."
    in_w = module.data_inputs[0].width if module.data_inputs else module.inputs[0].width
    out_w = module.outputs[0].width
    if out_w < in_w + 1:
        return True, f"Output width ({out_w}) < input width + 1 ({in_w + 1}) — overflow possible."
    return False, (
        f"Output width ({out_w}) ≥ input width + 1 ({in_w + 1}) — overflow cannot occur."
    )


def _build_negative(
    rtl: str,
    module: RtlModule,
    data_ins: List[Port],
    backend: Optional[str],
    language: str,
    self_check: bool,
) -> TestCategory:
    subcats: List[TestSubcategory] = []

    # x_propagation
    if module.outputs:
        if _only_two_state_backends(backend, language):
            subcats.append(
                TestSubcategory(
                    id="x_propagation",
                    name="X-propagation",
                    rationale=(
                        "X-propagation tests catch unintended dependencies and "
                        "uninitialized register hazards."
                    ),
                    status=CategoryStatus.NOT_APPLICABLE,
                    not_applicable_reason=(
                        "Selected simulator is 2-state; X-propagation requires 4-state "
                        "(Icarus, XSim, Questa)."
                    ),
                )
            )
        else:
            xp_cases = []
            for i, p in enumerate(data_ins):
                ins = {q.name: ("X" if q.name == p.name else "0") for q in data_ins}
                xp_cases.append(
                    TestCase(
                        id=f"neg_xp_{i+1:03d}",
                        description=f"Drive X on {p.name}",
                        inputs=ins,
                        expected_outputs=None,
                        rationale="Observe whether unrelated outputs remain defined.",
                        tags=["negative", "x_prop"],
                    )
                )
            subcats.append(
                TestSubcategory(
                    id="x_propagation",
                    name="X-propagation",
                    rationale=(
                        "X-propagation tests catch unintended dependencies and "
                        "uninitialized register hazards."
                    ),
                    status=CategoryStatus.ENABLED,
                    cases=xp_cases,
                )
            )
    else:
        subcats.append(
            TestSubcategory(
                id="x_propagation",
                name="X-propagation",
                rationale="Requires observable outputs.",
                status=CategoryStatus.NOT_APPLICABLE,
                not_applicable_reason="No output ports to observe.",
            )
        )

    # overflow
    ok, reason = _overflow_possible(module)
    if ok:
        a, b = data_ins[0], data_ins[1]
        subcats.append(
            TestSubcategory(
                id="overflow",
                name="Overflow",
                rationale="Tests result width truncation beyond representable range.",
                status=CategoryStatus.ENABLED,
                cases=[
                    _case(
                        "neg_ov_001",
                        "Max operands",
                        {a.name: _max_val(a), b.name: _max_val(b)},
                        module,
                        "Maximum sum may wrap/truncate at output width.",
                        ["negative", "overflow"],
                        self_check,
                    )
                ],
            )
        )
    else:
        subcats.append(
            TestSubcategory(
                id="overflow",
                name="Overflow",
                rationale="Arithmetic overflow beyond output width.",
                status=CategoryStatus.NOT_APPLICABLE,
                not_applicable_reason=reason,
            )
        )

    # out_of_range
    if _has_encoded_input_constraint(rtl, module):
        subcats.append(
            TestSubcategory(
                id="out_of_range",
                name="Out-of-range",
                rationale="Illegal encoded input values.",
                status=CategoryStatus.ENABLED,
                cases=[
                    TestCase(
                        id="neg_oor_001",
                        description="Reserved/illegal codepoint (placeholder)",
                        inputs={p.name: _fmt(_max_val(p), p.width) for p in data_ins},
                        rationale="Stimulus outside legal encoding (manual review).",
                        tags=["negative", "out_of_range"],
                    )
                ],
            )
        )
    else:
        bits = sum(p.width for p in data_ins)
        subcats.append(
            TestSubcategory(
                id="out_of_range",
                name="Out-of-range",
                rationale="Encoded / constrained inputs.",
                status=CategoryStatus.NOT_APPLICABLE,
                not_applicable_reason=(
                    f"All {bits}-bit input values are legal — no encoding constraint detected."
                ),
            )
        )

    # protocol_violation
    if _has_handshake_ports(module):
        subcats.append(
            TestSubcategory(
                id="protocol_violation",
                name="Protocol violation",
                rationale="Handshake timing / ordering stress.",
                status=CategoryStatus.ENABLED,
                cases=[
                    TestCase(
                        id="neg_proto_001",
                        description="Handshake violation (placeholder)",
                        inputs={},
                        rationale="Requires protocol-aware stimulus (future).",
                        tags=["negative", "protocol"],
                    )
                ],
            )
        )
    else:
        subcats.append(
            TestSubcategory(
                id="protocol_violation",
                name="Protocol violation",
                rationale="Valid/ready or req/ack misuse.",
                status=CategoryStatus.NOT_APPLICABLE,
                not_applicable_reason="No handshake/protocol signals detected.",
            )
        )

    return TestCategory(
        id="negative",
        name="Negative",
        rationale=(
            "Negative tests explore non-ideal conditions: unknowns, overflow, illegal "
            "codes, and protocol misuse."
        ),
        status=CategoryStatus.ENABLED,
        subcategories=subcats,
    )


def _build_random(
    module: RtlModule,
    data_ins: List[Port],
    input_space: int,
    self_check: bool,
) -> TestCategory:
    if input_space > 0 and input_space <= 16:
        return TestCategory(
            id="random",
            name="Random",
            rationale=(
                "Constrained-random fills coverage gaps the directed and corner tests miss. "
                "Seeded (42) for reproducibility."
            ),
            status=CategoryStatus.NOT_APPLICABLE,
            not_applicable_reason=(
                f"Input space is small ({input_space}) — exhaustive enumeration covers it; "
                "random adds no value."
            ),
        )
    n = 32 if input_bits_total(data_ins) <= 8 else (64 if input_bits_total(data_ins) <= 16 else 128)
    import random

    random.seed(42)
    cases: List[TestCase] = []
    limits = [_max_val(p) for p in data_ins]
    for i in range(n):
        ins = {p.name: random.randint(0, limits[j]) for j, p in enumerate(data_ins)}
        cases.append(
            _case(
                f"random_{i+1:03d}",
                f"Random vector {i+1}",
                ins,
                module,
                "Pseudo-random stimulus expands coverage beyond directed corners.",
                ["random"],
                self_check,
            )
        )
    return TestCategory(
        id="random",
        name="Random",
        rationale=(
            "Constrained-random fills coverage gaps the directed and corner tests miss. "
            "Seeded (42) for reproducibility."
        ),
        status=CategoryStatus.ENABLED,
        cases=cases,
    )


def input_bits_total(data_ins: List[Port]) -> int:
    return sum(p.width for p in data_ins)


def _build_exhaustive(
    module: RtlModule,
    data_ins: List[Port],
    input_space: int,
    self_check: bool,
) -> TestCategory:
    if input_space == 0:
        return TestCategory(
            id="exhaustive",
            name="Exhaustive",
            rationale="Full input enumeration.",
            status=CategoryStatus.NOT_APPLICABLE,
            not_applicable_reason="No data inputs.",
        )
    if input_space > 512:
        return TestCategory(
            id="exhaustive",
            name="Exhaustive",
            rationale=(
                "Exhaustive enumeration is the strongest possible verification — "
                "covers every input combination."
            ),
            status=CategoryStatus.NOT_APPLICABLE,
            not_applicable_reason=(
                f"Input space ({input_space:,}) too large for exhaustive — use directed + random."
            ),
        )
    limits = [_max_val(p) for p in data_ins]
    cases: List[TestCase] = []
    ranges = [range(lim + 1) for lim in limits]
    for idx, combo in enumerate(product(*ranges)):
        ins = {data_ins[j].name: combo[j] for j in range(len(data_ins))}
        cases.append(
            _case(
                f"exhaustive_{idx+1:03d}",
                f"Input combo {idx+1}/{input_space}",
                ins,
                module,
                "Complete enumeration of input space.",
                ["exhaustive"],
                self_check,
            )
        )
    return TestCategory(
        id="exhaustive",
        name="Exhaustive",
        rationale=(
            "Exhaustive enumeration is the strongest possible verification — "
            "covers every input combination."
        ),
        status=CategoryStatus.ENABLED,
        cases=cases,
    )


def _apply_toggles(
    categories: List[TestCategory],
    enabled_categories: Dict[str, bool],
    enabled_subcategories: Dict[str, bool],
    notes: List[PlanNote],
) -> None:
    for cat in categories:
        if cat.id in enabled_categories:
            want = enabled_categories[cat.id]
            if cat.status == CategoryStatus.NOT_APPLICABLE and want:
                notes.append(
                    PlanNote(
                        Severity.WARN,
                        message=(
                            f"Cannot enable category '{cat.id}' — it is not applicable: "
                            f"{cat.not_applicable_reason}"
                        ),
                    )
                )
            elif cat.status != CategoryStatus.NOT_APPLICABLE:
                cat.status = (
                    CategoryStatus.ENABLED if want else CategoryStatus.DISABLED
                )
        for sub in cat.subcategories:
            if sub.id in enabled_subcategories:
                want = enabled_subcategories[sub.id]
                if sub.status == CategoryStatus.NOT_APPLICABLE and want:
                    notes.append(
                        PlanNote(
                            Severity.WARN,
                            message=(
                                f"Cannot enable subcategory '{sub.id}' — not applicable: "
                                f"{sub.not_applicable_reason}"
                            ),
                        )
                    )
                elif sub.status != CategoryStatus.NOT_APPLICABLE:
                    sub.status = (
                        CategoryStatus.ENABLED if want else CategoryStatus.DISABLED
                    )


def _derive_methodology(categories: List[TestCategory]) -> str:
    enabled = [c for c in categories if c.status == CategoryStatus.ENABLED]
    names = [c.name for c in enabled]
    if not names:
        return "No categories enabled"
    base = " + ".join(names)
    ex = next((c for c in categories if c.id == "exhaustive"), None)
    if ex and ex.status == CategoryStatus.ENABLED:
        return f"{base} (input space exhaustively covered)"
    rnd = next((c for c in categories if c.id == "random"), None)
    if rnd and rnd.status == CategoryStatus.ENABLED:
        return f"{base} (random fills large input space)"
    return base


def _reference_model_text(module: RtlModule, self_check: bool) -> str:
    if not self_check:
        return "Monitor-only — no golden model; outputs are logged, not checked"
    return "Golden model derived from RTL assign statements (Python reference evaluator)"


def _coverage_goals(
    module: RtlModule, categories: List[TestCategory]
) -> List[CoverageGoal]:
    goals = [
        CoverageGoal(
            name="Statement coverage",
            target_percent=100.0,
            rationale="Directed + corner + exhaustive should hit every RTL line.",
        ),
        CoverageGoal(
            name="Toggle coverage",
            target_percent=90.0,
            rationale="Every bit should toggle in both directions across the plan.",
        ),
    ]
    if module.is_sequential and module.state_reg:
        goals.append(
            CoverageGoal(
                name="FSM state coverage",
                target_percent=100.0,
                rationale=f"All states of '{module.state_reg}' should be visited.",
            )
        )
    rnd = next((c for c in categories if c.id == "random"), None)
    if rnd and rnd.status == CategoryStatus.ENABLED:
        goals.append(
            CoverageGoal(
                name="Functional coverage",
                target_percent=80.0,
                rationale="Random stimulus improves functional coverage closure.",
            )
        )
    return goals


def _rtl_has_assertions(rtl: str) -> bool:
    clean = _strip_comments(rtl)
    return bool(re.search(r"\bassert\s*\(", clean) or re.search(r"\bassume\s*\(", clean))


def _pass_criteria(
    module: RtlModule,
    rtl: str,
    categories: List[TestCategory],
    self_check: bool,
) -> List[str]:
    crit: List[str] = []
    if self_check:
        crit.append("All directed test cases produce expected outputs")
        if any(c.id == "corner" and c.status == CategoryStatus.ENABLED for c in categories):
            crit.append("All corner cases match golden model")
        if any(c.id == "exhaustive" and c.status == CategoryStatus.ENABLED for c in categories):
            crit.append("Exhaustive enumeration matches golden model for every input")
    if _rtl_has_assertions(rtl):
        crit.append("All assertions hold throughout simulation")
    neg = next((c for c in categories if c.id == "negative"), None)
    if neg:
        xp = next((s for s in neg.subcategories if s.id == "x_propagation"), None)
        if xp and xp.status == CategoryStatus.ENABLED:
            crit.append("No X propagation to outputs from valid inputs")
    crit.append("Coverage targets met")
    return crit


def _plan_notes(
    module: RtlModule,
    categories: List[TestCategory],
    self_check: bool,
    backend: Optional[str],
    language: str,
) -> List[PlanNote]:
    notes: List[PlanNote] = []
    if self_check:
        notes.append(
            PlanNote(
                Severity.INFO,
                message="Self-checking enabled via inferred reference model",
            )
        )
    if _only_two_state_backends(backend, language):
        notes.append(
            PlanNote(
                Severity.WARN,
                message="X-prop tests disabled on 2-state simulator (reference)",
            )
        )
    return notes


def _mask_hex(module: RtlModule) -> str:
    if not module.outputs:
        return "0"
    w = module.outputs[0].width
    return f"0x{((1 << w) - 1):X}"
