"""工作流图的装配。

```
START
  │
[编码]      需求 → 代码
  │
  ├── 没产出代码 ──────────────────────┐
  ↓                                    │
[审查]      调用作业一整张图            │
  │                                    │
[判断]      还有高危问题 且 没到上限 ?  │
  ├── 是 ──→ [修复] ──┐                │
  │             ↑     │                │
  │             └─────┘  循环回审查     │
  └── 否 ──────────────────────────────┤
                                       ↓
                                    [汇总]
                                       ↓
                                      END
```

三处流程控制：

- **顺序执行**（必须项）：编码 → 审查
- **条件分支**（推荐项）：判断节点、以及编码失败时的兜底分支
- **循环**（推荐项）：修复 → 回到审查，最多 ``max_rounds`` 轮
"""

from __future__ import annotations

import logging
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ..logging_setup import new_run_dir
from .nodes import code_node, decide, fix_node, report_node, review_node
from .state import WorkflowState, empty_workflow_state

logger = logging.getLogger(__name__)


def after_code(state: WorkflowState) -> str:
    """编码这一关没产出代码的话，直接去出报告，别在流程里空转。"""
    return "review" if state.get("code", "").strip() else "report"


def build_workflow(*, checkpointer=None):
    """构建并编译工作流。"""
    graph = StateGraph(WorkflowState)

    graph.add_node("code", code_node)
    graph.add_node("review", review_node)
    graph.add_node("fix", fix_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "code")

    # 编码失败就不往下走了 —— 没有代码，审查无从谈起。
    graph.add_conditional_edges(
        "code",
        after_code,
        {"review": "review", "report": "report"},
    )

    # 条件分支：还有硬伤就修，否则收工。
    graph.add_conditional_edges(
        "review",
        decide,
        {"fix": "fix", "report": "report"},
    )

    # 循环：修完回到审查。终止条件在 decide 里（严重程度 + 轮数上限），
    # recursion_limit 只是最后一道保险。
    graph.add_edge("fix", "review")

    graph.add_edge("report", END)

    return graph.compile(checkpointer=checkpointer)


def run_workflow(
    requirement: str,
    *,
    workspace: Path | str | None = None,
    run_id: str = "workflow",
    max_rounds: int = 3,
    out_dir: Path | None = None,
    checkpointer=None,
) -> WorkflowState:
    """跑一次完整工作流，返回最终状态。

    Args:
        requirement: 自然语言需求。
        workspace: 工作目录，编码 Agent 会在这里写 ``solution.py``。
            ``None`` 表示在 ``runs/workflow-<时间戳>/workspace`` 下新建一个。
        run_id: 用来区分每次运行，会写进日志和线程 id。
        max_rounds: 修复轮数上限。
        out_dir: 产物目录，``None`` 表示自动建一个带时间戳的。

    Returns:
        最终状态，``state["report"]`` 是汇总报告。
    """
    if out_dir is None:
        out_dir = new_run_dir(Path("runs"), label="workflow")

    if workspace is None:
        workspace = out_dir / "workspace"

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    logger.info("工作流启动：需求=%r，工作目录=%s", requirement[:40], workspace)

    graph = build_workflow(checkpointer=checkpointer)
    initial = empty_workflow_state(
        requirement,
        str(workspace),
        out_dir=str(out_dir),
        run_id=run_id,
        max_rounds=max_rounds,
    )

    # recursion_limit 给得宽一些：每一轮是「审查 + 修复」两个节点，再加上
    # 首轮编码和末轮汇总。上限 3 轮的话 20 步足够，留足余量避免误伤。
    return graph.invoke(
        initial,
        {"configurable": {"thread_id": run_id}, "recursion_limit": 40},
    )
