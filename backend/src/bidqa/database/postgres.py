"""PostgreSQL 客户端（docs/01 §4.3）：schema.sql 幂等建表 + 业务库白名单查询 +
会话/反馈存储。字段白名单直接引用 §4.3 字段表（D3 不另行维护）；LIKE 通配符转义；
参数化 SQL；数值三重收口在图表规格构建处（_finite_int）。
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# docs/01 §4.3 字段表 = 查询/过滤白名单（单一来源，D3 直接引用）
BP_COLUMNS: tuple[str, ...] = (
    "project_no", "project_name", "project_category", "project_stage",
    "purchaser", "purchaser_contact", "agency", "agency_contact",
    "winner", "winner_contact", "award_amount", "content", "location", "region",
    "subject_matter", "source_url", "publish_date", "change_date", "plan_date",
    "award_date", "batch_id",
)
_DATE_COLUMNS = {"publish_date", "change_date", "plan_date", "award_date"}

# filters 中文别名 → 白名单列（docs/01 §4.3 过滤语义。只做键名映射，不新增字段）
FILTER_ALIASES: dict[str, str] = {
    "地区": "region", "区域": "region", "省份": "region", "省": "region",
    "城市": "region", "市区": "region", "省市": "region",
    "标的物": "subject_matter", "品目": "subject_matter", "采购内容": "subject_matter",
    "中标人": "winner", "中标单位": "winner", "供应商": "winner", "中标供应商": "winner",
    "采购人": "purchaser", "招标人": "purchaser", "采购单位": "purchaser",
    "代理机构": "agency", "项目名称": "project_name", "项目编号": "project_no",
    "项目分类": "project_category", "项目阶段": "project_stage",
    "中标金额": "award_amount", "金额": "award_amount", "项目地点": "location",
}

# 包含匹配列：真实数据的粒度混有省/市（「安徽省合肥市」与「安徽」），等值匹配会漏
_CONTAINS_COLUMNS = {"region"}

QUERY_TYPES = (
    "subject_agg",      # 标的物聚合（项目数/总额）
    "region_agg",       # 地区分布
    "supplier_topn",    # 供应商 TopN
    "purchaser_topn",   # 采购人 TopN
    "time_trend",       # 时间趋势（按月）
    "detail_search",    # 明细检索（关键词+过滤）
)


def escape_like(text: str) -> str:
    """LIKE 通配符转义（配 ESCAPE '\\'，docs/02 D3）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _finite_int(value: Any) -> int | None:
    """数值三重收口：布尔不当数字 / 非有限数拦截 / 小数归一（docs/01 §3.7）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return int(round(num))


def build_bp_query(args: dict[str, Any], reverse: bool = False) -> tuple[str, list[Any]]:
    """D3 查询构建（纯函数，单测锁定白名单与转义；非法字段一律抛 ValueError）。

    args: {query_type(6枚举), keyword?, filters?, top_n?, date_range?}
      filters: {白名单列: 标量}（精确匹配）；date_range: {field(白名单日期列), start?, end?}
    reverse: detail_search 关键词反向变体（关键词包含字段值；其余 query_type 忽略）
    """
    query_type = args.get("query_type", "")
    if query_type not in QUERY_TYPES:
        raise ValueError(f"未知 query_type: {query_type}")
    where: list[str] = []
    params: list[Any] = []

    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters 必须是对象")
    for field, value in filters.items():
        col = field if field in BP_COLUMNS else FILTER_ALIASES.get(field)
        if col is None:
            raise ValueError(f"过滤字段不在白名单: {field}")
        if col in _CONTAINS_COLUMNS:
            where.append(f"{col} LIKE %s ESCAPE '\\'")
            params.append(f"%{escape_like(str(value))}%")
        else:
            where.append(f"{col} = %s")
            params.append(value)

    date_range = args.get("date_range") or {}
    if date_range:
        field = date_range.get("field", "publish_date")
        if field not in _DATE_COLUMNS:
            raise ValueError(f"日期字段不在白名单: {field}")
        if start := date_range.get("start"):
            where.append(f"{field} >= %s")
            params.append(start)
        if end := date_range.get("end"):
            where.append(f"{field} <= %s")
            params.append(end)

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    top_n = args.get("top_n")
    if top_n is None:
        top_n = 10
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 100:
        raise ValueError("top_n 须为 1..100 整数")
    keyword = args.get("keyword")

    if query_type in ("subject_agg", "region_agg"):
        group_col = "subject_matter" if query_type == "subject_agg" else "region"
        sql = (
            f"SELECT {group_col} AS label, COUNT(*) AS projects, "
            f"SUM(award_amount) AS total_amount FROM bidding_procurement"
            f"{where_sql} GROUP BY {group_col} ORDER BY projects DESC, label LIMIT %s"
        )
        params.append(top_n)
        return sql, params
    if query_type in ("supplier_topn", "purchaser_topn"):
        group_col = "winner" if query_type == "supplier_topn" else "purchaser"
        sql = (
            f"SELECT {group_col} AS label, COUNT(*) AS projects, "
            f"SUM(award_amount) AS total_amount FROM bidding_procurement"
            f"{where_sql} GROUP BY {group_col} ORDER BY total_amount DESC NULLS LAST, projects DESC LIMIT %s"
        )
        params.append(top_n)
        return sql, params
    if query_type == "time_trend":
        sql = (
            "SELECT to_char(date_trunc('month', publish_date), 'YYYY-MM') AS label, "
            "COUNT(*) AS projects, SUM(award_amount) AS total_amount "
            f"FROM bidding_procurement{where_sql} "
            "GROUP BY 1 ORDER BY 1 LIMIT %s"
        )
        params.append(top_n)
        return sql, params
    # detail_search：keyword LIKE 三列（项目名/中标人/标的物），转义后参数化
    if keyword:
        like = f"%{escape_like(str(keyword))}%"
        if reverse:
            # 反向匹配（参考项目实证）：LLM 关键词常是实际名称的**超集**（如带
            # 「招标公告」后缀），正向 LIKE 落空时改查「关键词包含字段值」。
            # 仅 project_name（中标人/标的物反向噪音大）；char_length>=3 挡超短
            # 字段值造成全表命中；关键词是被匹配串而非模式，%/_ 无需转义。
            # psycopg 参数化查询下 SQL 文本里的字面 % 必须写作 %%
            where.append(
                "(char_length(project_name) >= 3 AND %s ILIKE '%%' || project_name || '%%')")
            params.append(str(keyword))
        else:
            where.append(
                "(project_name LIKE %s ESCAPE '\\' OR winner LIKE %s ESCAPE '\\' "
                "OR subject_matter LIKE %s ESCAPE '\\' OR project_no LIKE %s ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        where_sql = f" WHERE {' AND '.join(where)}"
    order_col = "publish_date"
    sql = (
        f"SELECT {', '.join(BP_COLUMNS)} FROM bidding_procurement{where_sql} "
        f"ORDER BY {order_col} DESC NULLS LAST LIMIT %s"
    )
    params.append(top_n)
    return sql, params


class PgClient:
    """连接惰性建立 + 断线重连；ensure_schema 幂等（docs/01 §4.3 开发友好约定）。

    **autocommit=True（2026-11-08）**：读查询不再滞留「闲置在事务中」——schema.sql
    引入 ALTER TABLE（ACCESS EXCLUSIVE）后，常驻连接若持 AccessShareLock 不放，
    任何进程重跑 ensure_schema 都会永久排队（实测事故：backend 读过 conversations
    后 pytest 全量卡死）。单语句自动提交；多语句写路径用 conn.transaction() 保原子。
    """

    def __init__(self, host: str, port: int, dbname: str, user: str, password: str,
                 connect_timeout: int = 5):
        self._conninfo = (
            f"host={host} port={port} dbname={dbname} user={user} "
            f"password={password} connect_timeout={connect_timeout}"
        )
        self._conn: psycopg.Connection | None = None

    def connection(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self._conninfo, row_factory=dict_row,
                                     autocommit=True)
        return self._conn

    def ensure_schema(self) -> None:
        """schema.sql 单一事实来源：按分号拆分逐条幂等执行（单事务保全有或全无）。"""
        script = SCHEMA_PATH.read_text(encoding="utf-8")
        statements = [s.strip() for s in script.split(";")
                      if s.strip() and not all(
                          line.strip().startswith("--") or not line.strip()
                          for line in s.strip().splitlines())]
        conn = self.connection()
        with conn.transaction():  # autocommit 连接下显式包事务：DDL 全有或全无
            for stmt in statements:
                conn.execute(stmt)

    def ping(self) -> bool:
        try:
            self.connection().execute("SELECT 1")
            return True
        except psycopg.Error:
            self._conn = None
            return False

    def commit(self) -> None:
        self.connection().commit()

    # ── D3 业务库查询 ──
    def query_bp(self, args: dict[str, Any]) -> tuple[list[dict], str]:
        """返回 (rows, sql)；SQL 构建异常原样抛 ValueError（工具层转坏参数）。

        detail_search 正向落空且关键词 ≥3 字时自动反向匹配兜底（关键词包含字段值，
        解决 LLM 传参是实际名称超集的问题）；filters/date_range 在反向变体里原样保留。
        """
        sql, params = build_bp_query(args)
        rows = self.connection().execute(sql, params).fetchall()
        keyword = str(args.get("keyword") or "").strip()
        if (not rows and args.get("query_type") == "detail_search"
                and 3 <= len(keyword) <= 200):
            rsql, rparams = build_bp_query(args, reverse=True)
            rows = self.connection().execute(rsql, rparams).fetchall()
            if rows:
                sql = rsql
        return rows, sql

    # ── F6 upsert（同批次=更新，跨批次=并存）──
    def upsert_bp(self, rows: list[dict[str, Any]], batch_id: str) -> int:
        cols = [c for c in BP_COLUMNS if c != "batch_id"]
        placeholders = ", ".join(f"%s" for _ in cols)
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in ("project_no", "batch_id"))
        updates += ", updated_at = now()"
        sql = (
            f"INSERT INTO bidding_procurement ({', '.join(cols)}, batch_id) "
            f"VALUES ({placeholders}, %s) "
            f"ON CONFLICT (project_no, batch_id) DO UPDATE SET {updates}"
        )
        conn = self.connection()
        count = 0
        with conn.transaction():  # 批量导入原子性：任一行失败整批回滚（autocommit 下显式包）
            for row in rows:
                values = [row.get(c) for c in cols] + [batch_id]
                conn.execute(sql, values)
                count += 1
        return count

    # ── F6-adjacent 标的物写回（subject-batch fetch --write）──
    def update_subject_matter(self, mapping: dict[str, str]) -> int:
        """按 project_name 批量写回标的物（同名行全部更新）；返回实际更新行数。"""
        conn = self.connection()
        updated = 0
        with conn.transaction():  # 批量写回原子性（autocommit 下显式包）
            for name, sm in mapping.items():
                cur = conn.execute(
                    "UPDATE bidding_procurement SET subject_matter = %s, updated_at = now() "
                    "WHERE project_name = %s",
                    (sm, name))
                updated += max(cur.rowcount, 0)
        return updated

    # ── 会话/反馈存储（B1；用户体系后按查看者过滤，docs/01 §3.1 会话隔离）──
    @staticmethod
    def _visibility(viewer_user_id: str | None, machine_domain: bool) -> tuple[str, list]:
        """可见域 SQL 片段：viewer=None+machine_domain=True → 仅 NULL（机器域）；
        viewer=U+True → NULL 或本人（人类 admin）；viewer=U+False → 仅本人（普通用户）；
        viewer=None+False → 不过滤（进程内历史加载等内部调用/旧兼容）。"""
        if viewer_user_id is None and machine_domain:
            return "user_id IS NULL", []
        if viewer_user_id is None:
            return "", []
        if machine_domain:
            return "(user_id IS NULL OR user_id = %s)", [viewer_user_id]
        return "user_id = %s", [viewer_user_id]

    def upsert_conversation(self, conv_id: str, title: str, messages: dict,
                            provider: str = "", user_id: str | None = None) -> None:
        """归属仅在 INSERT 时写入；已存在会话的 owner 不被更新覆盖。"""
        self.connection().execute(
            "INSERT INTO conversations (id, title, messages, provider, user_id, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, "
            "messages = EXCLUDED.messages, provider = EXCLUDED.provider, updated_at = now()",
            (conv_id, title, _json(messages), provider, user_id),
        )
        self.connection().commit()

    def get_conversation(self, conv_id: str, viewer_user_id: str | None = None,
                         machine_domain: bool = False) -> dict | None:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = ("SELECT id::text, title, messages, provider, created_at, updated_at "
               "FROM conversations WHERE id = %s")
        if cond:
            sql += f" AND {cond}"
        row = self.connection().execute(sql, (conv_id, *params)).fetchone()
        return dict(row) if row else None

    def list_conversations(self, limit: int = 50, viewer_user_id: str | None = None,
                           machine_domain: bool = False) -> list[dict]:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = ("SELECT id::text, title, provider, created_at, updated_at "
               "FROM conversations")
        if cond:
            sql += f" WHERE {cond}"
        sql += " ORDER BY updated_at DESC LIMIT %s"
        rows = self.connection().execute(sql, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conv_id: str, viewer_user_id: str | None = None,
                            machine_domain: bool = False) -> bool:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = "DELETE FROM conversations WHERE id = %s"
        if cond:
            sql += f" AND {cond}"
        cur = self.connection().execute(sql, (conv_id, *params))
        self.connection().commit()
        return cur.rowcount > 0

    def rename_conversation(self, conv_id: str, title: str,
                            viewer_user_id: str | None = None,
                            machine_domain: bool = False) -> bool:
        cond, params = self._visibility(viewer_user_id, machine_domain)
        sql = "UPDATE conversations SET title = %s, updated_at = now() WHERE id = %s"
        if cond:
            sql += f" AND {cond}"
        cur = self.connection().execute(sql, (title, conv_id, *params))
        self.connection().commit()
        return cur.rowcount > 0

    # ── B12 全库会话管理（admin 控制台旁路；不走可见域三元组）──
    @staticmethod
    def _empty_shell_cond() -> str:
        """空壳判定：items 里没有 assistant 回复（items 缺失一并视为空壳）；
        调用方需再拼 updated_at 保护窗条件（防误删刚流式完成的活跃会话）。"""
        return ("NOT COALESCE(messages->'items' @> "
                "'[{\"role\":\"assistant\"}]'::jsonb, false)")

    def list_all_conversations(self, limit: int = 200, q: str = "") -> list[dict]:
        """控制台全库列表（LEFT JOIN users 取属主名；q 对 title ILIKE 过滤；
        不带 q 过滤时返回全库）。owner 为 NULL = 机器域（user_id 亦为 NULL）。"""
        sql = ("SELECT c.id::text AS id, c.title, c.provider, c.updated_at, "
               "c.user_id::text AS user_id, u.username AS owner, "
               "c.messages->'items' AS items, "
               "(messages->'items' @> '[{\"role\":\"assistant\"}]'::jsonb) AS has_reply "
               "FROM conversations c LEFT JOIN users u ON u.id = c.user_id")
        params: list[Any] = []
        if q:
            sql += " WHERE c.title ILIKE %s ESCAPE '\\'"
            params.append(f"%{escape_like(q)}%")
        sql += " ORDER BY c.updated_at DESC LIMIT %s"
        params.append(limit)
        return [dict(r) for r in self.connection().execute(sql, params).fetchall()]

    def empty_conversations(self, protect_minutes: int = 10) -> dict[str, Any]:
        """空壳盘点（purge 预览）：{count, samples(标题×≤20)}；保护窗默认 10 分钟。"""
        cond = (f"{self._empty_shell_cond()} "
                "AND updated_at < now() - make_interval(mins => %s)")
        n = int(self.connection().execute(
            f"SELECT count(*) AS n FROM conversations WHERE {cond}",
            (protect_minutes,)).fetchone()["n"])
        samples = [r["title"] for r in self.connection().execute(
            f"SELECT title FROM conversations WHERE {cond} "
            "ORDER BY updated_at DESC LIMIT 20", (protect_minutes,)).fetchall()]
        return {"count": n, "samples": samples}

    def purge_empty_conversations(self, protect_minutes: int = 10) -> int:
        """删除空壳会话（同盘点条件）；返回删除行数。"""
        cur = self.connection().execute(
            f"DELETE FROM conversations WHERE {self._empty_shell_cond()} "
            "AND updated_at < now() - make_interval(mins => %s)", (protect_minutes,))
        self.connection().commit()
        return max(cur.rowcount, 0)

    def insert_feedback(self, conversation_id: str | None, question: str, rating: str,
                        sources: list, trace_id: str = "") -> int:
        if rating not in ("up", "down"):
            raise ValueError("rating 须为 up|down")
        row = self.connection().execute(
            "INSERT INTO feedback (conversation_id, question, rating, sources, trace_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (conversation_id, question, rating, _json(sources), trace_id)).fetchone()
        self.connection().commit()
        return int(row["id"])

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


_RE_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_uuid(value: str) -> bool:
    return bool(_RE_UUID.match(value or ""))
