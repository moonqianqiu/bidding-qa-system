"""G3 Agent 评测运行器（fake 脚本注入；快照命名/原子写/hit_cap 统计/工具选择准确率）。"""

from __future__ import annotations

import json

from pydantic import BaseModel

from bidqa.agent.loop import AgentLoop
from bidqa.eval.agent_eval import (
    atomic_write_json,
    load_golden,
    precheck_tool_registry,
    run_agent_eval,
    snapshot_path,
)
from bidqa.tools.base import BaseTool, ToolRegistry, tool_result
from bidqa.tools.runner import ToolRunner


class _EmptyArgs(BaseModel):
    pass


class _StubTool(BaseTool):
    """桩工具：只为注册表门禁与工具消息回填存在。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"stub {name}"
        self.args_model = _EmptyArgs

    def run(self, args) -> dict:
        return tool_result("stub ok")


class FakeChat:
    """按问题脚本化：question -> chat_tools 响应序列。"""

    def __init__(self, scripts: dict[str, list], plain: str = "基于已有结果作答。"):
        self.scripts = {q: list(v) for q, v in scripts.items()}
        self.plain = plain

    def chat_tools(self, messages, tools, temperature=0.3):
        q = next(m["content"] for m in messages if m["role"] == "user")
        script = self.scripts.get(q) or []
        return script.pop(0) if script else {"role": "assistant", "content": "直答"}

    def chat_stream_round(self, messages, tools, temperature=0.3):
        q = next(m["content"] for m in messages if m["role"] == "user")
        script = self.scripts.get(q) or []
        item = script.pop(0) if script else {"role": "assistant", "content": "直答"}
        if item.get("tool_calls"):
            for i, tc in enumerate(item["tool_calls"]):
                fn = tc["function"]
                yield ("tool", (i, tc["id"], fn["name"], fn["arguments"]))
        else:
            yield ("content", item.get("content") or "")

    def chat_stream(self, messages, temperature=0.3):
        yield self.plain

    def chat(self, messages, temperature=0.3):
        return self.plain


def tool_msg(name, arguments, call_id="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(arguments)}}]}


GOLDEN = """{"id": "a1", "query_type": "db_agg", "question": "安徽省各标的物项目数？", "expected_tools": ["search_database"]}
{"id": "a2", "query_type": "clarify", "question": "中标金额总共多少？", "expected_tools": ["ask_clarification"]}
{"id": "a3", "query_type": "db_topn", "question": "金额最高前3项目？", "expected_tools": ["search_database"]}
"""


def make_factory(chat: FakeChat, with_tools: bool = True):
    reg = ToolRegistry()
    if with_tools:  # 门禁要求：黄金集所需工具必须注册（模拟真实装配）
        for name in ("search_database", "search_knowledge", "search_graph"):
            reg.register(_StubTool(name))
    runner = ToolRunner()

    def factory():
        return AgentLoop(chat, reg, runner)

    return factory


def test_precheck_refuses_degraded_registry(tmp_path) -> None:
    """工具不齐（服务宕机场景）→ 拒绝跑批，不产出误导分数（2026-11-02 教训）。"""
    golden = tmp_path / "g.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    factory = make_factory(FakeChat({}), with_tools=False)
    try:
        run_agent_eval(str(golden), factory, settings=None)
    except RuntimeError as exc:
        assert "评测环境降级" in str(exc) and "search_database" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空注册表应被门禁拒绝")


def test_precheck_passes_with_full_registry(tmp_path) -> None:
    golden = tmp_path / "g.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    report = run_agent_eval(str(golden), make_factory(FakeChat({}), with_tools=True),
                            settings=None)
    assert report["summary"]["total"] == 3  # 门禁放行，正常跑完


def test_agent_eval_summary_and_snapshot(tmp_path, monkeypatch) -> None:
    golden = tmp_path / "agent_v0.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    chat = FakeChat({
        "安徽省各标的物项目数？": [tool_msg("search_database", {"query_type": "subject_agg"})],
        "中标金额总共多少？": [tool_msg("ask_clarification", {"question_for_user": "请补充地区时间"})],
        "金额最高前3项目？": [{"role": "assistant", "content": "没有工具也能答？"}],  # 期望 db 但没用工具 → FAIL
    })
    report = run_agent_eval(str(golden), make_factory(chat), settings=None)
    s = report["summary"]
    assert s["total"] == 3
    assert s["tool_selection_accuracy"] == round(2 / 3, 4)
    assert s["clarify_trigger_rate"] == 1.0
    assert s["hit_cap_rate"] == 0.0
    assert s["env_link"] == "internal"
    assert report["failed_cases"] == [{"id": "a3", "question": "金额最高前3项目？"}]

    # 快照命名与原子写（§4.4）
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bidqa.eval.agent_eval.git_short_hash", lambda: "abc1234")
    path = snapshot_path("agent", "nojudge")
    atomic_write_json(path, report)
    assert path.name.startswith("agent_") and "abc1234" in path.name
    assert path.name.endswith("_nojudge.json")
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "agent"
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_hit_cap_counted(tmp_path) -> None:
    golden = tmp_path / "g.jsonl"
    golden.write_text(
        json.dumps({"id": "h1", "query_type": "db_agg", "question": "q?",
                    "expected_tools": ["search_database"]}) + "\n", encoding="utf-8")
    # 4 轮都调工具 → 触顶
    script = [tool_msg("search_database", {"query_type": "subject_agg"}, call_id=f"c{i}")
              for i in range(4)]
    chat = FakeChat({"q?": script}, plain="收口回答。")
    report = run_agent_eval(str(golden), make_factory(chat), settings=None)
    assert report["summary"]["hit_cap_rate"] == 1.0  # >10% 目标会被 CI/晨报点名


def test_load_golden_skips_blank() -> None:
    p = __import__("pathlib").Path(__import__("tempfile").mkstemp()[1])
    p.write_text(GOLDEN.strip() + "\n\n", encoding="utf-8")
    assert len(load_golden(str(p))) == 3


def test_agent_snapshot_has_no_answer_text(tmp_path, monkeypatch) -> None:
    """P2-18（§4.4 明令快照不落回答正文）：agent 快照 cases 曾带 answer_head
    （正文前 80 字）并经 /api/eval/runs/{name} 原样返回。"""
    golden = tmp_path / "agent_v0.jsonl"
    golden.write_text(GOLDEN, encoding="utf-8")
    report = run_agent_eval(str(golden), make_factory(FakeChat({})), settings=None)
    assert all("answer_head" not in c for c in report["cases"])


def test_write_snapshot_applies_rolling_retention(tmp_path, monkeypatch) -> None:
    """P1-18：agent 快照从不清理——rolling_retention 只在 rag 快照写路径调用过。
    收口后 write_snapshot 统一触发滚动保留。"""
    import os

    from bidqa.eval.agent_eval import write_snapshot

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bidqa.eval.agent_eval.RESULTS_DIR", results)
    # retention 的扫描源在 ragas 模块——P1-20 绝对锚定后不再随 chdir 恰好对齐，须显式
    monkeypatch.setattr("bidqa.eval.ragas.RESULTS_DIR", results)
    for i in range(100):  # 造满 100 个旧 agent 快照（mtime 递增）
        f = results / f"agent_20261101_{i:04d}_judge.json"
        f.write_text("{}", encoding="utf-8")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    report = {"kind": "agent", "env": {"link": "internal"}, "summary": {}, "cases": []}

    class S:
        JUDGE_MODEL = ""

    path = write_snapshot(report, S())
    assert path.is_file()
    assert len(list(results.glob("*.json"))) == 100  # 新快照入列，最旧的被滚出（原不清理=101，红）
