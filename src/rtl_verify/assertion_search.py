"""Signal-wise assertion search: Monte Carlo Tree Self-Refine with a reward
that comes from the solver, not from a model's opinion.

Adapted from SANGAM (Gupta, Mali, Karfa; "SystemVerilog Assertion Generation
via Monte Carlo Tree Self-Refine", IEEE ICLAD 2025). Kept from the paper:

  - one search PER SIGNAL, over sets of assertions about that signal;
  - the MCTSr loop: a weak root answer, then n rollouts of select (UCT),
    expand (self-refine from feedback), evaluate, and back-propagate with
    Q'(a) = (Q(a) + max over children of Q) / 2;
  - exploration constant c = 1.4 and four rollouts as the defaults;
  - typed generation (width, connectivity, function);
  - a final combination step that pools every node's assertions and removes
    duplicates.

Changed, and why. The paper's node reward is a "Critic" LLM's score in
[-100, 100]. Its authors report the critic is "overly optimistic" and patch
that with three devices (a strict prompt, suppressing scores above 95, and
re-sampling the score each time a node is selected), and the tool feedback
they use is syntax errors only; whether assertions are actually correct is
settled afterwards by a person. They also name a limit: the framework cannot
guarantee that assertions are not redundant with one another.

Every one of those is a question a solver can answer exactly, so here:

  - REWARD IS DETERMINISTIC. Each assertion is classified by a real run:
    syntax/elaboration error, FALSIFIED, UNKNOWN, PROVEN but vacuous, PROVEN
    and implied by the others, or PROVEN, non-vacuous and independent. The
    node reward is computed from those classes. Nothing needs suppressing or
    re-sampling, and two runs of the same node score identically.
  - FEEDBACK IS EVIDENCE. The refiner is told which assertion failed and shown
    the counterexample's values for the signals involved, which guard never
    fired, and which assertion is implied by which. Syntax logs are the
    smallest part of that.
  - IRREDUNDANCY IS CHECKED. An assertion that the ones already kept imply
    logically is dropped, in the order produced. It is checked by a solver
    with every signal free: with the design in, every proven assertion is
    implied by the design alone, which would make all of them redundant with
    everything. This is the paper's stated gap.

A FALSIFIED assertion is not scored as a success and is not silently thrown
away: against a design believed correct it is either a wrong assertion or a
real bug, and the counterexample is returned for a person to decide. The
paper's own first step has the same property (its authors manually verify
against the specification).

The language model is behind a `Refiner` interface, so the search, the
reward and the combination are exercised with a scripted refiner in tests and
with a real model in use.
"""

from __future__ import annotations

import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from .analyzer import RtlModule
from .formal_props import generate_formal_wrapper, recommended_engine_chain
from .signal_bank import SignalInfo
from .vacuity import run_vacuity_check
from .waveform import vcd_to_json

CATEGORIES = ("width", "connectivity", "function")

# Reward per assertion class, on the paper's [-100, 100] scale.
SCORE = {
    "PROVEN": 100,              # proven, non-vacuous, not implied by the others
    "PROVEN_UNCHECKED": 70,     # proven; not in a shape whose guard can be checked for vacuity
    "REDUNDANT": -10,           # proven but logically implied by the other kept assertions
    "VACUOUS": -20,             # proven only because its trigger never happens
    "FALSIFIED": -30,           # a counterexample exists: wrong assertion or real bug
    "UNKNOWN": 0,               # solver could not decide
    "SYNTAX_ERROR": -100,       # does not elaborate, or mentions a signal that does not exist
}
_NEEDS_REVIEW = "FALSIFIED"

_ALLOWED_FUNCS = {"past", "onehot", "onehot0", "countones", "rose", "fell", "stable", "changed",
                  "isunknown", "signed", "unsigned", "clog2", "bits"}
_SIZED = re.compile(r"\d*'[sS]?[bBdDhHoO][0-9a-fA-FxXzZ_?]+")


@dataclass
class Assertion:
    name: str
    expr: str
    category: str = "function"


@dataclass
class Verdict:
    status: str                       # key of SCORE
    detail: str = ""
    counterexample: Optional[Dict[str, List[Tuple[int, str]]]] = None
    redundant_with: List[str] = field(default_factory=list)


@dataclass
class NodeEvaluation:
    verdicts: List[Tuple[Assertion, Verdict]]
    reward: float
    feedback: str


class Refiner(Protocol):
    def initial(self, info: SignalInfo) -> List[Assertion]: ...
    def refine(self, info: SignalInfo, assertions: List[Assertion], feedback: str, rollout: int) -> List[Assertion]: ...


