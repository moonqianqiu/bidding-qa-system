"""F6 业务库导入（docs/02）：Excel → 字段映射 → 归一（金额唯一入口）→ upsert。

统一试运行约定：--dry-run 打印条数 + 抽样 5 条 + 不写库。
字段映射按中文表头；缺失列记警告清单；金额/日期归一失败收集进 rejected 清单（不中断）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from bidqa.database.postgres import PgClient
from bidqa.ingest.normalize import normalize_amount, normalize_date, normalize_region

# 中文表头 → §4.3 字段（白名单外表头忽略并警告）
HEADER_MAP: dict[str, str] = {
    "项目编号": "project_no", "编号": "project_no",
    "项目名称": "project_name", "名称": "project_name",
    "项目分类": "project_category", "分类": "project_category",
    "项目阶段": "project_stage", "阶段": "project_stage",
    "招标人": "purchaser", "采购人": "purchaser",
    "招标人联系方式": "purchaser_contact", "采购人联系方式": "purchaser_contact",
    "代理机构": "agency", "招标代理机构": "agency",
    "代理机构联系方式": "agency_contact",
    "中标人": "winner", "中标供应商": "winner",
    "中标人联系方式": "winner_contact", "中标供应商联系方式": "winner_contact",
    "中标金额": "award_amount", "中标价格": "award_amount",
    "内容": "content", "招标内容": "content",
    "项目地点": "location", "地点": "location", "项目地址": "location",
    "地区": "region", "区域": "region",
    "省份": "_prov", "市区": "_city", "县城": "_county",  # 组合成 region（省+市两级，决策 #9）
    "标的物": "subject_matter", "采购标的": "subject_matter",
    "来源网址": "source_url", "网址": "source_url", "链接": "source_url",
    "发布时间": "publish_date", "发布日期": "publish_date",
    "变更时间": "change_date", "计划时间": "plan_date",
    "中标时间": "award_date", "中标日期": "award_date",
}


def read_rows(path: str | Path, sheet: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 Excel → (rows, warnings)。金额/地区/日期在此归一（唯一入口）。"""
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    it = ws.iter_rows(values_only=True)  # type: ignore[union-attr]
    try:
        header = [str(c or "").strip() for c in next(it)]
    except StopIteration:
        return [], ["空表"]
    mapped = []
    for col in header:
        field = HEADER_MAP.get(col)
        if col and field is None:
            warnings.append(f"未识别表头（忽略）: {col}")
        mapped.append(field)
    for idx, raw in enumerate(it, start=2):
        if all(v is None or str(v).strip() == "" for v in raw):
            continue
        row: dict[str, Any] = {}
        parts: dict[str, str] = {}
        for field, value in zip(mapped, raw):
            if field is None or value is None:
                continue
            if field.startswith("_"):
                parts[field] = str(value).strip()  # 省市区组合件
            elif field == "award_amount":
                row[field] = normalize_amount(value)  # None=归一失败，保留 None 绝不写 0
            elif field in ("publish_date", "change_date", "plan_date", "award_date"):
                row[field] = normalize_date(value)
            elif field == "region":
                row[field] = normalize_region(value)
            else:
                row[field] = str(value).strip()
        if parts:  # 省+市两级（决策 #9）：省份+市区；县级并入 location 语义不进 region
            row["region"] = (parts.get("_prov", "") + parts.get("_city", "")).strip() or \
                parts.get("_prov", "") + parts.get("_county", "")
        if not row.get("project_no"):
            warnings.append(f"第{idx}行缺项目编号，跳过")
            continue
        rows.append(row)
    wb.close()
    return rows, warnings


def import_db(xlsx_path: str | Path, pg: PgClient, batch_id: str,
              dry_run: bool = False, sheet: str | None = None) -> dict[str, Any]:
    rows, warnings = read_rows(xlsx_path, sheet=sheet)
    if dry_run:
        print(f"[dry-run] 将写入 {len(rows)} 条（batch_id={batch_id}）")
        for row in rows[:5]:
            print("  ", {k: row[k] for k in list(row)[:8]})
        for w in warnings:
            print("  警告:", w, file=sys.stderr)
        return {"rows": len(rows), "dry_run": True, "warnings": warnings}
    written = pg.upsert_bp(rows, batch_id)
    return {"rows": len(rows), "written": written, "warnings": warnings}
