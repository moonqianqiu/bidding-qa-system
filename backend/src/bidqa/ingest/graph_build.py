"""F5 图谱构建（docs/02）：业务数据 → 实体抽取/归一（LLM，批）→ Neo4j 五类节点。

约定：先建唯一性约束再导入；归一置信度 <0.85 的合并候选**不自动合并**，导出
`data/processed/graph_merge_review_{batch}.csv` 人工复核（回填后重跑归一）；
支持清空重建；--dry-run 打条数+抽样+不写库。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.database.neo4j import NeoClient
from bidqa.database.postgres import PgClient

MERGE_CONFIDENCE = 0.85
REVIEW_DIR = Path("data/processed")

_NORMALIZE_PROMPT = (
    "你是实体归一器。以下是招投标数据里的实体名列表（同栏位、可能同一实体多种写法）。"
    "输出严格 JSON：{\"groups\": [[\"写法A\",\"写法B\"]], \"confidence\": {\"写法A\": 0.95}}。"
    "只合并明显同一实体的写法（如全称/简写/错别字）；不确定就不要合并。"
)


def extract_entities(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """从业务行抽取五类实体名（确定性；去重保序）。"""
    fields = ("subject_matter", "winner", "purchaser", "agency", "region")
    out: dict[str, list[str]] = {f: [] for f in fields}
    seen: dict[str, set[str]] = {f: set() for f in fields}
    for row in rows:
        for field in fields:
            value = (row.get(field) or "").strip()
            if value and value not in seen[field]:
                seen[field].add(value)
                out[field].append(value)
    return out


def normalize_entities(entities: dict[str, list[str]],
                       chat: ChatClient | None) -> tuple[dict[str, str], list[dict]]:
    """LLM 归一 → (原名→归一名映射, 复核清单)。

    无 LLM/解析失败：不做合并（恒等映射，零复核项）——诚实降级。
    置信度 <0.85 的候选进复核清单（不自动合并）。
    """
    mapping: dict[str, str] = {}
    review: list[dict] = []
    if chat is None:
        return mapping, review
    names = [n for vs in entities.values() for n in vs]
    if len(names) < 2:
        return mapping, review
    try:
        raw = chat.chat([
            {"role": "system", "content": _NORMALIZE_PROMPT},
            {"role": "user", "content": json.dumps(names, ensure_ascii=False)},
        ], temperature=0.0)
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        groups = data.get("groups") or []
        conf = data.get("confidence") or {}
    except Exception:
        return mapping, review
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        canonical = str(group[0]).strip()
        for alias in group[1:]:
            alias = str(alias).strip()
            if not alias or alias == canonical:
                continue
            confidence = float(conf.get(alias, 0.0))
            if confidence >= MERGE_CONFIDENCE:
                mapping[alias] = canonical
            else:
                review.append({"alias": alias, "canonical": canonical,
                               "confidence": confidence})
    return mapping, review


def write_review_csv(review: list[dict], batch: str) -> Path | None:
    if not review:
        return None
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"graph_merge_review_{batch}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["alias", "canonical", "confidence"])
        writer.writeheader()
        writer.writerows(review)
    return path


def _apply_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    if not mapping:
        return rows
    out = []
    for row in rows:
        row = dict(row)
        for field in ("subject_matter", "winner", "purchaser", "agency", "region"):
            value = row.get(field)
            if value in mapping:
                row[field] = mapping[value]
        out.append(row)
    return out


def build_graph(pg: PgClient, neo: NeoClient, chat: ChatClient | None = None,
                batch: str = "default", dry_run: bool = False,
                clear: bool = False) -> dict[str, Any]:
    """业务库全量行 → 图谱。dry_run：打条数+抽样+不写库。"""
    rows = pg.connection().execute(
        "SELECT project_no, project_name, subject_matter, winner, purchaser, "
        "agency, region, award_amount, publish_date FROM bidding_procurement"
    ).fetchall()
    entities = extract_entities(rows)
    mapping, review = normalize_entities(entities, chat)
    rows = _apply_mapping(rows, mapping)

    if dry_run:
        print(f"[dry-run] 将写入 {len(rows)} 个 Project（batch={batch}）；"
              f"实体组: { {k: len(v) for k, v in entities.items()} }；"
              f"自动归一 {len(mapping)} 项；待人工复核 {len(review)} 项")
        for row in rows[:5]:
            print("  ", {k: row.get(k) for k in
                         ("project_no", "subject_matter", "winner", "region")})
        return {"rows": len(rows), "dry_run": True, "mapping": mapping, "review": review}

    neo.ensure_constraints()  # 约束先行（重跑必重复的已知坑，docs/01 §4.2）
    if clear:
        neo.clear_all()
    for row in rows:
        neo.upsert_project(row)
    review_path = write_review_csv(review, batch)
    return {"rows": len(rows), "mapping": mapping, "review": review,
            "review_csv": str(review_path) if review_path else None,
            "counts": neo.counts()}
