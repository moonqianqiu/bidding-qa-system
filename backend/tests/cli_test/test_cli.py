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
