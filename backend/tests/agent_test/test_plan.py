"""WP5a Plan-and-Solve（agent/plan.py）：should_plan 启发式、build_plan 解析、
计划注入 system prompt 与 LoopResult.plan_steps；开关关闭=零额外模型调用。

FakeChat 脚本化风格与 test_loop 一致：plain 脚本供规划调用（chat），
tools_script 供主循环（chat_stream_round）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from bidqa.agent.context import build_system_prompt
from bidqa.agent.loop import AgentLoop
from bidqa.agent.plan import MAX_STEPS, STEP_CHAR_LIMIT, build_plan, should_plan
from bidqa.tools.base import BaseTool, ToolRegistry, tool_result
from bidqa.tools.clarify import AskClarificationTool
from bidqa.tools.runner import ToolRunner


# ── fakes ──
class FakeChat:
    """脚本化模型：chat（规划）与 chat_stream_round（主循环）分离计数。"""

    def __init__(self, tools_script: list[dict] | None = None,
                 plain_script: list[str] | None = None):
        self.tools_script = list(tools_script or [])
        self.plain_script = list(plain_script or [])
        self.calls: list[dict] = []

    def chat(self, messages, temperature: float = 0.3) -> str:
        self.calls.append({"kind": "plain", "messages": messages})
        return self.plain_script.pop(0)

    def chat_stream_round(self, messages, tools, temperature: float = 0.3):
        self.calls.append({"kind": "stream_round", "messages": messages, "tools": tools})
        item = self.tools_script.pop(0)
        if item.get("tool_calls"):
            for i, tc in enumerate(item["tool_calls"]):
                fn = tc["function"]
                yield ("tool", (i, tc["id"], fn["name"], fn["arguments"]))
        else:
            yield ("content", item.get("content") or "")

    def chat_stream(self, messages, temperature: float = 0.3):
        self.calls.append({"kind": "stream", "messages": messages})
        text = self.plain_script.pop(0)
        yield text

    def chat_tools(self, messages, tools, temperature: float = 0.3) -> dict:
        self.calls.append({"kind": "tools", "messages": messages, "tools": tools})
        return self.tools_script.pop(0)


def tool_msg(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def text_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)


class FakeSearchTool(BaseTool[SearchArgs]):
    name = "search_knowledge"
    description = "知识库检索（fake）"
    args_model = SearchArgs

    def run(self, args: SearchArgs) -> dict:
        return tool_result(text=f"[1] 关于{args.query}的材料",
                           sources=[{"id": "chunk-1", "title": f"{args.query}规定",
                                     "score": 0.9}])


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FakeSearchTool())
    reg.register(AskClarificationTool())
    return reg


def plan_calls(chat: FakeChat) -> list[dict]:
    return [c for c in chat.calls if c["kind"] == "plain"]


# ── should_plan：真样例 ──
@pytest.mark.parametrize("question", [
    "分别列出合肥市2023年和2024年的中标金额与项目数量",      # 协调词「分别」
    "对比安徽省与江苏省的空调中标金额，并且给出供应商排名",   # 对比/并且
    "全省各地市2024年采购规模是多少？排名前五的又是哪些？",   # ≥2 问句
    "请梳理近三年（2022-2024年）安徽省各地市公共资源交易平台的工程施工类项目的中标"
    "金额变化趋势，并说明金额排名前三的地市与对应的中标候选人情况，同时给出各年度的"
    "项目数量对比结果",                                        # 长复合问题（≥40 字）
    "空调、电梯、消防设备、暖通设备的采购情况如何？",          # 顿号列举多实体（中长）
])
def test_should_plan_true_for_compound(question: str) -> None:
    assert should_plan(question) is True


# ── should_plan：假样例 ──
@pytest.mark.parametrize("question,kwargs", [
    ("空调采购有哪些？", {}),                     # 单问句短问题
    ("中标金额总共多少？", {}),                   # 澄清判据命中（无限定数字题）→ 不规划先追问
    ("多少钱？", {}),                             # 过短
    ("", {}),                                     # 空
    ("空调、电梯的采购情况", {}),                 # 单顿号短问题（列举信号不足）
    ("合肥市呢？", {"history_turns": 2}),         # 有历史的指代性短追问
])
def test_should_plan_false(question: str, kwargs: dict) -> None:
    assert should_plan(question, **kwargs) is False


def test_should_plan_followup_suppression() -> None:
    """有历史时短追问（<16 字）不规划；同一问题首轮（无历史）命中协调词会规划。"""
    q = "对比电梯与空调的维保要求"
    assert should_plan(q, history_turns=0) is True
    assert should_plan(q, history_turns=1) is False


def test_should_plan_kwargs_have_defaults() -> None:
    """has_image 不改变判定（视觉走独立槽位，计划只管取数）；kwargs 均可缺省。"""
    assert should_plan("分别列出2023年和2024年的中标金额",
                       has_image=True, history_turns=0) is True


# ── build_plan 解析 ──
class PlanChat:
    """单脚本 chat 桩（只被 build_plan 调用）。"""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, temperature: float = 0.3) -> str:
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_build_plan_normal_json() -> None:
    chat = PlanChat('{"steps": ["查合肥市2024年中标金额", "按供应商分组统计"]}')
    steps = build_plan(chat, "合肥市2024年中标金额和供应商排名？")
    assert steps == ["查合肥市2024年中标金额", "按供应商分组统计"]
    assert len(chat.calls) == 1
    # 规划调用带问题原文（主模型槽位，私有材料正文不进规划上下文）
    assert "合肥市2024年中标金额和供应商排名" in chat.calls[0][-1]["content"]


def test_build_plan_strips_code_fence_and_noise() -> None:
    chat = PlanChat('```json\n好的，计划如下：\n{"steps": ["查A", "查B"]}\n```\n请参考。')
    assert build_plan(chat, "问题") == ["查A", "查B"]


def test_build_plan_garbage_returns_empty() -> None:
    assert build_plan(PlanChat("我无法给出计划"), "问题") == []
    assert build_plan(PlanChat('{"other": 1}'), "问题") == []
    assert build_plan(PlanChat('{"steps": "不是列表"}'), "问题") == []


def test_build_plan_failure_returns_empty() -> None:
    """模型调用失败 → []（绝不阻塞回答）。"""
    assert build_plan(PlanChat(RuntimeError("boom")), "问题") == []


def test_build_plan_caps_steps_and_length() -> None:
    raw = '{"steps": [' + ", ".join(
        json.dumps(f"步骤{i}" + "x" * STEP_CHAR_LIMIT) for i in range(1, 6)) + ']}'
    chat = PlanChat(raw)
    steps = build_plan(chat, "问题")
    assert len(steps) == MAX_STEPS                     # ≤3 步
    assert all(len(s) <= STEP_CHAR_LIMIT for s in steps)  # 每条 ≤80 字


def test_build_plan_context_hint_appended() -> None:
    chat = PlanChat('{"steps": ["查A"]}')
    build_plan(chat, "问题", context_hint="用户附带了图片")
    assert "用户附带了图片" in chat.calls[0][-1]["content"]


# ── 计划节注入（context.py plan_section）──
def test_plan_section_format() -> None:
    from bidqa.agent.context import plan_section
    assert plan_section(["查A", "查B"]) == "[执行计划] 1) 查A 2) 查B"
    assert plan_section([]) == ""


def test_plan_section_into_system_prompt() -> None:
    p = build_system_prompt(tool_table="- t", first_round=True, qualified=True,
                            plan_steps=["查A", "查B"])
    assert "[执行计划] 1) 查A 2) 查B" in p
    assert "[执行计划]" not in build_system_prompt(
        tool_table="- t", first_round=True, qualified=True)


# ── 循环集成 ──
COMPOUND_Q = "分别列出合肥市2023年和2024年的中标金额与项目数量"


def test_plan_injected_and_recorded() -> None:
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "合肥"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=['{"steps": ["查合肥市2023年中标金额", "查合肥市2024年中标金额"]}'],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(COMPOUND_Q, plan_enabled=True)
    assert r.plan_steps == ["查合肥市2023年中标金额", "查合肥市2024年中标金额"]
    assert len(plan_calls(chat)) == 1                  # 规划恰一次（取数前）
    first_round = next(c for c in chat.calls if c["kind"] == "stream_round")
    assert "[执行计划]" in first_round["messages"][0]["content"]
    assert r.rounds == 2 and r.tool_called and not r.hit_cap


def test_plan_disabled_zero_model_calls() -> None:
    """开关关闭：行为与现状逐字一致、零额外调用（规划调用=0）。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "合肥"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=[],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(COMPOUND_Q, plan_enabled=False)
    assert plan_calls(chat) == []                      # 零规划调用
    assert r.plan_steps == []
    first_round = next(c for c in chat.calls if c["kind"] == "stream_round")
    assert "[执行计划]" not in first_round["messages"][0]["content"]
    assert r.rounds == 2 and r.answer == "依据[1]回答。"


