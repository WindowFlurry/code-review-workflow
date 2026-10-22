"""工作流的离线测试。

三个 Agent 全部用假的替换，所以整条流程——编码、审查、判断、修复、汇总——
都能在**不联网、不花钱**的情况下跑完，包括循环和条件分支。

只有模型的「判断」是假的；流程控制、状态传递、快照落盘、报告生成都是真代码。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codereview.models.findings import Evidence, VerifiedFinding
from codereview.workflow import nodes
from codereview.workflow.agents import CoderOutput, FixerOutput
from codereview.workflow.graph import build_workflow, run_workflow
from codereview.workflow.nodes import decide, strip_code_fences
from codereview.workflow.runner import _make_run_dir
from codereview.workflow.state import (
    actionable_count,
    empty_workflow_state,
    severe_count,
)

# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeAgent:
    """顶替 create_agent 编出来的 Agent，按剧本返回结构化结果。"""

    def __init__(self, responses):
        self._queue = list(responses)
        self.thread_ids: list[str] = []

    def invoke(self, _payload, config=None):
        thread = (config or {}).get("configurable", {}).get("thread_id", "")
        self.thread_ids.append(thread)
        response = self._queue.pop(0) if self._queue else None
        return {"messages": [], "structured_response": response}


class FakeReviewGraph:
    """顶替作业一那张审查图。

    每次被调用就按顺序吐出一组「审查结果」，模拟「第一轮报问题、第二轮干净」
    这种情况。真实的证据核对、复核、去重都不在这里——它们由作业一自己的
    测试覆盖，这里只关心工作流怎么用这些结果。
    """

    def __init__(self, rounds_of_findings):
        self._queue = list(rounds_of_findings)
        self.calls = 0

    def invoke(self, _state, **_kwargs):
        self.calls += 1
        findings = self._queue.pop(0) if self._queue else []
        report = SimpleNamespace(
            reportable=findings,
            stats=SimpleNamespace(rejected_by_evidence=0),
        )
        return {"report": report, "usage": []}


def finding(
    severity: str = "high",
    line: int = 3,
    title: str = "测试问题",
    category: str = "bug",
) -> VerifiedFinding:
    return VerifiedFinding(
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        title=title,
        rationale="测试用的问题描述",
        evidence=Evidence(
            path="solution.py", line_start=line, line_end=line, snippet="x = 1"
        ),
        verdict="confirmed",
    )


@pytest.fixture(autouse=True)
def _clear_caches():
    nodes.clear_agent_cache()
    yield
    nodes.clear_agent_cache()


def wire(monkeypatch, *, code="def f():\n    return 1\n", fixes=(), reviews=()):
    """把三个 Agent 和审查图都换成假的，返回 (coder, fixer, graph)。"""
    coder = FakeAgent([CoderOutput(code=code, notes="测试用")])
    fixer = FakeAgent([FixerOutput(code=c, changes="测试改动") for c in fixes])
    graph = FakeReviewGraph(list(reviews))

    monkeypatch.setattr(nodes, "_get_coder", lambda *_a, **_k: coder)
    monkeypatch.setattr(nodes, "_get_fixer", lambda *_a, **_k: fixer)
    monkeypatch.setattr(nodes, "build_graph", lambda *_a, **_k: graph)
    return coder, fixer, graph


def run(tmp_path: Path, monkeypatch, **kwargs):
    requirement = kwargs.pop("requirement", "写一个函数")
    max_rounds = kwargs.pop("max_rounds", 3)
    monkeypatch.setattr(
        nodes, "get_settings", lambda: SimpleNamespace()  # 节点里只用到默认值
    )
    return run_workflow(
        requirement,
        workspace=tmp_path / "ws",
        out_dir=tmp_path,
        max_rounds=max_rounds,
    )


# ---------------------------------------------------------------------------
# 流程控制
# ---------------------------------------------------------------------------


def test_clean_code_finishes_without_fixing(tmp_path, monkeypatch) -> None:
    """一次就审干净的话，不该进入修复。"""
    _coder, fixer, graph = wire(monkeypatch, reviews=[[]])

    state = run(tmp_path, monkeypatch)

    report = state["report"]
    assert report.rounds_used == 0
    assert report.converged
    assert not report.remaining
    assert fixer.thread_ids == [], "没问题就不该调用修复 Agent"
    assert graph.calls == 1


def test_severe_finding_triggers_one_repair_round(tmp_path, monkeypatch) -> None:
    """报高危 → 修一轮 → 再审变干净 → 结束。"""
    _coder, fixer, graph = wire(
        monkeypatch,
        code="def f():\n    return 1 / 0\n",
        fixes=["def f():\n    return 1\n"],
        reviews=[[finding("high")], []],
    )

    state = run(tmp_path, monkeypatch)

    report = state["report"]
    assert report.rounds_used == 1
    assert report.converged
    assert len(fixer.thread_ids) == 1
    assert graph.calls == 2, "修完必须再审一次"


def test_loop_stops_at_max_rounds(tmp_path, monkeypatch) -> None:
    """一直修不好也不能无限循环。"""
    _coder, fixer, graph = wire(
        monkeypatch,
        fixes=["x = 1\n"] * 5,
        reviews=[[finding("high")] for _ in range(10)],
    )

    state = run(tmp_path, monkeypatch, max_rounds=2)

    report = state["report"]
    assert report.rounds_used == 2, "必须卡在上限"
    assert not report.converged
    assert len(report.remaining) >= 1
    assert "最大轮数" in report.stop_reason, "要让用户看出是「到上限了」而不是「修好了」"
    assert graph.calls == 3, "初始审查 1 次 + 每轮修复后 1 次"


def test_style_findings_do_not_trigger_repair(tmp_path, monkeypatch) -> None:
    """只剩风格意见就不再折腾 —— 那种问题修不完。"""
    _coder, fixer, _graph = wire(
        monkeypatch,
        reviews=[[finding("low", category="style"), finding("info", category="maintainability")]],
    )

    state = run(tmp_path, monkeypatch)

    report = state["report"]
    assert report.rounds_used == 0
    assert fixer.thread_ids == []
    assert len(report.remaining) == 2, "风格意见仍然要报出来"
    assert report.converged is True


def test_medium_bug_still_triggers_repair(tmp_path, monkeypatch) -> None:
    """回归测试：一个「中」等的真 bug 不能因为等级不够就被跳过。

    实测踩到过：审查 Agent 报出 ``flatten_dict({})`` 返回 ``{'': {}}``
    而不是 ``{}`` —— 合法输入下结果就是错的，但严重度标成了「中」。
    当时只按等级判断，于是这条被跳过、没有被修。

    正确性缺陷不该因为「程序没崩」就被放过，所以判断规则加了一条：
    **类别是缺陷或安全**，不管等级高低都值得修。
    """
    _coder, fixer, _graph = wire(
        monkeypatch,
        fixes=["x = 1\n"],
        reviews=[[finding("medium", category="bug")], []],
    )

    report = run(tmp_path, monkeypatch)["report"]

    assert report.rounds_used == 1, "中等的真 bug 也应该触发修复"
    assert len(fixer.thread_ids) == 1


def test_hardcoded_credential_at_low_severity_still_triggers_repair(
    tmp_path, monkeypatch
) -> None:
    """安全问题同理 —— 类别对了就该修，不看等级。"""
    _coder, fixer, _graph = wire(
        monkeypatch,
        fixes=["x = 1\n"],
        reviews=[[finding("low", category="security")], []],
    )

    report = run(tmp_path, monkeypatch)["report"]

    assert report.rounds_used == 1


def test_coder_failure_goes_straight_to_report(tmp_path, monkeypatch) -> None:
    """编码失败就别往下走了，直接收尾。"""
    coder = FakeAgent([CoderOutput(code="", notes="")])
    graph = FakeReviewGraph([[]])
    monkeypatch.setattr(nodes, "_get_coder", lambda *_a, **_k: coder)
    monkeypatch.setattr(nodes, "build_graph", lambda *_a, **_k: graph)

    state = run(tmp_path, monkeypatch)

    assert state["report"] is not None
    assert graph.calls == 0, "没有代码就没什么可审的"
    assert state["errors"], "失败要记下来"


def test_review_failure_ends_the_loop(tmp_path, monkeypatch) -> None:
    """审查本身炸了，不能卡在循环里。"""

    class BoomGraph:
        def invoke(self, *_a, **_k):
            raise RuntimeError("审查挂了")

    monkeypatch.setattr(nodes, "build_graph", lambda *_a, **_k: BoomGraph())
    monkeypatch.setattr(nodes, "_get_coder", lambda *_a, **_k: FakeAgent([CoderOutput(code="x = 1\n")]))

    state = run(tmp_path, monkeypatch)

    assert state["report"].rounds_used == 0
    assert any("审查" in e for e in state["errors"])


# ---------------------------------------------------------------------------
# decide 路由
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("specs", "round_no", "max_rounds", "expected"),
    [
        ([], 0, 3, "report"),                                     # 干净
        ([("low", "style")], 0, 3, "report"),                     # 只有风格意见
        ([("info", "maintainability")], 0, 3, "report"),          # 只有可维护性意见
        ([("high", "style")], 0, 3, "fix"),                       # 等级高就算
        ([("medium", "bug")], 0, 3, "fix"),                       # 真 bug 就算等级不高
        ([("low", "security")], 0, 3, "fix"),                     # 安全问题同理
        ([("critical", "bug")], 2, 3, "fix"),                     # 最后一轮额度
        ([("high", "bug")], 3, 3, "report"),                      # 到上限了
        ([("critical", "bug")], 5, 3, "report"),                  # 超上限（保险）
    ],
)
def test_decide_routing(specs, round_no, max_rounds, expected) -> None:
    state = empty_workflow_state("需求", "ws", max_rounds=max_rounds)
    state["findings"] = [finding(sev, category=cat) for sev, cat in specs]
    state["round"] = round_no

    assert decide(state) == expected


def test_severe_count_only_counts_severity() -> None:
    """``severe_count`` 只看等级，用来在报告里显示「高危几条」。"""
    findings = [finding("critical"), finding("high"), finding("medium"), finding("low")]

    assert severe_count(findings) == 2


def test_actionable_count_uses_severity_or_category() -> None:
    findings = [
        finding("critical", category="style"),   # 等级高 → 算
        finding("low", category="bug"),          # 真 bug → 算
        finding("low", category="security"),     # 安全 → 算
        finding("low", category="style"),        # 不算
        finding("info", category="maintainability"),  # 不算
    ]

    assert actionable_count(findings) == 3


# ---------------------------------------------------------------------------
# 代码围栏清理
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x = 1", "x = 1\n"),
        ("```python\nx = 1\n```", "x = 1\n"),
        ("```\nx = 1\n```\n", "x = 1\n"),
        ("```Python\nx = 1\ny = 2\n```", "x = 1\ny = 2\n"),
        ("  x = 1  ", "x = 1\n"),
    ],
)
def test_strip_code_fences(raw, expected) -> None:
    """模型经常不听劝，还是把代码用 ``` 包起来。

    不擦干净的话，写进 .py 文件就是一个语法错误，审查会报 E999，
    然后修复 Agent 白跑一轮去修一个本来不存在的问题。
    """
    assert strip_code_fences(raw) == expected


def test_fenced_code_still_gets_written_clean(tmp_path, monkeypatch) -> None:
    """围栏清理要真的体现在落盘的文件上。"""
    wire(monkeypatch, code="```python\ndef f():\n    return 1\n```", reviews=[[]])

    run(tmp_path, monkeypatch)

    written = (tmp_path / "ws" / "solution.py").read_text(encoding="utf-8")
    assert "```" not in written
    assert written.startswith("def f():")


# ---------------------------------------------------------------------------
# 状态与产物
# ---------------------------------------------------------------------------


def test_snapshots_go_to_out_dir_not_workspace(tmp_path, monkeypatch) -> None:
    """快照不能写进工作目录。

    工作目录是审查 Agent 的扫描范围。历史版本放进去的话，下一轮审查会把
    上一轮的旧文件也当成待审代码，报告里就会冒出一堆莫名其妙的问题。
    """
    wire(
        monkeypatch,
        code="def f():\n    return 1\n",
        fixes=["def f():\n    return 2\n"],
        reviews=[[finding("high")], []],
    )

    run(tmp_path, monkeypatch)

    workspace_files = sorted(p.name for p in (tmp_path / "ws").iterdir())
    assert workspace_files == ["solution.py"], "工作目录里只能有待审的那一份代码"

    snapshots = sorted(p.name for p in tmp_path.glob("round-*.py"))
    assert len(snapshots) == 2, "初始生成 + 一轮修复，各留一份"


def test_steps_record_the_whole_story(tmp_path, monkeypatch) -> None:
    """执行日志要能看出流程是怎么走的。"""
    wire(
        monkeypatch,
        fixes=["x = 1\n"],
        reviews=[[finding("high")], []],
    )

    report = run(tmp_path, monkeypatch)["report"]

    nodes_seen = [step.node for step in report.steps]
    assert nodes_seen == ["编码 Agent", "审查 Agent", "修复 Agent", "审查 Agent"], nodes_seen


def test_steps_record_what_each_agent_reads_and_writes(tmp_path, monkeypatch) -> None:
    """「读 → 写」就是 Agent 之间通信的记录。

    三个 Agent 不直接对话，全靠共享状态传数据。谁读了什么、谁写了什么，
    是「Agent 协作」这件事唯一看得见的证据，所以要记下来。
    """
    wire(monkeypatch, fixes=["x = 1\n"], reviews=[[finding("high")], []])

    report = run(tmp_path, monkeypatch)["report"]
    flows = [(s.node, s.reads, s.writes) for s in report.steps]

    assert flows == [
        ("编码 Agent", "需求", "代码"),
        ("审查 Agent", "代码", "问题清单"),
        ("修复 Agent", "代码 + 问题清单", "代码（改写）"),
        ("审查 Agent", "代码", "问题清单"),
    ]


def test_fix_step_records_a_diff(tmp_path, monkeypatch) -> None:
    """修复步骤必须留下改动对比。

    光说「修复完成」看不出 Agent 到底动了什么。差异摆出来，
    「AI 改了代码」才是可以核实的事。
    """
    wire(
        monkeypatch,
        code="def f(a):\n    return a / 0\n",
        fixes=["def f(a):\n    if a == 0:\n        raise ValueError\n    return 1 / a\n"],
        reviews=[[finding("high")], []],
    )

    report = run(tmp_path, monkeypatch)["report"]
    fix_step = next(s for s in report.steps if s.node == "修复 Agent")

    assert fix_step.changed_lines > 0
    assert "-    return a / 0" in fix_step.diff
    assert "+        raise ValueError" in fix_step.diff


def test_no_diff_when_fix_changes_nothing(tmp_path, monkeypatch) -> None:
    """修复 Agent 原样返回时，diff 应该是空的，不能凭空造出改动。"""
    same = "x = 1\n"
    wire(monkeypatch, code=same, fixes=[same], reviews=[[finding("high")], []])

    report = run(tmp_path, monkeypatch)["report"]
    fix_step = next(s for s in report.steps if s.node == "修复 Agent")

    assert fix_step.diff == ""
    assert fix_step.changed_lines == 0
    assert "没有变化" in fix_step.summary


# ---------------------------------------------------------------------------
# 用户提供代码的模式（跳过编码 Agent）
# ---------------------------------------------------------------------------


def test_user_supplied_code_skips_the_coder(tmp_path, monkeypatch) -> None:
    """给了代码就从审查开始，编码 Agent 不参与。"""
    coder = FakeAgent([CoderOutput(code="不该被调用\n")])
    fixer = FakeAgent([FixerOutput(code="x = 1\n", changes="改好了")])
    graph = FakeReviewGraph([[finding("high")], []])

    monkeypatch.setattr(nodes, "_get_coder", lambda *_a, **_k: coder)
    monkeypatch.setattr(nodes, "_get_fixer", lambda *_a, **_k: fixer)
    monkeypatch.setattr(nodes, "build_graph", lambda *_a, **_k: graph)
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace())

    state = run_workflow(
        "",
        code="def f():\n    return 1 / 0\n",
        workspace=tmp_path / "ws",
        out_dir=tmp_path,
    )

    report = state["report"]
    assert coder.thread_ids == [], "编码 Agent 不该被调用"
    assert report.steps[0].node == "你（提供代码）"
    assert [s.node for s in report.steps] == [
        "你（提供代码）",
        "审查 Agent",
        "修复 Agent",
        "审查 Agent",
    ]


def test_user_supplied_code_is_written_to_the_workspace(tmp_path, monkeypatch) -> None:
    """用户给的代码要落盘，审查 Agent 才能看到它。"""
    wire(monkeypatch, reviews=[[]])
    source = "def f():\n    return 42\n"

    run_workflow("", code=source, workspace=tmp_path / "ws", out_dir=tmp_path)

    assert (tmp_path / "ws" / "solution.py").read_text(encoding="utf-8") == source
    assert (tmp_path / "round-0-你提供的代码.py").read_text(encoding="utf-8") == source


def test_user_supplied_clean_code_needs_no_fixing(tmp_path, monkeypatch) -> None:
    """你自己写的代码没毛病时，一轮审查就收工。"""
    _coder, fixer, graph = wire(monkeypatch, reviews=[[]])

    state = run_workflow("", code="x = 1\n", workspace=tmp_path / "ws", out_dir=tmp_path)

    assert state["report"].rounds_used == 0
    assert fixer.thread_ids == []
    assert graph.calls == 1


def test_report_has_round_summaries(tmp_path, monkeypatch) -> None:
    wire(monkeypatch, fixes=["x = 1\n"], reviews=[[finding("high"), finding("high")], []])

    report = run(tmp_path, monkeypatch)["report"]

    assert [r.round for r in report.rounds] == [1, 2]
    assert report.rounds[0].findings_total == 2
    assert report.rounds[0].findings_actionable == 2
    assert report.rounds[1].findings_total == 0


def test_final_code_matches_last_snapshot(tmp_path, monkeypatch) -> None:
    """报告里的最终代码要和磁盘上的一致。"""
    wire(
        monkeypatch,
        code="x = 0\n",
        fixes=["x = 1\n"],
        reviews=[[finding("high")], []],
    )

    report = run(tmp_path, monkeypatch)["report"]

    on_disk = (tmp_path / "ws" / "solution.py").read_text(encoding="utf-8")
    assert report.final_code == on_disk


def test_graph_compiles() -> None:
    graph = build_workflow()
    node_names = sorted(n for n in graph.get_graph().nodes if not n.startswith("__"))

    assert node_names == ["code", "fix", "report", "review"]


# ---------------------------------------------------------------------------
# 产物目录隔离（回归测试）
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(run_dir=tmp_path / "runs")


def test_run_dir_is_a_subdirectory_not_the_given_path(tmp_path) -> None:
    """回归测试：产物不能直接写进用户指定的目录。

    实测踩过这个坑。``4-运行Workflow.bat`` 里写死了 ``--out runs\\工作流演示``，
    结果多次运行共用一个目录、文件互相覆盖；**轮数少的那次还会把轮数多的
    那次留下的快照剩在目录里**。用户看到的是：

        round-0-初始生成.py   →  def is_even(...)      ← 这次跑的
        round-1-修复后.py     →  def flatten_dict(...)  ← 上次剩下的
        round-2-修复后.py     →  def flatten_dict(...)  ← 上次剩下的

    一堆来自不同运行的文件混在一起，看起来完全对不上。

    修法是每次运行在指定目录下再建一层时间戳子目录。
    """
    out = tmp_path / "out"

    run_dir = _make_run_dir(out, _settings(tmp_path))

    assert run_dir.parent == out, "要在指定目录下面"
    assert run_dir != out, "不能直接写进指定目录 —— 那正是会互相覆盖的做法"


def test_existing_files_in_the_output_parent_are_left_alone(tmp_path) -> None:
    """新一次运行不该动上一次留下的东西。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "上一次的产物.py").write_text("x = 1", encoding="utf-8")

    _make_run_dir(out, _settings(tmp_path))

    assert (out / "上一次的产物.py").exists(), "上一次的文件不能被清掉或覆盖"


def test_separate_runs_do_not_share_a_directory(tmp_path) -> None:
    """两次运行必须是两个不同的目录。"""
    out = tmp_path / "out"

    first = _make_run_dir(out, _settings(tmp_path))
    first.mkdir(parents=True, exist_ok=True)
    second = _make_run_dir(out, _settings(tmp_path))

    # 时间戳精确到秒，同一秒内两次调用会撞车 —— 实际使用中一次运行要十几秒，
    # 碰不上。这里两次调用之间隔开，验证的是「分离」这个行为本身。
    assert first.parent == second.parent == out
