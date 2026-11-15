"""G5 评测快照 API（docs/01 §3.1）：GET /api/eval/runs[/{name}]。

离线快照是历史产物，**不随 ONLINE_EVAL_ENABLED 关闭而 404**（P2-16 契约收口，
空目录=空列表）；在线评测的实时统计在 /api/system/metrics 的 online_eval 节，
由旗标门控。快照目录 eval/results/。
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
    files = sorted(_results_dir().glob("*.json"))
    runs = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            score = summary.get("avg_score")  # P2-15：avg_score=0（真零分）不被 or 链回退吞掉
            if score is None:
                score = summary.get("tool_selection_accuracy")
            runs.append({
                "name": path.name,
                "kind": data.get("kind", ""),
                "total": summary.get("total"),
                "score": score,
                "hit_cap_rate": summary.get("hit_cap_rate"),
                "link": (data.get("env") or {}).get("link", ""),
            })
        except Exception:
            continue
    return {"runs": runs[-20:]}  # 页面展示条数 20（docs/01 §4.4）


@router.get("/api/eval/runs/{name}")
def get_run(name: str, settings: Settings = Depends(get_settings)) -> Any:
    if "/" in name or ".." in name or not name.endswith(".json"):
        return _404()  # 非法标识一律 4041
    path = _results_dir() / name
    if not path.is_file():
        return _404()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _404()
