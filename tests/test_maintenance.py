"""Housekeeping: run-directory retention, and the recorded demo replay.

Both exist because of the same complaint from a real user: after a few runs
``runs/`` was full of folders, and the demo cost money every time it was shown.
"""

from __future__ import annotations

from pathlib import Path

from codereview.logging_setup import prune_run_dirs
from codereview.workflow.demo import load_demo_report


def _make_runs(base: Path, names: list[str]) -> None:
    for name in names:
        (base / name).mkdir(parents=True)
        (base / name / "workflow-report.md").write_text("x", encoding="utf-8")


def test_keeps_only_the_newest(tmp_path):
    names = [f"2026010{day}-120000" for day in range(1, 6)]  # 5 runs
    _make_runs(tmp_path, names)

    removed = prune_run_dirs(tmp_path, keep=2)

    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == names[-2:]
    assert sorted(p.name for p in removed) == names[:3]


def test_never_deletes_folders_it_did_not_create(tmp_path):
    # The user's own folders under runs/ must survive, whatever their age.
    _make_runs(tmp_path, ["20260101-120000", "我的审查", "工作流演示", "notes"])
    (tmp_path / "random.txt").write_text("keep me", encoding="utf-8")

    prune_run_dirs(tmp_path, keep=0)

    assert {p.name for p in tmp_path.iterdir()} == {
        "random.txt",
        "我的审查",
        "工作流演示",
        "notes",
    }


def test_keep_larger_than_count_is_a_noop(tmp_path):
    _make_runs(tmp_path, ["20260101-120000", "20260102-120000"])
    assert prune_run_dirs(tmp_path, keep=10) == []
    assert len(list(tmp_path.iterdir())) == 2


def test_missing_directory_is_not_an_error(tmp_path):
    assert prune_run_dirs(tmp_path / "does-not-exist") == []


def test_demo_report_loads_and_is_marked_as_recorded():
    report = load_demo_report()

    # It is a real run, so it has real steps, a verdict and final code.
    assert report.steps
    assert report.final_code.strip()
    assert report.stop_reason
    assert report.steps[0].node == "你（提供代码）"
    assert any(step.diff for step in report.steps)
