"""subject-batch 子命令（F6 数据修复三步）：build → run → fetch(--write)。

配方与提示词见 ingest/subject_batch.py（免费档 GLM-4.7-Flash）；本模块是命令流：
取参 → 打印 → 退出码。SQL 下沉（pending_project_names 在 ingest 层）；
run 阶段同步并发 + 退避重试 + 增量落盘（可断点续跑，失败项删行重跑）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from bidqa.config import get_settings


def cmd_subject_batch(args, _settings: Any) -> int:
    from bidqa.ingest.subject_batch import (
        build_requests, default_dir, load_id2name, parse_results, pending_project_names,
        run_sync, write_requests,
    )

    out_dir = Path(args.dir) if args.dir else default_dir()
    s = get_settings()

    if args.action == "build":
        from bidqa.database.factory import build_bp_repo

        repo = build_bp_repo()
        if repo is None:
            print("PostgreSQL 未配置/不可用", file=sys.stderr)
            return 2
        names = pending_project_names(repo)
        reqs = build_requests(names, s.SUBJECT_BATCH_MODEL)
        est_chars = sum(len(r["body"]["messages"][1]["content"]) for r in reqs)
        print(f"待提取 {len(reqs)} 条（去重后）；输入 ≈{est_chars} 字符（{s.SUBJECT_BATCH_MODEL} 免费档）")
        print("── 抽样 3 条 ──")
        for r in reqs[:3]:
            print(f"  {r['custom_id']} → {r['body']['messages'][1]['content'][:80]}…")
        if args.dry_run:
            print("试运行结束，未落盘")
            return 0
        write_requests(out_dir / "requests.jsonl", reqs)
        print(f"已落盘：{out_dir / 'requests.jsonl'}（+ names.json 映射）")
        print("下一步：uv run bidqa subject-batch run")
        return 0

    if args.action == "run":
        if not s.has_subject_batch:
            print("SUBJECT_BATCH_API_KEY 未配置（backend/.env），run 不可用", file=sys.stderr)
            return 2
        req_path = out_dir / "requests.jsonl"
        if not req_path.exists():
            print("requests.jsonl 不存在，先跑 subject-batch build", file=sys.stderr)
            return 2
        from bidqa.clients.chat_factory import build_subject_batch_chat

        client = build_subject_batch_chat()  # 批量通道单一构造入口（与图谱归一共用）
        if client is None:
            print("SUBJECT_BATCH_API_KEY 未配置（backend/.env），run 不可用", file=sys.stderr)
            return 2

        def chat_fn(messages, temperature):  # reasoning_effort=low 关思考流（2026-11-08 实证）
            data = client.chat_raw({
                "model": s.SUBJECT_BATCH_MODEL, "messages": messages,
                "temperature": temperature, "reasoning_effort": "low"})
            return str(data["choices"][0]["message"].get("content") or "")

        reqs = [json.loads(line) for line in req_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        print(f"待执行 {len(reqs)} 条（模型 {s.SUBJECT_BATCH_MODEL}，reasoning_effort=low，"
              f"workers={args.workers}，间隔 {args.delay_ms}ms，退避重试，可断点续跑）")
        report = run_sync(reqs, chat_fn, out_dir / "results_raw.jsonl",
                          workers=args.workers, delay_seconds=args.delay_ms / 1000)
        print(json.dumps(report, ensure_ascii=False))
        if report["failed"]:
            print(f"⚠️ {report['failed']} 条失败（已记行；重跑 run 不会自动重试失败项，"
                  "需删除对应结果行后再跑）")
        print("下一步：uv run bidqa subject-batch fetch")
        return 0

    # fetch：解析本地结果（run 已增量落盘）→ subject_map.json →（--write）写回 PG
    res_path = out_dir / "results_raw.jsonl"
    if not res_path.exists():
        print("results_raw.jsonl 不存在，先跑 subject-batch build", file=sys.stderr)
        return 2
    id2name = load_id2name(out_dir / "requests.jsonl")
    mapping, failures = parse_results(res_path.read_text(encoding="utf-8").splitlines(), id2name)
    (out_dir / "subject_map.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"解析：成功 {len(mapping)}，失败 {len(failures)}")
    for f in failures[:5]:
        print(f"  ! {f}")
    if not args.write:
        print(f"试运行结束：映射已落盘 {out_dir / 'subject_map.json'}，未写库（加 --write 生效）")
        return 0
    from bidqa.database.factory import build_bp_repo

    repo = build_bp_repo()
    if repo is None:
        print("PostgreSQL 未配置/不可用", file=sys.stderr)
        return 2
    updated = repo.update_subject_matter(mapping)
    print(f"已写回 PG：{updated} 行 subject_matter")
    return 0
