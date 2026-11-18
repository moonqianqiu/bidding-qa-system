"""B7 组装层单测：工具表组装唯一实现（build_registry）+ done 契约字段。

审计 P0-2：ask_clarification 生产注册表从未注册（全仓只有测试 fixture 注册）
→ C5 动态追问/D6 分支/done.tool_name=ask_clarification 全是死逻辑——
评测 33/33=1.0 只验 result.clarify，把死逻辑掩盖了（恒绿断言 or True）。
"""

from __future__ import annotations

from typing import Any

from bidqa.agent.assembly import SCHEMA_V, build_done, build_registry
from bidqa.agent.loop import LoopResult


class FakeChat:
    def chat(self, messages, temperature: float = 0.3) -> str:
        return "ok"

    def chat_tools(self, messages, tools, temperature: float = 0.3) -> dict:
        return {}


class FakeRetriever:
    def retrieve(self, query: str, k: int = 4):  # pragma: no cover - 形状桩
        return []


def test_build_registry_registers_ask_clarification() -> None:
    """P0-2：生产注册表必须注册 ask_clarification（能力缺失=不注册对应工具的
    口径不变；澄清工具只依赖 chat——无 chat 时整条问答路径本就不可用）。"""
    reg = build_registry(FakeRetriever(), FakeChat(), None, None, {}, None)
    assert "ask_clarification" in reg.names()
    # 澄清工具不随知识库降级消失（它是首轮门控的组成部分）
    reg2 = build_registry(None, FakeChat(), None, None, {}, None)
    assert "search_knowledge" not in reg2.names()
    assert "ask_clarification" in reg2.names()


def test_build_done_clarify_round_keeps_tool_name() -> None:
    """docs/01 §3.4：澄清轮经工具追问时 tool_name=ask_clarification（原恒 ""
    ——与契约冲突）；来源/图表置零与 schema_v 不变。"""
    result = LoopResult(answer="请问需要哪个地区与时间范围？", clarify=True,
                        first_tool="ask_clarification", tool_called=True)
    done = build_done(result, elapsed_ms=5, trace_id="t1")
    assert done["tool_name"] == "ask_clarification"
    assert done["clarify"] is True
    assert done["sources"] == [] and done["charts"] == []
    assert done["tool_called"] is True  # 确实经过了工具调用
    assert done["schema_v"] == SCHEMA_V


def test_build_done_fixed_fallback_clarify_tool_name_empty() -> None:
    """澄清固定话术回退（模型未调工具）：tool_name 保持 ""——契约如实区分两条路径。"""
    result = LoopResult(answer="generic", clarify=True)
    done = build_done(result, elapsed_ms=5, trace_id="t2")
    assert done["tool_name"] == "" and done["tool_called"] is False


def test_build_done_plan_reflect_fields_contract() -> None:
    """WP5b（docs/01 §3.4 schema_v=2）：plan_steps/reflected 透传 LoopResult，
    深拷贝隔离（done 修改不回写 result）。"""
    result = LoopResult(answer="答", plan_steps=["查法规依据", "查中标金额"], reflected=True)
    done = build_done(result, elapsed_ms=5, trace_id="t3")
    assert done["schema_v"] == 2
    assert done["plan_steps"] == ["查法规依据", "查中标金额"]
    assert done["reflected"] is True
    done["plan_steps"].append("脏数据")
    assert result.plan_steps == ["查法规依据", "查中标金额"]  # 深拷贝隔离


def test_build_done_empty_plan_fields_not_omitted() -> None:
    """无计划/未反思：字段**在位**且为 []/False（空值不省略——前端按字段在位消费
    schema_v>=2，缺键会把「没规划」误判成「旧结构」）。"""
    done = build_done(LoopResult(answer="答"), elapsed_ms=1, trace_id="t4")
    assert done["schema_v"] == 2
    assert "plan_steps" in done and done["plan_steps"] == []
    assert "reflected" in done and done["reflected"] is False


def test_build_done_clarify_zeroes_plan_fields() -> None:
    """澄清轮：plan_steps/reflected 恒空——与 sources/charts 同一置零口径
    （澄清轮没有可展示的取数计划/补查轨迹）。"""
    result = LoopResult(answer="请问需要哪个地区？", clarify=True,
                        plan_steps=["查A"], reflected=True)
    done = build_done(result, elapsed_ms=3, trace_id="t5")
    assert done["clarify"] is True
    assert done["plan_steps"] == [] and done["reflected"] is False
