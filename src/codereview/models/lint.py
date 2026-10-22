"""Deterministic analysis results (ruff, AST).

These come from tools, not the model. Their line numbers are ground truth and
are what the verifier uses to cross-check the model's claims.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LintFinding(BaseModel):
    """One ruff diagnostic, mapped from ``ruff check --output-format json``.

    Ruff's JSON gives absolute ``filename`` plus ``location.row`` and
    ``end_location.row`` -- a real line range from a real tool, which makes this
    the ideal anchor for a finding.
    """

    rule_id: str = Field(description="Rule code, e.g. F401.")
    message: str
    path: str = Field(description="Repo-relative POSIX path.")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    url: str | None = None
    fixable: bool = False


class FunctionInfo(BaseModel):
    """Structure of one function or method."""

    name: str
    line_start: int
    line_end: int
    arg_count: int = 0
    #: McCabe-style count: 1 + number of branching statements.
    cyclomatic_complexity: int = 1
    has_docstring: bool = False
    is_async: bool = False
    nesting_depth: int = 0


class DivisionRisk(BaseModel):
    """A ``/`` or ``//`` whose divisor is a bare parameter with no zero check.

    This is a *fact* about the syntax tree, not an opinion: nothing in the
    function body compares that name against zero before dividing, so a call
    with 0 raises ``ZeroDivisionError``.
    """

    function: str
    divisor: str
    line: int = Field(ge=1)


class AstSummary(BaseModel):
    """Structural facts about a Python file.

    When ``ok`` is false the file does not parse. That is reported by this
    deterministic tool rather than handed to the model to speculate about.
    """

    path: str
    ok: bool = True
    error: str | None = None
    #: Line the parse failure occurred on, when known. Cited as the finding's
    #: location so it lines up with ruff's syntax-error report on the same file
    #: and the two deduplicate instead of showing up twice.
    error_line: int | None = None
    line_count: int = 0
    imports: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    functions: list[FunctionInfo] = Field(default_factory=list)
    #: Bare ``except:`` / ``except Exception: pass`` occurrences with line numbers.
    bare_excepts: list[int] = Field(default_factory=list)
    mutable_default_args: list[str] = Field(
        default_factory=list, description="Function names with a mutable default argument."
    )
    unguarded_divisions: list[DivisionRisk] = Field(
        default_factory=list,
        description="Divisions by a parameter that is never checked against zero.",
    )
    has_main_guard: bool = False

    @property
    def max_complexity(self) -> int:
        return max((f.cyclomatic_complexity for f in self.functions), default=0)
