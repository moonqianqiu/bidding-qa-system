"""命令行入口：serve / doctor / ingest knowledge / eval（docs/03 M1）。

serve 强制 workers=1（docs/01 §6 硬性约定：限流/在线评测/日志缓冲等进程内状态
假定单进程）。M2 起继续挂 ingest graph/db 与 eval 完整模式。
"""

from __future__ import annotations

import argparse
import json
import sys

from bidqa.config import ConfigError, get_settings


def _eval_agent(args) -> int:
    """G3 离线 Agent 评测：内部函数层（docs/01 §3.6），真实主模型（M2-12 验收）。"""
    from bidqa.agent.loop import AgentLoop
    from bidqa.eval.agent_eval import run_agent_eval, write_snapshot
    from bidqa.service import (
        build_chat, build_neo, build_pg, build_retriever,
    )
    from bidqa.tools.base import ToolRegistry
    from bidqa.tools.db_tool import SearchDatabaseTool
    from bidqa.tools.graph_tool import SearchGraphTool
    from bidqa.tools.knowledge import SearchKnowledgeTool
    from bidqa.tools.runner import ToolRunner

    chat = build_chat()
    if chat is None:
        print("主模型未配置（CHAT_*），无法运行 agent 评测", file=sys.stderr)
        return 2
    registry = ToolRegistry()
    if retriever := build_retriever():
        registry.register(SearchKnowledgeTool(retriever, chat))
    if pg := build_pg():
        registry.register(SearchDatabaseTool(pg))
    if neo := build_neo():
        registry.register(SearchGraphTool(neo))
    runner = ToolRunner(registry)

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


def _subject_batch(args) -> int:
    """F6 数据修复三步（配方与提示词见 ingest/subject_batch.py，免费档 GLM-4.7-Flash）。"""
    from pathlib import Path

    from bidqa.ingest.subject_batch import (
        build_requests, default_dir, load_id2name, parse_results, run_sync, write_requests)
    from bidqa.service import build_pg

    out_dir = Path(args.dir) if args.dir else default_dir()
    s = get_settings()

    if args.action == "build":
        pg = build_pg()
        if pg is None:
            print("PostgreSQL 未配置/不可用", file=sys.stderr)
            return 2
        rows = pg.connection().execute(
            "SELECT DISTINCT project_name FROM bidding_procurement "
            "WHERE subject_matter IS NULL AND length(btrim(project_name)) > 0 ORDER BY 1"
        ).fetchall()
        reqs = build_requests((r["project_name"] for r in rows), s.SUBJECT_BATCH_MODEL)
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
        from bidqa.clients.llm import ChatClient

        client = ChatClient(s.SUBJECT_BATCH_BASE_URL, s.SUBJECT_BATCH_API_KEY,
                            s.SUBJECT_BATCH_MODEL)

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
        print("results_raw.jsonl 不存在，先跑 subject-batch run", file=sys.stderr)
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


