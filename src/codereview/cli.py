"""Command-line interface.

``tools`` subcommands exercise the deterministic layer with no LLM involved --
useful for demos, and the fastest way to debug a bad finding. ``review`` runs
the full agent graph.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .config import get_settings
from .errors import CodeReviewError
from .logging_setup import configure_logging

app = typer.Typer(
    name="codereview",
    help="An evidence-grounded code review agent.",
    no_args_is_help=True,
    add_completion=False,
)
tools_app = typer.Typer(help="Deterministic tools (no LLM calls).", no_args_is_help=True)
app.add_typer(tools_app, name="tools")

console = Console()
logger = logging.getLogger(__name__)


def _fail(message: str, code: int = 2) -> None:
    """Print an error the way a user can act on, then exit."""
    console.print(Panel(message, title="[red]出错[/red]", border_style="red"))
    raise typer.Exit(code)


# --------------------------------------------------------------------------
# tools -- deterministic layer
# --------------------------------------------------------------------------


@tools_app.command("list")
def tools_list(
    path: Annotated[Path, typer.Argument(help="Repository root.")] = Path(),
    glob: Annotated[str, typer.Option(help="Filename filter.")] = "*.py",
    limit: Annotated[int, typer.Option("--limit", help="Max entries.")] = 200,
) -> None:
    """List reviewable files."""
    from .errors import ToolError
    from .tools.fs_tools import list_files

    try:
        files = list_files(path, glob=glob, max_entries=limit)
    except (ToolError, CodeReviewError) as exc:
        _fail(str(exc))

    if not files:
        console.print(f"[yellow]no files matching {glob!r} under {path}[/yellow]")
        return

    for name in files:
        console.print(name)
    console.print(f"\n[dim]{len(files)} file(s)[/dim]")


@tools_app.command("read")
def tools_read(
    path: Annotated[Path, typer.Argument(help="Repository root.")] = Path(),
    target: Annotated[str, typer.Argument(help="File to read, relative to root.")] = "",
    start: Annotated[int | None, typer.Option(help="First line.")] = None,
    end: Annotated[int | None, typer.Option(help="Last line.")] = None,
) -> None:
    """Read a file with line numbers, exactly as the agent sees it."""
    from .tools.fs_tools import read_file

    try:
        text = read_file(path, target, start_line=start, end_line=end)
    except CodeReviewError as exc:
        _fail(str(exc))

    console.print(Syntax(text, "text", line_numbers=False, word_wrap=True))


@tools_app.command("lint")
def tools_lint(
    path: Annotated[Path, typer.Argument(help="Repository root.")] = Path(),
    target: Annotated[str | None, typer.Argument(help="File or directory.")] = None,
    fmt: Annotated[str, typer.Option("--format", help="table | json")] = "table",
) -> None:
    """Run ruff and show structured findings."""
    from .tools.lint_tools import run_lint

    settings = get_settings()
    try:
        findings = run_lint(path, [target] if target else None)
    except CodeReviewError as exc:
        _fail(str(exc))

    if fmt == "json":
        console.print_json(json.dumps([f.model_dump() for f in findings], ensure_ascii=False))
        return

    if not findings:
        console.print("[green]ruff reported no issues[/green]")
        return

    from rich.table import Table

    table = Table(title=f"ruff: {len(findings)} finding(s)", show_lines=False)
    table.add_column("rule", style="cyan", no_wrap=True)
    table.add_column("location", style="dim", no_wrap=True)
    table.add_column("message")

    for item in findings[: settings.max_files * 10]:
        table.add_row(item.rule_id, f"{item.path}:{item.line_start}", item.message)

    console.print(table)


@tools_app.command("ast")
def tools_ast(
    path: Annotated[Path, typer.Argument(help="Repository root.")] = Path(),
    target: Annotated[str, typer.Argument(help="Python file to analyse.")] = "",
) -> None:
    """Show the structural summary the reviewer receives."""
    from .tools.ast_tools import python_ast_summary

    try:
        summary = python_ast_summary(path, target)
    except CodeReviewError as exc:
        _fail(str(exc))

    console.print_json(json.dumps(summary.model_dump(), ensure_ascii=False, indent=2))


@tools_app.command("search")
def tools_search(
    path: Annotated[Path, typer.Argument(help="Repository root.")] = Path(),
    pattern: Annotated[str, typer.Argument(help="Regex to search for.")] = "",
    glob: Annotated[str, typer.Option(help="File filter.")] = "*.py",
) -> None:
    """Search the repository for a pattern."""
    from .tools.search_tools import format_matches, search_code

    try:
        matches = search_code(path, pattern, glob=glob)
    except CodeReviewError as exc:
        _fail(str(exc))

    console.print(Syntax(format_matches(matches), "text", word_wrap=True))


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


@app.command()
def doctor(
    offline: Annotated[bool, typer.Option("--offline", help="Skip the API round-trip.")] = False,
) -> None:
    """Check that the environment is usable before a real run.

    Verifies the API key, the exact model name, and every deterministic tool.
    The model name is worth checking explicitly: ``deepseek-chat`` was retired,
    but most tutorials and even the LangChain integration page still use it, so a
    stale copy-paste otherwise fails only once a run is underway.
    """
    from . import __version__

    console.print(f"[bold]代码审查助手[/bold] 版本 {__version__}")
    console.print(f"Python 版本：{sys.version.split()[0]}")

    settings = get_settings()
    problems: list[str] = []

    # -- 配置 --------------------------------------------------------
    if settings.has_api_key:
        console.print(f"[green]正常[/green]  AI 密钥已配置（尾号 {_key_tail(settings)}）")
    else:
        problems.append("没有配置 AI 密钥 DEEPSEEK_API_KEY（把 .env.example 复制成 .env 再填）")
        console.print("[red]失败[/red]  没有 AI 密钥")

    console.print(f"     使用的模型：{settings.deepseek_model}")

    if settings.deepseek_model in {"deepseek-chat", "deepseek-reasoner"}:
        console.print(
            "[yellow]注意[/yellow] 这个模型名已经停用了，"
            "现在应该用 'deepseek-flash'"
        )

    if settings.has_fallback:
        console.print(f"[green]正常[/green]  备用模型：{settings.fallback_model}")
    else:
        console.print("[dim]     备用模型：未配置（不影响使用）[/dim]")

    # -- 本地检查工具 -------------------------------------------------
    from .tools.lint_tools import ruff_available

    if ruff_available():
        console.print("[green]正常[/green]  ruff 静态检查工具可用")
    else:
        console.print("[yellow]注意[/yellow] 没找到 ruff，静态检查将不可用")

    import shutil

    if shutil.which("rg"):
        console.print("[green]正常[/green]  ripgrep 可用")
    else:
        console.print(
            "[dim]     没有 ripgrep，代码搜索会慢一些（不影响结果）[/dim]"
        )

    # -- 框架版本 -----------------------------------------------------
    # 版本号从包元数据读，不用 `module.__version__`：
    # langgraph 没定义这个属性，读它会直接抛 AttributeError。
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        import langchain  # noqa: F401
        import langgraph  # noqa: F401

        def _v(name: str) -> str:
            try:
                return pkg_version(name)
            except PackageNotFoundError:
                return "?"

        console.print(
            f"[green]正常[/green]  langchain {_v('langchain')}、"
            f"langgraph {_v('langgraph')}、"
            f"langchain-deepseek {_v('langchain-deepseek')}"
        )
    except ImportError as exc:
        problems.append(f"缺少 AI 框架依赖：{exc}")
        console.print(f"[red]失败[/red]  {exc}")

    # -- 真实调用测试 -------------------------------------------------
    if offline:
        console.print("[dim]     已跳过真实调用测试（--offline）[/dim]")
    elif settings.has_api_key:
        _doctor_api_roundtrip(settings, problems)

    if problems:
        console.print()
        _fail("发现以下问题：\n  - " + "\n  - ".join(problems))

    console.print("\n[bold green]一切正常，可以开始使用了[/bold green]")


def _key_tail(settings) -> str:
    key = settings.deepseek_api_key.get_secret_value()
    return key[-4:] if len(key) >= 4 else "已设置"


def _doctor_api_roundtrip(settings, problems: list[str]) -> None:
    """真实调用一次，确认密钥和模型名都能用。"""
    from .llm.factory import build_chat_model

    try:
        model = build_chat_model("reporter", settings, temperature=0.0)
        reply = model.invoke("Reply with the single word: ok")
        text = getattr(reply, "content", "")
        console.print(f"[green]正常[/green]  真实调用成功（返回 {str(text)[:40]!r}）")
    except Exception as exc:  # noqa: BLE001 - doctor should report, never crash
        problems.append(f"调用 AI 失败：{exc}")
        console.print(f"[red]失败[/red]  调用 AI：{exc}")
        console.print(f"[red]FAIL[/red] API call: {exc}")


# --------------------------------------------------------------------------
# review / ask
# --------------------------------------------------------------------------


@app.command()
def review(
    target: Annotated[Path, typer.Argument(help="Repository or file to review.")] = Path(),
    mode: Annotated[str, typer.Option(help="graph | single")] = "graph",
    thread: Annotated[str, typer.Option(help="Thread id for memory.")] = "default",
    out: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    formats: Annotated[str, typer.Option(help="Comma-separated: console,md,json")] = "console,md,json",
    offline: Annotated[bool, typer.Option("--offline", help="Deterministic analysis only; no LLM.")] = False,
    quiet: Annotated[bool, typer.Option("-q", "--quiet", help="Print only the report.")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Review a repository and write the report.

    Requires DEEPSEEK_API_KEY. Use --offline to run only the deterministic
    analysers (ruff + AST) without a key; the report is labelled as partial.

    All tools are read-only: the agent can never execute, modify, or delete the
    code it is reviewing.
    """
    from .runner import run_review

    try:
        run_review(
            target=target,
            mode=mode,
            thread_id=thread,
            out_dir=out,
            formats=[f.strip() for f in formats.split(",") if f.strip()],
            offline=offline,
            quiet=quiet,
            verbose=verbose,
        )
    except CodeReviewError as exc:
        _fail(str(exc))


