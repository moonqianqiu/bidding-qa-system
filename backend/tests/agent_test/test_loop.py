"""C3/C4/C5/C6 主循环（fake 模型脚本化；docs/02 C3 触顶/超时格式、C4 泄漏闸、C5 闸门）。"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from bidqa.agent.loop import EMPTY_ANSWER_TEXT_EN, MAX_ROUNDS, AgentLoop
from bidqa.tools.base import BaseTool, ToolRegistry, tool_result
from bidqa.tools.clarify import AskClarificationTool
from bidqa.tools.knowledge import SearchKnowledgeTool
from bidqa.tools.runner import ToolRunner


# ── fakes ──
class FakeChat:
    """脚本化模型：chat_tools 依次弹出脚本项；chat 单独脚本。"""

    def __init__(self, tools_script: list[dict] | None = None,
                 plain_script: list[str] | None = None,
                 thinking_script: list[tuple[str, str]] | None = None):
        self.tools_script = list(tools_script or [])
        self.plain_script = list(plain_script or [])
        self.thinking_script = list(thinking_script or [])
        self.calls: list[dict] = []

    def chat_tools(self, messages, tools, temperature: float = 0.3) -> dict:
        self.calls.append({"kind": "tools", "messages": messages, "tools": tools})
        return self.tools_script.pop(0)

    def chat(self, messages, temperature: float = 0.3) -> str:
        self.calls.append({"kind": "plain", "messages": messages})
        return self.plain_script.pop(0)

    def chat_stream_round(self, messages, tools, temperature: float = 0.3):
        """流式 FC 轮次：text 脚本拆两片 content；tool 脚本逐段产出。"""
        self.calls.append({"kind": "stream_round", "messages": messages, "tools": tools})
        item = self.tools_script.pop(0)
        if item.get("tool_calls"):
            for i, tc in enumerate(item["tool_calls"]):
                fn = tc["function"]
                yield ("tool", (i, tc["id"], fn["name"], fn["arguments"]))
        else:
            content = item.get("content") or ""
            half = len(content) // 2
            if half:
                yield ("content", content[:half])
            yield ("content", content[half:])

    def chat_stream(self, messages, temperature: float = 0.3):
        self.calls.append({"kind": "stream", "messages": messages})
        text = self.plain_script.pop(0)
        half = len(text) // 2
        if half:
            yield text[:half]
        yield text[half:]

    def chat_stream_thinking(self, messages, thinking_model, temperature: float = 0.3):
        self.calls.append({"kind": "thinking", "messages": messages,
                           "model": thinking_model})
        yield from self.thinking_script


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
                           sources=[{"id": "chunk-1", "title": f"{args.query}规定", "score": 0.9}])


class SlowTool(BaseTool[SearchArgs]):
    name = "slow_tool"
    description = "慢工具（fake）"
    args_model = SearchArgs

    def run(self, args: SearchArgs) -> dict:
        time.sleep(1.0)
        return tool_result(text="slow done")


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FakeSearchTool())
    reg.register(AskClarificationTool())
    return reg


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))

    def types(self) -> list[str]:
        return [k for k, _ in self.events]


# ── C3 基本路径 ──
def test_direct_answer_no_tools() -> None:
    chat = FakeChat(tools_script=[text_msg("知识库未包含相关内容。")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("招标公告在哪里查看？", on_event=rec)
    assert r.answer == "知识库未包含相关内容。"
    assert r.rounds == 1 and not r.tool_called and not r.hit_cap
    kinds = rec.types()
    assert kinds[0] == "status" and set(kinds) == {"status", "token"}  # status*→token*（契约主干）


def test_tool_round_then_answer() -> None:
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "单一来源"}),
        text_msg("依据材料[1]：可以采用单一来源。"),
    ])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("单一来源采购适用条件？")
    assert r.tool_called and r.first_tool == "search_knowledge"
    assert r.tool_trace == [[{"name": "search_knowledge", "arguments": {"query": "单一来源"}}]]
    assert r.sources and r.sources[0]["id"] == "chunk-1"
    assert r.rounds == 2
    # 第二次调用消息里应含 assistant(tool_calls) + tool 消息（取数结果回填对话）
    second = chat.calls[1]["messages"]
    assert any(m.get("tool_calls") for m in second)
    assert any(m["role"] == "tool" and "单一来源" in m["content"] for m in second)


def test_cap_forced_finish() -> None:
    script = [tool_msg("search_knowledge", {"query": f"q{i}"}, call_id=f"c{i}")
              for i in range(MAX_ROUNDS)]
    chat = FakeChat(tools_script=script, plain_script=["基于已有结果直接作答：材料见[1]。"])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("不限定的数字问题有多少？")  # 有澄清工具与判据不触发：问题含限定？——保证走普通路径
    assert r.hit_cap is True and r.rounds == MAX_ROUNDS
    assert r.answer.startswith("基于已有结果")
    # 强制收口调用是无 tools 的流式生成
    assert chat.calls[-1]["kind"] == "stream"


def test_timeout_result_backfilled_as_tool_message() -> None:
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "x"}, call_id="cslow"),
        text_msg("工具超时，据已有信息回答。"),
    ])
    reg = ToolRegistry()

    class SlowSearch(SlowTool):
        name = "search_knowledge"  # 执行期=挂载子集（P1-9 收口）：假名工具不在挂载表永不执行

    reg.register(SlowSearch())
    loop = AgentLoop(chat, reg, ToolRunner(batch_timeout=0.2))
    r = loop.run("空调采购有哪些？")
    assert r.degraded and any("timeout" in d for d in r.degraded)
    tool_messages = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"]
    assert '"error": "timeout"' in tool_messages[0]["content"].replace("：", ":") or \
        "timeout" in tool_messages[0]["content"]


# ── C5 澄清闸门 ──
def test_clarify_gate_triggers_deterministically() -> None:
    chat = FakeChat(tools_script=[tool_msg("ask_clarification",
                                           {"question_for_user": "请补充地区与时间范围"})])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("中标金额总共多少？", on_event=rec)
    assert r.clarify is True
    assert r.answer == "请补充地区与时间范围"
    # 动态追问经工具（P0-2 契约收口）：tool_called=True + 轨迹只含澄清工具
    # （「不检索不生成」的语义=不碰取数工具，原断言 tool_called=False 编码的是死逻辑行为）
    assert r.first_tool == "ask_clarification"
    assert all(c["name"] == "ask_clarification" for tr in r.tool_trace for c in tr)
    assert "向您确认" in [p.get("stage") for k, p in rec.events if k == "status"]
    assert r.phase_times.get("澄清提问") == 0


def test_clarify_fallback_when_model_ignores() -> None:
    """模型不配合（直接回答文本）→ 固定话术兜底（100% 触发保证）。"""
    chat = FakeChat(tools_script=[text_msg("我觉得是 42。")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("中标金额总共多少？")
    assert r.clarify is True
    assert "请补充" in r.answer


def test_clarify_not_triggered_with_qualifier() -> None:
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "空调"}),
        text_msg("依据[1]回答。"),
    ])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购的主要供应商有哪些？")
    assert r.clarify is False and r.tool_called is True


# ── C4 泄漏闸 ──
def test_leak_reset_and_regenerate() -> None:
    chat = FakeChat(tools_script=[text_msg("先调用search_knowledge({'query':'x'})再回答")],
                    plain_script=["依据材料[1]，答案是 A。"])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("空调采购有哪些？", on_event=rec)
    assert r.answer == "依据材料[1]，答案是 A。"
    assert "reset" in rec.types()
    reset_idx = rec.types().index("reset")
    assert "status" in rec.types()[reset_idx + 1]  # reset 之后必现 status（契约）


def test_leak_inline_call_executed_and_loop_continues() -> None:
    chat = FakeChat(tools_script=[
        text_msg("让我调用search_knowledge({\"query\": \"空调\"})查证"),
        text_msg("依据[1]：空调采购共 12 个项目。"),
    ])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("空调采购有哪些？", on_event=rec)
    assert r.answer.startswith("依据[1]")
    assert r.tool_called is True and r.sources  # 兜底执行真实发生


def test_leak_sanitized_when_regeneration_still_leaks() -> None:
    chat = FakeChat(tools_script=[text_msg('{"tool_calls": [{"name": "x"}]}')],
                    plain_script=['{"tool_calls": [{"name": "search_knowledge"}]} 答案'])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购有哪些？")
    assert "tool_calls" not in r.answer
    assert any(d == "leak_sanitized" for d in r.degraded)


# ── C6 深度思考 ──
def test_thinking_stream_events_ordered() -> None:
    chat = FakeChat(tools_script=[text_msg("草稿：答案是 A")],
                    thinking_script=[("thinking", "思考：需要核对材料"),
                                     ("content", "依据材料[1]，答案是 A。")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("空调采购有哪些？", thinking=True, thinking_model="m-think", on_event=rec)
    assert r.answer == "依据材料[1]，答案是 A。"
    kinds = rec.types()
    assert kinds.index("thinking") < kinds.index("token")  # 契约：thinking* 先于 token*
    assert ("thinking", {"text": "思考：需要核对材料"}) in rec.events


def test_thinking_body_fallback_when_content_empty() -> None:
    chat = FakeChat(tools_script=[text_msg("草稿")],
                    thinking_script=[("thinking", "只有思考流")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购有哪些？", thinking=True, thinking_model="m-think")
    assert r.answer == "只有思考流"
    assert any(d == "thinking_as_body" for d in r.degraded)


def test_empty_model_output_gets_honest_fallback() -> None:
    """模型返回空正文且无工具调用：不给空气泡（2026-11-02 现场复现，耗时 100s+ 后正文为空）。"""
    chat = FakeChat(tools_script=[text_msg("")], plain_script=["", ""])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("安徽省各市有多少项目？", on_event=rec)
    assert r.answer.strip()                     # 有可见文案
    assert "empty_answer" in r.degraded         # 降级原因如实记录
    assert "token" in rec.types()               # 文案真的下发到前端


def test_empty_final_round_retries_once_and_recovers() -> None:
    """工具轮之后的最终生成轮空正文（2026-11-10 trace：图表正常、正文空）：
    服务端自动补发一次无工具直答 —— 与前端提示「重试一次」一致，只是自动做。"""
    chat = FakeChat(tools_script=[
        tool_msg("search_knowledge", {"query": "空调"}),
        text_msg(""),  # 最终生成轮空正文
    ], plain_script=["依据材料[1]：空调供应商共 3 家。"])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    rec = Recorder()
    r = loop.run("空调供应商有哪些？", on_event=rec)
    assert r.answer == "依据材料[1]：空调供应商共 3 家。"
    assert "empty_answer_retry" in r.degraded   # 补发发生 = 降级信号如实记录
    assert "empty_answer" not in r.degraded     # 诚实文案未启用
    assert r.sources                            # 工具轮取数结果不丢
    retry_call = chat.calls[-1]
    assert retry_call["kind"] == "stream"       # 补发走无工具直答
    assert any(m["role"] == "tool" for m in retry_call["messages"])  # 带着取数结果补发


def test_empty_final_round_retry_leak_sanitized() -> None:
    """补发文本同样过 C4 泄漏闸：仍泄漏 → 净化保底（剥泄漏模式，与既有 sanitize 契约一致）。"""
    chat = FakeChat(tools_script=[text_msg("")],
                    plain_script=["调用search_knowledge({'query':'x'})后作答：答案是 A"])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购有哪些？")
    assert "调用search_knowledge(" not in r.answer
    assert any(d == "leak_sanitized" for d in r.degraded)


def test_empty_final_round_retry_still_empty_falls_back() -> None:
    """补发仍空 → 诚实文案兜底（原行为）。"""
    chat = FakeChat(tools_script=[text_msg("")], plain_script=["", ""])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调供应商有哪些？")
    assert r.answer == EMPTY_ANSWER_TEXT_EN or "本次没有拿到模型回答" in r.answer
    assert "empty_answer" in r.degraded


# ── P1 英文回答（lang 参数全链路）──
def test_english_lang_injects_language_section() -> None:
    """lang=en → system prompt 带语言节；zh（默认）不带（行为不变）。"""
    chat = FakeChat(tools_script=[text_msg("Answer [1].")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    loop.run("空调采购项目有哪些？", lang="en")  # 有标的物限定 → 直接作答
    assert "[语言]" in chat.calls[0]["messages"][0]["content"]

    chat2 = FakeChat(tools_script=[text_msg("中文回答 [1]。")])
    loop2 = AgentLoop(chat2, make_registry(), ToolRunner())
    loop2.run("空调采购项目有哪些？")
    assert "[语言]" not in chat2.calls[0]["messages"][0]["content"]


def test_english_clarify_question_when_lang_en() -> None:
    """lang=en + 无限定数字题 → 澄清话术英文（模型不理会时兜底话术也要英文）。"""
    chat = FakeChat(tools_script=[{}])  # 模型不调 ask_clarification → 兜底
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("有多少个中标项目？", lang="en")
    assert r.clarify is True
    assert r.answer.startswith("To answer accurately")
    # 话术来自兜底路径：澄清工具确实被递给了模型（原恒绿 `or True` 掩盖 P0-2）
    assert any("ask_clarification" in str(c.get("tools"))
               for c in chat.calls if c["kind"] == "tools")


def test_english_empty_answer_fallback() -> None:
    """lang=en + 模型空正文 → 空答案兜底文案英文。"""
    chat = FakeChat(tools_script=[text_msg("")])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("空调采购项目有哪些？", lang="en")
    assert r.answer == EMPTY_ANSWER_TEXT_EN


# ── 断流即收（C3 清债 #1：2026-11-11 演示日缺陷——流中异常丢弃已流正文+重放全文）──
def test_stream_plain_keeps_partial_on_break() -> None:
    """流中异常：保留已流正文（恰一次下发，不重放、不追加兜底）。"""
    from bidqa.agent.loop import AgentLoop as _Loop

    class BreakStreamChat(FakeChat):
        def chat_stream(self, messages, temperature: float = 0.3):
            yield "已流出的一半。"
            raise RuntimeError("connection reset")

    events: list[tuple[str, dict]] = []
    loop = _Loop(BreakStreamChat(), ToolRegistry(), ToolRunner(ToolRegistry()))
    text = loop._stream_plain([{"role": "user", "content": "q"}],
                              lambda k, p: events.append((k, p)))
    assert text == "已流出的一半。"
    tokens = [payload["text"] for kind, payload in events if kind == "token"]
    assert tokens == ["已流出的一半。"]  # 恰一次，无全文重放


def test_stream_plain_empty_stream_falls_back_once() -> None:
    """空流（零 token 即断）：回退非流式一次并下发（原语义保留）。"""
    from bidqa.agent.loop import AgentLoop as _Loop

    class EmptyThenFallbackChat(FakeChat):
        def chat_stream(self, messages, temperature: float = 0.3):
            raise RuntimeError("upstream empty before first byte")
        def chat(self, messages, temperature: float = 0.3):
            return "非流式兜底回答。"

    events: list[tuple[str, dict]] = []
    loop = _Loop(EmptyThenFallbackChat(), ToolRegistry(), ToolRunner(ToolRegistry()))
    text = loop._stream_plain([{"role": "user", "content": "q"}],
                              lambda k, p: events.append((k, p)))
    assert text == "非流式兜底回答。"
    assert [payload["text"] for kind, payload in events if kind == "token"] == ["非流式兜底回答。"]


# ── 泄漏恢复的挂载口径（Round 4 既有债）：恢复路径 = 会话挂载子集，不是全库注册表 ──
def test_leak_recovery_respects_mounted_subset() -> None:
    """web_enabled=False（search_web 已注册但未挂载）时：内联 search_web 泄漏
    绝不执行（回落重生成）；挂载工具的恢复续写 tools 也不含未挂载工具。"""
    ran: list[str] = []

    class WebTool(BaseTool[SearchArgs]):
        name = "search_web"
        description = "联网检索（fake）"
        args_model = SearchArgs

        def run(self, args: SearchArgs) -> dict:
            ran.append(args.query)
            return tool_result(text=f"[1] 网页内容：{args.query}")

    reg = make_registry()
    reg.register(WebTool())
    runner = ToolRunner()

    # 未挂载工具的内联泄漏：不执行，走重生成
    chat = FakeChat(tools_script=[
        text_msg("让我调用search_web({\"query\": \"公告\"})查证"),
    ], plain_script=["依据材料[1]：公告已发布。"])
    loop = AgentLoop(chat, reg, runner)
    r = loop.run("空调采购有哪些？", web_enabled=False)
    assert ran == []  # 未挂载工具绝不执行
    assert r.answer == "依据材料[1]：公告已发布。"

    # 对照：挂载工具的内联泄漏照旧兜底执行；续写轮 tools 不含 search_web
    chat2 = FakeChat(tools_script=[
        text_msg("让我调用search_knowledge({\"query\": \"空调\"})查证"),
        text_msg("依据[1]：空调采购共 12 个项目。"),
    ])
    loop2 = AgentLoop(chat2, reg, runner)
    r2 = loop2.run("空调采购有哪些？", web_enabled=False)
    assert r2.tool_called is True and r2.sources
    recoveries = [c for c in chat2.calls if c["kind"] == "tools"]
    assert recoveries  # 恢复续写走 chat_tools
    names = [t["function"]["name"] for t in recoveries[0]["tools"]]
    assert "search_web" not in names  # 续写口径 = 挂载子集
    assert "search_knowledge" in names


def test_clarify_via_tool_records_contract_fields() -> None:
    """C5/D6 动态追问（审计 P0-2）：模型经 ask_clarification 工具追问 → 动态话术
    + 契约字段落位（first_tool/tool_trace——done.tool_name 由此可对账）。原实现
    澄清工具生产未注册，此路径不可达，固定话术 100% 兜底。"""
    chat = FakeChat(tools_script=[tool_msg("ask_clarification",
                                           {"question_for_user": "请问需要哪个地区与时间范围？"})])
    loop = AgentLoop(chat, make_registry(), ToolRunner())
    r = loop.run("中标金额总共多少？")  # 无限定数字题 → C5 澄清闸门
    assert r.clarify is True
    assert r.answer == "请问需要哪个地区与时间范围？"
    assert r.first_tool == "ask_clarification"
    assert r.tool_trace and r.tool_trace[0][0]["name"] == "ask_clarification"


def test_hallucinated_unmounted_tool_not_executed_via_fc() -> None:
    """P1-9（审计）：web_enabled=False 时模型经 FC 幻觉点名 search_web（全量
    注册表里有）——执行期按挂载子集回 unknown_tool，绝不真执行。与内联泄漏
    （上一用例）互为两条触发面：模型 schemas 里没有的工具不该被批处理执行。"""
    ran: list[str] = []

    class WebTool(BaseTool[SearchArgs]):
        name = "search_web"
        description = "联网检索（fake）"
        args_model = SearchArgs

        def run(self, args: SearchArgs) -> dict:
            ran.append(args.query)
            return tool_result(text=f"[1] 网页内容：{args.query}")

    reg = make_registry()
    reg.register(WebTool())
    chat = FakeChat(tools_script=[
        tool_msg("search_web", {"query": "公告"}),
        text_msg("材料不可用，依据已有知识回答。"),
    ])
    loop = AgentLoop(chat, reg, ToolRunner())
    r = loop.run("空调采购有哪些？", web_enabled=False)
    assert ran == []  # 未挂载幻觉点名绝不执行（修复前按全量注册表会真跑）
    assert r.answer == "材料不可用，依据已有知识回答。"