def _user(args) -> int:
    """用户体系管理（docs/01 §3.1）：add/reset/disable/enable/list。

    首次调用即触发 build_users_store（schema 幂等建表 + 空表种子管理员）；
    --password 缺省走 getpass 交互（避免口令进 shell 历史）。
    """
    import getpass

    from bidqa.service import build_users_store

    store = build_users_store()
    if store is None:
        print("PostgreSQL 未配置/不可用，用户体系无法操作", file=sys.stderr)
        return 2
    action = args.user_action
    if action == "add":
        password = args.password or getpass.getpass(f"为 {args.name} 设置口令: ")
        uid = store.create_user(args.name.strip(), password, args.role)
        if uid is None:
            print(f"用户已存在：{args.name}", file=sys.stderr)
            return 1
        print(f"已创建 {args.role}: {args.name.strip()}（{uid}）")
        return 0
    if action == "reset":
        password = args.password or getpass.getpass(f"为 {args.name} 设置新口令: ")
        revoked = store.reset_password(args.name.strip(), password)
        if revoked is None:
            print(f"用户不存在：{args.name}", file=sys.stderr)
            return 1
        extra = f"（吊销会话 {revoked} 个）" if revoked else ""
        print(f"已重置口令：{args.name.strip()}{extra}")
        return 0
    if action in ("disable", "enable"):
        if not store.set_disabled(args.name.strip(), action == "disable"):
            print(f"用户不存在：{args.name}", file=sys.stderr)
            return 1
        print(f"已{'停用' if action == 'disable' else '启用'}：{args.name.strip()}")
        return 0
    if action == "list":
        rows = store.list_users()
        if not rows:
            print("（无用户）")
            return 0
        for r in rows:
            print(f"{r['username']:<24} {r['role']:<8} "
                  f"{'停用' if r['disabled'] else '启用':<4} {r['created_at']}")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bidqa", description="招投标智能问答机器人后端")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 API 服务（uvicorn，workers=1）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    sub.add_parser("doctor", help="校验配置并打印能力摘要")

    p_prune = sub.add_parser("prune", help="数据保留清理（H7）")
    p_prune.add_argument("--days", type=int, default=30,
                         help="清理 N 天前的 conversations/feedback（默认 30）")
    p_prune.add_argument("--dry-run", action="store_true", help="只打印将删除的行数")

    p_sub = sub.add_parser("subject-batch",
                           help="标的物批量提取：PG 项目名 → 免费档 GLM-4.7-Flash 同步并发 → 写回（F6 数据修复）")
    p_sub.add_argument("action", choices=["build", "run", "fetch"])
    p_sub.add_argument("--dir", default=None, help="批次工作目录（默认 data/run/subject_batch）")
    p_sub.add_argument("--workers", type=int, default=2, help="run 阶段并发线程数（默认 2）")
    p_sub.add_argument("--delay-ms", type=int, default=500,
                       help="run 阶段全局调用起点间隔毫秒（默认 500，防触发免费档限流）")
    p_sub.add_argument("--write", action="store_true",
                       help="fetch 阶段写回 PG（默认试运行：只解析落盘不写库）")
    p_sub.add_argument("--dry-run", action="store_true", help="build 阶段只打印抽样不落盘")

    p_ingest = sub.add_parser("ingest", help="数据入库（docs/02 F 域）")
    ingest_sub = p_ingest.add_subparsers(dest="ingest_target", required=True)
    p_know = ingest_sub.add_parser("knowledge", help="FAQ/法规文本 → 嵌入入库（F1/F4）")
    p_know.add_argument("--input", required=True, help="xlsx 路径（问/答 两列）")
    p_know.add_argument("--batch-id", default=None, help="溯源批次号（默认按日期生成）")
    p_know.add_argument("--dry-run", action="store_true",
                        help="试运行：打印条数+抽样5条，不写库")
    p_db = ingest_sub.add_parser("db", help="业务 Excel → PostgreSQL（F6）")
    p_db.add_argument("--input", required=True, help="xlsx 路径（表头映射见 ingest/db_import.py）")
    p_db.add_argument("--sheet", default=None, help="工作表名（默认活动表）")
    p_db.add_argument("--batch-id", default=None)
    p_db.add_argument("--dry-run", action="store_true")
    p_graph = ingest_sub.add_parser("graph", help="业务库 → Neo4j 图谱（F5）")
    p_graph.add_argument("--batch-id", default="default")
    p_graph.add_argument("--clear", action="store_true", help="清空重建")
    p_graph.add_argument("--dry-run", action="store_true")

    from bidqa.database.qdrant import SOURCE_TYPES  # 白名单单一来源
    p_pdf = ingest_sub.add_parser("pdf", help="PDF → 页码契约 Markdown → 切分 → 嵌入入库（F2/F3/F4）")
    p_pdf.add_argument("--input", required=True, help="PDF 路径")
    p_pdf.add_argument("--source-type", required=True, choices=SOURCE_TYPES,
                       help="来源类型（白名单见 database/qdrant.py）")
    p_pdf.add_argument("--title", required=True, help="来源标题（Chunk.title / 溯源）")
    p_pdf.add_argument("--batch-id", default=None, help="溯源批次号（默认按日期生成）")
    p_pdf.add_argument("--url", default="", help="来源链接")
    p_pdf.add_argument("--dry-run", action="store_true",
                       help="试运行：打印条数+抽样5条，不写库、不调模型")

    p_eval = sub.add_parser("eval", help="黄金集评测（docs/02 G1）")
    p_eval.add_argument("--golden", required=True, help="黄金集 jsonl 路径")
    p_eval.add_argument("--mode", default="smoke", choices=["smoke", "agent"],
                        help="smoke=来源可定位（M1）；agent=工具循环全链路（M2，G3 内部函数层）")

    p_user = sub.add_parser("user", help="用户体系管理（三角色 admin/user/guest）")
    user_sub = p_user.add_subparsers(dest="user_action", required=True)
    p_uadd = user_sub.add_parser("add", help="建号（仅管理员操作面；本轮 CLI，控制台界面下轮）")
    p_uadd.add_argument("name", help="用户名")
    p_uadd.add_argument("--role", default="user", choices=["admin", "user"])
    p_uadd.add_argument("--password", default=None, help="口令（缺省交互输入）")
    p_ureset = user_sub.add_parser("reset", help="重置口令")
    p_ureset.add_argument("name", help="用户名")
    p_ureset.add_argument("--password", default=None, help="口令（缺省交互输入）")
    p_udis = user_sub.add_parser("disable", help="停用账号（其会话即刻失效）")
    p_udis.add_argument("name", help="用户名")
    p_uena = user_sub.add_parser("enable", help="启用账号")
    p_uena.add_argument("name", help="用户名")
    user_sub.add_parser("list", help="列出用户")

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "serve":
        try:
            settings.validate_core()
        except ConfigError as exc:
            print(f"[bidqa] 启动失败：{exc}", file=sys.stderr)
            return 2
        import uvicorn

        uvicorn.run("bidqa.main:app", host=args.host, port=args.port, workers=1, log_level="info")
        return 0

    if args.command == "ingest":
        from datetime import datetime

        if args.ingest_target == "knowledge":
            from bidqa.ingest.embed_store import ingest_chunks
            from bidqa.ingest.faq_excel import load_faq_chunks
            from bidqa.service import build_embedding, build_repo

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

        if args.ingest_target == "pdf":
            from bidqa.database.qdrant import Chunk
            from bidqa.ingest.embed_store import ingest_chunks
            from bidqa.ingest.enhance_split import (
                enhance_blocks, split_markdown, to_ingest_blocks,
            )
            from bidqa.ingest.pdf_parse import parse_pdf
            from bidqa.service import build_embedding, build_repo

            batch_id = args.batch_id or f"pdf-{datetime.now():%Y%m%d-%H%M%S}"
            try:
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

        if args.ingest_target == "db":
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

        if args.ingest_target == "graph":
            from bidqa.ingest.graph_build import build_graph
            from bidqa.service import build_chat, build_neo, build_pg

            pg, neo = build_pg(), build_neo()
            if pg is None or neo is None:
                print("PG/Neo4j 未配置或不可用", file=sys.stderr)
                return 2
            result = build_graph(pg, neo, chat=build_chat(), batch=args.batch_id,
                                 dry_run=args.dry_run, clear=args.clear)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0

        parser.error("未知 ingest 目标")

    if args.command == "subject-batch":
        return _subject_batch(args)

    if args.command == "user":
        return _user(args)

    if args.command == "prune":
        from datetime import datetime, timedelta

        from bidqa.service import build_pg

        pg = build_pg()
        if pg is None:
            print("PostgreSQL 未配置/不可用", file=sys.stderr)
            return 2
        cutoff = datetime.now() - timedelta(days=args.days)
        conn = pg.connection()
        conv = conn.execute(
            "SELECT count(*) AS c FROM conversations WHERE updated_at < %s", (cutoff,)).fetchone()
        fb = conn.execute(
            "SELECT count(*) AS c FROM feedback WHERE created_at < %s", (cutoff,)).fetchone()
        print(f"将清理 {args.days} 天前的会话 {conv['c']} 条、反馈 {fb['c']} 条")
        if args.dry_run:
            print("试运行结束，未删除")
            return 0
        with conn.transaction():  # 两表清理原子性（autocommit 连接下显式包）
            conn.execute("DELETE FROM conversations WHERE updated_at < %s", (cutoff,))
            conn.execute("DELETE FROM feedback WHERE created_at < %s", (cutoff,))
        print(f"已清理（conversations {conv['c']}，feedback {fb['c']}）")
        return 0

    if args.command == "eval":
        if args.mode == "agent":
            return _eval_agent(args)
        from bidqa.eval.smoke import run_smoke
        from bidqa.service import build_retriever, build_repo

        report = run_smoke(build_repo(), build_retriever(), args.golden)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        for r in report["cases"]:
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"  [{mark}] {r['id']} {r['question'][:36]} located={r['located']}/{r['refs']} "
                  f"recalled={r['recalled']} {r['elapsed_ms']}ms")
        return 0 if report["summary"]["located_rate"] == 1.0 else 1

    # doctor：配置体检
    missing = settings.missing_core()
    if missing:
        print("核心配置缺失: " + ", ".join(missing))
        print("→ 复制 backend/.env.example 为 backend/.env 并填写三槽位")
        return 1
    caps = {
        "thinking": settings.has_thinking, "vision": settings.has_vision,
        "judge": settings.has_judge, "embedding": settings.has_embedding,
        "rerank": settings.has_rerank, "neo4j": settings.has_neo4j,
        "postgres": settings.has_postgres, "web_search": settings.has_web_search,
        "pdf_api": settings.has_pdf_api, "subject_batch": settings.has_subject_batch,
    }
    print("核心配置 OK")
    print("鉴权模式:", "token" if settings.auth_enabled else "dev（未配置 API_AUTH_TOKEN，放行）")
    for name, ok in caps.items():
        print(f"  {name:<10} {'就绪' if ok else '降级（未配置）'}")
    # 语料覆盖度：库里到底有哪几类 source_type（失败记「不可用」，绝不记 0）
    try:
        from bidqa.service import build_repo
        dist = build_repo().source_type_counts()
        print(f"  source_type 分布: {dist or '（无数据）'}")
    except Exception:
        print("  source_type 分布: 不可用")
    # 用户体系态（docs/01 §3.1）：开关/用户数/游客开关/默认口令警告
    if not settings.USERS_ENABLED:
        print("用户体系: 关（USERS_ENABLED=false，单机 token 模式）")
        return 0
    try:
        from bidqa.service import build_users_store
        store = build_users_store()
    except Exception:
        store = None
    if store is None:
        print("  用户体系  降级（PG 不可用，登录不可用）")
        return 0
    try:
        n_users = store.count_users()
        guest = "开" if store.guest_enabled() else "关"
        print(f"  用户体系  就绪（users={n_users}，游客开关={guest}，会话 TTL={settings.SESSION_TTL_HOURS}h）")
        names = {u["username"] for u in store.list_users()}
        if settings.ADMIN_USERNAME in names and settings.ADMIN_PASSWORD == "admin123":
            print("  ⚠ 管理员仍在用默认口令（admin/admin123），请 `uv run bidqa user reset admin --password <新口令>` 改密")
    except Exception as exc:
        print(f"  用户体系  异常（{type(exc).__name__}: {exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
