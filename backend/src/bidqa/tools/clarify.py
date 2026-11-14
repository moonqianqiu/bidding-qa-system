"""D6 ask_clarification：澄清追问工具（docs/02 D6；仅首轮挂载，挂载条件见 C2）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from bidqa.tools.base import BaseTool, tool_result


class AskClarificationArgs(BaseModel):
    question_for_user: str = Field(min_length=2, max_length=500,
                                   description="向用户追问的完整问题")


class AskClarificationTool(BaseTool):
    name = "ask_clarification"
    description = ("向用户追问澄清。仅当问题缺少地区/时间/标的物全部限定条件且属于数字类"
                   "（多少/排名/金额）时使用；追问必须一次性列出需要用户补充的信息。")
    args_model = AskClarificationArgs

    def run(self, args: AskClarificationArgs) -> dict[str, Any]:
        payload = tool_result(text=args.question_for_user)
        payload["clarify"] = True  # C5/SSE 澄清轮判定标记（done 阶段两段 phase_times）
        return payload
