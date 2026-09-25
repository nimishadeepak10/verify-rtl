"""Automatic regression detection: re-run every property this project has
ever proven for a design whenever that design's RTL changes, and flag
anything that used to pass and now doesn't.

There's no long-running daemon or file watcher in this project's
architecture (a CLI + a FastAPI web app, both invoked on demand) -- so
"automatically" here means: the NEXT time `/api/formal` is actually
called for a given module, it checks whether the RTL differs from the
last time this module was checked, and if so, re-runs every property
recorded from that prior run (not just whatever the caller happens to be
submitting this specific call) before doing anything else. A user editing
RTL and re-running verification through the normal flow gets regression
detection for free, without a separate command to remember.

Storage: one JSON file per module name under `regressions/` (gitignored,
like `logs/` -- rebuildable state, not source) recording the RTL's hash
and every property's last verdict. Keyed by module name only, matching
this project's existing `logs/formal_runs.jsonl` convention -- if two
genuinely different designs share a module name, their baselines will
collide; documented here rather than silently assumed away.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[2]
REGRESSION_DIR = _ROOT / "regressions"

_GOOD_VERDICTS = frozenset({"PROVEN", "REACHED"})


def _verdict_is_good(verdict: Optional[str]) -> bool:
    return verdict in _GOOD_VERDICTS


def compute_rtl_hash(rtl_source: str) -> str:
    """Stable content hash -- whitespace-sensitive on purpose: a formatting-
    only change is still a change worth re-checking, since this tool has no
    way to know in advance that a change is "just" cosmetic."""
    return "sha256:" + hashlib.sha256(rtl_source.encode("utf-8")).hexdigest()


def _baseline_path(module_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in module_name)
    return REGRESSION_DIR / f"{safe}.json"


@dataclass
class BaselineProperty:
    name: str
    expr: str
    kind: str
    verdict: str


@dataclass
class Baseline:
    module_name: str
    rtl_hash: str
    recorded_at: float
    properties: list[BaselineProperty] = field(default_factory=list)


def load_baseline(module_name: str) -> Optional[Baseline]:
    path = _baseline_path(module_name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Baseline(
            module_name=data["module_name"],
            rtl_hash=data["rtl_hash"],
            recorded_at=data["recorded_at"],
            properties=[BaselineProperty(**p) for p in data.get("properties", [])],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        # A corrupt or hand-edited baseline file must not crash a real
        # verification run -- treat it as "no baseline" rather than raising.
        return None


def save_baseline(module_name: str, rtl_hash: str, properties: list[BaselineProperty]) -> None:
    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = _baseline_path(module_name)
    data = {
        "module_name": module_name,
        "rtl_hash": rtl_hash,
        "recorded_at": time.time(),
        "properties": [
            {"name": p.name, "expr": p.expr, "kind": p.kind, "verdict": p.verdict}
            for p in properties
        ],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@dataclass
class RegressionEntry:
    name: str
    expr: str
    kind: str
    old_verdict: str
    new_verdict: str


@dataclass
class RegressionReport:
    baseline_existed: bool
    rtl_changed: bool
    checked: list[RegressionEntry] = field(default_factory=list)  # every baseline property re-run
    regressions: list[RegressionEntry] = field(default_factory=list)  # good -> bad
    fixed: list[RegressionEntry] = field(default_factory=list)  # bad -> good
    unchanged: list[RegressionEntry] = field(default_factory=list)  # same category either way


def diff_against_baseline(
    baseline: Optional[Baseline],
    new_rtl_hash: str,
    rerun_results: dict[str, str],  # property name -> new verdict, for every baseline property actually re-run
) -> RegressionReport:
    """Classify each re-run baseline property as a regression, a fix, or
    unchanged. `rerun_results` must contain an entry for every property in
    `baseline.properties` that was actually re-executed -- a property the
    caller couldn't re-run (e.g. it referenced a signal the new RTL no
    longer has) should be passed through with whatever verdict the actual
    attempt produced (ERROR, typically), not omitted, so it's still
    visible as a real outcome rather than silently dropped.
    """
    if baseline is None:
        return RegressionReport(baseline_existed=False, rtl_changed=True)
    rtl_changed = baseline.rtl_hash != new_rtl_hash
    report = RegressionReport(baseline_existed=True, rtl_changed=rtl_changed)
    if not rtl_changed:
        return report

    for prop in baseline.properties:
        new_verdict = rerun_results.get(prop.name)
        if new_verdict is None:
            continue  # not re-run this call -- nothing to compare yet
        entry = RegressionEntry(
            name=prop.name, expr=prop.expr, kind=prop.kind,
            old_verdict=prop.verdict, new_verdict=new_verdict,
        )
        report.checked.append(entry)
        was_good, is_good = _verdict_is_good(prop.verdict), _verdict_is_good(new_verdict)
        if was_good and not is_good:
            report.regressions.append(entry)
        elif not was_good and is_good:
            report.fixed.append(entry)
        else:
            report.unchanged.append(entry)
    return report
