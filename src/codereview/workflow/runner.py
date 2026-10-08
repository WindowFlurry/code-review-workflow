"""跑一次工作流，并把结果渲染出来。

终端输出刻意做成「**能看见流程在走**」：每一步做了什么、第几轮、报了几条问题，
都按顺序列出来。作业二的 PPT 把「执行日志与状态追踪」列为推荐项，
这份逐步骤的输出就是它。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..config import Settings, get_settings
from ..errors import ConfigError
from ..logging_setup import configure_logging, new_run_dir
from ..models.report import SEVERITY_COLOURS
from ..report import labels
from .graph import run_workflow
from .state import WorkflowReport, actionable_count, severe_count

logger = logging.getLogger(__name__)


def run(
    requirement: str,
    *,
    max_rounds: int = 3,
    out_dir: Path | None = None,
    formats: list[str] | None = None,
    verbose: bool = False,
    quiet: bool = False,
    settings: Settings | None = None,
    console: Console | None = None,
) -> WorkflowReport:
    """跑一次完整工作流。

    Args:
        requirement: 自然语言需求，例如「写一个函数判断回文」。
        max_rounds: 修复轮数上限。
        out_dir: 产物目录，``None`` 表示在 runs 下自动建一个带时间戳的。
        formats: 要写哪些格式，可选 ``console`` / ``md`` / ``json``。
        verbose: 打开 DEBUG 日志。
        quiet: 只打印报告。
        settings: 测试用注入。
        console: 渲染目标。

    Returns:
        汇总报告。
    """
    settings = settings or get_settings()
    formats = formats if formats is not None else ["console"]
    console = console or Console()

    # 和作业一一样的提前失败：没有密钥的话，几个 Agent 会各自失败一遍，
    # 最后产出一份「什么也没做但看起来跑完了」的报告。
    if not settings.has_api_key:
        raise ConfigError(
            "没有配置 AI 密钥（DEEPSEEK_API_KEY），工作流里的三个 Agent 都无法运行。\n"
            "  · 把密钥填进 .env 文件（可以先复制 .env.example）\n"
            "运行 `codereview doctor` 可以检查其他配置是否正常。"
        )

    if out_dir is None:
        out_dir = new_run_dir(settings.run_dir, label="workflow")

    configure_logging(out_dir, verbose=verbose, quiet=quiet)

    console.print("[bold]多 Agent 工作流[/bold]  [dim]编码 → 审查 → 修复[/dim]")
    console.print(f"[dim]需求：{requirement}[/dim]")
    console.print(f"[dim]最多修 {max_rounds} 轮[/dim]\n")

    state = run_workflow(
        requirement,
        run_id=out_dir.name,
        max_rounds=max_rounds,
        out_dir=out_dir,
    )

    report = state.get("report")
    if report is None:
        raise RuntimeError("工作流结束但没有产出报告")

    if "console" in formats:
        render_console(report, console)

    if "md" in formats:
        path = out_dir / "workflow-report.md"
        path.write_text(render_markdown(report), encoding="utf-8")
        console.print(f"[dim]文字报告：{path}[/dim]")

    # JSON 总是写：它是给程序读的那一份，也方便以后做统计。
    json_path = out_dir / "workflow-report.json"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if "json" in formats:
        console.print(f"[dim]数据报告：{json_path}[/dim]")

    console.print(f"[dim]本次运行的产物都在：{out_dir}[/dim]\n")
    return report


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------


def render_console(report: WorkflowReport, console: Console | None = None) -> None:
    """把工作流的执行过程和结果打印出来。"""
    console = console or Console()

    _render_timeline(report, console)
    _render_verdict(report, console)

    if report.remaining:
        _render_findings(report, console)

    console.print()
    console.print("[bold]最终代码[/bold]")
    console.print(
        Syntax(report.final_code or "# （没有产出代码）", "python", line_numbers=True,
               word_wrap=True, theme="ansi_dark")
    )

    _render_stats(report, console)


def _render_timeline(report: WorkflowReport, console: Console) -> None:
    """执行日志：每一步做了什么，按顺序列出来。"""
    if not report.steps:
        return

    table = Table(title="执行过程", show_header=True, header_style="bold")
    table.add_column("轮次", justify="center", style="dim", no_wrap=True)
    table.add_column("节点", no_wrap=True)
    table.add_column("做了什么")

    for step in report.steps:
        label = "初始" if step.round == 0 else f"第 {step.round} 轮"
        text = Text(step.summary)
        if step.detail:
            text.append(f"\n{step.detail}", style="dim")
        table.add_row(label, step.node, text)

    console.print(table)
    console.print()


def _render_verdict(report: WorkflowReport, console: Console) -> None:
    """为什么停下来 —— 这是流程控制的结果，要说清楚。"""
    if report.converged and not report.remaining:
        style, title, body = "green", "干净通过", report.stop_reason
    elif report.converged:
        style, title, body = "green", "无高危问题", report.stop_reason
    else:
        style, title, body = "yellow", "仍有未解决的问题", report.stop_reason

    console.print(Panel(body, border_style=style, title=f"[{style}]{title}[/{style}]"))


def _render_findings(report: WorkflowReport, console: Console) -> None:
    console.print()
    console.print(f"[bold]最后一次审查剩余的问题（{len(report.remaining)} 条）[/bold]")

    for index, finding in enumerate(report.remaining, start=1):
        colour = SEVERITY_COLOURS.get(finding.severity, "white")
        heading = Text()
        heading.append(f"{index}. ", style="bold")
        heading.append(f"[{labels.severity(finding.severity)}]", style=f"bold {colour}")
        heading.append(f" {finding.title}", style="bold")
        console.print(heading)

        if finding.evidence:
            location = f"{finding.evidence.path}:{finding.evidence.line_start}"
            console.print(f"   [dim]{location} · {labels.category(finding.category)}[/dim]")
        console.print(f"   {finding.rationale}")
        console.print()


def _render_stats(report: WorkflowReport, console: Console) -> None:
    table = Table(title="本次运行统计", show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim", justify="right")
    table.add_column()

    rounds = f"{report.rounds_used} 轮"
    if report.rounds_used >= report.max_rounds:
        rounds += "（已达上限）"
    table.add_row("修复轮数", rounds)

    if report.rounds:
        story = " → ".join(f"{r.findings_total}" for r in report.rounds)
        table.add_row("每轮审查报的问题数", story)

    remaining = report.remaining
    table.add_row(
        "剩余问题",
        f"{len(remaining)} 条（高危 {severe_count(remaining)} 条，"
        f"需要修复 {actionable_count(remaining)} 条）",
    )
    table.add_row("AI 调用次数", str(report.usage.llm_calls))

    total_in = report.usage.input_tokens
    if total_in:
        table.add_row(
            "用掉的字数(令牌)",
            f"输入 {total_in:,}，输出 {report.usage.output_tokens:,}",
        )

    table.add_row("耗时", f"{report.duration_s:.1f} 秒")

    if report.errors:
        table.add_row("[yellow]出错的步骤[/yellow]", str(len(report.errors)))

    console.print()
    console.print(table)

    for error in report.errors[:5]:
        console.print(f"[dim]  · {error}[/dim]")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(report: WorkflowReport) -> str:
    """Markdown 版报告，可以直接当交付物。"""
    lines: list[str] = ["# 多 Agent 工作流报告", ""]
    lines.append("## 需求")
    lines.append("")
    lines.append(report.requirement)
    lines.append("")

    lines.append("## 执行过程")
    lines.append("")
    lines.append("| 轮次 | 节点 | 做了什么 |")
    lines.append("|---|---|---|")
    for step in report.steps:
        label = "初始" if step.round == 0 else f"第 {step.round} 轮"
        detail = f"<br><sub>{step.detail}</sub>" if step.detail else ""
        lines.append(f"| {label} | {step.node} | {step.summary}{detail} |")
    lines.append("")

    lines.append("## 结果")
    lines.append("")
    lines.append(f"**{report.stop_reason}**")
    lines.append("")
    lines.append(
        f"共 {report.rounds_used} 轮修复，剩余 {len(report.remaining)} 条问题"
        f"（高危 {severe_count(report.remaining)} 条，"
        f"需要修复 {actionable_count(report.remaining)} 条）。"
    )
    lines.append("")

    if report.rounds:
        lines.append("| 第几轮审查 | 问题总数 | 其中需要修复 |")
        lines.append("|---|---|---|")
        for item in report.rounds:
            lines.append(
                f"| {item.round} | {item.findings_total} | {item.findings_actionable} |"
            )
        lines.append("")

    if report.remaining:
        lines.append("## 剩余问题")
        lines.append("")
        for index, finding in enumerate(report.remaining, start=1):
            lines.append(f"### {index}. {finding.title}")
            lines.append("")
            if finding.evidence:
                lines.append(f"`{finding.evidence.path}:{finding.evidence.line_start}`")
                lines.append("")
            lines.append(finding.rationale)
            lines.append("")

    lines.append("## 最终代码")
    lines.append("")
    lines.append("```python")
    lines.append(report.final_code)
    lines.append("```")
    lines.append("")

    lines.append("## 运行统计")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 修复轮数 | {report.rounds_used} / {report.max_rounds} |")
    lines.append(f"| 剩余问题 | {len(report.remaining)} |")
    lines.append(f"| AI 调用次数 | {report.usage.llm_calls} |")
    lines.append(f"| 输入令牌 | {report.usage.input_tokens:,} |")
    lines.append(f"| 输出令牌 | {report.usage.output_tokens:,} |")
    lines.append(f"| 耗时 | {report.duration_s:.1f} 秒 |")
    lines.append("")

    if report.errors:
        lines.append("## 出错的步骤")
        lines.append("")
        for error in report.errors:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines)
