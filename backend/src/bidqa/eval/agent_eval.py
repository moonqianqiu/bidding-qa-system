"""G3 离线 Agent 评测（docs/02；内部函数层 docs/01 §3.6——不经 HTTP、不占限流）。

走 AgentLoop 编排函数本身：工具选择准确率 + hit_cap 率 + 轮次 + 延迟。
WP5c：Plan/Reflection 纳入口径——summary 增 plan_rate（plan_steps 非空用例占比，
观测指标无门禁）/reflected_count（reflected=true 用例数）；cases 逐例落
plan_steps/reflected（异常用例恒 []/False，消费方免判键）；env.agent_flags
记录 A/B 开关（plan_enabled/reflect_enabled 透传 AgentLoop.run，默认 False=基线）。
快照 §4.4：原子写 + 命名 {kind}_{YYYYMMDD}_{git短hash}_{judge}.json；env.link="internal"。
chat 由调用方注入（真实模型或 fake 脚本），评测代码不感知厂商。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[3]  # backend/（src/bidqa/eval 上溯三级）
RESULTS_DIR = BACKEND_DIR / "eval" / "results"  # 绝对锚定（P1-20/P1-33：相对 CWD 曾随启动目录分家）
HIT_CAP_TARGET = 0.10  # hit_cap 率目标 <10%（docs/02 §0.1）


def load_golden(path: str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]


def git_short_hash() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip()
    except Exception:
        return os.environ.get("BIDQA_GIT_HASH", "unknown")


def judge_model_name(settings: Any) -> str:
    return (settings.JUDGE_MODEL or "nojudge").replace("/", "_")


def snapshot_path(kind: str, judge: str) -> Path:
    """命名规范（docs/01 §4.4）：{kind}_{YYYYMMDD}_{git短hash}_{判分模型}.json"""
    import datetime
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    return RESULTS_DIR / f"{kind}_{date_str}_{git_short_hash()}_{judge}.json"


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")  # pid 唯一：并发写同名不互踩（P1-20/P2-17）
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 原子写
    finally:
        tmp.unlink(missing_ok=True)  # 成功后 tmp 已不存在；失败清理残留（P2-17）


def precheck_tool_registry(cases: list[dict], loop: Any) -> None:
    """执行前门禁：黄金集需要的工具必须全部注册，否则拒绝跑批。

    背景（2026-11-02 教训）：PG/Neo4j 服务静默宕机时工具注册表只剩知识库，
    模型被迫全选 search_knowledge，产出 0.54 的误导分数——降级环境不产出评测分数。
    """
    expected: set[str] = set()
    for case in cases:
        expected.update(case.get("expected_tools") or [])
    expected.discard("ask_clarification")  # 澄清工具由首轮门控动态挂载，属正常
    registry = getattr(loop, "registry", None)
    names = set(registry.names() if registry is not None and hasattr(registry, "names") else [])
    missing = sorted(expected - names)
    if missing:
        raise RuntimeError(
            f"评测环境降级：黄金集所需工具未注册 {missing}（已注册: {sorted(names)}）。"
            "请确认 PG/Neo4j/Qdrant 服务在线后重跑；降级环境不产出评测分数。")


def run_agent_eval(golden_path: str, loop_factory: Any, settings: Any, *,
                   plan_enabled: bool = False, reflect_enabled: bool = False) -> dict:
    """loop_factory() -> AgentLoop：每次用例新建（fake 脚本/真实客户端均可注入）。

    WP5c：plan_enabled/reflect_enabled 透传 AgentLoop.run（A/B 能力版开关，
    默认 False=基线）；env.agent_flags 落快照使两批可区分。
    """
    cases = load_golden(golden_path)
    precheck_tool_registry(cases, loop_factory())  # 工具不齐 → 先拒绝，不产出误导分数
    results = []
    for case in cases:
        t0 = time.perf_counter()
        loop = loop_factory()
        try:
            result = loop.run(case["question"], plan_enabled=plan_enabled,
                              reflect_enabled=reflect_enabled)
            error = ""
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if result is None:
            results.append({"id": case["id"], "question": case["question"],
                            "pass": False, "error": error, "elapsed_ms": elapsed_ms,
                            "plan_steps": [], "reflected": False})  # 恒在位（WP5c）
            continue
        expected = set(case.get("expected_tools") or [])
        used = {c["name"] for r in result.tool_trace for c in r}
        if result.clarify:
            tool_hit = expected == {"ask_clarification"}
        else:
            tool_hit = bool(expected & used) and "ask_clarification" not in used
        results.append({
            "id": case["id"], "question": case["question"],
            "query_type": case.get("query_type"),
            "expected_tools": sorted(expected), "used_tools": sorted(used),
            "tool_hit": tool_hit, "clarify": result.clarify,
            "rounds": result.rounds, "hit_cap": result.hit_cap,
            "sources": len(result.sources), "elapsed_ms": elapsed_ms,
            "plan_steps": list(result.plan_steps), "reflected": bool(result.reflected),
            "pass": tool_hit,
        })

    passed = sum(1 for r in results if r["pass"])
    hit_caps = sum(1 for r in results if r.get("hit_cap"))
    planned = sum(1 for r in results if r.get("plan_steps"))  # WP5c：plan_steps 非空
    reflected_count = sum(1 for r in results if r.get("reflected"))
    clarifies = [r for r in results if r.get("query_type") == "clarify"]
    clarify_hit = sum(1 for r in clarifies if r.get("clarify"))
    latencies = sorted(r["elapsed_ms"] for r in results)
    summary = {
        "total": len(results),
        "tool_selection_accuracy": round(passed / len(results), 4) if results else None,
        "hit_cap_rate": round(hit_caps / len(results), 4) if results else None,
        "hit_cap_target": HIT_CAP_TARGET,
        "clarify_trigger_rate": round(clarify_hit / len(clarifies), 4) if clarifies else None,
        "plan_rate": round(planned / len(results), 4) if results else None,  # WP5c 观测指标
        "reflected_count": reflected_count,  # WP5c 观测指标
        "latency_p95_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "env_link": "internal",  # 内部函数层（docs/01 §3.6）
    }
    return {"kind": "agent",
            "env": {"link": "internal",
                    "chat_model": (getattr(settings, "CHAT_MODEL", "") or ""),
                    "agent_flags": {"plan": bool(plan_enabled),
                                    "reflect": bool(reflect_enabled)}},  # WP5c A/B 可区分
            "summary": summary, "cases": results, "failed_cases": [
                {"id": r["id"], "question": r["question"]} for r in results if not r["pass"]]}


def write_snapshot(report: dict, settings: Any) -> Path:
    from bidqa.eval.ragas import rolling_retention
    path = snapshot_path(report["kind"], judge_model_name(settings))
    atomic_write_json(path, report)
    rolling_retention()  # agent 快照同样滚动保留（P1-18：原只在 rag 快照路径调用）
    return path
