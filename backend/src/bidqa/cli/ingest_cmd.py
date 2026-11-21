"""ingest 子命令（docs/02 F 域）：knowledge/pdf/db/graph 四条入库管道的 CLI 接线。

薄壳原则：解析/切分/嵌入/建图逻辑全在 ingest/ 包（本文件只取参、打印、退出码）。
试运行约定：--dry-run 打条数+抽样，不写库、不调模型（F 统一约定）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def cmd_ingest(args, settings: Any) -> int:
    if args.ingest_target == "knowledge":
        return _ingest_knowledge(args)
    if args.ingest_target == "pdf":
        return _ingest_pdf(args, settings)
    if args.ingest_target == "db":
        return _ingest_db(args)
    if args.ingest_target == "graph":
        return _ingest_graph(args)
    raise AssertionError(f"未接线的 ingest 目标: {args.ingest_target}")


def _ingest_knowledge(args) -> int:
    from bidqa.ingest.embed_store import ingest_chunks
    from bidqa.ingest.faq_excel import load_faq_chunks
    from bidqa.database.factory import build_repo
    from bidqa.rag.factory import build_embedding

    batch_id = args.batch_id or f"faq-{datetime.now():%Y%m%d-%H%M%S}"
    chunks, errors = load_faq_chunks(args.input, batch_id)
    print(f"解析：{len(chunks)} 条有效，{len(errors)} 条跳过/去重")
    if args.dry_run:
        print("── dry-run 抽样 5 条 ──")
        for c in chunks[:5]:
            print(f"  [{c.source_type}] {c.title[:40]} | id={c.id[:8]} | text={c.text[:40]}…")
        print("试运行结束，未写库")
        return 0
    for e in errors[:10]:
        print(f"  ! {e}")
    repo = build_repo()
    repo.ensure_collection()
    report = ingest_chunks(repo, chunks, build_embedding())
    print(f"入库完成：总 {report.total}，拒收 {len(report.rejected)}，"
          f"写入 {report.upserted}，dense/sparse {'已写' if report.embedded else '未写（BM25 灾备模式）'}")
    print(f"集合 {repo.collection} 现有 {repo.count()} 点")
    return 0 if not report.rejected else 1


def _ingest_pdf(args, settings: Any) -> int:
    from bidqa.database.factory import build_repo
    from bidqa.database.qdrant import Chunk
    from bidqa.ingest.embed_store import ingest_chunks
    from bidqa.ingest.enhance_split import enhance_blocks, split_markdown, to_ingest_blocks
    from bidqa.ingest.pdf_parse import parse_pdf, parse_txt
    from bidqa.rag.factory import build_embedding

    batch_id = args.batch_id or f"pdf-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        if args.input.lower().endswith(".txt"):
            doc = parse_txt(args.input)  # 纯文本直入（法规采集产物），零外部调用
        else:
            # 未配 MINERU_API_KEY → pdfplumber 本地兜底，零外部调用
            doc = parse_pdf(args.input, api_key=settings.MINERU_API_KEY or None)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 2
    # 本轮不接摘要模型（chat=None）：摘要留空合法入库，零收费
    blocks = enhance_blocks(split_markdown(doc["markdown"]), None)
    chunks = [Chunk(**p) for p in to_ingest_blocks(
        blocks, source_type=args.source_type, title=args.title,
        batch_id=batch_id, url=args.url)]
    degraded = f"，{doc['degraded']}" if doc.get("degraded") else ""
    print(f"解析：{len(chunks)} 块（engine={doc['engine']}{degraded}）")
    if args.dry_run:
        print("── dry-run 抽样 5 条 ──")
        for c in chunks[:5]:
            print(f"  [{c.source_type}] {c.title[:40]} | page={c.page} "
                  f"| ref={c.ref} | text={c.text[:40]}…")
        print("试运行结束，未写库")
        return 0
    repo = build_repo()
    repo.ensure_collection()
    report = ingest_chunks(repo, chunks, build_embedding())
    print(f"入库完成：总 {report.total}，拒收 {len(report.rejected)}，"
          f"写入 {report.upserted}，dense/sparse "
          f"{'已写' if report.embedded else '未写（BM25 灾备模式）'}")
    for e in report.rejected[:10]:
        print(f"  ! {e}")
    print(f"集合 {repo.collection} 现有 {repo.count()} 点")
    return 0 if not report.rejected else 1


def _ingest_db(args) -> int:
    from bidqa.database.factory import build_bp_repo
    from bidqa.ingest.db_import import import_db

    repo = build_bp_repo()
    if repo is None:
        print("PostgreSQL 未配置/不可用", file=sys.stderr)
        return 2
    batch_id = args.batch_id or f"db-{datetime.now():%Y%m%d-%H%M%S}"
    result = import_db(args.input, repo, batch_id, dry_run=args.dry_run,
                       sheet=args.sheet)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _ingest_graph(args) -> int:
    from bidqa.container import build_agent_stack
    from bidqa.database.factory import build_neo, build_pg
    from bidqa.ingest.graph_build import build_graph

    if args.mapping_file and not Path(args.mapping_file).exists():
        print(f"映射文件不存在：{args.mapping_file}", file=sys.stderr)
        return 2
    pg, neo = build_pg(), build_neo()
    if pg is None or neo is None:
        print("PG/Neo4j 未配置或不可用", file=sys.stderr)
        return 2
    # 归一是显式动作：默认走数据加工批量通道（SUBJECT_BATCH_*），--normalize-channel chat 可改主槽位
    chat = None
    if args.normalize:
        if args.normalize_channel == "batch":
            from bidqa.clients.chat_factory import build_subject_batch_chat
            chat = build_subject_batch_chat()
            if chat is None:
                print("SUBJECT_BATCH_API_KEY 未配置（数据加工批量通道），无法执行 --normalize；"
                      "可改用 --normalize-channel chat", file=sys.stderr)
                return 2
        else:
            chat = build_agent_stack()[0]
            if chat is None:
                print("主模型未配置，无法执行 --normalize", file=sys.stderr)
                return 2
    if args.dry_run and not args.normalize and not args.mapping_file:
        print("提示：未启用 --normalize/--mapping-file，本次为恒等映射预览（零模型调用）")
    result = build_graph(pg, neo, chat=chat, batch=args.batch_id,
                         dry_run=args.dry_run, clear=args.clear,
                         normalize=args.normalize, mapping_file=args.mapping_file,
                         scope_batch=args.scope_batch)
    # 不把上千条映射全量打到终端：摘要 + 产物路径（完整内容在 mapping JSON）
    keys = ("rows", "dry_run", "mapping_items", "review_items", "mapping_source",
            "normalize_calls", "normalize_fields", "oversized_groups",
            "failed_batches", "mapping_json", "review_csv", "counts")
    summary = {k: result[k] for k in keys if k in result}
    if args.normalize:
        summary["normalize_channel"] = args.normalize_channel
        summary["normalize_model"] = getattr(chat, "model", None)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0
