"""WP5a Reflection 证据自检（agent/reflect.py）：工具取数后、最终作答前自检
「证据是否足以回答」，不足且有剩余轮次 → 注入提示补查（恰 1 次）；
关闭/无工具/hit_cap → 零额外调用；need_more=false → 不补查。

FakeChat 脚本化风格与 test_loop 一致：plain 脚本供反思调用（chat）。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from bidqa.agent.loop import MAX_ROUNDS, AgentLoop
from bidqa.agent.reflect import (
    HINT_CHAR_LIMIT,
    REFLECT_SECTION_PREFIX,
    reflect_on_evidence,
    summarize_observations,
)
from bidqa.tools.base import BaseTool, ToolRegistry, tool_result
from bidqa.tools.clarify import AskClarificationTool
from bidqa.tools.runner import ToolCall, ToolOutcome, ToolRunner


# ── fakes ──
class FakeChat:
    """脚本化模型：chat（反思）与 chat_stream_round（主循环）分离计数。"""

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
        yield self.plain_script.pop(0)

    def chat_tools(self, messages, tools, temperature: float = 0.3) -> dict:
        self.calls.append({"kind": "tools", "messages": messages, "tools": tools})
        return self.tools_script.pop(0)


class ReflectChat:
    """单脚本 chat 桩（只被 reflect_on_evidence 调用）。"""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, temperature: float = 0.3) -> str:
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


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


def plain_calls(chat: FakeChat) -> list[dict]:
    return [c for c in chat.calls if c["kind"] == "plain"]


def stream_rounds(chat: FakeChat) -> list[dict]:
    return [c for c in chat.calls if c["kind"] == "stream_round"]


# ── reflect_on_evidence 单测 ──
def test_reflect_need_more_true() -> None:
    chat = ReflectChat('{"need_more": true, "hint": "缺少2024年各地市金额"}')
    need, hint = reflect_on_evidence(chat, "问题", "- search_knowledge: 空结果")
    assert need is True and hint == "缺少2024年各地市金额"
    user = chat.calls[0][-1]["content"]
    assert "【问题】问题" in user and "【取数摘要】" in user  # 问题+结构化摘要


def test_reflect_no_need() -> None:
    need, hint = reflect_on_evidence(
        ReflectChat('{"need_more": false, "hint": ""}'), "问题", "摘要")
    assert need is False and hint == ""


def test_reflect_parses_code_fence() -> None:
    need, hint = reflect_on_evidence(
        ReflectChat('```json\n{"need_more": true, "hint": "还缺时间维度"}\n```'),
        "问题", "摘要")
    assert need is True and hint == "还缺时间维度"


def test_reflect_garbage_or_failure_returns_false() -> None:
    """解析失败 / 调用异常 → (False, "")：绝不阻塞回答。"""
    for reply in ("我不知道", '{"steps": [1]}', ""):
        assert reflect_on_evidence(ReflectChat(reply), "问题", "摘要") == (False, "")
    assert reflect_on_evidence(ReflectChat(RuntimeError("boom")), "问题", "摘要") == (False, "")


def test_reflect_hint_clipped() -> None:
    long_hint = "缺" * (HINT_CHAR_LIMIT + 50)
    need, hint = reflect_on_evidence(
        ReflectChat(json.dumps({"need_more": True, "hint": long_hint}, ensure_ascii=False)),
        "问题", "摘要")
    assert need is True and len(hint) == HINT_CHAR_LIMIT  # ≤120 字


def test_reflect_summary_carries_no_material_text() -> None:
    """隐私：摘要只含工具名/空结果/计数，材料正文不进反思上下文。"""
    outcomes = [ToolOutcome(call=ToolCall(name="search_knowledge", arguments={}),
                            payload={"text": "机密材料正文内容", "sources": [{"id": "a"}],
                                     "charts": [], "empty": False},
                            ok=True, elapsed_ms=1)]
    summary = summarize_observations(outcomes)
    assert "search_knowledge" in summary and "机密材料正文" not in summary
    chat = ReflectChat('{"need_more": false}')
    reflect_on_evidence(chat, "问题", summary)
    assert "机密材料正文" not in chat.calls[0][-1]["content"]


# ── summarize_observations（结构化摘要）──
def _outcome(name: str, payload: dict, ok: bool = True) -> ToolOutcome:
    return ToolOutcome(call=ToolCall(name=name, arguments={}), payload=payload,
                       ok=ok, elapsed_ms=1)


def test_summarize_counts_and_empty() -> None:
    outcomes = [
        _outcome("search_knowledge", {"text": "t", "sources": [{}, {}, {}],
                                      "charts": [], "empty": False}),
        _outcome("search_database", {"text": "无匹配数据", "sources": [], "charts": [],
                                     "empty": True}),
        _outcome("search_graph", {"error": "timeout", "tool": "search_graph"}, ok=False),
        _outcome("search_web", {"text": "t", "sources": [{}], "charts": [{}],
                                "empty": False}),
    ]
    summary = summarize_observations(outcomes)
    assert "search_knowledge: 来源 3 条" in summary
    assert "search_database: 空结果" in summary
    assert "search_graph: 失败(timeout)" in summary
    assert "search_web" in summary and "图表 1 张" in summary


def test_summarize_no_outcomes() -> None:
    assert summarize_observations([]) == "（无取数）"


# ── 循环集成 ──
Q = "空调采购有哪些？"


def test_need_more_triggers_extra_tool_round() -> None:
    """need_more=true 且有余轮 → 补查轮发生（轮次+1）、reflected=True、恰 1 次反思。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}, call_id="c1"),
            tool_msg("search_knowledge", {"query": "空调 维保"}, call_id="c2"),
            text_msg("依据[1][2]回答。"),
        ],
        plain_script=['{"need_more": true, "hint": "缺少维保响应时限"}'],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert r.reflected is True
    assert r.rounds == 3                                # 2 轮工具 + 最终作答（补查轮真实发生）
    assert len(r.tool_trace) == 2                       # 第 2 轮=补查
    assert len(plain_calls(chat)) == 1                  # 反思恰一次（第 2 轮不再反思）
    rounds = stream_rounds(chat)
    # FakeChat 记录同一 messages 列表引用 → 按「存在性」断言注入（末项会随轮次增长）
    hints = [m for m in rounds[1]["messages"] if m.get("role") == "system"
             and REFLECT_SECTION_PREFIX in str(m.get("content"))]
    assert len(hints) == 1 and "缺少维保响应时限" in hints[0]["content"]


