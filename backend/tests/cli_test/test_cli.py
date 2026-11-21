"""CLI 冒烟：doctor 报告核心缺失；serve 在核心缺失时拒绝启动（不进入 uvicorn）。"""

import pytest

from bidqa.cli import main as cli  # C5：入口在 cli.main（patch 缝=模块级 get_settings）
from bidqa.config import Settings


def test_doctor_missing_core_returns_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "核心配置缺失" in out


def test_serve_missing_core_returns_2(monkeypatch):
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    assert cli.main(["serve"]) == 2  # 在 uvicorn 导入/运行之前拦截


def test_doctor_ok_returns_0(settings_factory, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_factory())
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "核心配置 OK" in out


# ── 用户体系（docs/01 §3.1）：user 子命令 + doctor 用户体系行 ──
class FakeStore:
    def __init__(self):
        self.users = [{"username": "admin", "role": "admin", "disabled": False,
                       "created_at": "2026-11-08"}]
        self.created: list[tuple[str, str]] = []
        self.resets: list[str] = []
        self.disabled: list[tuple[str, bool]] = []

    def verify_password_ok(self, username: str, candidate: str) -> bool:
        # P1-5：doctor 对账号真实哈希体检（fake=admin 仍是默认口令）
        return username == "admin" and candidate == "admin123"

    def create_user(self, name, password, role):
        if name in {u["username"] for u in self.users}:
            return None
        self.created.append((name, role))
        self.users.append({"username": name, "role": role, "disabled": False,
                           "created_at": "-"})
        return "uid-1"

    def reset_password(self, name, password):
        if name not in {u["username"] for u in self.users}:
            return None  # store 层新契约：用户不存在=None；成功=吊销会话数（int）
        self.resets.append(name)
        return 0

    def set_disabled(self, name, disabled):
        if name not in {u["username"] for u in self.users}:
            return False
        self.disabled.append((name, disabled))
        return True

    def list_users(self):
        return self.users

    def count_users(self):
        return len(self.users)

    def guest_enabled(self):
        return True


def test_user_add_and_list(monkeypatch, capsys):
    store = FakeStore()
    monkeypatch.setattr("bidqa.auth.factory.build_users_store", lambda: store)
    assert cli.main(["user", "add", "bob", "--role", "user", "--password", "pw"]) == 0
    assert store.created == [("bob", "user")]
    assert cli.main(["user", "add", "bob", "--password", "pw"]) == 1  # 重名拒绝
    assert cli.main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "bob" in out and "admin" in out


def test_user_reset_unknown_returns_1(monkeypatch, capsys):
    monkeypatch.setattr("bidqa.auth.factory.build_users_store", lambda: FakeStore())
    assert cli.main(["user", "reset", "ghost", "--password", "x"]) == 1
    assert "不存在" in capsys.readouterr().err


def test_user_disable_enable(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr("bidqa.auth.factory.build_users_store", lambda: store)
    assert cli.main(["user", "disable", "admin"]) == 0
    assert cli.main(["user", "enable", "admin"]) == 0
    assert store.disabled == [("admin", True), ("admin", False)]
    assert cli.main(["user", "disable", "ghost"]) == 1


def test_doctor_armed_reports_users_and_default_password(monkeypatch, capsys,
                                                         settings_factory):
    """armed：doctor 报用户数/游客开关；默认口令未改时告警。"""
    monkeypatch.setattr(cli, "get_settings",
                        lambda: settings_factory(USERS_ENABLED="true"))
    monkeypatch.setattr("bidqa.auth.factory.build_users_store", lambda: FakeStore())
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "用户体系" in out and "users=1" in out and "游客开关=开" in out
    assert "默认口令" in out  # admin/admin123 未改 → 告警


def test_doctor_disabled_reports_off(monkeypatch, capsys, settings_factory):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_factory())
    assert cli.main(["doctor"]) == 0
    assert "USERS_ENABLED=false" in capsys.readouterr().out


# ── ingest pdf：F2/F3 接线 + source_type 分布可见性（docs/01 §4.1）──
_MD = "<!-- page:1 -->\n第一章 总则\n第一条 为了规范招标投标活动。"


def _fake_parse_pdf(path, api_key=None):
    return {"markdown": _MD, "engine": "pdfplumber"}


def _pdf_args(*extra):
    return ["ingest", "pdf", "--input", "x.pdf", "--source-type", "法规",
            "--title", "招标投标法", *extra]


