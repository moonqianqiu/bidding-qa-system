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
        report = run_agent_eval(args.golden, loop_factory, get_settings())
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
    ok = (summary["tool_selection_accuracy"] or 0) >= 0.9 \
        and (summary["hit_cap_rate"] or 0) < 0.10 \
        and (summary["clarify_trigger_rate"] in (None, 1.0))
    return 0 if ok else 1


def cmd_eval(args, _settings: Any) -> int:
    if args.mode == "agent":
        return _eval_agent(args, _settings)
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
