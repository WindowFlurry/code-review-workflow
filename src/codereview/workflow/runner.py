"""跑一次工作流，并把结果渲染出来。

终端输出刻意做成「**能看见流程在走**」：每一步做了什么、第几轮、报了几条问题，
都按顺序列出来。作业二的 PPT 把「执行日志与状态追踪」列为推荐项，
这份逐步骤的输出就是它。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..config import Settings, get_settings
from ..errors import ConfigError
from ..logging_setup import configure_logging, new_run_dir, prune_run_dirs
from ..models.report import SEVERITY_COLOURS
from ..report import labels
from .graph import run_workflow
from .nodes import SOLUTION_FILENAME
from .state import WorkflowReport, actionable_count, severe_count

logger = logging.getLogger(__name__)


def _make_run_dir(out_dir: Path | None, settings: Settings) -> Path:
    """给这次运行安排一个独立目录。

    用户指定 ``--out`` 时，在它下面再建一层时间戳子目录，而不是直接写进去。

    直接写进去会出事：同一个目录被多次运行共用，文件互相覆盖，轮数少的那次
    还会把轮数多的那次留下的快照剩在目录里 —— 结果就是一堆来自不同运行的
    文件混在一起，看起来完全对不上。实测踩过这个坑。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if out_dir is None:
        return new_run_dir(settings.run_dir, label="workflow")

    run_dir = Path(out_dir) / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _flatten_workspace(run_dir: Path) -> None:
    """把最终代码挪到产物目录根下，并删掉空掉的工作目录。

    ``workspace/`` 是给审查 Agent 扫的临时目录，运行结束就没用了。
    结果文件藏在一层子目录里，用户还得猜点哪个；挪出来之后，一次运行的
    产物就是「一份代码 + 一份报告 + 一份日志」，一眼能看完。
    """
    final = run_dir / "workspace" / SOLUTION_FILENAME
    if not final.is_file():
        return

    try:
        final.replace(run_dir / SOLUTION_FILENAME)
        (run_dir / "workspace").rmdir()
    except OSError as exc:
        logger.warning("整理产物目录失败（不影响结果）：%s", exc)


