"""C4 防泄漏：泄漏判定规则集（docs/02 C4 四类）+ 正文工具调用兜底解析。

命中任一规则 → SSE reset 事件（前端清空已流正文）；兜底路径：正文中的工具调用
解析出真实执行（解析失败才走重生成/净化）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from bidqa.tools.runner import ToolCall


def build_rules(tool_names: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """四类规则（docs/02 C4）：每类在测试中 1 正 1 反用例锁定。"""
    names = "|".join(sorted(re.escape(n) for n in tool_names)) or r"(?!x)x"
    return [
        # ①中文调用语法：调用<工具名>(
        ("rule1_chinese_call", re.compile(rf"调用\s*(?:{names})\s*\(")),
        # ②tool_calls JSON 结构 或 "name": "<工具名>"（docs/02 C4 原文格式）
        ("rule2_json_structure", re.compile(rf'"tool_calls"\s*:|"name"\s*:\s*"(?:{names})"')),
        # ③```tool / ```json 代码栅栏内出现已注册工具名
        ("rule3_fence", re.compile(rf"```(?:tool|json)\s[^\x00]*?\b(?:{names})\b[^\x00]*?```")),
        # ④<tool_call> 类标签
        ("rule4_tag", re.compile(r"</?tool_call>|</?function_call>")),
        # ⑤DeepSeek 系 DSML 工具调用语法（2026-11-15 空回事故实测泄漏形态：
        #   <｜DSML｜tool_calls>/<｜DSML｜invoke name=…>/<｜DSML｜parameter …>，
        #   开闭标签均含 ｜DSML｜ 前缀；先具体后通用，只收实测形态）
        ("rule5_dsml", re.compile(r"<｜/?DSML｜|</｜?DSML")),
    ]


def detect_leak(text: str, tool_names: list[str]) -> str | None:
    """返回命中的规则 id；未泄漏返回 None。"""
    for rule_id, pattern in build_rules(tool_names):
        if pattern.search(text):
            return rule_id
    return None


def sanitize(text: str, tool_names: list[str]) -> str:
    """最后手段：剥掉泄漏模式（重生成仍泄漏时保底可用）。"""
    for _, pattern in build_rules(tool_names):
        text = pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_inline_tool_call(text: str) -> ToolCall | None:
    """文本兜底（C4）：从正文中解析出可真实执行的工具调用。

    支持：①调用<名>(JSON) ②```json {"name":…,"arguments":…}``` ③<tool_call>JSON</tool_call>
    解析不出（无调用/参数非法）→ None。
    """
    m = re.search(r"调用\s*([A-Za-z_]\w*)\s*\(\s*(\{.*?\})\s*\)", text, re.S)
    if m:
        try:
            return ToolCall(name=m.group(1), arguments=json.loads(m.group(2)))
        except json.JSONDecodeError:
            pass

    fenced = re.search(r"```(?:json|tool)\s*([^\x00]*?)```", text)
    if fenced:
        try:
            data = json.loads(fenced.group(1))
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                args = data.get("arguments")
                return ToolCall(name=data["name"],
                                arguments=args if isinstance(args, dict) else {})
        except json.JSONDecodeError:
            pass

    tagged = re.search(r"<tool_call>([^\x00]*?)</tool_call>", text)
    if tagged:
        try:
            data = json.loads(tagged.group(1))
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                args = data.get("arguments")
                return ToolCall(name=data["name"],
                                arguments=args if isinstance(args, dict) else {})
        except json.JSONDecodeError:
            pass
    return None
