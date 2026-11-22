"""G3 Agent 评测运行器（fake 脚本注入；快照命名/原子写/hit_cap 统计/工具选择准确率）。

WP5c：Plan/Reflection 纳入评测口径——plan_rate/reflected_count、cases 逐例
plan_steps/reflected、env.agent_flags（A/B 可区分）；真实黄金集扩例合法性
（澄清闸门双向对账零成本钉进契约测试）。
"""

from __future__ import annotations

import json
from pathlib import Path

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
    """按问题脚本化：question -> chat_tools 响应序列。

    WP5c：plan/reflect 复用主模型槽位 chat()——按 system 头部分流（规划器/自检器），
    未配置对应回复时回落 plain（既有用例行为不变）。
    """

    def __init__(self, scripts: dict[str, list], plain: str = "基于已有结果作答。",
                 plan_reply: str | None = None, reflect_reply: str | None = None):
        self.scripts = {q: list(v) for q, v in scripts.items()}
        self.plain = plain
        self.plan_reply = plan_reply
        self.reflect_reply = reflect_reply

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
        head = messages[0]["content"] if messages else ""
        if "规划器" in head and self.plan_reply is not None:
            return self.plan_reply
        if "自检器" in head and self.reflect_reply is not None:
            return self.reflect_reply
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
    # 落盘隔离（AGENTS 红线）：RESULTS_DIR 在 P1-20/P1-33 后是 Path(__file__) 绝对锚定，
    # 单纯 chdir 不再生效——必须显式打桩到 tmp_path，否则全量 pytest 每次都往
    # backend/eval/results/ 写一份 abc1234 假快照（2026-11-19 实测发现的历史污染）。
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bidqa.eval.agent_eval.RESULTS_DIR", results)
    monkeypatch.setattr("bidqa.eval.agent_eval.git_short_hash", lambda: "abc1234")
    path = snapshot_path("agent", "nojudge")
    atomic_write_json(path, report)
    assert path.parent == results  # 只写临时目录，绝不落真实 results/
    assert path.name.startswith("agent_") and "abc1234" in path.name
    assert path.name.endswith("_nojudge.json")
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == "agent"
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    # NIGHT-036：golden 段——金集文件主名入名，段序 kind_日期_golden_git_judge
    import datetime
    date_str = datetime.date.today().strftime("%Y%m%d")
    path_g = snapshot_path("agent", "nojudge", golden=str(golden))  # golden 文件名 agent_v0.jsonl
    assert path_g.name == f"agent_{date_str}_agent_v0_abc1234_nojudge.json"
    assert snapshot_path("agent", "nojudge", golden=None) == path  # 不可得退回旧四段名


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


def test_same_day_two_goldens_produce_distinct_snapshots(tmp_path, monkeypatch) -> None:
    """NIGHT-036（2026-11-22 实测）：rag_v0 快照被 rag_law_v1 覆盖——同日同模式
    两份金集必须各产出独立文件。fake 报告落 tmp 目录，不跑真实评测。"""
    from bidqa.eval.ragas import write_rag_snapshot

    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bidqa.eval.agent_eval.RESULTS_DIR", results)
    monkeypatch.setattr("bidqa.eval.ragas.RESULTS_DIR", results)  # retention 扫描源
    monkeypatch.setattr("bidqa.eval.agent_eval.git_short_hash", lambda: "abc1234")

    golden_v0 = tmp_path / "rag_v0.jsonl"
    golden_v0.write_text("{}", encoding="utf-8")
    golden_law = tmp_path / "rag_law_v1.jsonl"
    golden_law.write_text("{}", encoding="utf-8")
    report = {"kind": "rag", "env": {"link": "internal"}, "summary": {}, "cases": []}

    class S:
        JUDGE_MODEL = ""

    path_v0 = write_rag_snapshot(report, S(), golden=str(golden_v0))
    path_law = write_rag_snapshot(report, S(), golden=str(golden_law))
    assert path_v0.name != path_law.name  # 同日同模式两金集不再互踩（原四段名=同名覆盖）
    assert "rag_v0" in path_v0.name and "rag_law_v1" in path_law.name
    assert path_v0.is_file() and path_law.is_file()  # 两份都独立在盘
    assert json.loads(path_v0.read_text(encoding="utf-8"))["kind"] == "rag"
    assert json.loads(path_law.read_text(encoding="utf-8"))["kind"] == "rag"
    # 同金集重跑同日 → 同名覆盖（预期内：同一批次的最新结果只留一份）
    assert write_rag_snapshot(report, S(), golden=str(golden_v0)).name == path_v0.name


# ── WP5c：黄金集扩例合法性 + Plan/Reflection 评测口径 ──
REPO_GOLDEN = (Path(__file__).resolve().parents[2].parent
               / "eval" / "golden" / "agent_v0.jsonl")


def _golden_num(case: dict) -> int:
    return int(case["id"].split("-")[1])


