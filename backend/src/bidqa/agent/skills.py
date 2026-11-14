"""C9 技能注入（docs/02:94 首批 bid-analysis/debug-answer；懒加载指令，零额外往返）。

技能 = `agent/skills/*.md` 里的指令文件（首行 `# 名称`，`描述：`/`别名：` 头部行，
其余为正文）。两层匹配：①问题点名技能（名称/别名子串命中，大小写不敏感）
②指代词（该技能/这个技能/上述技能）回看最近一条助手回复提到的技能
（参考项目三层匹配的第二层，2026-11-08 建）。命中 → 正文以【技能指令: name】注入
system prompt 的 [技能] 节（build_system_prompt 现成钩子）；运行期技能清单
进程内缓存（新增技能文件需重启后端生效）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_HEADER_RE = re.compile(r"^(描述|别名)\s*[：:]\s*(.*)$")
_REFERENTS = ("该技能", "这个技能", "上述技能")  # 二层触发词（参考项目原文措辞）


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    aliases: tuple[str, ...]
    body: str


def _parse_skill(text: str) -> Skill | None:
    """解析单个技能文件；缺名称或缺正文 → None（跳过，不抛错）。"""
    name = desc = ""
    aliases: list[str] = []
    body: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and not name:
            name = line[2:].strip()
            continue
        m = _HEADER_RE.match(line)
        if m:
            if m.group(1) == "描述":
                desc = m.group(2).strip()
            else:
                aliases = [a.strip() for a in re.split("[,，]", m.group(2)) if a.strip()]
            continue
        body.append(line)
    if not name or not body:
        return None
    return Skill(name=name, description=desc, aliases=tuple(aliases), body="\n".join(body))


def load_skills(directory: Path) -> list[Skill]:
    """加载目录下全部技能（文件序稳定）；目录缺失/不可读 → 空清单（诚实降级）。"""
    if not directory.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(directory.glob("*.md")):
        try:
            skill = _parse_skill(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if skill is not None:
            skills.append(skill)
    return skills


@lru_cache(maxsize=1)
def get_skills() -> tuple[Skill, ...]:
    """运行期技能清单（进程内缓存；测试直接调 load_skills 打桩）。"""
    return tuple(load_skills(SKILLS_DIR))


def match_skills(question: str, skills: tuple[Skill, ...] | list[Skill],
                 history: list[dict] | None = None) -> list[Skill]:
    """两层匹配，按文件序返回。

    ①点名：问题含技能 name/别名（子串，大小写不敏感），命中优先返回；
    ②指代回看：一层未命中且问题含指代词 → 回看最近一条助手回复，其 content
    提到的技能（name/别名，文件序首个）——无历史/无指代词/未提及 → []。
    """
    q = question.casefold()
    hits = [s for s in skills
            if s.name.casefold() in q or any(a.casefold() in q for a in s.aliases)]
    if hits or history is None:
        return hits
    if not any(r in question for r in _REFERENTS):
        return []
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "").casefold()
        return [s for s in skills
                if s.name.casefold() in content
                or any(a.casefold() in content for a in s.aliases)][:1]
    return []


def skill_text_for(question: str, skills: tuple[Skill, ...] | list[Skill],
                   history: list[dict] | None = None) -> str | None:
    """命中 → 【技能指令】正文拼接（多技能换行）；无命中 → None（[技能] 节保持空）。"""
    matched = match_skills(question, skills, history)
    if not matched:
        return None
    return "\n".join(f"【技能指令: {s.name}】{s.body}" for s in matched)