@dataclass
class Node:
    id: int
    assertions: List[Assertion]
    parent: Optional[int]
    reward: float = 0.0
    q: float = 0.0
    visits: int = 0
    children: List[int] = field(default_factory=list)
    evaluation: Optional[NodeEvaluation] = None


@dataclass
class SearchResult:
    signal: str
    nodes: List[Node]
    kept: List[Tuple[Assertion, Verdict]]            # PROVEN, non-vacuous, irredundant
    needs_review: List[Tuple[Assertion, Verdict]]    # FALSIFIED, with counterexamples
    dropped: List[Tuple[Assertion, Verdict]]         # syntax errors, vacuous, redundant, unknown
    refiner_calls: int
    best_node: int

    def view(self) -> dict:
        def row(a: Assertion, v: Verdict) -> dict:
            return {"name": a.name, "expr": a.expr, "category": a.category, "status": v.status,
                    "detail": v.detail, "counterexample": v.counterexample,
                    "redundant_with": v.redundant_with}
        return {
            "signal": self.signal,
            "kept": [row(a, v) for a, v in self.kept],
            "needs_review": [row(a, v) for a, v in self.needs_review],
            "dropped": [row(a, v) for a, v in self.dropped],
            "refiner_calls": self.refiner_calls,
            "best_node": self.best_node,
            "tree": [{"id": n.id, "parent": n.parent, "reward": round(n.reward, 2), "q": round(n.q, 2),
                      "visits": n.visits, "children": n.children,
                      "assertions": [a.expr for a in n.assertions]} for n in self.nodes],
        }


# ------------------------------------------------------------------ evaluation


def normalize(expr: str) -> str:
    """Whitespace-free form with redundant outer parentheses removed, so
    trivially re-spelled copies of one assertion compare equal."""
    s = re.sub(r"\s+", "", expr)
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        depth = 0
        wraps = True
        for i, ch in enumerate(s):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(s) - 1:
                wraps = False
                break
        if not wraps:
            break
        s = s[1:-1]
    return s


def unknown_identifiers(expr: str, known: set) -> List[str]:
    """Names in `expr` that are not module signals. The paper forbids the
    model from inventing signals; this makes that a checked fact."""
    s = _SIZED.sub(" ", expr)
    s = re.sub(r"\$\w+", " ", s)
    bad = []
    for tok in re.findall(r"[A-Za-z_]\w*", s):
        if tok not in known and tok not in bad:
            bad.append(tok)
    return bad