def run(
    requirement: str,
    *,
    code: str = "",
    max_rounds: int = 3,
    out_dir: Path | None = None,
    formats: list[str] | None = None,
    verbose: bool = False,
    quiet: bool = False,
    keep_runs: int = 10,
    settings: Settings | None = None,
    console: Console | None = None,
) -> WorkflowReport:
    """跑一次完整工作流。

    Args:
        requirement: 自然语言需求，例如「写一个函数判断回文」。
            用户提供了 ``code`` 时，这一项只作为背景说明，可以留空。
        code: 用户自己写的代码。给了就跳过编码 Agent，直接进入审查。
        max_rounds: 修复轮数上限。
        out_dir: 产物目录的父目录。每次运行会在它下面再建一个带时间戳的
            子目录；``None`` 表示在 ``runs`` 下自动建。
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

    run_dir = _make_run_dir(out_dir, settings)
    configure_logging(run_dir, verbose=verbose, quiet=quiet)

    # 每次运行一个目录，不会互相覆盖，但目录只增不减。留最近 10 次足够对比，
    # 更早的不清理的话，跑几十次之后 runs 里就全是文件夹了。
    pruned = prune_run_dirs(run_dir.parent, keep=keep_runs)
    for stale in pruned:
        logger.debug("已清理旧运行目录：%s", stale)

    user_supplied = bool(code.strip())

    console.print("[bold]多 Agent 工作流[/bold]  [dim]编码 → 审查 → 修复[/dim]")
    if user_supplied:
        console.print(
            f"[dim]代码来源：你自己提供（{len(code.splitlines())} 行）"
            "—— 跳过编码 Agent，直接从审查开始[/dim]"
        )
        if requirement.strip():
            console.print(f"[dim]需求说明：{requirement}[/dim]")
    else:
        console.print(f"[dim]需求：{requirement}[/dim]")
    console.print(f"[dim]最多修 {max_rounds} 轮[/dim]\n")

    state = run_workflow(
        requirement,
        code=code,
        run_id=run_dir.name,
        max_rounds=max_rounds,
        out_dir=run_dir,
    )

    report = state.get("report")
    if report is None:
        raise RuntimeError("工作流结束但没有产出报告")

    _flatten_workspace(run_dir)

    if "console" in formats:
        render_console(report, console)

    if "md" in formats:
        path = run_dir / "workflow-report.md"
        path.write_text(render_markdown(report), encoding="utf-8")
        console.print(f"[dim]文字报告：{path}[/dim]")

    # JSON 总是写：它是给程序读的那一份，也方便以后做统计。
    json_path = run_dir / "workflow-report.json"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if "json" in formats:
        console.print(f"[dim]数据报告：{json_path}[/dim]")

    console.print(f"[dim]本次运行的产物都在：{run_dir}[/dim]\n")
    return report


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------


def render_console(report: WorkflowReport, console: Console | None = None) -> None:
    """把工作流的执行过程和结果打印出来。"""
    console = console or Console()

    _render_timeline(report, console)
    _render_diffs(report, console)
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
    """执行日志：谁读了什么、写了什么、做了什么。

    「读 → 写」那一列就是 **Agent 之间通信的直接证据**：三个 Agent 不直接
    对话，全靠共享状态传数据，所以这一列列出来，协作过程就是看得见的。
    """
    if not report.steps:
        return

    table = Table(title="执行过程", show_header=True, header_style="bold")
    table.add_column("轮次", justify="center", style="dim", no_wrap=True)
    table.add_column("谁", no_wrap=True)
    table.add_column("读 → 写", style="cyan")
    # overflow="fold"：窗口窄的时候换行，而不是把后半句截成省略号。
    table.add_column("做了什么", overflow="fold")

    for step in report.steps:
        label = "初始" if step.round == 0 else f"第 {step.round} 轮"
        flow = f"{step.reads or '—'} → {step.writes or '—'}"

        text = Text(step.summary)
        if step.detail:
            text.append(f"\n{_short(step.detail)}", style="dim")

        table.add_row(label, step.node, flow, text)

    console.print(table)
    console.print()


def _short(text: str, limit: int = 56) -> str:
    """压成一行。

    表格里塞进一整段「改了什么」，窗口一窄就会被折成十几行，整张表都没法看了。
    详细内容在下面的「代码改动」里还会完整出现，这里只要一句提示。
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _render_diffs(report: WorkflowReport, console: Console) -> None:
    """把每一轮改动的代码摆出来。

    光说「修复完成」看不出 Agent 到底动了什么。差异摆出来，
    「AI 改了代码」才是眼睛能看到的、可以核实的事。
    """
    changed = [step for step in report.steps if step.diff]
    if not changed:
        return

    console.print("[bold]代码改动[/bold]")
    console.print()

    for step in changed:
        console.print(f"[bold]第 {step.round} 轮修复[/bold]  [dim]改动 {step.changed_lines} 行[/dim]")
        console.print(_colourise_diff(step.diff))
        console.print()


def _colourise_diff(diff_text: str) -> Text:
    """给 diff 上色：新增绿、删除红、文件头灰。"""
    text = Text()
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style="dim")
        elif line.startswith("+"):
            text.append(line + "\n", style="green")
        elif line.startswith("-"):
            text.append(line + "\n", style="red")
        elif line.startswith("@@"):
            text.append(line + "\n", style="cyan")
        else:
            text.append(line + "\n")
    return text


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
    # 「已达上限」只在**真的因此停下**时才说。刚好在第 N 轮修干净、
    # 而 N 又正好等于上限时，说「已达上限」会让人误以为是没修完被强行掐断。
    if report.rounds_used >= report.max_rounds and report.remaining:
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

    if report.requirement.strip():
        lines.append("## 需求")
        lines.append("")
        lines.append(report.requirement)
        lines.append("")

    lines.append("## 执行过程")
    lines.append("")
    lines.append("「读 → 写」这一列是 Agent 之间通信的记录：三个 Agent 不直接对话，")
    lines.append("全靠共享状态传递数据。")
    lines.append("")
    lines.append("| 轮次 | 谁 | 读 → 写 | 做了什么 |")
    lines.append("|---|---|---|---|")
    for step in report.steps:
        label = "初始" if step.round == 0 else f"第 {step.round} 轮"
        flow = f"{step.reads or '—'} → {step.writes or '—'}"
        detail = f"<br><sub>{step.detail}</sub>" if step.detail else ""
        lines.append(f"| {label} | {step.node} | {flow} | {step.summary}{detail} |")
    lines.append("")

    changed = [step for step in report.steps if step.diff]
    if changed:
        lines.append("## 代码改动")
        lines.append("")
        lines.append("每一轮修复实际改了什么：")
        lines.append("")
        for step in changed:
            lines.append(f"### 第 {step.round} 轮修复（改动 {step.changed_lines} 行）")
            lines.append("")
            if step.detail:
                lines.append(step.detail)
                lines.append("")
            lines.append("```diff")
            lines.append(step.diff.rstrip())
            lines.append("```")
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
