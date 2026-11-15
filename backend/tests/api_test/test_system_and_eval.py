"""H2 系统监控 + G5 评测快照 API（404 门/脱敏/环形缓冲）+ H7 prune CLI。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

import bidqa.api.system.metrics as sys_mod  # C4：metrics 缓存命名空间
from bidqa.main import create_app
from bidqa.obs import logs as obs_mod


def make_client(settings_factory, **extra):
    app = create_app(settings_factory(**extra))
    return TestClient(app)


def test_metrics_shape_and_cache(settings_factory, monkeypatch) -> None:
    sys_mod._metrics_cache = None
    obs_mod.request_stats.clear()
    client = make_client(settings_factory)
    r1 = client.get("/api/system/metrics")
    assert r1.status_code == 200
    body = r1.json()
    assert body["uptime_s"] >= 0 and "requests" in body
    assert body["resources"]["cpu_percent"] is not None  # psutil 在或「不可用」都不炸
    r2 = client.get("/api/system/metrics")
    assert r2.json() == body  # 5s 缓存


def test_logs_mask_and_filter(settings_factory) -> None:
    client = make_client(settings_factory)
    obs_mod.log_buffer.append({"ts": "12:00:00", "level": "INFO", "name": "t",
                               "message": obs_mod.mask_secrets("key sk-abcd1234567890abcd leak")})
    obs_mod.log_buffer.append({"ts": "12:00:01", "level": "ERROR", "name": "t",
                               "message": "boom 空调"})
    logs = client.get("/api/system/logs", params={"q": "sk-"}).json()["logs"]
    assert all("1234567890abcd" not in entry["message"] for entry in logs)  # 已脱敏
    errs = client.get("/api/system/logs", params={"level": "ERROR"}).json()["logs"]
    assert any("boom" in entry["message"] for entry in errs)


def test_setup_log_buffer_attaches_stderr_stream() -> None:
    """应用日志落盘（2026-11-08 修复）：bidqa logger 须有 StreamHandler（stderr → backend.log）。"""
    import logging

    obs_mod.setup_log_buffer()
    bidqa = logging.getLogger("bidqa")
    assert any(isinstance(h, logging.StreamHandler) for h in bidqa.handlers)
    handlers_before = list(bidqa.handlers)
    obs_mod.setup_log_buffer()  # 幂等：重复 setup 不叠 handler
    assert list(bidqa.handlers) == handlers_before


def test_providers_never_leak_keys(settings_factory, monkeypatch) -> None:
    """provider 边界（§3.1）：名册只回名字+模型，key 绝不出现；未知名/缺件档位不回传。"""
    monkeypatch.setenv("CHAT_PROFILES", "fast")
    monkeypatch.setenv("CHAT_FAST_BASE_URL", "https://fast.example.com/v1")
    monkeypatch.setenv("CHAT_FAST_API_KEY", "sk-secret-fast-1234567890")
    monkeypatch.setenv("CHAT_FAST_MODEL", "fast-model")
    client = make_client(settings_factory)
    body = client.get("/api/system/providers").json()
    assert body["default_model"] == "test-model"
    assert body["profiles"] == [{"name": "fast", "model": "fast-model"}]
    assert "sk-secret-fast" not in json.dumps(body)  # key 绝不回传
    assert "api_key" not in json.dumps(body)  # 连字段名都不出现


def test_eval_runs_not_gated_by_online_flag(settings_factory, monkeypatch, tmp_path) -> None:
    """P2-16 契约收口：离线快照是历史产物，不随 ONLINE_EVAL_ENABLED 关闭而 404
    （原总开关把离线评测可见性连带吞掉）；在线评测统计在 metrics 的 online_eval
    节由旗标门控。P2-15：avg_score=0（真零分）不得被 or 链回退吞掉。"""
    import bidqa.api.eval_runs as mod

    monkeypatch.setattr(mod, "_results_dir", lambda: tmp_path)
    (tmp_path / "agent_x.json").write_text(json.dumps({
        "kind": "agent", "env": {"link": "internal"},
        "summary": {"total": 33, "tool_selection_accuracy": 1.0}}), encoding="utf-8")
    (tmp_path / "rag_zero.json").write_text(json.dumps({
        "kind": "rag", "env": {"link": "internal"},
        "summary": {"total": 3, "avg_score": 0.0}}), encoding="utf-8")
    client = make_client(settings_factory)  # ONLINE_EVAL_ENABLED=false（默认）
    runs = {r["name"]: r for r in client.get("/api/eval/runs").json()["runs"]}
    assert runs["agent_x.json"]["score"] == 1.0  # avg_score 缺失回退 tool_selection_accuracy
    assert runs["rag_zero.json"]["score"] == 0.0  # 真零分不被回退（原 `or` 链吞掉=红）
    assert client.get("/api/eval/runs/agent_x.json").status_code == 200


def test_setup_log_buffer_idempotent_and_stderr_masked() -> None:
    """P2-4（审计）：①setup 曾给 root 无条件叠 MemoryLogHandler（3× create_app →
    每条日志重复 3 份）；②stderr StreamHandler 未脱敏——backend.log 由它落盘，
    裸 sk- 密钥曾直接入盘（硬约束 6「先脱敏后截断」只覆盖了内存环）。"""
    import io
    import logging as _logging

    from bidqa.obs.logs import MaskedStreamHandler, MemoryLogHandler, setup_log_buffer

    root = _logging.getLogger()
    # 清场：其他用例的 create_app 可能已叠过 handler，本用例从零计数
    for h in [h for h in root.handlers if isinstance(h, MemoryLogHandler)]:
        root.removeHandler(h)
    bidqa = _logging.getLogger("bidqa")
    for h in [h for h in bidqa.handlers if isinstance(h, MaskedStreamHandler)]:
        bidqa.removeHandler(h)
    for _ in range(3):
        setup_log_buffer()
    assert len([h for h in root.handlers if isinstance(h, MemoryLogHandler)]) == 1
    assert len([h for h in bidqa.handlers if isinstance(h, MaskedStreamHandler)]) == 1
    stream = io.StringIO()
    h = MaskedStreamHandler(stream)
    h.setFormatter(_logging.Formatter("%(message)s"))
    rec = _logging.LogRecord("t", _logging.INFO, "p", 1,
                             "key sk-abcd1234567890abcd end", (), None)
    h.handle(rec)
    assert "abcd1234567890abcd" not in stream.getvalue()  # 裸 key 不落盘
    assert "sk-abcd****abcd" in stream.getvalue()


def test_mask_secrets_rules_beyond_sk() -> None:
    """P2-39（审计）：mask_secrets 曾只有 sk- 一条规则——Bearer token / api_key= /
    URL 内嵌凭据原样进日志环并经 /api/system/logs 回显（硬约束 6「先具体后通用」
    实际只有一条具体规则）。"""
    from bidqa.obs.logs import mask_secrets

    text = mask_secrets("auth Bearer tok_abcd12345678 q=1 api_key=secvalue123456 "
                        "url https://user:pa55w0rd@host/x")
    assert "tok_abcd12345678" not in text  # Bearer token
    assert "secvalue123456" not in text  # api_key=
    assert "user:pa55w0rd@" not in text  # URL 凭据
    assert "sk-abcd****abcd" == mask_secrets("sk-abcd1234567890abcd")  # 原规则不回归


def test_lifespan_shutdown_runs_four_steps(monkeypatch, settings_factory) -> None:
    """P1-8：应用停机钩子四步的后三步——②SSE 广播（sse 单测覆盖）③runner/压缩
    池 cancel futures（close）④连接池关闭（docs/01 §6；①停收新请求是 uvicorn 的）。"""
    import bidqa.agent.compress as compress_mod
    import bidqa.application.chat.worker as worker_mod
    import bidqa.database.factory as db_factory

    calls: list[str] = []

    class SpyRunner:
        def close(self) -> None:
            calls.append("runner")

    monkeypatch.setattr(worker_mod, "_shared_runner", SpyRunner())
    monkeypatch.setattr(compress_mod, "close", lambda: calls.append("compress"))
    monkeypatch.setattr(db_factory, "close_pools", lambda: calls.append("pools"))
    with TestClient(create_app(settings_factory())):
        pass  # 进入+退出触发 lifespan
    assert calls == ["runner", "compress", "pools"]  # 顺序=四步的③④


def test_eval_runs_list_and_detail(settings_factory, monkeypatch, tmp_path) -> None:
    import bidqa.api.eval_runs as mod

    monkeypatch.setattr(mod, "_results_dir", lambda: tmp_path)
    (tmp_path / "rag_20261102_abc_judge.json").write_text(json.dumps({
        "kind": "rag", "env": {"link": "internal"},
        "summary": {"total": 8, "avg_score": 0.87}}), encoding="utf-8")
    client = make_client(settings_factory, ONLINE_EVAL_ENABLED="true")
    runs = client.get("/api/eval/runs").json()["runs"]
    assert runs[0]["name"] == "rag_20261102_abc_judge.json"
    assert runs[0]["score"] == 0.87
    detail = client.get("/api/eval/runs/rag_20261102_abc_judge.json")
    assert detail.status_code == 200 and detail.json()["kind"] == "rag"
    assert client.get("/api/eval/runs/..%2Fetc.json").status_code == 404
    assert client.get("/api/eval/runs/missing.json").status_code == 404


def test_prune_cli(pg_client) -> None:  # noqa: ARG001
    """H7 prune：dry-run 不删；真实执行删除 N 天前数据。"""
    from bidqa.cli.main import main  # C5：入口在 cli.main

    assert main(["prune", "--days", "30", "--dry-run"]) == 0
    assert main(["prune", "--days", "30"]) == 0
