"""Wire a review run together: configure, execute, render, persist.

Kept separate from ``cli.py`` so the same run can be driven from the Streamlit
app or from a test without going through argument parsing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from .agent.graph import build_graph
from .agent.state import empty_state, total_usage
from .config import Settings, get_settings
from .errors import ConfigError
from .llm.factory import describe_model
from .logging_setup import configure_logging, new_run_dir, prune_run_dirs
from .memory.checkpointer import get_checkpointer
from .models.report import ReviewReport
from .report import render_console, write_json, write_markdown

logger = logging.getLogger(__name__)


def run_review(
    *,
    target: Path,
    mode: str = "graph",
    thread_id: str = "default",
    out_dir: Path | None = None,
    formats: list[str] | None = None,
    offline: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    settings: Settings | None = None,
    console: Console | None = None,
) -> ReviewReport:
    """Run a review end to end and write the requested report formats.

    Args:
        target: Repository to review.
        mode: ``graph`` for the full pipeline, ``single`` for the single-agent
            baseline.
        thread_id: Identifies the conversation thread for persistence.
        out_dir: Where to write reports. A timestamped directory under
            ``settings.run_dir`` by default.
        formats: Any of ``console``, ``md``, ``json``.
        offline: Run only the deterministic analysers. No model calls are made,
            and the report is labelled as partial.
        verbose: Debug-level logging.
        quiet: Suppress progress logging; only the report is printed. The run log
            file still records everything.
        settings: Injected for tests.
        console: Rich console to render to.

    Returns:
        The completed :class:`ReviewReport`.
    """
    settings = settings or get_settings()
    formats = formats or ["console"]
    console = console or Console()

    target = Path(target).resolve()
    if not target.exists():
        raise FileNotFoundError(f"no such path: {target}")

    # A single file is reviewed by sandboxing to its parent directory and
    # restricting the inventory to that one name. Passing the file itself as the
    # root does not work -- the sandbox resolves paths *relative to* a root, and
    # every tool would then be asked to read a path inside a file.
    only_files: tuple[str, ...] | None = None
    if target.is_file():
        only_files = (target.name,)
        target = target.parent

    # Fail fast rather than silently degrading. Without this check, a missing key
    # makes every LLM node fail individually, and the run still produces a report
    # built only from the deterministic findings -- which looks like a complete
    # review and quietly under-reports coverage.
    if not offline and not settings.has_api_key:
        raise ConfigError(
            "没有配置 AI 密钥（DEEPSEEK_API_KEY），无法调用 AI 审查和复核。\n"
            "  · 把密钥填进 .env 文件（可以先复制 .env.example），或者\n"
            "  · 加上 --offline 参数，只做静态检查\n"
            "运行 `codereview doctor` 可以检查其他配置是否正常。"
        )

    if offline:
        console.print("[yellow]离线模式：只做静态检查（ruff + 语法分析），不调用 AI[/yellow]")

    run_dir = out_dir or new_run_dir(settings.run_dir, label=mode)
    configure_logging(run_dir, verbose=verbose, quiet=quiet)

    # 和作业二同一条规则：每次一个目录，但只留最近 10 次，不然越攒越多。
    if out_dir is None:
        prune_run_dirs(settings.run_dir, keep=10)

    logger.info("review of %s starting (mode=%s, model=%s)", target, mode, describe_model(settings))

    # Persisting the thread is what lets `codereview ask` follow up afterwards.
    checkpointer = get_checkpointer(run_dir / "threads.sqlite")

    if mode == "single":
        from .agent.simple_agent import run_simple_review

        state = run_simple_review(
            target,
            run_id=thread_id,
            only_files=only_files,
            settings=settings,
            console=console,
        )
    else:
        graph = build_graph(checkpointer=checkpointer, settings=settings)
        state = graph.invoke(
            empty_state(str(target), thread_id, offline=offline, only_files=only_files),
            {"configurable": {"thread_id": thread_id}, "recursion_limit": 200},
        )

    report = state.get("report")
    if report is None:
        raise RuntimeError("the graph finished without producing a report")

    _log_usage(state)

    if "console" in formats:
        render_console(report, console)

    if "md" in formats:
        path = write_markdown(report, run_dir / "report.md")
        console.print(f"[dim]文字报告：{path}[/dim]")

    # The JSON report is always written, even when not requested. It is not just
    # an output format -- `codereview ask` loads it to seed the follow-up agent,
    # so skipping it silently breaks the memory feature for that run.
    json_path = write_json(report, run_dir / "report.json")
    if "json" in formats:
        console.print(f"[dim]数据报告：{json_path}[/dim]")

    console.print(f"[dim]本次运行的文件都在：{run_dir}[/dim]")
    console.print(f"[dim]想追问可以运行：codereview ask --thread {thread_id}[/dim]\n")
    return report


def _log_usage(state: dict) -> None:
    usage = total_usage(state.get("usage", []))
    logger.info(
        "llm_calls=%d tool_calls=%d input_tokens=%d output_tokens=%d cache_read=%d "
        "malformed=%d failed_files=%d",
        usage.llm_calls,
        usage.tool_calls,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.malformed_responses,
        usage.failed_files,
    )