def test_ingest_pdf_dry_run_does_not_write(monkeypatch, capsys):
    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_pdf", _fake_parse_pdf)
    calls = {"repo": 0, "embedding": 0}

    def no_repo():
        calls["repo"] += 1
        raise AssertionError("dry-run 不应建 repo")

    monkeypatch.setattr("bidqa.database.factory.build_repo", no_repo)
    monkeypatch.setattr("bidqa.rag.factory.build_embedding",
                        lambda: calls.__setitem__("embedding", 1))
    assert cli.main(_pdf_args("--dry-run")) == 0
    out = capsys.readouterr().out
    assert "pdfplumber" in out and "法规" in out
    assert calls == {"repo": 0, "embedding": 0}  # 写库与模型路径全程打桩


def test_ingest_pdf_writes_non_faq_source_type(monkeypatch):
    from bidqa.database.qdrant import QdrantRepo

    repo = QdrantRepo(url=":memory:", collection="test_cli_pdf_v1")
    repo.ensure_collection()
    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_pdf", _fake_parse_pdf)
    monkeypatch.setattr("bidqa.database.factory.build_repo", lambda: repo)
    monkeypatch.setattr("bidqa.rag.factory.build_embedding", lambda: None)
    try:
        assert cli.main(_pdf_args()) == 0
        assert repo.source_type_counts().get("法规", 0) > 0
    finally:
        repo.close()


def test_ingest_pdf_rejects_unknown_source_type(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["ingest", "pdf", "--input", "x.pdf",
                  "--source-type", "新闻", "--title", "t"])
    assert exc.value.code == 2
    assert "新闻" in capsys.readouterr().err  # 锁定 choices 拒绝，而非「子命令不存在」


