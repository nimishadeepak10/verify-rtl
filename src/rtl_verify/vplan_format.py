"""Format VerificationPlan as human-readable vplan document."""

from __future__ import annotations

from .vplan import CategoryStatus, Severity, TestCategory, VerificationPlan


def format_vplan_text(plan: VerificationPlan) -> str:
    w = 64
    lines: list[str] = []
    lines.append("=" * w)
    lines.append(f"VERIFICATION PLAN — {plan.dut_name}")
    lines.append("=" * w)
    lines.append("")
    lines.append("1. DESIGN UNDER TEST")
    lines.append(f"   Module:         {plan.dut_name}")
    lines.append(f"   Type:           {plan.design_type}")
    lines.append(f"   Function:       {plan.dut_summary}")
    ps = plan.port_summary
    lines.append(
        f"   Ports:          {ps.get('inputs', 0)} inputs, {ps.get('outputs', 0)} outputs"
    )
    lines.append("")
    lines.append("2. VERIFICATION STRATEGY")
    lines.append(f"   Methodology:    {plan.methodology}")
    lines.append(f"   Reference:      {plan.reference_model}")
    lines.append("")
    lines.append("3. TEST CATEGORIES")
    for cat in plan.categories:
        lines.extend(_format_category(cat))
    lines.append("")
    lines.append("4. COVERAGE GOALS")
    for g in plan.coverage_goals:
        lines.append(f"   • {g.name}  ≥ {g.target_percent:g}%")
    lines.append("")
    lines.append("5. PASS / FAIL CRITERIA")
    for c in plan.pass_criteria:
        lines.append(f"   • {c}")
    lines.append("")
    lines.append("6. NOTES")
    if plan.notes:
        for n in plan.notes:
            tag = n.severity.value.upper()
            lines.append(f"   [{tag}]  {n.message}")
    else:
        lines.append("   (none)")
    lines.append("")
    lines.append("=" * w)
    enabled_cats = sum(1 for c in plan.categories if c.status == CategoryStatus.ENABLED)
    lines.append(
        f"Total planned: {plan.total_planned_cases} cases across "
        f"{enabled_cats} enabled categories"
    )
    lines.append("=" * w)
    return "\n".join(lines)


def _format_category(cat: TestCategory) -> list[str]:
    lines: list[str] = []
    mark = _status_mark(cat.status)
    count = cat.enabled_case_count
    lines.append(f"   {mark} {cat.name:<18} {count} case{'s' if count != 1 else ''}")
    lines.append(f"     Rationale: {cat.rationale}")
    if cat.status == CategoryStatus.NOT_APPLICABLE:
        lines.append(f"     N/A: {cat.not_applicable_reason}")
        return lines
    if cat.status == CategoryStatus.DISABLED:
        lines.append("     (disabled by user)")
        return lines
    for tc in cat.cases[:12]:
        lines.append(f"     • {tc.id}: {tc.description}")
    if len(cat.cases) > 12:
        lines.append(f"     • ... {len(cat.cases) - 12} more cases")
    for sub in cat.subcategories:
        sm = _status_mark(sub.status)
        sc = len(sub.cases) if sub.status == CategoryStatus.ENABLED else 0
        if sub.status == CategoryStatus.NOT_APPLICABLE:
            lines.append(f"     {sm} {sub.name:<16} N/A: {sub.not_applicable_reason}")
        elif sub.status == CategoryStatus.DISABLED:
            lines.append(f"     {sm} {sub.name:<16} (disabled)")
        else:
            lines.append(f"     {sm} {sub.name:<16} {sc} case{'s' if sc != 1 else ''}")
            for tc in sub.cases[:6]:
                lines.append(f"       • {tc.id}: {tc.description}")
            if len(sub.cases) > 6:
                lines.append(f"       • ... {len(sub.cases) - 6} more")
    return lines


def _status_mark(status: CategoryStatus) -> str:
    if status == CategoryStatus.ENABLED:
        return "✓"
    if status == CategoryStatus.DISABLED:
        return "○"
    return "N/A"
