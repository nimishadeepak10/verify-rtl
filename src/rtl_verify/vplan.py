"""Industry-style verification plan data model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class CategoryStatus(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_APPLICABLE = "n/a"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass
class TestCase:
    """A single concrete test vector."""

    id: str
    description: str
    inputs: Dict[str, str]
    expected_outputs: Optional[Dict[str, str]] = None
    rationale: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class TestSubcategory:
    """Sub-grouping within a category (e.g. X-propagation under Negative)."""

    id: str
    name: str
    rationale: str
    status: CategoryStatus
    not_applicable_reason: Optional[str] = None
    cases: List[TestCase] = field(default_factory=list)


@dataclass
class TestCategory:
    """One category in the verification plan."""

    id: str
    name: str
    rationale: str
    status: CategoryStatus
    not_applicable_reason: Optional[str] = None
    subcategories: List[TestSubcategory] = field(default_factory=list)
    cases: List[TestCase] = field(default_factory=list)

    @property
    def enabled_case_count(self) -> int:
        if self.status != CategoryStatus.ENABLED:
            return 0
        n = len(self.cases)
        n += sum(
            len(sc.cases)
            for sc in self.subcategories
            if sc.status == CategoryStatus.ENABLED
        )
        return n


@dataclass
class CoverageGoal:
    name: str
    target_percent: float
    rationale: str


@dataclass
class PlanNote:
    severity: Severity
    message: str


@dataclass
class VerificationPlan:
    """Industry-style verification plan for a single DUT."""

    dut_name: str
    dut_summary: str
    design_type: str
    port_summary: Dict[str, int]
    methodology: str
    reference_model: str
    categories: List[TestCategory]
    coverage_goals: List[CoverageGoal]
    pass_criteria: List[str]
    notes: List[PlanNote]
    total_planned_cases: int = 0

    def to_dict(self) -> dict:
        return _to_jsonable(self)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj
