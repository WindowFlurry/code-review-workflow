"""工作流的状态定义。

**这里就是「Agent 间通信」的实现方式。**

三个 Agent 不直接对话，而是读写同一份共享状态：编码 Agent 写入 ``code``，
审查 Agent 读 ``code`` 并写入 ``findings``，修复 Agent 读 ``findings`` 再改写
``code``。谁写什么、谁读什么，在下面这张表里一目了然：

| 字段 | 谁写 | 谁读 |
|---|---|---|
| ``requirement`` | 调用方 | 编码 Agent |
| ``code`` | 编码 Agent、修复 Agent | 审查 Agent、修复 Agent |
| ``findings`` | 审查 Agent | 修复 Agent、判断节点 |
| ``round`` | 修复节点 | 判断节点 |
| ``steps`` / ``usage`` / ``errors`` | 所有节点 | 出报告 |

``steps``、``usage``、``errors`` 用 ``operator.add`` 归纳器，和作业一一致：
多个节点各写各的，合并而不是互相覆盖。
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field

# 直接复用作业一的模型，不重新定义：
# 审查 Agent 产出的就是 VerifiedFinding，工作流原样接住。
from ..agent.state import Usage
from ..models.findings import VerifiedFinding

#: 无论如何都值得修的等级。
SEVERE_LEVELS = ("critical", "high")

#: 无论等级高低都值得修的类别。
#:
#: 只按严重程度判断是不够的。实测中审查 Agent 报出过一个真正的正确性缺陷
#: ——``flatten_dict({})`` 返回 ``{'': {}}`` 而不是 ``{}``——但它被标成了
#: 「中」，于是被跳过、没有修。一个会在合法输入上给出错误结果的 bug，
#: 不该因为它没让程序崩溃就被放过。
#:
#: 所以判断规则改成两条并集：**等级高** 或者 **类别是缺陷/安全**。
#: 风格和可维护性方面的意见仍然不触发修复 —— 那种问题修不完。
ACTIONABLE_CATEGORIES = ("bug", "security")


def is_actionable(finding: VerifiedFinding) -> bool:
    """这条问题值不值得再跑一轮去修它。"""
    return (
        finding.severity in SEVERE_LEVELS
        or finding.category in ACTIONABLE_CATEGORIES
    )


class WorkflowStep(BaseModel):
    """一次状态变化，构成执行日志。

    作业二的 PPT 把「执行日志与状态追踪」列为推荐项。这个结构就是它：
    每一步做了什么、发生在第几轮，都记下来，最后在报告里按时间顺序展示。
    """

    round: int = Field(description="第几轮。编码固定是第 0 轮。")
    node: str = Field(description="哪个节点写的，中文名。")
    summary: str = Field(description="一句话说明这一步做了什么。")
    detail: str = Field(default="", description="补充信息，比如报了几条问题。")


class RoundSummary(BaseModel):
    """一轮的概览，用于报告里的「逐轮演进」。"""

    round: int
    code_lines: int
    findings_total: int
    #: 其中值得修的有几条（高危，或属于缺陷/安全类别）。
    findings_actionable: int


class WorkflowReport(BaseModel):
    """工作流跑完之后交给用户看的东西。"""

    requirement: str
    final_code: str
    rounds_used: int
    max_rounds: int
    stop_reason: str = Field(description="为什么停下来，用中文说清楚。")
    remaining: list[VerifiedFinding] = Field(
        default_factory=list,
        description="最后一次审查仍然存在的问题。空表示干净。",
    )
    rounds: list[RoundSummary] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_s(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def converged(self) -> bool:
        """最后一轮审查没剩下值得修的问题。"""
        return not any(is_actionable(f) for f in self.remaining)


class WorkflowState(TypedDict, total=False):
    """在图的各个节点之间流动的状态。"""

    # --- 输入 ---
    requirement: str
    workspace: str
    #: 产物目录。和 workspace 分开：快照写在这里，不会被审查 Agent 扫到。
    #: （如果把历史快照写进 workspace，下一轮审查会把它们当成待审代码一起看，
    #: 报告里就会出现一堆「上一轮的旧文件」的问题。）
    out_dir: str
    max_rounds: int
    run_id: str
    started_at: datetime

    # --- 流动的数据：Agent 之间就靠这几个字段传递 ---
    code: str
    findings: list[VerifiedFinding]
    round: int
    stop_reason: str

    # --- 可观测性，用 operator.add 合并 ---
    steps: Annotated[list[WorkflowStep], operator.add]
    rounds: Annotated[list[RoundSummary], operator.add]
    usage: Annotated[list[Usage], operator.add]
    errors: Annotated[list[str], operator.add]

    # --- 输出 ---
    report: WorkflowReport | None


def empty_workflow_state(
    requirement: str,
    workspace: str,
    *,
    out_dir: str = "",
    run_id: str = "workflow",
    max_rounds: int = 3,
) -> WorkflowState:
    """建一个初始状态。"""
    return WorkflowState(
        requirement=requirement,
        workspace=workspace,
        out_dir=out_dir or workspace,
        max_rounds=max_rounds,
        run_id=run_id,
        started_at=datetime.now(),
        code="",
        findings=[],
        round=0,
        stop_reason="",
        steps=[],
        rounds=[],
        usage=[],
        errors=[],
        report=None,
    )


def actionable_count(findings: list[VerifiedFinding]) -> int:
    """数一数有几条是值得继续修的。"""
    return sum(1 for f in findings if is_actionable(f))


def severe_count(findings: list[VerifiedFinding]) -> int:
    """数一数有几条是高危的（只按等级，用于报告展示）。"""
    return sum(1 for f in findings if f.severity in SEVERE_LEVELS)
