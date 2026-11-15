"""C4 防泄漏：四类规则每类 1 正 1 反用例（docs/02 C4 测试清单要求）+ 兜底解析。"""

from __future__ import annotations

import pytest

from bidqa.agent import leaks

NAMES = ["search_knowledge", "search_database", "search_graph", "ask_clarification"]


# ── 四类规则（正例命中 / 反例不命中）──
def test_rule1_chinese_call() -> None:
    assert leaks.detect_leak("我先调用search_knowledge({'query':'空调'})查一下", NAMES) == "rule1_chinese_call"
    assert leaks.detect_leak("这个问题需要调用知识库才能回答", NAMES) is None


def test_rule2_json_structure() -> None:
    assert leaks.detect_leak('{"tool_calls": [{"name": "x"}]}', NAMES) == "rule2_json_structure"
    assert leaks.detect_leak('{"name": "search_database", "arguments": {}}', NAMES) == "rule2_json_structure"
    assert leaks.detect_leak('{"name": "张三", "amount": 100}', NAMES) is None  # 非工具名的 "name" 不算


def test_rule3_fence() -> None:
    leak = "结果如下：\n```tool\nsearch_graph(query_type='supplier_topn')\n```"
    assert leaks.detect_leak(leak, NAMES) == "rule3_fence"
    leak_json = "```json\n{\"tool\": \"search_graph\"}\n```"
    assert leaks.detect_leak(leak_json, NAMES) == "rule3_fence"
    clean = "```json\n{\"project\": \"某采购项目\", \"amount\": 660000}\n```"
    assert leaks.detect_leak(clean, NAMES) is None  # 栅栏内无已注册工具名


def test_rule4_tag() -> None:
    assert leaks.detect_leak("<tool_call>{'name':'x'}</tool_call>", NAMES) == "rule4_tag"
    assert leaks.detect_leak("工具调用已记录在案", NAMES) is None


def test_rule5_dsml() -> None:
    """2026-11-15 空回事故实测泄漏形态（DeepSeek 系 DSML 特殊 token）。"""
    leak = ('\n\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="search_database">\n'
            '<｜DSML｜parameter name="query_type" string="supplier_topn">')
    assert leaks.detect_leak(leak, NAMES) == "rule5_dsml"
    assert leaks.detect_leak(
        "空调采购项目共 12 个，中标总额 1,004,150 元。", NAMES) is None


def test_sanitize_strips_dsml() -> None:
    dirty = '结论如下\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="search_database">'
    clean = leaks.sanitize(dirty, NAMES)
    assert "DSML" not in clean
    assert "结论如下" in clean


def test_clean_text_passes_all_rules() -> None:
    assert leaks.detect_leak(
        "根据检索材料[1]，空调采购项目共 12 个，中标总额 660 万元。", NAMES) is None


# ── 兜底解析（C4：正文工具调用解析出真实执行）──
def test_parse_chinese_call() -> None:
    call = leaks.parse_inline_tool_call('调用search_knowledge({"query": "空调", "top_k": 3})')
    assert call is not None and call.name == "search_knowledge"
    assert call.arguments == {"query": "空调", "top_k": 3}


def test_parse_fenced_json() -> None:
    call = leaks.parse_inline_tool_call('```json\n{"name": "search_graph", "arguments": {"query_type": "supplier_topn"}}\n```')
    assert call is not None and call.name == "search_graph"
    assert call.arguments == {"query_type": "supplier_topn"}


def test_parse_tagged() -> None:
    call = leaks.parse_inline_tool_call('<tool_call>{"name": "echo", "arguments": {"text": "hi"}}</tool_call>')
    assert call is not None and call.name == "echo"


def test_parse_invalid_returns_none() -> None:
    assert leaks.parse_inline_tool_call("普通回答，没有工具调用") is None
    assert leaks.parse_inline_tool_call('调用search_knowledge(不是JSON)') is None


def test_sanitize_strips_patterns() -> None:
    dirty = "答案[1]如下\n调用search_knowledge({})\n<tool_call>x</tool_call>"
    clean = leaks.sanitize(dirty, NAMES)
    assert "search_knowledge" not in clean
    assert "tool_call" not in clean
    assert "答案[1]如下" in clean


@pytest.mark.parametrize("text", ["", "无内容"])
def test_sanitize_never_raises(text: str) -> None:
    assert isinstance(leaks.sanitize(text, NAMES), str)
