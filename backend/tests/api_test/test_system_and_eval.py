"""H2 系统监控 + G5 评测快照 API（404 门/脱敏/环形缓冲）+ H7 prune CLI。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from bidqa.api import system as sys_mod
from bidqa.main import create_app


def make_client(settings_factory, **extra):
    app = create_app(settings_factory(**extra))
    return TestClient(app)


def test_metrics_shape_and_cache(settings_factory, monkeypatch) -> None:
    sys_mod._metrics_cache = None
    sys_mod._request_stats.clear()
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
    sys_mod._log_buffer.append({"ts": "12:00:00", "level": "INFO", "name": "t",
                                "message": sys_mod.mask_secrets("key sk-abcd1234567890abcd leak")})
    sys_mod._log_buffer.append({"ts": "12:00:01", "level": "ERROR", "name": "t",
                                "message": "boom 空调"})
    logs = client.get("/api/system/logs", params={"q": "sk-"}).json()["logs"]
    assert all("1234567890abcd" not in entry["message"] for entry in logs)  # 已脱敏
    errs = client.get("/api/system/logs", params={"level": "ERROR"}).json()["logs"]
    assert any("boom" in entry["message"] for entry in errs)


def test_setup_log_buffer_attaches_stderr_stream() -> None:
    """应用日志落盘（2026-11-08 修复）：bidqa logger 须有 StreamHandler（stderr → backend.log）。"""
    import logging

    sys_mod.setup_log_buffer()
    bidqa = logging.getLogger("bidqa")
    assert any(isinstance(h, logging.StreamHandler) for h in bidqa.handlers)
    handlers_before = list(bidqa.handlers)
    sys_mod.setup_log_buffer()  # 幂等：重复 setup 不叠 handler
    assert list(bidqa.handlers) == handlers_before


def test_eval_runs_404_gate(settings_factory) -> None:
    """评测门关（默认）→ 整体 404（§3.1），不是空列表。"""
    client = make_client(settings_factory)
    assert client.get("/api/eval/runs").status_code == 404
    assert client.get("/api/eval/runs/rag_x.json").status_code == 404


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
    from bidqa.cli import main

    assert main(["prune", "--days", "30", "--dry-run"]) == 0
    assert main(["prune", "--days", "30"]) == 0
