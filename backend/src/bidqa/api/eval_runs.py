"""G5 评测快照 API（docs/01 §3.1）：GET /api/eval/runs[/{name}]；实时统计 /api/eval/live。

EVAL 门=ONLINE_EVAL_ENABLED（§3.1 的 EVAL_ENABLED 语义）：关闭时整体 404（不假装空列表）。
快照目录 eval/results/（命名规范按文件名排序=按时间）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from bidqa.config import Settings, get_settings
from bidqa.errors import EC_NOT_FOUND, error_body

router = APIRouter()


def _results_dir() -> Path:
    return Path("eval/results")


def _404() -> JSONResponse:
    return JSONResponse(status_code=404, content=error_body(EC_NOT_FOUND, "资源不存在"))


@router.get("/api/eval/runs")
def list_runs(settings: Settings = Depends(get_settings)) -> Any:
    if not settings.ONLINE_EVAL_ENABLED:  # 评测门：关=整体 404（§3.1）
        return _404()
    files = sorted(_results_dir().glob("*.json"))
    runs = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            runs.append({
                "name": path.name,
                "kind": data.get("kind", ""),
                "total": summary.get("total"),
                "score": summary.get("avg_score") or summary.get("tool_selection_accuracy"),
                "hit_cap_rate": summary.get("hit_cap_rate"),
                "link": (data.get("env") or {}).get("link", ""),
            })
        except Exception:
            continue
    return {"runs": runs[-20:]}  # 页面展示条数 20（docs/01 §4.4）


@router.get("/api/eval/runs/{name}")
def get_run(name: str, settings: Settings = Depends(get_settings)) -> Any:
    if not settings.ONLINE_EVAL_ENABLED:
        return _404()
    if "/" in name or ".." in name or not name.endswith(".json"):
        return _404()  # 非法标识一律 4041
    path = _results_dir() / name
    if not path.is_file():
        return _404()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _404()