def test_ingest_pdf_txt_suffix_dispatches_parse_txt(monkeypatch, capsys):
    """txt 后缀直入（NIGHT-019）：parse_txt 接管、parse_pdf 不得被触碰。"""
    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_txt",
                        lambda path: {"markdown": _MD, "engine": "txt"})

    def _boom(path, api_key=None):
        raise AssertionError("txt 后缀不得走 PDF 解析")

    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_pdf", _boom)
    args = ["ingest", "pdf", "--input", "x.txt", "--source-type", "法规",
            "--title", "74号令", "--dry-run"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "engine=txt" in out and "法规" in out


def test_ingest_pdf_txt_empty_body_returns_2(monkeypatch, capsys):
    """txt 正文剥空 → ValueError → 解析失败退出码 2（不写库）。"""
    def _empty(path):
        raise ValueError("纯文本无正文")

    monkeypatch.setattr("bidqa.ingest.pdf_parse.parse_txt", _empty)
    args = ["ingest", "pdf", "--input", "x.txt", "--source-type", "法规",
            "--title", "t", "--dry-run"]
    assert cli.main(args) == 2
    assert "解析失败" in capsys.readouterr().err


class _FakeRepoDist:
    def __init__(self, dist):
        self._dist = dist

    def source_type_counts(self):
        return self._dist


def test_doctor_reports_source_type_distribution(monkeypatch, capsys,
                                                 settings_factory):
    monkeypatch.setattr(cli, "get_settings", lambda: settings_factory())
    monkeypatch.setattr("bidqa.database.factory.build_repo",
                        lambda: _FakeRepoDist({"FAQ": 75, "法规": 3}))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "source_type 分布" in out and "法规" in out


def test_doctor_source_type_unavailable_is_honest(monkeypatch, capsys,
                                                  settings_factory):
    def boom():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(cli, "get_settings", lambda: settings_factory())
    monkeypatch.setattr("bidqa.database.factory.build_repo", boom)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "source_type 分布: 不可用" in out
    assert "source_type 分布: {}" not in out  # 绝不记 0 / 空 dict 伪装


# ── ingest graph：归一是显式付费动作，默认/试运行不建 chat ──
def test_ingest_graph_normalize_and_mapping_file_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli.main(["ingest", "graph", "--normalize", "--mapping-file", "x.json"])
    assert exc.value.code == 2


def test_ingest_graph_missing_mapping_file_returns_2(capsys):
    assert cli.main(["ingest", "graph", "--mapping-file", "no-such.json"]) == 2
    assert "映射文件不存在" in capsys.readouterr().err


def _patch_graph_deps(monkeypatch, captured: dict):
    monkeypatch.setattr("bidqa.database.factory.build_pg", lambda: object())
    monkeypatch.setattr("bidqa.database.factory.build_neo", lambda: object())

    def fake_build_graph(pg, neo, **kwargs):
        captured.update(kwargs)
        return {"rows": 0, "dry_run": True, "mapping_items": 0,
                "review_items": 0, "normalize_calls": 0, "failed_batches": []}

    monkeypatch.setattr("bidqa.ingest.graph_build.build_graph", fake_build_graph)


def test_ingest_graph_dry_run_skips_chat_without_normalize(monkeypatch, capsys):
    def boom():
        raise AssertionError("未加 --normalize 不应构建 chat")

    monkeypatch.setattr("bidqa.container.build_agent_stack", boom)
    captured: dict = {}
    _patch_graph_deps(monkeypatch, captured)
    assert cli.main(["ingest", "graph", "--dry-run"]) == 0
    assert captured["chat"] is None and captured["normalize"] is False
    assert "恒等映射预览" in capsys.readouterr().out


def test_ingest_graph_normalize_defaults_to_batch_channel(monkeypatch):
    monkeypatch.setattr("bidqa.clients.chat_factory.build_subject_batch_chat",
                        lambda: "batch-chat")

    def boom():
        raise AssertionError("默认批量通道不应构建 Agent 栈")

    monkeypatch.setattr("bidqa.container.build_agent_stack", boom)
    captured: dict = {}
    _patch_graph_deps(monkeypatch, captured)
    assert cli.main(["ingest", "graph", "--normalize", "--dry-run"]) == 0
    assert captured["chat"] == "batch-chat"
    assert captured["normalize"] is True


def test_ingest_graph_normalize_channel_chat_uses_main_slot(monkeypatch):
    def boom():
        raise AssertionError("显式 chat 通道不应构建批量客户端")

    monkeypatch.setattr("bidqa.clients.chat_factory.build_subject_batch_chat", boom)
    monkeypatch.setattr("bidqa.container.build_agent_stack", lambda: ("fake-chat",))
    captured: dict = {}
    _patch_graph_deps(monkeypatch, captured)
    assert cli.main(["ingest", "graph", "--normalize", "--normalize-channel", "chat",
                     "--dry-run"]) == 0
    assert captured["chat"] == "fake-chat"
    assert captured["normalize"] is True


def test_ingest_graph_normalize_batch_unconfigured_returns_2(monkeypatch, capsys):
    monkeypatch.setattr("bidqa.clients.chat_factory.build_subject_batch_chat",
                        lambda: None)
    captured: dict = {}
    _patch_graph_deps(monkeypatch, captured)
    assert cli.main(["ingest", "graph", "--normalize", "--dry-run"]) == 2
    assert "SUBJECT_BATCH_API_KEY" in capsys.readouterr().err


# ── 审计 P1-19/P2-15：rag CLI 模式 + 退出门禁的 None 口径 ──
def test_eval_cli_accepts_rag_mode(monkeypatch, tmp_path, capsys):
    """P1-19：G2 离线 RAGAS 曾无 CLI 入口（--mode 只有 smoke|agent）→
    5032 异源门禁在命令行不可达。接线后 --mode rag 可达（fake 注入，不真跑判分）。"""
    monkeypatch.setattr("bidqa.rag.factory.build_retriever", lambda: object())
    monkeypatch.setattr("bidqa.clients.chat_factory.build_chat", lambda: object())
    monkeypatch.setattr("bidqa.eval.ragas.run_rag_eval",
                        lambda *a, **k: {"summary": {"avg_score": 0.9}, "cases": []})
    monkeypatch.setattr("bidqa.eval.ragas.write_rag_snapshot",
                        lambda *a, **k: tmp_path / "r.json")
    assert cli.main(["eval", "--golden", str(tmp_path / "g.jsonl"), "--mode", "rag"]) == 0
    assert "avg_score" in capsys.readouterr().out


def test_eval_agent_gate_none_means_not_measured(monkeypatch, tmp_path, capsys):
    """P2-15：CLI 门禁 `hit_cap or 0` 曾把 None（没测出）当通过；avg_score=0
    （真零分）也不该被 or 链吞掉。None=不通过。"""
    import types

    import bidqa.container as container_mod
    import bidqa.eval.agent_eval as agent_eval_mod

    monkeypatch.setattr(container_mod, "build_agent_stack",
                        lambda: (object(), object(), object()))
    monkeypatch.setattr(agent_eval_mod, "write_snapshot",
                        lambda *a, **k: tmp_path / "s.json")

    def run(gate_summary):
        monkeypatch.setattr(agent_eval_mod, "run_agent_eval", lambda *a, **k: gate_summary)
        return cli.main(["eval", "--golden", str(tmp_path / "g.jsonl"), "--mode", "agent"])

    assert run({"summary": {"total": 0, "tool_selection_accuracy": None,
                            "hit_cap_rate": None, "clarify_trigger_rate": None},
                "cases": []}) == 1  # 全 None=没测出 → 不通过
    assert run({"summary": {"total": 33, "tool_selection_accuracy": 1.0,
                            "hit_cap_rate": None, "clarify_trigger_rate": 1.0},
                "cases": []}) == 1  # hit_cap None 曾 `or 0` → 误判通过（红）


def test_eval_agent_plan_reflect_flags_passthrough(monkeypatch, tmp_path):
    """WP5c：--plan/--reflect 透传 run_agent_eval（A/B 能力版开关）；缺省=False
    （快照 env.agent_flags 由此可区分基线/能力版两批）。"""
    import bidqa.container as container_mod
    import bidqa.eval.agent_eval as agent_eval_mod

    monkeypatch.setattr(container_mod, "build_agent_stack",
                        lambda: (object(), object(), object()))
    monkeypatch.setattr(agent_eval_mod, "write_snapshot",
                        lambda *a, **k: tmp_path / "s.json")
    gate = {"summary": {"total": 1, "tool_selection_accuracy": 1.0,
                        "hit_cap_rate": 0.0, "clarify_trigger_rate": 1.0},
            "cases": []}
    captured: dict = {}

    def fake_run(golden, factory, settings, **kwargs):
        captured.update(kwargs)
        return gate

    monkeypatch.setattr(agent_eval_mod, "run_agent_eval", fake_run)
    argv = ["eval", "--golden", str(tmp_path / "g.jsonl"), "--mode", "agent"]
    assert cli.main(argv + ["--plan", "--reflect"]) == 0
    assert captured == {"plan_enabled": True, "reflect_enabled": True}
    captured.clear()
    assert cli.main(argv) == 0  # 不给 flags → 默认 False（基线）
    assert captured == {"plan_enabled": False, "reflect_enabled": False}


# ── WP6：retrieval CLI 模式（fake 注入：不真跑检索、不调 embedding API）──
def _retrieval_report(avg_recall, cases):
    return {"kind": "retrieval",
            "summary": {"metrics_avg": {"recall@5": avg_recall, "recall@10": avg_recall,
                                        "ndcg@5": avg_recall, "ndcg@10": avg_recall,
                                        "mrr": avg_recall}},
            "cases": cases}


def test_eval_cli_accepts_retrieval_mode(monkeypatch, tmp_path, capsys):
    """WP6：--mode retrieval 接线；env 由 build_retrieval_env 组装（指纹脱敏+语料注记）。"""
    captured: dict = {}
    report = _retrieval_report(
        0.5, [{"id": "rag_flow_001", "question": "q", "measured": True, "hits": 5,
               "metrics": {"recall@5": 0.5, "recall@10": 0.5, "ndcg@5": 0.4,
                           "ndcg@10": 0.4, "mrr": 1.0}}])

    def fake_run(golden, retriever, ks=(5, 10), env=None):
        captured["golden"] = golden
        captured["env"] = env
        return report

    monkeypatch.setattr("bidqa.database.factory.build_repo", lambda: object())
    monkeypatch.setattr("bidqa.rag.factory.build_retriever", lambda: object())
    monkeypatch.setattr("bidqa.eval.retrieval_eval.run_retrieval_eval", fake_run)
    monkeypatch.setattr("bidqa.eval.retrieval_eval.write_retrieval_snapshot",
                        lambda *a, **k: tmp_path / "r.json")
    assert cli.main(["eval", "--golden", str(tmp_path / "g.jsonl"),
                     "--mode", "retrieval"]) == 0  # 有实测均值 → 通过
    out = capsys.readouterr().out
    assert "metrics_avg" in out and "快照" in out and "recall@5=0.5" in out
    assert captured["golden"] == str(tmp_path / "g.jsonl")
    # env 组装在位：语料注记 + 指纹槽（retrieval 模式无 chat/judge 调用，仅留档）
    assert captured["env"]["note"] and "75" in captured["env"]["note"]
    assert {"link", "chat", "embedding"} <= set(captured["env"])


def test_eval_cli_retrieval_all_none_is_not_measured(monkeypatch, tmp_path, capsys):
    """P2-15 口径：全 None=没测出 → 退出 1，None 不冒充通过。"""
    report = {"kind": "retrieval",
              "summary": {"metrics_avg": {"recall@5": None, "recall@10": None,
                                          "ndcg@5": None, "ndcg@10": None, "mrr": None}},
              "cases": [{"id": "c1", "question": "q", "measured": False,
                         "metrics": {"recall@5": None}}]}
    monkeypatch.setattr("bidqa.database.factory.build_repo", lambda: object())
    monkeypatch.setattr("bidqa.rag.factory.build_retriever", lambda: object())
    monkeypatch.setattr("bidqa.eval.retrieval_eval.run_retrieval_eval",
                        lambda *a, **k: report)
    monkeypatch.setattr("bidqa.eval.retrieval_eval.write_retrieval_snapshot",
                        lambda *a, **k: tmp_path / "r.json")
    assert cli.main(["eval", "--golden", str(tmp_path / "g.jsonl"),
                     "--mode", "retrieval"]) == 1
    assert "没测出" in capsys.readouterr().out
