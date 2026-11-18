"""eval 子命令（docs/02 G1/G3）：smoke（来源可定位）与 agent（工具循环全链路）。

agent 模式走内部函数层（docs/01 §3.6）：完整问答栈由 container.build_agent_stack
装配（与线上 worker 同一 build_registry 出口）；工具不齐拒绝跑批，不产出误导分数。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from bidqa.config import get_settings


def _eval_agent(args, _settings: Any) -> int:
    """G3 离线 Agent 评测：内部函数层，真实主模型（M2-12 验收）。"""
    from bidqa.agent.loop import AgentLoop
    from bidqa.container import build_agent_stack
    from bidqa.eval.agent_eval import run_agent_eval, write_snapshot

    chat, registry, runner = build_agent_stack()
    if chat is None:
        print("主模型未配置（CHAT_*），无法运行 agent 评测", file=sys.stderr)
        return 2

    def loop_factory():
        return AgentLoop(chat, registry, runner)

    try:
        # WP5c：--plan/--reflect 透传 AgentLoop.run（A/B 能力版开关；快照
        # env.agent_flags 记录，与基线批可区分）。真跑=主模型槽位付费调用。
        report = run_agent_eval(args.golden, loop_factory, get_settings(),
                                plan_enabled=bool(args.plan),
                                reflect_enabled=bool(args.reflect))
    except RuntimeError as exc:  # 工具不齐（服务宕机）→ 拒绝跑批，不产出误导分数
        print(f"评测中止：{exc}", file=sys.stderr)
        return 2
    path = write_snapshot(report, get_settings())
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for r in report["cases"]:
        mark = "PASS" if r["pass"] else "FAIL"
        extra = (f" tools={','.join(r.get('used_tools') or []) or '-'}"
                 f" clarify={r.get('clarify')} hit_cap={r.get('hit_cap')}") if "used_tools" in r else ""
        print(f"  [{mark}] {r['id']} {r['question'][:32]}{extra}")
    print(f"快照: {path}")
    summary = report["summary"]
    # None=没测出 → 不通过（P2-15：`or 0` 曾把 hit_cap None 当 0 通过）
    accuracy = summary["tool_selection_accuracy"]
    hit_cap = summary["hit_cap_rate"]
    ok = (accuracy is not None and accuracy >= 0.9
          and hit_cap is not None and hit_cap < 0.10
          and summary["clarify_trigger_rate"] in (None, 1.0))
    return 0 if ok else 1


def _eval_rag(args, _settings: Any) -> int:
    """G2 离线 RAGAS 评测 CLI 入口（审计 P1-19：原生产调用点为 0，5032 异源
    门禁在命令行不可达）。⚠ 判分槽位收费（AGENTS 红线）：真跑批前向用户报价确认；
    测试/开发一律注入 fake judge 或读离线缓存。"""
    from bidqa.clients.chat_factory import build_chat
    from bidqa.eval.ragas import run_rag_eval, write_rag_snapshot
    from bidqa.rag.factory import build_retriever

    chat = build_chat()
    if chat is None:
        print("主模型未配置（CHAT_*），无法运行 rag 评测", file=sys.stderr)
        return 2
    try:
        report = run_rag_eval(args.golden, build_retriever(), chat, get_settings())
    except Exception as exc:  # 5032 异源 / 判分未配置 → 拒跑，不产出误导分数
        print(f"评测中止：{exc}", file=sys.stderr)
        return 2
    path = write_rag_snapshot(report, get_settings())
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    avg = report["summary"].get("avg_score")
    print(f"快照: {path}")
    return 0 if avg is not None and avg >= 0.9 else 1


def cmd_eval(args, _settings: Any) -> int:
    if args.mode == "agent":
        return _eval_agent(args, _settings)
    if args.mode == "rag":
        return _eval_rag(args, _settings)
    from bidqa.eval.smoke import run_smoke
    from bidqa.database.factory import build_repo
    from bidqa.rag.factory import build_retriever

    report = run_smoke(build_repo(), build_retriever(), args.golden)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for r in report["cases"]:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  [{mark}] {r['id']} {r['question'][:36]} located={r['located']}/{r['refs']} "
              f"recalled={r['recalled']} {r['elapsed_ms']}ms")
    return 0 if report["summary"]["located_rate"] == 1.0 else 1
