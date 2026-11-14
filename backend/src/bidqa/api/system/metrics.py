"""H2 系统监控端点（docs/02）：资源快照（5s 缓存）+ 内存日志读取。

观测状态在 obs/logs（重构 C1 归位）——本模块只是其 HTTP 视图 + 资源探测。
脱敏先具体后通用：API key 形态先掩码（obs.mask_secrets 写入侧已脱敏）；
取不到的资源项记「不可用」绝不记 0。单进程约定（docs/01 §6）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query

from bidqa.config import get_settings
from bidqa.obs.logs import log_buffer, request_stats

router = APIRouter()

_started_at = time.monotonic()
_metrics_cache: tuple[float, dict] | None = None
_METRICS_TTL = 5.0


@router.get("/api/system/metrics")
def system_metrics() -> dict[str, Any]:
    global _metrics_cache
    now = time.monotonic()
    if _metrics_cache and now - _metrics_cache[0] < _METRICS_TTL:
        return _metrics_cache[1]

    settings = get_settings()
    resources: dict[str, Any] = {"note": "进程内快照"}
    try:
        proc = __import__("psutil", fromlist=["Process"]).Process()
        resources["cpu_percent"] = proc.cpu_percent(interval=0.05)
        mem = proc.memory_info()
        resources["rss_mb"] = round(mem.rss / 1024 / 1024, 1)
    except Exception:
        resources["cpu_percent"] = "不可用"  # 取不到记「不可用」，绝不记 0
        resources["rss_mb"] = "不可用"
    try:
        import shutil

        usage = shutil.disk_usage(settings.QDRANT_URL.split("//")[-1] or ".")
        resources["disk_free_gb"] = round(usage.free / 1024**3, 1)
    except Exception:
        resources["disk_free_gb"] = "不可用"

    metrics = {
        "uptime_s": int(now - _started_at),
        "requests": request_stats,
        "log_buffer_size": len(log_buffer),
        "resources": resources,
        "online_eval": _online_stats(),
    }
    _metrics_cache = (now, metrics)
    return metrics


def _online_stats() -> dict[str, Any]:
    """G4 在线评测计数（模块不可用 = 全 0 态，不抛错）。"""
    try:
        from bidqa.eval.online import get_online
        settings = get_settings()
        return {"enabled": bool(settings.ONLINE_EVAL_ENABLED),
                "sample_rate": settings.ONLINE_EVAL_SAMPLE_RATE,
                **get_online().stats()}
    except Exception:
        return {"enabled": False, "sample_rate": 0.0}


@router.get("/api/system/logs")
def system_logs(level: str = Query(default="INFO"),
                q: str = Query(default="", max_length=100)) -> Any:
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    min_level = order.get(level.upper(), 20)
    logs = [entry for entry in log_buffer
            if order.get(entry["level"], 20) >= min_level
            and (not q or q.lower() in entry["message"].lower())]
    return {"logs": list(logs)[-200:], "total": len(logs), "buffered": len(log_buffer)}