class SolverEvaluator:
    """Classifies assertions by running the solver. Verdicts for a given
    expression are cached; redundancy depends on the set and is cached by
    (expression, other expressions)."""

    def __init__(self, module: RtlModule, rtl_path: Path, backend, work_root: Optional[Path] = None,
                 timeout_sec: int = 60, depth_override: int = 0, target_count: int = 4):
        self.module = module
        self.rtl_path = rtl_path
        self.backend = backend
        self.work_root = work_root or Path(tempfile.mkdtemp(prefix="assert_search_"))
        self.timeout_sec = timeout_sec
        self.depth_override = depth_override
        self.target_count = target_count
        self.known = {p.name for p in module.ports}
        self._single: Dict[str, Verdict] = {}
        self._implied: Dict[Tuple[str, Tuple[str, ...]], bool] = {}
        self._n = 0
        self.solver_runs = 0

    # -- single runs ------------------------------------------------------
    def _prove(self, props: Sequence[Tuple[str, str, str]]):
        self._n += 1
        d = self.work_root / f"run_{self._n}"
        d.mkdir(parents=True, exist_ok=True)
        wrapper = d / "wrapper.sv"
        wrapper.write_text(generate_formal_wrapper(self.module, list(props)), encoding="utf-8")
        chain = recommended_engine_chain(self.module, kind="assert", depth_override=self.depth_override)
        per_attempt = max(30, self.timeout_sec // len(chain))
        res = None
        for i, cfg in enumerate(chain):
            self.solver_runs += 1
            res = self.backend.run(self.rtl_path, wrapper, d / f"engine_{i}",
                                   top=f"{self.module.name}_formal_top", depth=cfg["depth"],
                                   mode=cfg["mode"], engine=cfg["engine"], timeout_sec=per_attempt)
            if res.status in ("PASS", "FAIL"):
                break
        return res

    def _cex(self, vcd_path: Optional[Path], expr: str):
        if vcd_path is None:
            return None
        wf = vcd_to_json(vcd_path, module=self.module)
        if "error" in wf:
            return None
        mentioned = set(re.findall(r"[A-Za-z_]\w*", expr))
        out: Dict[str, List[Tuple[int, str]]] = {}
        for sig in wf["signals"]:
            base = sig["name"].split(".")[-1]
            if base in mentioned and base not in out:
                out[base] = [(t["time"], t["value"]) for t in sig["transitions"]][-10:]
        return out or None

    def judge(self, a: Assertion) -> Verdict:
        key = normalize(a.expr)
        if key in self._single:
            return self._single[key]
        v = self._judge(a)
        self._single[key] = v
        return v

    def _judge(self, a: Assertion) -> Verdict:
        if not a.expr.strip():
            return Verdict("SYNTAX_ERROR", "empty expression")
        bad = unknown_identifiers(a.expr, self.known)
        if bad:
            return Verdict("SYNTAX_ERROR", f"mentions signals that do not exist in this design: {bad}")
        try:
            res = self._prove([("candidate", a.expr, "assert")])
        except ValueError as e:
            return Verdict("SYNTAX_ERROR", str(e))
        if res.status == "FAIL":
            return Verdict("FALSIFIED", "a counterexample exists", self._cex(res.vcd_path, a.expr))
        if res.status == "PASS":
            vac = run_vacuity_check(self.module, self.rtl_path, self.backend, "candidate", a.expr,
                                    timeout_sec=self.timeout_sec, depth_override=self.depth_override,
                                    work_root=self.work_root / f"vac_{self._n}")
            if vac.status == "VACUOUS":
                return Verdict("VACUOUS", "proven, but its guard is never reachable")
            if vac.status == "NON_VACUOUS":
                return Verdict("PROVEN", "proven; guard reachable")
            return Verdict("PROVEN_UNCHECKED", "proven; vacuity not decidable for this shape")
        if res.status == "ERROR":
            line = next((ln for ln in (res.log or "").splitlines() if "ERROR" in ln or "syntax" in ln.lower()), "")
            return Verdict("SYNTAX_ERROR", line.strip()[:200] or "the tool could not elaborate it")
        return Verdict("UNKNOWN", f"solver ended {res.status}")

    # -- redundancy -------------------------------------------------------
    def _free_check(self, others: Sequence[Assertion], target: Assertion) -> bool:
        """Does `others` logically imply `target` with the design taken OUT of
        the picture? Every signal is a free input, so only the assertions'
        own meaning counts. (With the design in, every PROVEN assertion is
        implied by the design alone, which would make all of them
        redundant with everything.)"""
        # The design's own clock port clocks the check; a design with none gets
        # a synthetic one.
        clk = self.module.clock_port or "__impl_clk"
        decls = [] if self.module.clock_port else ["    input __impl_clk;"]
        for p in self.module.ports:
            rng = p.range_str()
            decls.append(f"    input {rng + ' ' if rng else ''}{p.name};")
        lines = []
        for i, o in enumerate(others):
            lines.append(self._guarded(f"o{i}", o.expr, "assume"))
        lines.append(self._guarded("target", target.expr, "assert"))
        names = ([] if self.module.clock_port else ["__impl_clk"]) + [p.name for p in self.module.ports]
        text = ('module __impl_check(' + ', '.join(names) + ');' + chr(10)
                + chr(10).join(decls) + chr(10)
                + '`ifdef FORMAL' + chr(10) + f'    always @(posedge {clk}) begin' + chr(10)
                + chr(10).join(lines) + chr(10) + '    end' + chr(10) + '`endif' + chr(10) + 'endmodule' + chr(10))
        self._n += 1
        d = self.work_root / f"free_{self._n}"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "impl.sv"
        f.write_text(text, encoding="utf-8")
        self.solver_runs += 1
        res = self.backend.run(f, f, d / "run", top="__impl_check", depth=8, mode="bmc",
                               engine="smtbmc", timeout_sec=self.timeout_sec)
        return res.status == "PASS"

    @staticmethod
    def _guarded(name: str, expr: str, kind: str) -> str:
        line = f"{name}: {kind} ({expr});"
        if "$past(" in expr:
            return f"        if (!$initstate()) begin {line} end"
        return f"        {line}"

    def implied_by(self, a: Assertion, others: Sequence[Assertion]) -> bool:
        key = (normalize(a.expr), tuple(sorted(normalize(o.expr) for o in others)))
        if key not in self._implied:
            self._implied[key] = self._free_check(others, a)
        return self._implied[key]

    def prune_redundant(self, items: List[Tuple[Assertion, Verdict]]):
        """Greedy, in order: drop an assertion implied by the ones still kept.
        Returns (kept, redundant) with `redundant_with` filled in."""
        kept = list(items)
        redundant: List[Tuple[Assertion, Verdict]] = []
        for a, v in list(items):
            others = [k for k, _ in kept if k is not a]
            if not others:
                continue
            if self.implied_by(a, others):
                kept = [(k, kv) for k, kv in kept if k is not a]
                nv = Verdict("REDUNDANT", "logically implied by the other kept assertions",
                             redundant_with=[o.name for o in others])
                redundant.append((a, nv))
        return kept, redundant

    # -- nodes ------------------------------------------------------------
    def evaluate(self, assertions: List[Assertion]) -> NodeEvaluation:
        seen, uniq = set(), []
        for a in assertions:                       # duplicates inside one node count once
            k = normalize(a.expr)
            if k not in seen:
                seen.add(k)
                uniq.append(a)
        judged = [(a, self.judge(a)) for a in uniq]
        good = [(a, v) for a, v in judged if v.status in ("PROVEN", "PROVEN_UNCHECKED")]
        _, redundant = self.prune_redundant(good)
        red = {id(ra): rv for ra, rv in redundant}
        final = [(a, red.get(id(a), v)) for a, v in judged]
        total = sum(SCORE[v.status] for _, v in final)
        denom = max(len(final), self.target_count, 1)
        reward = max(-100.0, min(100.0, total / denom))
        return NodeEvaluation(final, reward, self._feedback(final))

    @staticmethod
    def _feedback(items: List[Tuple[Assertion, Verdict]]) -> str:
        lines = []
        for a, v in items:
            head = f"- [{a.category}] {a.expr}"
            if v.status in ("PROVEN", "PROVEN_UNCHECKED"):
                lines.append(f"{head}\n    PROVEN and useful. Keep it.")
            elif v.status == "REDUNDANT":
                lines.append(f"{head}\n    PROVEN but implied by {v.redundant_with}. Replace it with a "
                             "different property of this signal.")
            elif v.status == "VACUOUS":
                lines.append(f"{head}\n    PROVEN only because its antecedent can never happen. Fix the "
                             "antecedent so it describes a reachable situation.")
            elif v.status == "FALSIFIED":
                cex = "; ".join(f"{s}: {', '.join(f'{val}@{t}' for t, val in tr[-4:])}"
                                for s, tr in (v.counterexample or {}).items())
                lines.append(f"{head}\n    FALSIFIED. Counterexample values -> {cex or 'n/a'}. Correct the "
                             "claim, or keep it only if the design is meant to satisfy it.")
            elif v.status == "SYNTAX_ERROR":
                lines.append(f"{head}\n    INVALID: {v.detail}. Use only existing signal names.")
            else:
                lines.append(f"{head}\n    UNDECIDED ({v.detail}). Prefer a simpler formulation.")
        return "\n".join(lines)


# ---------------------------------------------------------------------- search


def _uct(node: Node, parent_visits: int, c: float, eps: float = 1e-6) -> float:
    # Q is kept in [-1, 1] here so the exploration constant means what it does
    # in the paper's formula; the paper's [-100, 100] scale would make c
    # negligible.
    return node.q / 100.0 + c * math.sqrt((math.log(max(parent_visits, 1)) + 1.0) / (node.visits + eps))


def search_signal(info: SignalInfo, evaluator: SolverEvaluator, refiner: Refiner, rollouts: int = 4,
                  c: float = 1.4, max_children: int = 2) -> SearchResult:
    nodes: List[Node] = []
    calls = 0

    def add(assertions: List[Assertion], parent: Optional[int]) -> Node:
        n = Node(id=len(nodes), assertions=assertions, parent=parent)
        n.evaluation = evaluator.evaluate(assertions)
        n.reward = n.q = n.evaluation.reward
        n.visits = 1
        nodes.append(n)
        if parent is not None:
            nodes[parent].children.append(n.id)
        return n

    root_assertions = refiner.initial(info)
    calls += 1
    add(root_assertions, None)

    for r in range(1, rollouts + 1):
        open_nodes = [n for n in nodes if len(n.children) < max_children]
        if not open_nodes:
            break
        total_visits = sum(n.visits for n in nodes)

        def score(n: Node) -> float:
            pv = nodes[n.parent].visits if n.parent is not None else total_visits
            return _uct(n, pv, c)

        chosen = max(open_nodes, key=score)            # greedy on UCT, as in the paper
        new_assertions = refiner.refine(info, chosen.assertions, chosen.evaluation.feedback, r)
        calls += 1
        add(new_assertions, chosen.id)
        # Back-propagation: Q'(a) = (Q(a) + max_child Q) / 2, walking to the root.
        cur: Optional[int] = chosen.id
        while cur is not None:
            node = nodes[cur]
            best_child = max(nodes[k].q for k in node.children)
            node.q = 0.5 * (node.q + best_child)
            node.visits += 1
            cur = node.parent

    # Combination: pool every node's assertions, then one global pass.
    pooled: Dict[str, Tuple[Assertion, Verdict]] = {}
    for n in nodes:
        for a, v in n.evaluation.verdicts:
            k = normalize(a.expr)
            base = evaluator.judge(a)                  # the unconditional verdict, not a set-relative one
            pooled.setdefault(k, (a, base))
    good = [(a, v) for a, v in pooled.values() if v.status in ("PROVEN", "PROVEN_UNCHECKED")]
    kept, redundant = evaluator.prune_redundant(good)
    review = [(a, v) for a, v in pooled.values() if v.status == _NEEDS_REVIEW]
    dropped = [(a, v) for a, v in pooled.values() if v.status not in ("PROVEN", "PROVEN_UNCHECKED", _NEEDS_REVIEW)]
    dropped += redundant
    best = max(nodes, key=lambda n: n.reward).id
    return SearchResult(signal=info.name, nodes=nodes, kept=kept, needs_review=review,
                        dropped=dropped, refiner_calls=calls, best_node=best)


# ---------------------------------------------------------------- LLM refiner

_SYSTEM = """You are a formal-verification engineer writing SystemVerilog assertions for ONE signal of an RTL design.

Rules:
- Every expression must be a single boolean expression over the listed signal names only. Do not invent signals.
  Internal registers are available under the exact names given in the signal information.
- Write if-then claims as `!(guard) || (conclusion)`. Use $past(x) for previous-cycle values.
- Cover these categories where they apply to the signal: width (value range / bit-width facts), connectivity
  (how it relates to the signals that drive or read it), function (the behavior the specification describes).
- Each assertion must state something the specification or the RTL statements support. Do not guess.
- When you receive feedback from a solver, treat it as ground truth: a counterexample means the claim is wrong
  for this design; a vacuous claim needs a reachable antecedent; a redundant claim should be replaced by a
  different property. Keep assertions that were proven and useful.
Return JSON only."""

_SCHEMA = {
    "type": "object",
    "properties": {"assertions": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "expr": {"type": "string"},
                       "category": {"type": "string", "enum": list(CATEGORIES)}},
        "required": ["name", "expr", "category"], "additionalProperties": False}}},
    "required": ["assertions"], "additionalProperties": False,
}


