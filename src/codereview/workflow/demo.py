"""回放一次录好的工作流运行。

为什么要有这个：示例演示如果用真模型跑，每按一次回车就花一次钱，而且模型有
随机性——同一个文件两次跑出来的问题数可能不一样，演示效果反而不稳定。

所以把**一次真实运行**的完整报告存成 JSON，用**同一套渲染代码**打印出来。
省的是令牌，不是真实性：下面的表格、diff、统计全部来自那次真实运行。

想让老师看现场跑，用 ``codereview workflow "..."`` 或 ``--code 文件.py``。
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from .state import WorkflowReport

#: 录好的那次运行。路径跟着包走，打包之后也在。
DEMO_PATH = Path(__file__).with_name("demo_report.json")


def load_demo_report(path: Path | None = None) -> WorkflowReport:
    """读回录好的报告。"""
    target = path or DEMO_PATH
    return WorkflowReport.model_validate(json.loads(target.read_text(encoding="utf-8")))


def replay(console: Console | None = None) -> WorkflowReport:
    """把录好的那次运行原样打印出来。不联网，不花钱。"""
    console = console or Console()
    report = load_demo_report()

    console.print("[bold]示例演示[/bold]  [dim]编码 → 审查 → 修复[/dim]")
    console.print(
        "[yellow]注意：下面是一段事先跑好存下来的结果回放，"
        "不会调用 AI，也不花钱。[/yellow]"
    )
    console.print("[dim]想看现场跑：输入一句需求，或者把 .py 文件拖进来。[/dim]\n")

    from .runner import render_console

    render_console(report, console)

    console.print("\n[dim]以上是存档回放（来源：示例代码\\工作流演示\\两数相加.py）[/dim]")
    return report
