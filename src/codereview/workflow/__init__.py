"""多 Agent 工作流：编码 → 审查 → 修复。

这是作业二的内容。它复用作业一里已经做好的审查 Agent，而不是重新写一遍：
工作流里最重的一步（审查）直接调用 ``codereview.agent.graph.build_graph()``，
白拿静态检查、逐文件 AI 审查、证据核对、独立复核和去重汇总。

三个 Agent 各管一段，通过共享状态传递数据，不直接对话：

    编码 Agent   需求（自然语言） → 代码
    审查 Agent   代码 → 已验证的问题清单     ← 复用作业一
    修复 Agent   代码 + 问题清单 → 新代码

「修完再审」构成循环，「还有硬伤吗」构成条件分支 —— 这两样正是作业二
推荐的高级流程控制。
"""

from .graph import build_workflow, run_workflow
from .runner import run
from .state import WorkflowReport, WorkflowState, WorkflowStep, empty_workflow_state

__all__ = [
    "WorkflowReport",
    "WorkflowState",
    "WorkflowStep",
    "build_workflow",
    "empty_workflow_state",
    "run",
    "run_workflow",
]
