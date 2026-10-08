"""工作流里的两个新 Agent：编码 和 修复。

第三个 Agent（审查）不在这里 —— 它直接复用作业一的 ``build_reviewer``，
连同证据核对和独立复核一起。

**为什么这两个 Agent 没有工具**

作业一的工具有一个共同点：全是**只读**的。当初是刻意的——AI 碰不到文件系统，
就不可能改坏东西。

编码和修复要产出代码，所以有两条路：

1. 给它们新建一套「写文件」工具 —— 要额外处理沙箱写入、路径逃逸、
   并发写冲突，成本高，而且把只读这条安全边界打破了。
2. **让它们返回完整的代码文本，由工作流负责写盘** —— 选这条。

结果是一样的（文件被写出来了），但 AI 始终碰不到文件系统：
它只能在结构化的返回值里填一段字符串。安全边界一点没松动。

代价是每轮重写整个文件。产出限定为单文件，所以无所谓。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

from ..config import Settings
from ..llm.factory import build_chat_model
from ..models.findings import VerifiedFinding

# ---------------------------------------------------------------------------
# 结构化输出：Agent 返回的就是这两样东西，工作流拿它去写文件
# ---------------------------------------------------------------------------


class CoderOutput(BaseModel):
    """编码 Agent 的产出。"""

    code: str = Field(
        description="完整的 Python 文件内容。是整份文件，不是片段，不要加 Markdown 代码围栏。"
    )
    notes: str = Field(
        default="", description="一两句话说明设计思路，会写进执行日志。"
    )


class FixerOutput(BaseModel):
    """修复 Agent 的产出。"""

    code: str = Field(
        description="修复之后的完整 Python 文件内容。是整份文件，不要加 Markdown 代码围栏。"
    )
    changes: str = Field(
        default="", description="这一轮具体改了什么，逐条列出，会写进执行日志。"
    )


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

CODER_PROMPT = """\
你是一名 Python 程序员。根据用户给出的需求，写出一个**完整、可运行**的 Python 文件。

要求：

- 一个文件装下全部内容，不要拆分。
- 函数职责单一，命名用英文小写下划线。
- 每个函数写 docstring，用中文，说明参数和返回值。
- 关键逻辑写中文注释。
- 代码里如果要处理边界情况（空输入、除零、类型不对），**要真的处理**，
  不要只写注释说明「这里应该处理」。
- 只写需求要求的东西，不要额外加命令行界面、测试、日志框架。

`code` 字段填**整份文件的内容**：从第一行到最后一行，不要省略，
不要用 ``` 包起来，不要写「其余部分不变」这种话。
"""

FIXER_PROMPT = """\
你是一名 Python 程序员。你拿到一份代码和一份代码审查报告，请修复报告里指出的问题。

规则：

- 把报告里每一条问题**都修掉**。报告是经过验证的，问题确实存在。
- **只改有问题的地方。** 没被指出的代码原样保留，不要顺手重构、不要改风格、
  不要重命名没被点名的变量。
- 不要删功能。修完之后程序的原有行为要保持不变。
- 如果某条问题你认为报告说得不对，可以不改，但要在 `changes` 里说明理由。
- 修完自己要检查一遍：括号配对了吗？缩进对吗？变量名拼对了吗？

`code` 字段填**修复后的整份文件内容**：从第一行到最后一行，不要省略，
不要用 ``` 包起来，不要写「其余部分不变」这种话。
"""


def format_findings_for_fixer(findings: list[VerifiedFinding]) -> str:
    """把审查结果整理成修复 Agent 能直接用的清单。

    每条都带上**具体行号**和**原始代码片段**。这些片段来自作业一的证据核对，
    是从文件里逐字符读回来的，不是模型写的 —— 所以修复 Agent 看到的是真东西，
    它不会去修一个不存在的问题。这是两个作业之间最自然的一处衔接。
    """
    if not findings:
        return "（本次审查没有发现问题）"

    lines: list[str] = []
    for index, item in enumerate(findings, start=1):
        lines.append(f"### 问题 {index}：{item.title}")
        lines.append(f"- 严重程度：{item.severity}")
        lines.append(f"- 类别：{item.category}")

        if item.evidence:
            location = f"{item.evidence.path}:{item.evidence.line_start}"
            if item.evidence.line_end != item.evidence.line_start:
                location += f"-{item.evidence.line_end}"
            lines.append(f"- 位置：{location}")
            lines.append("- 相关代码：")
            lines.append("```python")
            lines.append(item.evidence.snippet)
            lines.append("```")

        lines.append(f"- 说明：{item.rationale}")
        if item.suggested_fix:
            lines.append(f"- 建议改法：{item.suggested_fix}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def build_coder(settings: Settings):
    """构建编码 Agent：一句需求进去，一份代码出来。"""
    return create_agent(
        model=build_chat_model("coder", settings),
        tools=None,
        system_prompt=CODER_PROMPT,
        # 和作业一一样必须用 ToolStrategy：交给框架自己挑的话，它会选
        # provider 原生的结构化输出，DeepSeek 会返回 400。
        response_format=ToolStrategy(CoderOutput),
    )


def build_fixer(settings: Settings):
    """构建修复 Agent：代码 + 问题清单进去，修好的代码出来。"""
    return create_agent(
        model=build_chat_model("fixer", settings),
        tools=None,
        system_prompt=FIXER_PROMPT,
        response_format=ToolStrategy(FixerOutput),
    )
