"""C1 上下文构建 + C2 工具表组装（docs/02 C 域）。

C1 模板分节骨架（每节注入条件可独立单测）：
[角色][技能][前情提要][执行计划][澄清规则][工具约束][输出格式]
C2 挂载条件与提示词规则同源：基础集无条件；联网仅开关开；澄清仅首轮。
技能(C9)已建：点名/指代回看两层匹配，由 loop 组装 skill_text 注入；压缩摘要(C7)仍为延后钩子。
[执行计划]（WP5a）仅 Plan-and-Solve 规划成功时注入（agent/plan.py），无计划恒空。
"""

from __future__ import annotations

from bidqa.tools.base import ToolRegistry

BASE_TOOLS = ("search_knowledge", "search_database", "search_graph")
WEB_TOOLS = ("search_web",)  # D5 Exa 并入 search_web 第二供应商（供应商在 WEB_SEARCH_PROVIDERS 配置层解决）
CLARIFY_TOOL = "ask_clarification"


def mount_tools(base: ToolRegistry, *, web_enabled: bool, first_round: bool) -> ToolRegistry:
    """C2：按轮次挂载。未注册/未配置的工具静默跳过（能力降级语义）。"""
    names = [n for n in BASE_TOOLS if base.get(n)]
    if web_enabled:
        names += [n for n in WEB_TOOLS if base.get(n)]
    if first_round and base.get(CLARIFY_TOOL):
        names.append(CLARIFY_TOOL)
    sub = ToolRegistry()
    for name in names:
        sub.register(base.get(name))  # type: ignore[arg-type]
    return sub


ROLE_SECTION = (
    "[角色] 招投标领域问答助手：只依据取数结果作答；材料未覆盖→回答「知识库未包含」；"
    "涉及数字的结论必须标注来源或图表，不得编造。"
)
OUTPUT_SECTION = (
    "[输出格式] ①引用材料标注 [n]（n 为材料序号）②数据类结论可触发图表时给出图表要素"
    "（类型/标题含口径与时间窗/数据项）③禁止在正文中出现任何工具调用语法"
    "（调用X(…)/JSON tool_calls/```tool 代码栅栏/<tool_call> 标签一律不得出现）。"
)
CLARIFY_SECTION = (
    "[澄清规则] 判据：限定条件=地区/时间/标的物。三者全无的数字类问题（多少/排名/金额等）"
    "→ 调用 ask_clarification 向用户追问，不检索不回答；任一限定条件存在 → 直接作答。"
)
HIT_CAP_SECTION = (
    "已达工具轮次上限：禁止再调用任何工具，基于已有取数结果直接作答；"
    "材料不足处如实说明「知识库未包含」。"
)
# 英文界面 → 英文回答（P1）：仅 lang="en" 时注入；引用与来源标题保持原文
LANGUAGE_EN_SECTION = (
    "[语言] 回答正文必须使用英文（English）；引用标注 [n] 保留；"
    "来源材料的标题、条款与引文保持原文，不翻译专有名词。"
)


def skill_section(text: str | None) -> str:
    """[技能] 问题点名技能时注入（C9；无命中恒空，零额外往返）。"""
    return f"[技能] {text}" if text else ""


def plan_section(steps: list[str]) -> str:
    """[执行计划] Plan-and-Solve 计划节（WP5a）：有计划才注入，无计划/规划失败恒空。"""
    if not steps:
        return ""
    body = " ".join(f"{i}) {s}" for i, s in enumerate(steps, 1))
    return f"[执行计划] {body}"


def summary_section(text: str | None) -> str:
    """[前情提要] 历史压缩摘要（C7 滚动增量；有摘要即注入，无摘要/压缩未就绪时为空）。"""
    return f"[前情提要] {text}" if text else ""


def tool_section(table: str, *, hit_cap: bool) -> str:
    body = (
        "[工具约束] 可用工具（与实际挂载同源）：\n"
        f"{table}\n"
        "选择指引：①优先单个最对口工具，一轮内可并行多个互补工具；"
        "②拿到结果（包括「无匹配数据」）后若足以回答，立即作答，"
        "不要再用其他工具交叉验证或重复取数；③「无匹配数据」时先尝试换一次"
        "关键词或放宽过滤，仍无结果就如实告知用户数据未覆盖。"
    )
    return body + ("\n" + HIT_CAP_SECTION if hit_cap else "")


def build_system_prompt(*, tool_table: str, first_round: bool, qualified: bool,
                        skill_text: str | None = None, summary_text: str | None = None,
                        plan_steps: list[str] | None = None,
                        hit_cap: bool = False, lang: str = "zh") -> str:
    """C1 拼装。澄清规则仅 history 为空（首轮）时注入；触顶追加收口约束。
    plan_steps=[执行计划] 节（WP5a Plan-and-Solve；仅复合问题规划成功时非空）。"""
    sections = [ROLE_SECTION]
    if skill := skill_section(skill_text):
        sections.append(skill)
    if summ := summary_section(summary_text):
        sections.append(summ)
    if plan := plan_section(list(plan_steps or [])):
        sections.append(plan)
    if first_round and not qualified:
        sections.append(CLARIFY_SECTION)
    sections.append(tool_section(tool_table, hit_cap=hit_cap))
    sections.append(OUTPUT_SECTION)
    if lang == "en":
        sections.append(LANGUAGE_EN_SECTION)
    return "\n".join(s for s in sections if s)