@app.command()
def workflow(
    requirement: Annotated[str, typer.Argument(help="需求描述，或者你提供的代码文件路径。")] = "",
    code: Annotated[Path | None, typer.Option("--code", help="你自己写的代码文件；给了就跳过编码 Agent。")] = None,
    max_rounds: Annotated[int, typer.Option("--max-rounds", help="最多修几轮。")] = 3,
    out: Annotated[Path | None, typer.Option(help="产物目录的父目录。")] = None,
    formats: Annotated[str, typer.Option(help="逗号分隔：console,md,json")] = "console,md",
    quiet: Annotated[bool, typer.Option("-q", "--quiet", help="只打印报告。")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """跑一次「编码 → 审查 → 修复」多 Agent 工作流。

    **两种用法**：

    1. 让 AI 写代码 —— 给一句需求，编码 Agent 写、审查 Agent 挑毛病、
       修复 Agent 改，改完再送回审查。

           codereview workflow "写一个函数，判断字符串是不是回文"

    2. 审你自己写的代码 —— 用 --code 指定文件，跳过编码 Agent，
       直接从审查开始。这条路径下参与协作的是审查和修复两个 Agent。

           codereview workflow --code 我的代码.py

    每次运行都会在产物目录下新建一个带时间戳的子目录，互不干扰。
    """
    from .workflow.runner import run as run_workflow_cli

    source = ""
    if code is not None:
        try:
            source = code.read_text(encoding="utf-8")
        except OSError as exc:
            _fail(f"读不到这个文件：{code}\n{exc}")
        except UnicodeDecodeError:
            # 工作流要拿它去审查，编码不对的话后面每一步都看不清楚。
            _fail(f"这个文件不是 UTF-8 编码，读不了：{code}")
    elif requirement and requirement.endswith(".py") and Path(requirement).is_file():
        # 顺手支持「把文件路径直接当参数传」，不用记 --code 这个选项。
        try:
            source = Path(requirement).read_text(encoding="utf-8")
            requirement = ""
        except (OSError, UnicodeDecodeError):
            pass

    if not source and not requirement.strip():
        _fail(
            "要给点什么才行。两种用法：\n"
            '  · 让 AI 写：codereview workflow "写一个函数，判断是不是回文"\n'
            "  · 审你写的：codereview workflow --code 你的文件.py"
        )

    try:
        run_workflow_cli(
            requirement,
            code=source,
            max_rounds=max_rounds,
            out_dir=out,
            formats=[f.strip() for f in formats.split(",") if f.strip()],
            verbose=verbose,
            quiet=quiet,
        )
    except CodeReviewError as exc:
        _fail(str(exc))


@app.command()
def ask(
    question: Annotated[str | None, typer.Argument(help="Question; omit for a REPL.")] = None,
    thread: Annotated[str, typer.Option(help="Thread id of a previous review.")] = "default",
    run_dir: Annotated[Path | None, typer.Option("--run-dir", help="Review run to discuss.")] = None,
    repl: Annotated[bool, typer.Option("--repl", help="Interactive mode.")] = False,
) -> None:
    """Ask follow-up questions about a completed review.

    The conversation is persisted to SQLite, so this works in a new process after
    the review has finished. Without --run-dir the most recent completed review
    is used, and the one chosen is always printed.
    """
    from .memory.qa import ask_question, run_repl

    try:
        if repl or question is None:
            run_repl(thread_id=thread, console=console, run_dir=run_dir)
        else:
            console.print(ask_question(thread_id=thread, question=question, run_dir=run_dir))
    except FileNotFoundError as exc:
        _fail(str(exc))
    except CodeReviewError as exc:
        _fail(str(exc))


def main() -> None:
    configure_logging()
    app()


if __name__ == "__main__":
    main()
