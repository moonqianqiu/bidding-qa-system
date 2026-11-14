"""C9 技能注入（loader 解析/匹配命中/注入零往返；技能文件不进真实目录打桩）。

匹配语义：名称/别名子串命中（英文大小写不敏感），无命中恒空——build_system_prompt
的 [技能] 节保持空，行为与 C9 延后期一致。
"""

from __future__ import annotations

from pathlib import Path

from bidqa.agent import skills as skills_mod
from bidqa.agent.skills import Skill, load_skills, match_skills, skill_text_for

_SKILL_MD = """# bid-analysis

描述：端到端分析指引。
别名：分析招投标, 采购对比, 中标趋势

步骤：
1. 选择数据源。
2. 结构化回答。
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── loader ──
def test_parse_skill_full(tmp_path: Path) -> None:
    _write(tmp_path / "bid-analysis.md", _SKILL_MD)
    loaded = load_skills(tmp_path)
    assert len(loaded) == 1
    s = loaded[0]
    assert s.name == "bid-analysis"
    assert s.description == "端到端分析指引。"
    assert s.aliases == ("分析招投标", "采购对比", "中标趋势")
    assert "1. 选择数据源。" in s.body


def test_load_skills_skips_invalid_files(tmp_path: Path) -> None:
    _write(tmp_path / "no-name.md", "只有正文没有标题\n")
    _write(tmp_path / "no-body.md", "# 名字\n\n描述：只有头\n")
    _write(tmp_path / "ok.md", _SKILL_MD)
    assert [s.name for s in load_skills(tmp_path)] == ["bid-analysis"]


def test_load_skills_missing_dir(tmp_path: Path) -> None:
    assert load_skills(tmp_path / "不存在") == []


# ── 匹配 ──
def _skills() -> tuple[Skill, ...]:
    return (
        Skill(name="bid-analysis", description="", aliases=("分析招投标", "采购对比", "供应商分析"), body="正文A"),
        Skill(name="debug-answer", description="", aliases=("为什么回答不对", "回答有问题"), body="正文B"),
    )


def test_match_by_name_alias_and_casefold() -> None:
    assert [s.name for s in match_skills("帮我做采购对比分析", _skills())] == ["bid-analysis"]
    assert [s.name for s in match_skills("这个回答有问题吧", _skills())] == ["debug-answer"]
    # 英文名大小写不敏感
    assert [s.name for s in match_skills("Run Bid-Analysis for me", _skills())] == ["bid-analysis"]
    assert match_skills("空调采购项目有哪些？", _skills()) == []  # 无命中


def test_skill_text_for_hit_and_miss() -> None:
    assert skill_text_for("为什么回答不对", _skills()) == "【技能指令: debug-answer】正文B"
    assert skill_text_for("空调采购有哪些？", _skills()) is None


# ── loop 集成：命中注入 / 不命中 prompt 与现状一致 ──
class _FakeChat:
    """最小脚本化模型：记录每轮 messages，正文直接产出（test_loop 的 FakeChat 不可跨文件导入）。"""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[list[dict]] = []

    def chat_stream_round(self, messages, tools, temperature=0.3):
        self.calls.append(messages)
        yield ("content", self.content)

    def chat_tools(self, messages, tools, temperature=0.3):
        self.calls.append(messages)
        return {"content": self.content}


def _run_once(monkeypatch, question: str) -> list[list[dict]]:
    from bidqa.agent.loop import AgentLoop
    from bidqa.tools.base import ToolRegistry

    monkeypatch.setattr(skills_mod, "get_skills", lambda: _skills())
    chat = _FakeChat("结论 [1]。")
    loop = AgentLoop(chat, ToolRegistry(), ToolRegistry())
    loop.run(question)
    return chat.calls


def test_loop_injects_skill_when_named(monkeypatch) -> None:
    calls = _run_once(monkeypatch, "帮我做供应商分析")
    sys_msg = calls[0][0]["content"]
    assert "[技能]" in sys_msg and "【技能指令: bid-analysis】" in sys_msg


def test_loop_no_skill_keeps_prompt_unchanged(monkeypatch) -> None:
    calls = _run_once(monkeypatch, "空调采购项目有哪些？")
    assert "[技能]" not in calls[0][0]["content"]


# ── 二层匹配：指代词回看历史（参考项目三层匹配的第二层，2026-11-08 建）──
def test_match_layer2_referent_looks_back_history() -> None:
    history = [
        {"role": "user", "content": "帮我做供应商分析"},
        {"role": "assistant", "content": "好的，可以随时让我做供应商分析或采购对比。"},
    ]
    assert [s.name for s in match_skills("按这个技能再分析一轮", _skills(), history)] == ["bid-analysis"]


def test_match_layer2_referent_without_mention_is_empty() -> None:
    history = [{"role": "assistant", "content": "今天天气不错。"}]
    assert match_skills("按这个技能再来一次", _skills(), history) == []


def test_no_referent_ignores_history() -> None:
    history = [{"role": "assistant", "content": "可以让我做供应商分析。"}]
    assert match_skills("空调采购项目有哪些？", _skills(), history) == []


def test_layer1_direct_naming_beats_layer2() -> None:
    history = [{"role": "assistant", "content": "推荐用 debug-answer。"}]
    assert [s.name for s in match_skills("为什么回答不对", _skills(), history)] == ["debug-answer"]


def test_loop_layer2_injects_via_history(monkeypatch) -> None:
    from bidqa.agent.loop import AgentLoop
    from bidqa.tools.base import ToolRegistry

    monkeypatch.setattr(skills_mod, "get_skills", lambda: _skills())
    chat = _FakeChat("结论 [1]。")
    loop = AgentLoop(chat, ToolRegistry(), ToolRegistry())
    history = [{"role": "user", "content": "帮我做供应商分析"},
               {"role": "assistant", "content": "可以随时让我做供应商分析。"}]
    loop.run("按这个技能再分析一轮", history=history)
    assert "【技能指令: bid-analysis】" in chat.calls[0][0]["content"]
