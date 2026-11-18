"""WP5a Plan-and-Solve 复合问题规划（docs/02 C3 行为补充）。

触发：should_plan 启发式（确定性、零模型调用）命中复合问题 → 取数前**一次**
规划调用（主模型槽位 chat 参数，绝不碰 JUDGE_*）；计划以 [执行计划] 节并入
system prompt 驱动多轮取数。简单问题/开关关闭=零额外调用。
失败降级：规划调用失败或解析不出 → 返回 []（等同无计划，绝不阻塞回答）。
解析抗代码围栏与前后噪声；步骤 ≤3、每条 ≤80 字。
单进程约定（docs/01 §6）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from bidqa.agent.clarify import is_underqualified

MAX_STEPS = 3
STEP_CHAR_LIMIT = 80
MIN_QUESTION_CHARS = 8    # 过短问题不规划（含澄清补充的短语）
FOLLOWUP_MAX_CHARS = 16   # 有历史时，短追问（指代性补充）不规划
PLAN_LENGTH_CHARS = 40    # 长复合问题直接规划

_QUESTION_MARK_RE = re.compile(r"[？？?]")
# 协调词（spec 口径）：分别/对比/并且/以及/同时/分别列出（「分别」已覆盖）
_COORD_RE = re.compile(r"分别|对比|并且|以及|同时")

_PLAN_SYSTEM = (
    "你是招投标问答的取数规划器。针对【问题】给出 1-3 步取数计划：每步一个动作、"
    "不超过 80 字，写明查什么数据（知识库/业务库/图谱/联网维度）与先后顺序；"
    "不写答案、不复述问题。只输出 JSON：{\"steps\": [\"第一步\", \"第二步\"]}。"
)


def should_plan(question: str, *, has_image: bool = False,
                history_turns: int = 0) -> bool:
    """True=建议出计划。启发式（确定性、无模型调用）：

    - 澄清判据命中（无限定数字题）→ False：应先追问不规划（澄清闸门优先）；
    - 过短问题（< 8 字）→ False；有历史时短追问（< 16 字）→ False（指代性补充
      靠历史上下文作答，规划反而引入噪声）；
    - 复合信号（任一命中即规划）：≥2 个问句 / 协调词（分别/对比/并且/以及/同时）/
      长度 ≥ 40 字 / 顿号列举多实体的中长问题（≥20 字）。
    has_image 不改变判定（视觉走独立槽位，计划只管取数）。
    """
    q = (question or "").strip()
    if not q or is_underqualified(q):
        return False
    if len(q) < MIN_QUESTION_CHARS:
        return False
    if history_turns > 0 and len(q) < FOLLOWUP_MAX_CHARS:
        return False
    if len(q) >= PLAN_LENGTH_CHARS:
        return True
    if len(_QUESTION_MARK_RE.findall(q)) >= 2:
        return True
    if _COORD_RE.search(q):
        return True
    return q.count("、") >= 1 and len(q) >= 20


def build_plan(chat: Any, question: str, context_hint: str = "") -> list[str]:
    """规划调用（主模型槽位）。返回步骤列表（≤3 条、每条 ≤80 字）；
    任何失败/解析不出 → []（等同无计划，调用方零分支）。"""
    user = f"【问题】{question}"
    if context_hint:
        user += f"\n【补充】{context_hint}"
    try:
        raw = str(chat.chat([
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": user},
        ], temperature=0.1))
    except Exception:
        return []
    return parse_plan_steps(raw)


def parse_plan_steps(raw: str) -> list[str]:
    """解析规划输出：JSON {"steps": [...]}；步骤截到 3 条、每条截到 80 字。"""
    obj = extract_json_object(raw)
    if obj is None:
        return []
    steps = obj.get("steps")
    if not isinstance(steps, list):
        return []
    out: list[str] = []
    for item in steps:
        text = str(item).strip()
        if text:
            out.append(text[:STEP_CHAR_LIMIT])
        if len(out) >= MAX_STEPS:
            break
    return out


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """抗代码围栏/前后噪声：取首个 { 到最后一个 } 的片段解析。
    解析失败或非对象 → None（plan/reflect 共用单入口）。"""
    text = (raw or "").strip()
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