def test_golden_agent_v0_extended_valid() -> None:
    """真实黄金集扩例（WP5c）：总数 40、id 唯一、新例带 note、澄清闸门双向对账。

    对账零成本钉进契约测试：query_type=clarify 必须命中澄清判据、其余用例不得
    误触发（误触发 → 付费跑批全红，2026-11-15 扩例轮先例）。
    """
    from bidqa.agent.clarify import is_underqualified

    cases = load_golden(str(REPO_GOLDEN))
    assert len(cases) == 40  # 33 + 7（WP5c）
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    fresh = [c for c in cases if _golden_num(c) >= 34]
    assert 6 <= len(fresh) <= 8 and all(c.get("note") for c in fresh)
    for c in cases:
        assert c["id"] and c["question"] and c["expected_tools"]
        assert all(c["expected_tools"]) and c.get("query_type")
        assert is_underqualified(c["question"]) == (c["query_type"] == "clarify")
    assert any(len(c["expected_tools"]) >= 2 for c in fresh)  # ANY-of 双渠道表达
    # 复合/计划路径用例真实存在：should_plan 可命中（--plan 才有 plan_rate 观测意义）
    from bidqa.agent.plan import should_plan
    assert any(should_plan(c["question"]) for c in fresh)


def test_golden_anyof_scores_single_channel_hit(tmp_path) -> None:
    """ANY-of 语义（真实黄金集例）：期望多工具时命中任意一条合法路径即 PASS。"""
    cases = load_golden(str(REPO_GOLDEN))
    case = next(c for c in cases if c["id"] == "ag-009")  # db+graph 双渠道
    assert len(case["expected_tools"]) == 2
    golden = tmp_path / "one.jsonl"
    golden.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    chat = FakeChat({case["question"]:
                     [tool_msg("search_graph", {"query_type": "supplier_topn"})]})
    report = run_agent_eval(str(golden), make_factory(chat), settings=None)
    assert report["cases"][0]["pass"] is True  # 只走图谱一条路径也过


def test_agent_eval_plan_and_reflect_metrics(tmp_path) -> None:
    """WP5c 口径：plan_rate=plan_steps 非空占比、reflected_count=补查用例数；
    cases 逐例落两字段、env.agent_flags 记录 A/B 开关。"""
    golden = tmp_path / "g.jsonl"
    golden.write_text(
        json.dumps({"id": "p1", "query_type": "db_agg", "question": "分别统计空调和电梯的中标项目数量",
                    "expected_tools": ["search_database"]}, ensure_ascii=False) + "\n" +
        json.dumps({"id": "p2", "query_type": "db_topn", "question": "空调采购金额最高的项目是哪个？",
                    "expected_tools": ["search_database"]}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    chat = FakeChat({
        "分别统计空调和电梯的中标项目数量":
            [tool_msg("search_database", {"query_type": "subject_agg"})],
        "空调采购金额最高的项目是哪个？":
            [tool_msg("search_database", {"query_type": "subject_topn"})],
    }, plan_reply='{"steps": ["查空调项目数", "查电梯项目数"]}',
       reflect_reply='{"need_more": true, "hint": "缺少时间维度拆分"}')
    report = run_agent_eval(str(golden), make_factory(chat), settings=None,
                            plan_enabled=True, reflect_enabled=True)
    s = report["summary"]
    assert s["total"] == 2
    assert s["plan_rate"] == 0.5      # 复合题出计划、简单题不出
    assert s["reflected_count"] == 2  # 两例工具轮后自检均判不足
    assert report["env"]["agent_flags"] == {"plan": True, "reflect": True}
    c1, c2 = report["cases"]
    assert c1["plan_steps"] == ["查空调项目数", "查电梯项目数"] and c1["reflected"] is True
    assert c2["plan_steps"] == [] and c2["reflected"] is True
    # 既有口径不受影响
    assert s["tool_selection_accuracy"] == 1.0 and s["hit_cap_rate"] == 0.0


def test_agent_eval_default_flags_baseline(tmp_path) -> None:
    """WP5c 基线：不传 flags → plan_rate=0、reflected_count=0、agent_flags=false；
    开关关时规划/自检零模型调用（chat() 被误调即抛错 → 用例落 error，测试变红）。"""

    class _NoExtraCallChat(FakeChat):
        def chat(self, messages, temperature=0.3):
            raise AssertionError("开关关闭时不应有规划/自检调用")

    golden = tmp_path / "g.jsonl"
    golden.write_text(
        json.dumps({"id": "b1", "query_type": "db_agg", "question": "分别统计空调和电梯的中标项目数量",
                    "expected_tools": ["search_database"]}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    chat = _NoExtraCallChat({"分别统计空调和电梯的中标项目数量":
                             [tool_msg("search_database", {"query_type": "subject_agg"})]})
    report = run_agent_eval(str(golden), make_factory(chat), settings=None)
    assert report["summary"]["plan_rate"] == 0.0
    assert report["summary"]["reflected_count"] == 0
    assert report["env"]["agent_flags"] == {"plan": False, "reflect": False}
    case = report["cases"][0]
    assert case["plan_steps"] == [] and case["reflected"] is False
    assert "error" not in case  # 证明零额外调用（否则 loop 抛错落 error）


def test_agent_eval_error_case_carries_plan_fields(tmp_path) -> None:
    """WP5c：异常用例同样带 plan_steps/reflected（快照逐例字段恒在位，消费方免判键）。"""

    class _BoomChat(FakeChat):
        def chat_stream_round(self, messages, tools, temperature=0.3):
            raise RuntimeError("boom")

    golden = tmp_path / "g.jsonl"
    golden.write_text(
        json.dumps({"id": "e1", "query_type": "db_agg", "question": "q？",
                    "expected_tools": ["search_database"]}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    report = run_agent_eval(str(golden), make_factory(_BoomChat({})), settings=None)
    case = report["cases"][0]
    assert case["pass"] is False and "boom" in case["error"]
    assert case["plan_steps"] == [] and case["reflected"] is False
    assert report["summary"]["plan_rate"] == 0.0
    assert report["summary"]["reflected_count"] == 0
