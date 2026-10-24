"""工作流的五个节点。

其中 ``review_node`` 是这次作业的关键：**它不实现任何审查逻辑，只是调用
作业一那张图**。一行调用换回来静态检查、逐文件 AI 审查、证据核对、
独立复核、去重汇总 —— 这就是 PPT 说的「复用项目1中开发的 Agent」。
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime
from pathlib import Path

from ..agent.graph import build_graph
from ..agent.state import Usage, empty_state, total_usage
from ..config import Settings, get_settings
from ..models.findings import VerifiedFinding
from .agents import (
    CoderOutput,
    FixerOutput,
    build_coder,
    build_fixer,
    format_findings_for_fixer,
)
from .state import (
    RoundSummary,
    WorkflowReport,
    WorkflowState,
    WorkflowStep,
    actionable_count,
    severe_count,
)

logger = logging.getLogger(__name__)

#: 编码/修复 Agent 产出的文件名。固定名字，所以不存在路径逃逸的问题。
SOLUTION_FILENAME = "solution.py"

#: Agent 实例缓存。构建模型客户端不贵但也不免费，工作流里要反复用。
_AGENT_CACHE: dict[str, object] = {}


def _get_coder(settings: Settings):
    if "coder" not in _AGENT_CACHE:
        _AGENT_CACHE["coder"] = build_coder(settings)
    return _AGENT_CACHE["coder"]


def _get_fixer(settings: Settings):
    if "fixer" not in _AGENT_CACHE:
        _AGENT_CACHE["fixer"] = build_fixer(settings)
    return _AGENT_CACHE["fixer"]


def clear_agent_cache() -> None:
    """测试用：清掉缓存的 Agent，避免跨测试串味。"""
    _AGENT_CACHE.clear()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)


def strip_code_fences(text: str) -> str:
    """去掉模型自作主张加的 Markdown 代码围栏。

    提示词里已经说了「不要用 ``` 包起来」，但模型经常还是加。加着也不影响
    运行？会影响 —— 写进 .py 文件之后 `````python`` 就是一个语法错误，
    审查 Agent 立刻会报 E999，然后修复 Agent 花一整轮去修一个本来不存在的问题。
    所以这里必须擦干净。
    """
    stripped = text.strip()
    match = _FENCE.match(stripped)
    if match:
        return match.group("body").strip() + "\n"
    return stripped + "\n"


def write_solution(workspace: Path, code: str) -> Path:
    """把代码写进工作目录。**只有工作流能写盘，AI 碰不到文件系统。**"""
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / SOLUTION_FILENAME
    target.write_text(code, encoding="utf-8")
    return target


def make_diff(old: str, new: str, filename: str = SOLUTION_FILENAME) -> tuple[str, int]:
    """算出改前改后的差异。

    返回 ``(diff 文本, 改动行数)``。没有变化时 diff 为空。

    这是「AI 改代码」最直接的证据 —— 光说「修复完成」看不出它到底动了什么，
    把差异摆出来才看得见。
    """
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{filename}（改前）",
            tofile=f"{filename}（改后）",
            # 只要 2 行上下文：够看清改动落在哪个函数里，又不会把整个文件刷屏。
            n=2,
        )
    )
    if not diff_lines:
        return "", 0

    # 数一下真正变化的内容行，不算 diff 自己的 +++ / --- 头。
    changed = sum(
        1
        for line in diff_lines
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )
    return "".join(diff_lines), changed


def seed_workspace(workspace: Path, code: str) -> None:
    """把用户提供的代码放进工作目录。

    和工作流自己写盘走的是同一套逻辑 —— 差别只是这份代码的来源是用户而不是
    Agent。写进去之后，审查 Agent 看到的就是一份普通的待审文件。
    """
    write_solution(workspace, code)


def _extract(result: dict, schema):
    """从 Agent 的返回里取出结构化结果。

    ``create_agent`` 把结构化输出放在 ``structured_response``。取不到就返回
    None，由调用方决定怎么办 —— 不抛异常，一个节点失败不该让整条流程崩掉。
    """
    payload = result.get("structured_response")
    if isinstance(payload, schema):
        return payload
    if isinstance(payload, dict):
        try:
            return schema.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("结构化输出解析失败：%s", exc)
    return None


# ---------------------------------------------------------------------------
# 节点 1：编码
# ---------------------------------------------------------------------------


def code_node(state: WorkflowState) -> dict:
    """编码 Agent：把需求变成代码。"""
    settings = get_settings()
    requirement = state["requirement"]
    workspace = Path(state["workspace"])

    logger.info("[编码] 开始，需求：%s", requirement[:60])

    try:
        agent = _get_coder(settings)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": f"需求：\n\n{requirement}"}]},
            config={"configurable": {"thread_id": f"{state['run_id']}:code"}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("编码 Agent 失败")
        return {
            "code": "",
            "steps": [
                WorkflowStep(round=0, node="编码 Agent", reads="需求", summary=f"失败：{exc}")
            ],
            "errors": [f"编码 Agent 失败：{exc}"],
            "usage": [],
        }

    output = _extract(result, CoderOutput)
    if output is None or not output.code.strip():
        return {
            "code": "",
            "steps": [
                WorkflowStep(round=0, node="编码 Agent", reads="需求", summary="没有产出可用代码")
            ],
            "errors": ["编码 Agent 没有返回代码"],
            "usage": [_usage_of(result)],
        }

    code = strip_code_fences(output.code)
    path = write_solution(workspace, code)

    logger.info("[编码] 完成，%d 行，写入 %s", len(code.splitlines()), path.name)

    return {
        "code": code,
        # 编码 Agent 写出来的第一版，也是「改之前」的那一版。
        "original_code": code,
        "original_supplied": False,
        "steps": [
            WorkflowStep(
                round=0,
                node="编码 Agent",
                reads="需求",
                writes="代码",
                summary=f"生成代码 {len(code.splitlines())} 行，已写入 {SOLUTION_FILENAME}",
                detail=output.notes,
            )
        ],
        "errors": [],
        "usage": [_usage_of(result)],
    }


# ---------------------------------------------------------------------------
# 节点 2：审查（复用作业一）
# ---------------------------------------------------------------------------


def review_node(state: WorkflowState) -> dict:
    """调用作业一的整张图来审查工作目录。

    **这个函数是 HW2「复用 HW1」的核心。** 它自己不写一行审查逻辑：

    - 静态检查（ruff + 语法分析）→ 作业一的 ``static_scan``
    - 逐文件 AI 审查 → 作业一的 ``review_file`` 节点
    - 证据核对（引用对不上就丢弃）→ 作业一的 ``bind_evidence``
    - 独立复核（第二个 AI 挑刺）→ 作业一的 ``verify`` 节点
    - 去重排序 → 作业一的 ``aggregate`` 节点

    交出去的只有工作目录的路径，拿回来的是**已经验证过的问题清单**。
    """
    workspace = state["workspace"]
    round_no = state.get("round", 0)
    run_id = f"{state['run_id']}:review:{round_no}"

    logger.info("[审查] 第 %d 轮，调用作业一的审查图", round_no + 1)

    try:
        graph = build_graph()
        result = graph.invoke(
            empty_state(workspace, run_id),
            # 和另外两个节点保持一致，用关键字传 config。位置传也能跑，
            # 但同一个文件里两种写法混着，改动时容易踩坑。
            config={"configurable": {"thread_id": run_id}, "recursion_limit": 200},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("审查失败")
        # 审查失败时返回空清单，让流程走到报告节点收尾，而不是在循环里打转。
        return {
            "findings": [],
            "steps": [
                WorkflowStep(
                    round=round_no, node="审查 Agent", reads="代码", summary=f"失败：{exc}"
                )
            ],
            "errors": [f"审查失败：{exc}"],
            "usage": [],
        }

    report = result.get("report")
    findings: list[VerifiedFinding] = list(report.reportable) if report else []
    stats = report.stats if report else None

    actionable = actionable_count(findings)
    logger.info(
        "[审查] 第 %d 轮完成：%d 条问题，其中 %d 条值得修",
        round_no + 1,
        len(findings),
        actionable,
    )

    detail_parts = []
    if stats:
        detail_parts.append(
            f"静态检查丢弃 {stats.rejected_by_evidence} 条引用对不上的结论"
        )
    if actionable:
        detail_parts.append(f"其中 {actionable} 条需要修复")
    elif findings:
        detail_parts.append("都只是风格或可维护性方面的意见，不触发修复")

    return {
        "findings": findings,
        "steps": [
            WorkflowStep(
                round=round_no,
                node="审查 Agent",
                reads="代码",
                writes="问题清单",
                summary=f"第 {round_no + 1} 轮审查完成，发现 {len(findings)} 条问题",
                detail="；".join(detail_parts),
            )
        ],
        "rounds": [
            RoundSummary(
                round=round_no + 1,
                code_lines=len(state.get("code", "").splitlines()),
                findings_total=len(findings),
                findings_actionable=actionable,
            )
        ],
        "errors": [],
        "usage": [_sum_usage(result.get("usage", []))],
    }


# ---------------------------------------------------------------------------
# 节点 3：判断（条件分支）
# ---------------------------------------------------------------------------


def decide(state: WorkflowState) -> str:
    """决定是继续修，还是收工。

    **这是作业二要的「条件分支」。** 两个条件同时满足才继续：

    1. **还有值得修的问题** —— 高危，或者属于「缺陷 / 安全」类别。
       只按严重程度判断是不够的：实测中一个真正的正确性 bug（合法输入下
       返回错误结果）被标成了「中」，就被漏掉了。反之，纯风格意见不触发修复，
       因为那种问题修不完。
    2. **还没到轮数上限** —— 硬性兜底，防止改来改去停不下来。

    两条出口都会在报告里说明**为什么停的**，用户能看出是「修好了」
    还是「到上限了」。
    """
    findings = state.get("findings", [])
    actionable = actionable_count(findings)
    round_no = state.get("round", 0)
    max_rounds = state.get("max_rounds", 3)

    if actionable and round_no < max_rounds:
        logger.info("[判断] 还有 %d 条值得修的问题，进入第 %d 轮修复", actionable, round_no + 1)
        return "fix"

    if actionable:
        logger.info("[判断] 已到轮数上限，带着 %d 条问题收工", actionable)
        return "report"

    logger.info("[判断] 没有需要修复的问题，收工")
    return "report"


def explain_stop(state: WorkflowState) -> str:
    """把「为什么停下来」写成一句人话。"""
    findings = state.get("findings", [])
    actionable = actionable_count(findings)
    round_no = state.get("round", 0)
    max_rounds = state.get("max_rounds", 3)

    if not findings:
        return f"第 {round_no + 1} 轮审查未发现问题，提前结束。"

    if not actionable:
        return (
            f"第 {round_no + 1} 轮审查后剩下的 {len(findings)} 条问题都只是风格或"
            "可维护性方面的意见，没有需要修复的缺陷，结束。"
        )

    return (
        f"已达最大轮数 {max_rounds} 轮，仍有 {actionable} 条需要修复的问题未解决，停止。"
    )


# ---------------------------------------------------------------------------
# 节点 4：修复
# ---------------------------------------------------------------------------


def fix_node(state: WorkflowState) -> dict:
    """修复 Agent：按审查结果改代码。"""
    settings = get_settings()
    workspace = Path(state["workspace"])
    findings = state.get("findings", [])
    round_no = state.get("round", 0) + 1

    logger.info("[修复] 第 %d 轮，%d 条问题", round_no, len(findings))

    prompt = (
        f"## 当前代码\n\n```python\n{state.get('code', '')}\n```\n\n"
        f"## 代码审查报告（共 {len(findings)} 条）\n\n"
        f"{format_findings_for_fixer(findings)}\n\n"
        "请修掉以上问题，返回修复后的完整文件。"
    )

    try:
        agent = _get_fixer(settings)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"configurable": {"thread_id": f"{state['run_id']}:fix:{round_no}"}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("修复 Agent 失败")
        return {
            "round": round_no,
            "steps": [
                WorkflowStep(
                    round=round_no,
                    node="修复 Agent",
                    reads="代码 + 问题清单",
                    summary=f"失败：{exc}",
                )
            ],
            "errors": [f"修复 Agent 失败：{exc}"],
            "usage": [],
        }

    output = _extract(result, FixerOutput)
    if output is None or not output.code.strip():
        return {
            "round": round_no,
            "steps": [
                WorkflowStep(
                    round=round_no,
                    node="修复 Agent",
                    reads="代码 + 问题清单",
                    summary="没有产出可用代码",
                )
            ],
            "errors": ["修复 Agent 没有返回代码"],
            "usage": [_usage_of(result)],
        }

    previous = state.get("code", "")
    code = strip_code_fences(output.code)
    write_solution(workspace, code)

    diff, changed = make_diff(previous, code)

    logger.info("[修复] 第 %d 轮完成，%d 行，改动 %d 行", round_no, len(code.splitlines()), changed)

    return {
        "code": code,
        "round": round_no,
        "steps": [
            WorkflowStep(
                round=round_no,
                node="修复 Agent",
                reads="代码 + 问题清单",
                writes="代码（改写）",
                summary=(
                    f"第 {round_no} 轮修复完成，改动 {changed} 行"
                    if changed
                    else f"第 {round_no} 轮修复完成，代码没有变化"
                ),
                detail=output.changes,
                diff=diff,
                changed_lines=changed,
            )
        ],
        "errors": [],
        "usage": [_usage_of(result)],
    }


# ---------------------------------------------------------------------------
# 节点 5：汇总
# ---------------------------------------------------------------------------


def report_node(state: WorkflowState) -> dict:
    """把整条流程的结果汇总成一份报告。"""
    findings = state.get("findings", [])

    report = WorkflowReport(
        requirement=state.get("requirement", ""),
        original_code=state.get("original_code", ""),
        original_supplied=state.get("original_supplied", False),
        final_code=state.get("code", ""),
        rounds_used=state.get("round", 0),
        max_rounds=state.get("max_rounds", 3),
        stop_reason=explain_stop(state),
        remaining=findings,
        rounds=state.get("rounds", []),
        steps=state.get("steps", []),
        usage=_sum_usage(state.get("usage", [])),
        errors=state.get("errors", []),
        started_at=state.get("started_at"),
        finished_at=datetime.now(),
    )

    logger.info(
        "工作流结束：%d 轮，剩余 %d 条问题（高危 %d）",
        report.rounds_used,
        len(report.remaining),
        severe_count(report.remaining),
    )

    return {"report": report}


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------


def _usage_of(result: dict) -> Usage:
    """从 Agent 返回的消息里取出令牌用量。"""
    usage = Usage(llm_calls=0)
    for message in result.get("messages", []):
        metadata = getattr(message, "usage_metadata", None)
        if not metadata:
            continue
        usage.llm_calls += 1
        usage.input_tokens += int(metadata.get("input_tokens", 0) or 0)
        usage.output_tokens += int(metadata.get("output_tokens", 0) or 0)
    return usage


def _sum_usage(usages) -> Usage:
    """把一串用量记录加总。作业一里已有 ``total_usage``，这里包一层兼容。"""
    if not usages:
        return Usage()
    return total_usage(list(usages))