def test_simple_question_zero_plan_calls_even_enabled() -> None:
    """简单问题 + 开关开：should_plan 不命中 → 零额外调用。"""
    chat = FakeChat(tools_script=[text_msg("知识库未包含相关内容。")], plain_script=[])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购有哪些？", plan_enabled=True)
    assert plan_calls(chat) == [] and r.plan_steps == []
    assert r.rounds == 1 and r.answer == "知识库未包含相关内容。"


def test_plan_failure_degrades_to_no_plan() -> None:
    """规划输出垃圾 → []：无计划节、主流程照常（不阻塞回答）。"""
    chat = FakeChat(tools_script=[text_msg("直接回答。")],
                    plain_script=["抱歉我不会输出 JSON"])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(COMPOUND_Q, plan_enabled=True)
    assert r.plan_steps == []
    assert r.answer == "直接回答。" and r.rounds == 1


def test_plan_failure_exception_degrades_to_no_plan() -> None:
    """规划调用抛异常（脚本空）→ 降级无计划，主流程照常。"""
    chat = FakeChat(tools_script=[text_msg("直接回答。")], plain_script=[])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(COMPOUND_Q, plan_enabled=True)
    assert r.plan_steps == [] and r.answer == "直接回答。"


def test_clarify_gate_wins_over_plan() -> None:
    """澄清判据命中的问题不规划（先追问）：零规划调用、clarify 路径不变。"""
    chat = FakeChat(tools_script=[tool_msg("ask_clarification",
                                           {"question_for_user": "请补充地区与时间范围"})],
                    plain_script=[])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("中标金额总共多少？", plan_enabled=True)
    assert r.clarify is True
    assert plan_calls(chat) == [] and r.plan_steps == []
