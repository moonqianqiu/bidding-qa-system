"""WP5a Reflection 证据自检（docs/02 C3 行为补充）。

触发：本轮至少执行过 1 个工具、尚未 hit_cap、且本轮（一次问答）未反思过 →
恰 1 次自检调用（主模型槽位 chat 参数，绝不碰 JUDGE_*）；
need_more=true 且还有剩余轮次 → 注入 [证据自检] 提示继续补查（最多 1 次反思）；
**不做答案后重写**（reset 流不涉及）。关闭/异常/无工具=零调用。
隐私白名单：自检上下文只带问题原文 + 结构化取数摘要（工具名/是否空结果/关键
计数），材料正文/来源明细绝不进入。
单进程约定（docs/01 §6）。
"""

from __future__ import annotations

from typing import Any

from bidqa.agent.plan import extract_json_object

HINT_CHAR_LIMIT = 120
REFLECT_SECTION_PREFIX = "[证据自检]"

_REFLECT_SYSTEM = (
    "你是招投标问答的证据自检器。根据【问题】与【取数摘要】判断现有证据是否足以"
    "完整回答问题。只输出 JSON：{\"need_more\": true, \"hint\": \"还缺什么、查什么\"} "
    "或 {\"need_more\": false, \"hint\": \"\"}。证据足够时一律 need_more=false；"
    "hint 不超过 120 字，只写缺失项与建议取数动作，不写答案。"
)

REFLECT_HINT_SECTION = (
    REFLECT_SECTION_PREFIX + " 现有取数可能不足以完整回答问题，还缺：{hint}。"
    "如需补查请立即调用工具补齐；无法补齐处作答时如实说明「知识库未包含」。"
)


def reflect_on_evidence(chat: Any, question: str,
                        observations_summary: str) -> tuple[bool, str]:
    """证据自检调用（主模型槽位）。返回 (need_more, hint)；
    调用失败/解析不出 → (False, "")（等同证据足够，不补查、不阻塞）。"""
    try:
        raw = str(chat.chat([
            {"role": "system", "content": _REFLECT_SYSTEM},
            {"role": "user", "content":
                f"【问题】{question}\n【取数摘要】{observations_summary}"},
        ], temperature=0.1))
    except Exception:
        return False, ""
    return parse_verdict(raw)


def parse_verdict(raw: str) -> tuple[bool, str]:
    """解析自检输出：JSON {"need_more": bool, "hint": str}；hint 截到 120 字。"""
    obj = extract_json_object(raw)
    if obj is None:
        return False, ""
    hint = str(obj.get("hint") or "").strip()[:HINT_CHAR_LIMIT]
    return bool(obj.get("need_more")), hint


def summarize_observations(outcomes: list[Any]) -> str:
    """结构化取数摘要（工具名/是否空结果/关键计数）——只描述元数据，
    不带材料正文（隐私白名单，自检上下文最小化）。"""
    lines: list[str] = []
    for out in outcomes:
        name = str(getattr(getattr(out, "call", None), "name", "") or "unknown")
        payload = getattr(out, "payload", None) or {}
        if not getattr(out, "ok", False):
            lines.append(f"- {name}: 失败({payload.get('error')})")
            continue
        if payload.get("empty"):
            lines.append(f"- {name}: 空结果")
            continue
        counts: list[str] = []
        if sources := len(payload.get("sources") or []):
            counts.append(f"来源 {sources} 条")
        if charts := len(payload.get("charts") or []):
            counts.append(f"图表 {charts} 张")
        lines.append(f"- {name}: " + ("；".join(counts) if counts else "有结果"))
    return "\n".join(lines) if lines else "（无取数）"
