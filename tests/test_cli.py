"""命令行的交互模式。

``.bat`` 现在只负责启动（纯 ASCII，会在 GitHub 上正常显示），所有中文提示
都从 Python 输出。这里测的就是那些交互分支：回车看示例、留空取消、
拖拽路径带引号。全部不联网、不花钱。
"""

from __future__ import annotations

from typer.testing import CliRunner

from codereview.cli import _clean_path_input, app

runner = CliRunner()


def test_clean_path_input_strips_drag_and_drop_quotes() -> None:
    assert _clean_path_input('  "C:\\a b\\x.py"  ') == "C:\\a b\\x.py"
    assert _clean_path_input("'x.py'") == "x.py"
    assert _clean_path_input("   ") == ""


def test_workflow_interactive_empty_input_replays_the_demo() -> None:
    """直接回车 = 看示例，走回放，不调用 AI。"""
    result = runner.invoke(app, ["workflow", "-i"], input="\n")

    assert result.exit_code == 0
    assert "执行过程" in result.stdout
    assert "不会调用 AI" in result.stdout


def test_review_interactive_empty_input_cancels() -> None:
    """留空回车不该报错，也不该去调用模型。"""
    result = runner.invoke(app, ["review", "-i"], input="\n")

    assert result.exit_code == 0
    assert "已取消" in result.stdout