class LLMRefiner:
    """Language-model refiner. `signals_overview` is a short list of the
    module's signal names so the model can see what exists."""

    def __init__(self, module: RtlModule, signals_overview: str = "", complete_structured=None):
        from . import llm_client
        self._call = complete_structured or llm_client.complete_structured
        self._overview = signals_overview or ", ".join(p.name for p in module.ports)
        self._module = module

    def _ask(self, user: str) -> List[Assertion]:
        out = self._call(_SYSTEM, user, _SCHEMA, max_tokens=4000)
        items = out.get("assertions", [])
        res = []
        for i, it in enumerate(items):
            expr = str(it.get("expr", "")).strip()
            cat = it.get("category") if it.get("category") in CATEGORIES else "function"
            name = re.sub(r"\W", "_", str(it.get("name") or f"a{i}"))
            res.append(Assertion(name=f"{name}_{i}", expr=expr, category=cat))
        return res

    def initial(self, info: SignalInfo) -> List[Assertion]:
        # Deliberately a SHORT weak answer, as in the paper: a root that already
        # holds many assertions would confine every path to its neighbourhood.
        return self._ask(
            f"Design signals available: {self._overview}\n\n{info.prompt_block()}\n\n"
            "Write ONE or TWO short, clearly correct assertions about this signal (a weak first answer; "
            "it will be refined).")

    def refine(self, info: SignalInfo, assertions: List[Assertion], feedback: str, rollout: int) -> List[Assertion]:
        return self._ask(
            f"Design signals available: {self._overview}\n\n{info.prompt_block()}\n\n"
            f"Current assertions with solver feedback (refinement round {rollout}):\n{feedback}\n\n"
            "Return the improved full set: keep what was proven and useful, repair or replace the rest, and "
            "add new correct assertions across width, connectivity and function.")
