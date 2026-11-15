"""D3 查询构建纯函数（docs/02 D3）：六类查询 → 参数化 SQL。

从 postgres.py 拆出（重构 C2）：纯函数无 IO，单测锁定白名单与转义；
非法字段一律抛 ValueError（工具层转坏参数应答）。
"""

from __future__ import annotations

from typing import Any

from bidqa.database.bp_schema import (
    BP_COLUMNS, CONTAINS_COLUMNS, DATE_COLUMNS, FILTER_ALIASES, QUERY_TYPES,
    escape_like,
)


def build_bp_query(args: dict[str, Any], reverse: bool = False) -> tuple[str, list[Any]]:
    """D3 查询构建（纯函数，单测锁定白名单与转义；非法字段一律抛 ValueError）。

    args: {query_type(6枚举), keyword?, filters?, top_n?, date_range?}
      filters: {白名单列: 标量}（精确匹配）；date_range: {field(白名单日期列), start?, end?}
      keyword: 全部 query_type 生效——四列（项目名/中标人/标的物/编号）片段匹配，
        聚合类=过滤后聚合；reverse 反向变体仅 detail_search（bp_repo 正向落空兜底）
    reverse: detail_search 反向变体（关键词包含字段值；其余 query_type 忽略）
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
        if col in CONTAINS_COLUMNS:
            where.append(f"{col} LIKE %s ESCAPE '\\'")
            params.append(f"%{escape_like(str(value))}%")
        else:
            where.append(f"{col} = %s")
            params.append(value)

    date_range = args.get("date_range") or {}
    if date_range:
        field = date_range.get("field", "publish_date")
        if field not in DATE_COLUMNS:
            raise ValueError(f"日期字段不在白名单: {field}")
        if start := date_range.get("start"):
            where.append(f"{field} >= %s")
            params.append(start)
        if end := date_range.get("end"):
            where.append(f"{field} <= %s")
            params.append(end)

    top_n = args.get("top_n")
    if top_n is None:
        top_n = 10
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 100:
        raise ValueError("top_n 须为 1..100 整数")
    keyword = args.get("keyword")
    # keyword 对全部查询类型生效（docs/01 §4.3）：四列（项目名/中标人/标的物/编号）
    # 片段匹配，转义后参数化；聚合类=过滤后聚合。此前只在 detail_search 分支生效，
    # 聚合类传 keyword 被静默丢弃（「空调的主要供应商」返回过全库 TopN）。唯一例外：
    # detail_search 的 reverse 反向变体（bp_repo 正向落空时触发）让位于反向。
    if keyword and not (reverse and query_type == "detail_search"):
        like = f"%{escape_like(str(keyword))}%"
        where.append(
            "(project_name LIKE %s ESCAPE '\\' OR winner LIKE %s ESCAPE '\\' "
            "OR subject_matter LIKE %s ESCAPE '\\' OR project_no LIKE %s ESCAPE '\\')"
        )
        params.extend([like, like, like, like])
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

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
    # detail_search：正向 keyword 已在上方统一处理；此处只剩反向变体兜底
    # （bp_repo 正向落空且关键词 ≥3 字时触发）
    if keyword and reverse:
        # 反向匹配（参考项目实证）：LLM 关键词常是实际名称的**超集**（如带
        # 「招标公告」后缀），正向 LIKE 落空时改查「关键词包含字段值」。
        # 仅 project_name（中标人/标的物反向噪音大）；char_length>=3 挡超短
        # 字段值造成全表命中；关键词是被匹配串而非模式，%/_ 无需转义。
        # psycopg 参数化查询下 SQL 文本里的字面 % 必须写作 %%
        where.append(
            "(char_length(project_name) >= 3 AND %s ILIKE '%%' || project_name || '%%')")
        params.append(str(keyword))
        where_sql = f" WHERE {' AND '.join(where)}"
    order_col = "publish_date"
    sql = (
        f"SELECT {', '.join(BP_COLUMNS)} FROM bidding_procurement{where_sql} "
        f"ORDER BY {order_col} DESC NULLS LAST LIMIT %s"
    )
    params.append(top_n)
    return sql, params
