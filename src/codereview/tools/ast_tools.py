"""Structural analysis of Python source via the stdlib ``ast`` module.

Gives the reviewer facts no model can hallucinate -- real function boundaries,
real complexity numbers, real nesting depth -- and gives the verifier a second
independent source of truth alongside ruff.

A file that does not parse is reported as ``ok=False`` rather than raising. A
syntax error is itself a finding, and it is reported by *this* deterministic
tool, not guessed at by the model.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from ..models.lint import AstSummary, DivisionRisk, FunctionInfo
from .sandbox import read_text_safely, resolve_in_root

logger = logging.getLogger(__name__)

#: Node types that each add one to cyclomatic complexity.
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.BoolOp,
    ast.IfExp,
    ast.comprehension,
)

#: Nodes that introduce a nested scope for depth purposes.
_NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)


class _FunctionCollector(ast.NodeVisitor):
    """Collects function definitions with complexity and nesting depth."""

    def __init__(self) -> None:
        self.functions: list[FunctionInfo] = []
        self.bare_excepts: list[int] = []
        self.mutable_defaults: list[str] = []
        self.division_risks: list[DivisionRisk] = []

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _complexity(node: ast.AST) -> int:
        return 1 + sum(1 for child in ast.walk(node) if isinstance(child, _BRANCH_NODES))

    @staticmethod
    def _depth(top: ast.AST) -> int:
        """Deepest nesting level inside *top*, counting only block-introducing nodes."""
        best = 0
        stack: list[tuple[ast.AST, int]] = [(top, 0)]
        while stack:
            current, depth = stack.pop()
            best = max(best, depth)
            for child in ast.iter_child_nodes(current):
                stack.append((child, depth + 1 if isinstance(child, _NESTING_NODES) else depth))
        return best

    @staticmethod
    def _param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        args = fn.args
        names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
        for extra in (args.vararg, args.kwarg):
            if extra is not None:
                names.add(extra.arg)
        return names

    @staticmethod
    def _zero_checked_names(fn: ast.AST) -> set[str]:
        """Names the function compares against literal zero, anywhere.

        Deliberately coarse: *any* comparison with 0 counts as "the author
        thought about it". A precise check would have to prove the guard
        dominates the division, and a false positive here costs more (the
        reviewer reports a bug that is already handled) than a miss.
        """
        checked: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare):
                parts = [node.left, *node.comparators]
                has_zero = any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, int)
                    and not isinstance(part.value, bool)
                    and part.value == 0
                    for part in parts
                )
                if has_zero:
                    checked.update(part.id for part in parts if isinstance(part, ast.Name))
            elif (
                isinstance(node, ast.UnaryOp)
                and isinstance(node.op, ast.Not)
                and isinstance(node.operand, ast.Name)
            ):
                checked.add(node.operand.id)
        return checked

    @staticmethod
    def _walk_own_scope(fn: ast.AST):
        """Walk *fn*'s body without descending into nested functions.

        Nested defs are recorded separately by ``visit_FunctionDef``, so their
        divisions would otherwise be attributed to the enclosing function.
        """
        stack = list(ast.iter_child_nodes(fn))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))

    @staticmethod
    def _has_mutable_default(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """True for ``def f(items=[])`` / ``def f(opts={})`` / ``def f(s=set())``.

        Evaluated once at definition time in Python, so every call shares the
        same object -- a classic and genuinely dangerous bug.
        """
        defaults = list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d is not None]
        for default in defaults:
            if isinstance(default, ast.List | ast.Dict | ast.Set):
                return True
            if (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id in {"list", "dict", "set"}
            ):
                return True
        return False

    # -- visitor ---------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record(node)
        self.generic_visit(node)

    def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(
            FunctionInfo(
                name=node.name,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                arg_count=len(node.args.args) + len(node.args.kwonlyargs),
                cyclomatic_complexity=self._complexity(node),
                has_docstring=ast.get_docstring(node) is not None,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                nesting_depth=self._depth(node),
            )
        )
        if self._has_mutable_default(node):
            self.mutable_defaults.append(node.name)

        # `total / count` with no check on count is a guaranteed crash for
        # count == 0. Left to the model it is a coin flip -- measured runs
        # missed it roughly half the time -- so it is computed here instead.
        params = self._param_names(node)
        checked = self._zero_checked_names(node)
        seen: set[str] = set()
        for child in self._walk_own_scope(node):
            if not isinstance(child, ast.BinOp) or not isinstance(child.op, ast.Div | ast.FloorDiv):
                continue
            divisor = child.right
            if (
                isinstance(divisor, ast.Name)
                and divisor.id in params
                and divisor.id not in checked
                and divisor.id not in seen
            ):
                seen.add(divisor.id)
                self.division_risks.append(
                    DivisionRisk(function=node.name, divisor=divisor.id, line=child.lineno)
                )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        # A bare `except:` also catches KeyboardInterrupt and SystemExit.
        if node.type is None:
            self.bare_excepts.append(node.lineno)
        self.generic_visit(node)


def python_ast_summary(
    root: Path | str,
    path: str,
    max_bytes: int = 262_144,
) -> AstSummary:
    """Parse *path* and summarise its structure.

    Never raises on bad source: a ``SyntaxError`` comes back as
    ``AstSummary(ok=False, error=...)`` so the pipeline can turn it into a
    finding instead of crashing.

    Raises:
        SandboxViolation: *path* escapes *root*.
        ToolError: the file is missing, binary, or unreadable.
    """
    target = resolve_in_root(root, path)
    text, _encoding, _truncated = read_text_safely(target, max_bytes=max_bytes, root=root)

    line_count = len(text.splitlines())

    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return AstSummary(
            path=path.replace("\\", "/"),
            ok=False,
            error=f"{exc.msg} (line {exc.lineno})" if exc.lineno else str(exc.msg),
            error_line=exc.lineno,
            line_count=line_count,
        )
    except (ValueError, RecursionError) as exc:
        # e.g. source containing NUL bytes, or pathological nesting.
        return AstSummary(
            path=path.replace("\\", "/"),
            ok=False,
            error=f"could not parse: {exc}",
            line_count=line_count,
        )

    collector = _FunctionCollector()
    collector.visit(tree)

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]

    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in ast.walk(tree)
    )

    return AstSummary(
        path=path.replace("\\", "/"),
        ok=True,
        line_count=line_count,
        imports=sorted(set(imports)),
        classes=sorted(set(classes)),
        functions=collector.functions,
        bare_excepts=collector.bare_excepts,
        mutable_default_args=collector.mutable_defaults,
        unguarded_divisions=collector.division_risks,
        has_main_guard=has_main_guard,
    )


def to_candidates(summary: AstSummary):
    """Turn AST facts into tool-sourced candidates.

    Only unambiguous, provable defects become findings. Complexity and docstring
    gaps are deliberately *not* emitted -- they are opinions, and the whole point
    of this module is to emit facts.
    """
    from ..models.findings import CandidateFinding, Evidence

    candidates = []

    if not summary.ok:
        line = summary.error_line or 1
        candidates.append(
            CandidateFinding(
                category="bug",
                severity="critical",
                title=f"文件有语法错误，无法解析：{summary.error}",
                rationale=(
                    f"`{summary.path}` 无法按 Python 语法解析（{summary.error}）。"
                    "这个文件不能被 import，里面任何代码都跑不起来。"
                ),
                evidence=Evidence(path=summary.path, line_start=line, line_end=line),
                suggested_fix="先修掉这一行的语法错误，再回头看其他问题。",
                confidence=1.0,
                source="ast",
                rule_id="AST_PARSE_ERROR",
            )
        )
        return candidates

    for line in summary.bare_excepts:
        candidates.append(
            CandidateFinding(
                category="bug",
                severity="medium",
                title="裸露的 `except:` 会吞掉所有异常",
                rationale=(
                    "裸露的 `except:` 连 `KeyboardInterrupt`（Ctrl-C）和 `SystemExit` "
                    "都会一起捕获，程序既没法中断，真正出错时也看不到原因。"
                    "应该明确写出你想捕获的异常类型。"
                ),
                evidence=Evidence(path=summary.path, line_start=line, line_end=line),
                suggested_fix="except (ValueError, OSError) as exc:  # 写清楚预期会出什么错",
                confidence=0.9,
                source="ast",
                rule_id="AST_BARE_EXCEPT",
            )
        )

    # Cite the function's def line, not the top of the file -- a finding that
    # points at line 1 is useless to a reader and is rightly treated as noise.
    span = {fn.name: (fn.line_start, fn.line_end) for fn in summary.functions}

    for name in summary.mutable_default_args:
        start, end = span.get(name, (1, 1))
        candidates.append(
            CandidateFinding(
                category="bug",
                severity="high",
                title=f"`{name}()` 的参数默认值是可变对象",
                rationale=(
                    f"`{name}` 的参数默认值用了列表/字典/集合。Python 里默认值只在函数"
                    "定义时创建一次，所有调用共用同一个对象——上一次调用往里加的东西，"
                    "下一次调用还留着。应该默认写成 `None`，在函数体里再创建。"
                ),
                evidence=Evidence(path=summary.path, line_start=start, line_end=end),
                suggested_fix=(
                    f"def {name}(..., items=None):\n"
                    "    if items is None:\n"
                    "        items = []"
                ),
                confidence=0.9,
                source="ast",
                rule_id="AST_MUTABLE_DEFAULT",
            )
        )

    for risk in summary.unguarded_divisions:
        candidates.append(
            CandidateFinding(
                category="bug",
                severity="medium",
                title=f"`{risk.function}()` 用参数 `{risk.divisor}` 做除数，没有防除零",
                rationale=(
                    f"`{risk.function}` 第 {risk.line} 行用 `{risk.divisor}` 当除数，"
                    f"但整个函数里没有任何地方检查过 `{risk.divisor}` 是不是 0。"
                    "调用方传 0 进来（比如列表为空时统计个数）就会抛 "
                    "`ZeroDivisionError`，程序直接崩掉。"
                ),
                evidence=Evidence(path=summary.path, line_start=risk.line, line_end=risk.line),
                suggested_fix=(
                    f"if {risk.divisor} == 0:\n"
                    f'    return 0.0  # 或者 raise ValueError("{risk.divisor} 不能为 0")'
                ),
                confidence=0.9,
                source="ast",
                rule_id="AST_ZERO_DIVISION",
            )
        )

    return candidates
