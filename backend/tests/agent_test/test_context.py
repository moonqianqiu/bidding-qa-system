"""C1 上下文构建 + C2 挂载条件（docs/02：每节注入条件可独立单测；挂载与提示词同源）。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from bidqa.agent.context import (
    CLARIFY_SECTION,
    HIT_CAP_SECTION,
    build_system_prompt,
    mount_tools,
)
from bidqa.tools.base import BaseTool, ToolRegistry
from bidqa.tools.knowledge import SearchKnowledgeTool
from bidqa.tools.clarify import AskClarificationTool


class EchoArgs(BaseModel):
    text: str = "x"


class EchoTool(BaseTool):
    name = "echo"
    description = "回显文本（测试用）"
    args_model = EchoArgs

    def run(self, args: BaseModel) -> dict:
        return {}


class _Args(BaseModel):
    x: int = 1


class _Stub(BaseTool):
    def __init__(self, name: str):
        self.name = name
        self.description = f"{name} 描述"
        self.args_model = _Args

    def run(self, args: BaseModel) -> dict:
        return {}


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(SearchKnowledgeTool(retriever=None))  # type: ignore[arg-type]
    reg.register(AskClarificationTool())
    reg.register(EchoTool())
    for name in ("search_database", "search_graph", "search_web"):
        reg.register(_Stub(name=name))
    return reg


def test_mount_base_set_unconditional() -> None:
    mounted = mount_tools(make_registry(), web_enabled=False, first_round=False)
    assert mounted.names() == ["search_knowledge", "search_database", "search_graph"]


def test_mount_web_only_when_enabled() -> None:
    off = mount_tools(make_registry(), web_enabled=False, first_round=False)
    on = mount_tools(make_registry(), web_enabled=True, first_round=False)
    assert "search_web" not in off.names() and "search_web" in on.names()


def test_mount_clarify_only_first_round() -> None:
    r1 = mount_tools(make_registry(), web_enabled=False, first_round=True)
    r2 = mount_tools(make_registry(), web_enabled=False, first_round=False)
    assert "ask_clarification" in r1.names() and "ask_clarification" not in r2.names()


def test_mount_skips_unregistered() -> None:
    reg = ToolRegistry()
    reg.register(EchoTool())  # 只有噪声工具
    mounted = mount_tools(reg, web_enabled=True, first_round=True)
    assert mounted.names() == []  # 未注册的基础/联网/澄清工具全部静默跳过


def test_clarify_rules_injected_only_first_round_unqualified() -> None:
    table = "- t"
    assert CLARIFY_SECTION in build_system_prompt(
        tool_table=table, first_round=True, qualified=False)
    assert CLARIFY_SECTION not in build_system_prompt(
        tool_table=table, first_round=True, qualified=True)
    assert CLARIFY_SECTION not in build_system_prompt(
        tool_table=table, first_round=False, qualified=False)


def test_hit_cap_constraint_appended() -> None:
    assert HIT_CAP_SECTION in build_system_prompt(
        tool_table="- t", first_round=False, qualified=True, hit_cap=True)
    assert HIT_CAP_SECTION not in build_system_prompt(
        tool_table="- t", first_round=False, qualified=True, hit_cap=False)


def test_skill_and_summary_hooks() -> None:
    p = build_system_prompt(tool_table="- t", first_round=True, qualified=True,
                            skill_text="标书分析技能正文", summary_text="此前讨论过空调采购")
    assert "[技能] 标书分析技能正文" in p
    assert "[前情提要] 此前讨论过空调采购" in p


def test_prompt_contains_tool_table_same_source() -> None:
    reg = make_registry()
    mounted = mount_tools(reg, web_enabled=False, first_round=True)
    p = build_system_prompt(tool_table=mounted.prompt_table(),
                            first_round=True, qualified=True)
    for name in ("search_knowledge", "search_database", "search_graph", "ask_clarification"):
        assert name in p
    assert "search_web" not in p  # 未挂载的工具不得出现在提示词（同源）


@pytest.mark.parametrize("section", ["[角色]", "[输出格式]"])
def test_always_on_sections(section: str) -> None:
    assert section in build_system_prompt(tool_table="- t", first_round=True, qualified=True)


# ── P1 英文回答：语言节仅在 lang=en 时注入 ──
def test_language_section_only_when_en() -> None:
    from bidqa.agent.context import LANGUAGE_EN_SECTION, build_system_prompt
    base = build_system_prompt(tool_table="(工具表)", first_round=True, qualified=True)
    assert LANGUAGE_EN_SECTION not in base            # zh（默认）行为不变
    en = build_system_prompt(tool_table="(工具表)", first_round=True,
                             qualified=True, lang="en")
    assert en.endswith(LANGUAGE_EN_SECTION)           # 语言节收尾
