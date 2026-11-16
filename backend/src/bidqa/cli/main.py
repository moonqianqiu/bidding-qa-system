"""bidqa 命令行入口（重构 C5 拆包）：argparse 只管接线，handler 一行分发。

serve 强制 workers=1（docs/01 §6 硬性约定：限流/在线评测/日志缓冲等进程内状态
假定单进程）。原 484 行单文件按命令拆为 serve/doctor/prune/user_cmd/eval_cmd/
ingest_cmd/subject_batch_cmd 七个模块。
"""

from __future__ import annotations

import argparse

from bidqa.config import get_settings


def _build_parser() -> argparse.ArgumentParser:
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
    p_sub.add_argument("--batch-id", default=None,
                       help="fetch --write 写回范围：指定批次（P1-16）；缺省=全表按项目名")
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
    p_graph.add_argument("--batch-id", default="default",
                         help="产物命名用的批次标签（mapping/review 文件名；不限定数据范围）")
    p_graph.add_argument("--scope-batch", default=None,
                         help="建图数据范围：指定=只取该批次行（P1-16）；缺省=全表，"
                              "跨批次同号项目在 project_no 唯一键下会合并/覆盖（有告警）")
    p_graph.add_argument("--clear", action="store_true", help="清空重建")
    p_graph.add_argument("--dry-run", action="store_true",
                         help="试运行：打条数+抽样+不写库（单独使用不调模型）")
    g_norm = p_graph.add_mutually_exclusive_group()
    g_norm.add_argument("--normalize", action="store_true",
                        help="执行实体归一（默认走数据加工批量通道 SUBJECT_BATCH_*；产物 "
                             "data/processed/graph_alias_mapping_<batch>.json + "
                             "graph_merge_review_<batch>.csv）")
    g_norm.add_argument("--mapping-file", default=None,
                        help="复用已生成的归一映射 JSON（零模型调用）")
    p_graph.add_argument("--normalize-channel", choices=("batch", "chat"), default="batch",
                         help="归一通道：batch=SUBJECT_BATCH_* 数据加工批量通道（默认）；"
                              "chat=主模型槽位（仅 --normalize 生效）")

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
    p_eval.add_argument("--mode", default="smoke", choices=["smoke", "agent", "rag"],
                        help="smoke=来源可定位（M1）；agent=工具循环全链路（M2，G3 内部函数层）；"
                             "rag=离线 RAGAS 三维判分（G2，判分槽位收费——真跑前报价确认）")

    p_user = sub.add_parser("user", help="用户体系管理（三角色 admin/user/guest）")
    user_sub = p_user.add_subparsers(dest="user_action", required=True)
    p_uadd = user_sub.add_parser("add", help="建号（仅管理员操作面）")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()  # 模块级名：测试 patch cli.main.get_settings 注入

    if args.command == "serve":
        from bidqa.cli.serve import cmd_serve
        return cmd_serve(args, settings)
    if args.command == "doctor":
        from bidqa.cli.doctor import cmd_doctor
        return cmd_doctor(args, settings)
    if args.command == "prune":
        from bidqa.cli.prune import cmd_prune
        return cmd_prune(args, settings)
    if args.command == "user":
        from bidqa.cli.user_cmd import cmd_user
        return cmd_user(args, settings)
    if args.command == "subject-batch":
        from bidqa.cli.subject_batch_cmd import cmd_subject_batch
        return cmd_subject_batch(args, settings)
    if args.command == "ingest":
        from bidqa.cli.ingest_cmd import cmd_ingest
        return cmd_ingest(args, settings)
    if args.command == "eval":
        from bidqa.cli.eval_cmd import cmd_eval
        return cmd_eval(args, settings)
    raise AssertionError(f"未接线的命令: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