def test_no_need_no_extra_round() -> None:
    """need_more=false → 不补查：行为与现状一致（reflected=False）。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=['{"need_more": false, "hint": ""}'],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert r.reflected is False and r.rounds == 2 and r.answer == "依据[1]回答。"
    assert len(plain_calls(chat)) == 1                  # 自检调用发生，只是判定证据足够


def test_reflect_no_need_checks_only_once_across_rounds() -> None:
    """need_more=false 也不得每轮重复自检：一次问答恰 1 次（成本红线）。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "a"}, call_id="c1"),
            tool_msg("search_knowledge", {"query": "b"}, call_id="c2"),
            text_msg("依据[1][2]回答。"),
        ],
        plain_script=['{"need_more": false, "hint": ""}'],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert r.reflected is False and r.rounds == 3
    assert len(plain_calls(chat)) == 1                  # 两轮工具也只自检一次


def test_reflect_disabled_zero_calls() -> None:
    """开关关闭：零额外调用，行为与现状逐字一致。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=[],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=False)
    assert plain_calls(chat) == [] and r.reflected is False and r.rounds == 2


def test_no_tools_no_reflect() -> None:
    """无工具轮（首轮直答）→ 零反思调用。"""
    chat = FakeChat(tools_script=[text_msg("知识库未包含相关内容。")], plain_script=[])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert plain_calls(chat) == [] and r.reflected is False and r.rounds == 1


def test_hit_cap_round_never_reflects() -> None:
    """触顶轮（rounds=MAX_ROUNDS）不反思——hit_cap 无剩余轮次可补查；
    且一次问答只自检一次（首轮工具后），不随轮次递增。"""
    script = [tool_msg("search_knowledge", {"query": f"q{i}"}, call_id=f"c{i}")
              for i in range(MAX_ROUNDS)]
    chat = FakeChat(
        tools_script=script,
        plain_script=['{"need_more": false, "hint": ""}',
                      "基于已有结果直接作答：材料见[1]。"],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert r.hit_cap is True and r.rounds == MAX_ROUNDS
    assert len(plain_calls(chat)) == 1                   # 恰 1 次自检（非每轮一次）
    assert chat.calls[-1]["kind"] == "stream"           # 收口走无工具流式生成


def test_reflect_failure_degrades_silently() -> None:
    """反思调用抛异常（脚本空）→ 不补查、主流程照常。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=[],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q, reflect_enabled=True)
    assert r.reflected is False and r.answer == "依据[1]回答。" and r.rounds == 2


def test_reflect_and_plan_combined() -> None:
    """两者同开：先规划（取数前）后反思（取数后）——调用序=计划→主循环→反思。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=['{"steps": ["查空调采购项目"]}', '{"need_more": false, "hint": ""}'],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("对比合肥市与芜湖市的空调采购金额", plan_enabled=True, reflect_enabled=True)
    assert r.plan_steps == ["查空调采购项目"]
    assert r.reflected is False and r.rounds == 2
    kinds = [c["kind"] for c in chat.calls]
    assert kinds == ["plain", "stream_round", "plain", "stream_round"]  # 计划→轮1→反思→轮2
    assert "对比合肥市与芜湖市的空调采购金额" in chat.calls[0]["messages"][-1]["content"]
    assert chat.calls[2]["messages"][-1]["content"].startswith("【问题】")  # 反思上下文


@pytest.mark.parametrize("flag", ["plan_enabled", "reflect_enabled"])
def test_defaults_are_off(flag: str) -> None:
    """run() 默认双关（零额外调用）——生产由 api 层经 Settings 注入。"""
    chat = FakeChat(
        tools_script=[
            tool_msg("search_knowledge", {"query": "空调"}),
            text_msg("依据[1]回答。"),
        ],
        plain_script=[],
    )
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run(Q)
    assert plain_calls(chat) == []
    assert r.plan_steps == [] and r.reflected is False
