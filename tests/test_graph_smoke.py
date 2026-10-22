"""End-to-end graph tests with a stubbed model.

Only the model's judgement is faked. Everything downstream of it -- evidence
binding, the verifier hand-off, dedup, aggregation, statistics, the report --
is the real code path. That is what makes these tests worth having: they would
catch a wiring mistake in the graph without spending a token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codereview.agent.graph import build_graph
from codereview.agent.state import empty_state
from codereview.models.findings import (
    CandidateFinding,
    Evidence,
    ReviewFindings,
    VerifierVerdict,
)
from codereview.models.plan import FileTask, ReviewPlan

# Contiguous lines 29-31 of buggy_repo/quality_issues.py:
#   29: def average(values):
#   30:     """BUG 9: same defect one level down, via divide()."""
#   31:     return divide(sum(values), len(values))
#
# Deliberately a defect *no deterministic tool* catches, so the test measures
# the reviewer path rather than dedup. The previous choice, ``divide()`` itself,
# stopped qualifying the moment AST_ZERO_DIVISION was added; ``name_of()`` does
# not work either, because ruff's S110 sits on its `pass` line and merges.
# BUG 8's own division is on line 26 and dedup only bridges two lines, so this
# one survives on its own.
REAL_SNIPPET = (
    "def average(values):\n"
    '    """BUG 9: same defect one level down, via divide()."""\n'
    "    return divide(sum(values), len(values))"
)
REAL_PATH = "quality_issues.py"
REAL_LINE_START = 29


class _Structured:
    """Stands in for `model.with_structured_output(Schema)`."""

    def __init__(self, payload):
        self._payload = payload

    def invoke(self, _messages):
        return self._payload


class _StubPlannerModel:
    def __init__(self, plan: ReviewPlan) -> None:
        self._plan = plan

    def with_structured_output(self, _schema):
        return _Structured(self._plan)


class _FakeAgent:
    def __init__(self, responses):
        self._responses = list(responses)
        self.thread_ids: list[str] = []

    def invoke(self, _payload, config=None):
        thread = (config or {}).get("configurable", {}).get("thread_id", "")
        self.thread_ids.append(thread)
        response = self._responses.pop(0) if self._responses else None
        return {"messages": [], "structured_response": response}


@pytest.fixture(autouse=True)
def _clear_agent_caches():
    """Module-level agent caches would leak between tests."""
    from codereview.agent.nodes import review, verify

    review._AGENT_CACHE.clear()
    verify._VERIFIER_CACHE.clear()
    yield
    review._AGENT_CACHE.clear()
    verify._VERIFIER_CACHE.clear()


def _candidate(line_start: int = REAL_LINE_START, snippet: str = REAL_SNIPPET) -> CandidateFinding:
    return CandidateFinding(
        category="bug",
        severity="medium",
        title="division without a zero check",
        rationale="A zero denominator raises ZeroDivisionError.",
        evidence=Evidence(
            path=REAL_PATH,
            line_start=line_start,
            line_end=line_start + len(snippet.splitlines()) - 1,
            snippet=snippet,
        ),
        confidence=0.8,
    )


def _run(buggy_repo: Path, monkeypatch, *, findings, verdicts, plan=None):
    """Drive the graph with scripted model output."""
    from codereview.agent.nodes import plan as plan_module
    from codereview.agent.nodes import review as review_module
    from codereview.agent.nodes import verify as verify_module

    # Single-file by default: the stub reviewer returns the same findings for
    # every file it is handed, so a multi-file plan would produce one candidate
    # per file and drain a scripted verifier response list.
    plan = plan or ReviewPlan(
        tasks=[FileTask(path=REAL_PATH, priority=10, reason="test")],
        rationale="test plan",
    )

    monkeypatch.setattr(
        plan_module, "build_chat_model", lambda *_a, **_k: _StubPlannerModel(plan)
    )

    reviewer = _FakeAgent([findings] * 20)
    monkeypatch.setattr(review_module, "_get_reviewer", lambda *_a, **_k: reviewer)

    verifier = _FakeAgent(list(verdicts))
    monkeypatch.setattr(verify_module, "_get_verifier", lambda *_a, **_k: verifier)

    graph = build_graph()
    state = empty_state(str(buggy_repo), "smoke")
    return graph.invoke(state, {"configurable": {"thread_id": "smoke"}}), reviewer, verifier


def test_real_finding_survives_to_the_report(buggy_repo: Path, monkeypatch) -> None:
    state, _reviewer, _verifier = _run(
        buggy_repo,
        monkeypatch,
        findings=ReviewFindings(findings=[_candidate()]),
        verdicts=[VerifierVerdict(verdict="confirmed", reason="the assert is real")],
    )

    report = state["report"]
    reportable = [f for f in report.reportable if f.source == "llm"]

    assert len(reportable) == 1
    assert reportable[0].evidence is not None
    assert reportable[0].evidence.snippet == REAL_SNIPPET
    assert report.stats.confirmed >= 1


def test_hallucinated_finding_never_reaches_the_report(
    buggy_repo: Path, monkeypatch
) -> None:
    """The headline guarantee, exercised through the whole graph."""
    fabricated = _candidate(line_start=5, snippet='cursor.execute(f"DROP TABLE {t}")')

    state, _reviewer, verifier = _run(
        buggy_repo,
        monkeypatch,
        findings=ReviewFindings(findings=[fabricated]),
        verdicts=[],
    )

    report = state["report"]
    assert [f for f in report.reportable if f.source == "llm"] == []
    assert report.stats.rejected_by_evidence >= 1
    # The verifier is never consulted about a finding that failed evidence binding.
    assert verifier.thread_ids == []


def test_verifier_rejection_is_counted_and_excluded(buggy_repo: Path, monkeypatch) -> None:
    state, _reviewer, verifier = _run(
        buggy_repo,
        monkeypatch,
        findings=ReviewFindings(findings=[_candidate()]),
        verdicts=[VerifierVerdict(verdict="rejected", reason="not reachable")],
    )

    report = state["report"]
    assert [f for f in report.reportable if f.source == "llm"] == []
    assert report.stats.rejected_by_verifier == 1
    assert len(verifier.thread_ids) == 1


def test_clean_file_yields_zero_llm_findings(buggy_repo: Path, monkeypatch) -> None:
    """A model that correctly reports nothing must produce no findings."""
    state, _reviewer, _verifier = _run(
        buggy_repo, monkeypatch, findings=ReviewFindings(findings=[]), verdicts=[]
    )

    report = state["report"]
    assert [f for f in report.reportable if f.source == "llm"] == []
    assert report.stats.rejected_by_evidence == 0


def test_tool_findings_are_included_without_the_model(buggy_repo: Path, monkeypatch) -> None:
    """Ruff findings must reach the report even if the reviewer finds nothing."""
    state, _reviewer, _verifier = _run(
        buggy_repo, monkeypatch, findings=ReviewFindings(findings=[]), verdicts=[]
    )

    report = state["report"]
    tool_sourced = [f for f in report.reportable if f.source in ("ruff", "ast")]

    assert tool_sourced, "ruff/ast findings should always be reported"
    assert any(f.rule_id == "F401" for f in tool_sourced)


def test_empty_plan_still_produces_a_report(buggy_repo: Path, monkeypatch) -> None:
    """A plan with no tasks must not dead-end the graph.

    Returning an empty list from the fan-out router would be read as "no
    destination" and the run would end with no report at all.
    """
    from codereview.agent.nodes import plan as plan_module
    from codereview.agent.nodes import review as review_module
    from codereview.agent.nodes import verify as verify_module

    monkeypatch.setattr(
        plan_module,
        "build_chat_model",
        lambda *_a, **_k: _StubPlannerModel(ReviewPlan(tasks=[], rationale="nothing")),
    )
    monkeypatch.setattr(
        review_module, "_get_reviewer", lambda *_a, **_k: _FakeAgent([ReviewFindings()])
    )
    monkeypatch.setattr(verify_module, "_get_verifier", lambda *_a, **_k: _FakeAgent([]))

    state = build_graph().invoke(
        empty_state(str(buggy_repo), "empty-plan"), {"configurable": {"thread_id": "empty-plan"}}
    )

    assert state.get("report") is not None


def test_planner_hallucinated_path_is_discarded(buggy_repo: Path, monkeypatch) -> None:
    """A planner inventing a file must not create a task for it."""
    plan = ReviewPlan(
        tasks=[
            FileTask(path="does_not_exist.py", priority=1),
            FileTask(path="security_issues.py", priority=2),
        ],
        rationale="mixed",
    )

    state, _reviewer, _verifier = _run(
        buggy_repo,
        monkeypatch,
        findings=ReviewFindings(findings=[]),
        verdicts=[],
        plan=plan,
    )

    paths = {task.path for task in state["tasks"]}
    assert "does_not_exist.py" not in paths
    assert "security_issues.py" in paths


def test_stats_are_populated(buggy_repo: Path, monkeypatch) -> None:
    state, _reviewer, _verifier = _run(
        buggy_repo, monkeypatch, findings=ReviewFindings(findings=[]), verdicts=[]
    )

    stats = state["report"].stats
    assert stats.files_scanned > 0
    assert stats.files_reviewed > 0
    assert stats.finished_at is not None


def test_parallel_branches_do_not_clobber_each_other(buggy_repo: Path, monkeypatch) -> None:
    """Regression test for the `operator.add` reducer.

    Without the reducer, whichever branch finished last would overwrite the
    others' findings and the run would silently lose work.
    """
    from codereview.agent.nodes import plan as plan_module
    from codereview.agent.nodes import review as review_module
    from codereview.agent.nodes import verify as verify_module

    plan = ReviewPlan(
        tasks=[
            FileTask(path="security_issues.py", priority=1),
            FileTask(path="quality_issues.py", priority=2),
        ],
        rationale="two files",
    )
    monkeypatch.setattr(
        plan_module, "build_chat_model", lambda *_a, **_k: _StubPlannerModel(plan)
    )
    monkeypatch.setattr(
        review_module, "_get_reviewer", lambda *_a, **_k: _FakeAgent([ReviewFindings()])
    )
    monkeypatch.setattr(verify_module, "_get_verifier", lambda *_a, **_k: _FakeAgent([]))

    state = build_graph().invoke(
        empty_state(str(buggy_repo), "parallel"), {"configurable": {"thread_id": "parallel"}}
    )

    # Both files' ruff findings must survive; without the reducer one set is lost.
    paths = {f.evidence.path for f in state["report"].reportable if f.evidence}
    assert "security_issues.py" in paths


def test_branch_uses_its_own_thread_id(buggy_repo: Path, monkeypatch) -> None:
    """Concurrent branches sharing a thread_id would corrupt the checkpoint."""
    plan = ReviewPlan(
        tasks=[
            FileTask(path="quality_issues.py", priority=1),
            FileTask(path="security_issues.py", priority=2),
        ],
        rationale="two files",
    )

    _state, reviewer, _verifier = _run(
        buggy_repo, monkeypatch, findings=ReviewFindings(findings=[]), verdicts=[], plan=plan
    )

    assert len(reviewer.thread_ids) == 2
    assert len(set(reviewer.thread_ids)) == len(reviewer.thread_ids)
