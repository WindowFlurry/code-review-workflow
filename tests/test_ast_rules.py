"""The deterministic AST rules.

These are the findings that cannot be hallucinated away: they are computed from
the parse tree. ``AST_ZERO_DIVISION`` exists because measured runs showed the
model spotting ``total / count`` only about half the time -- a coin flip is not
good enough for something that crashes the program.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codereview.tools.ast_tools import python_ast_summary, to_candidates


def _write(tmp_path: Path, source: str) -> Path:
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    return tmp_path


def _divisions(tmp_path: Path):
    summary = python_ast_summary(tmp_path, "sample.py")
    return [(r.function, r.divisor, r.line) for r in summary.unguarded_divisions]


def test_flags_division_by_unguarded_parameter(tmp_path):
    _write(
        tmp_path,
        "def average(total, count):\n"
        "    return total / count\n",
    )
    assert _divisions(tmp_path) == [("average", "count", 2)]


def test_flags_floor_division(tmp_path):
    _write(
        tmp_path,
        "def per_page(total, pages):\n"
        "    return total // pages\n",
    )
    assert _divisions(tmp_path) == [("per_page", "pages", 2)]


@pytest.mark.parametrize(
    "guard",
    [
        "    if count == 0:\n        return 0.0\n",
        "    if 0 == count:\n        return 0.0\n",
        "    if count != 0:\n        return total / count\n    return 0.0\n",
        "    if count <= 0:\n        raise ValueError('count')\n",
        "    if not count:\n        return 0.0\n",
    ],
)
def test_does_not_flag_when_zero_was_considered(tmp_path, guard):
    body = guard if "return total / count" in guard else guard + "    return total / count\n"
    _write(tmp_path, f"def average(total, count):\n{body}")
    assert _divisions(tmp_path) == []


def test_ignores_constant_divisor(tmp_path):
    _write(tmp_path, "def half(x):\n    return x / 2\n")
    assert _divisions(tmp_path) == []


def test_ignores_local_and_attribute_divisors(tmp_path):
    # Only *parameters* are flagged: a local variable may have been validated
    # in a way a shallow AST pass cannot see, and flagging it would be noise.
    _write(
        tmp_path,
        "def ratio(total, count):\n"
        "    n = max(count, 1)\n"
        "    return total / n\n",
    )
    assert _divisions(tmp_path) == []


def test_ignores_modulo(tmp_path):
    # `%` on a string is formatting, not arithmetic -- too many false positives.
    _write(tmp_path, "def show(fmt, value):\n    return fmt % value\n")
    assert _divisions(tmp_path) == []


def test_attributes_division_to_its_own_function(tmp_path):
    _write(
        tmp_path,
        "def outer(items):\n"
        "    def inner(total, count):\n"
        "        return total / count\n"
        "    return inner\n",
    )
    assert _divisions(tmp_path) == [("inner", "count", 3)]


def test_candidate_points_at_the_division_line(tmp_path):
    _write(
        tmp_path,
        "def average(total, count):\n" "    extra = 1\n" "    return total / count\n",
    )
    candidates = to_candidates(python_ast_summary(tmp_path, "sample.py"))
    division = [c for c in candidates if c.rule_id == "AST_ZERO_DIVISION"]
    assert len(division) == 1
    assert division[0].evidence.line_start == 3
    assert division[0].category == "bug"
