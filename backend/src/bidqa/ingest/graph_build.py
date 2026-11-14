"""F5 图谱构建（docs/02）：业务数据 → 实体抽取/归一（LLM，分字段分批）→ Neo4j 五类节点。

约定：先建唯一性约束再导入；归一按字段独立分批（每批 ≤200 名 / ≤4000 字符），
**单批失败只诚实降级该批**并记入 stats，不静默吞掉全局；置信度 <0.85 的合并候选
不自动合并，导出 `data/processed/graph_merge_review_{batch}.csv` 人工复核；
归一映射固定落盘 `data/processed/graph_alias_mapping_{batch}.json`，重建时用
`--mapping-file` 复用（零模型调用）；支持清空重建；`--dry-run` 打条数+抽样+不写库。
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.database.connection import PgConn
from bidqa.database.neo4j import NeoClient

logger = logging.getLogger(__name__)

MERGE_CONFIDENCE = 0.85
# 分批上限：名称数 + 字符数双约束（中文实体名通常 4-30 字，200 名 ≈ 2-6k 字符）
NORMALIZE_BATCH_NAMES = 200
NORMALIZE_BATCH_CHARS = 4000
REVIEW_DIR = Path("data/processed")

# 五类实体字段：抽取、归一、行内改写、映射文件共用同一顺序（单一事实来源）
ENTITY_FIELDS = ("subject_matter", "winner", "purchaser", "agency", "region")
_FIELD_HINTS = {
    "subject_matter": "标的物",
    "winner": "中标供应商",
    "purchaser": "采购人",
    "agency": "代理机构",
    "region": "地区",
}

_NORMALIZE_PROMPT = (
    "你是实体归一器。以下是招投标数据里的实体名列表（同一字段、可能同一实体多种写法）。"
    "输出严格 JSON：{\"groups\": [[\"写法A\",\"写法B\"]], \"confidence\": {\"写法A\": 0.95}}。"
    "只合并明显同一实体的写法（如全称/简写/错别字）；不确定就不要合并。"
)


def extract_entities(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """从业务行抽取五类实体名（确定性；去重保序）。"""
    out: dict[str, list[str]] = {f: [] for f in ENTITY_FIELDS}
    seen: dict[str, set[str]] = {f: set() for f in ENTITY_FIELDS}
    for row in rows:
        for field in ENTITY_FIELDS:
            value = (row.get(field) or "").strip()
            if value and value not in seen[field]:
                seen[field].add(value)
                out[field].append(value)
    return out


def _batch_names(names: list[str], *, max_names: int = NORMALIZE_BATCH_NAMES,
                 max_chars: int = NORMALIZE_BATCH_CHARS) -> Iterator[list[str]]:
    """按名称数/字符数双上限切批；单个超长名称独占一批（不会死循环）。"""
    batch: list[str] = []
    size = 0
    for name in names:
        if batch and (len(batch) >= max_names or size + len(name) > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(name)
        size += len(name)
    if batch:
        yield batch


def _parse_reply(raw: str) -> dict[str, Any]:
    """从模型回复中提取严格 JSON 对象；无 JSON / 非对象 → ValueError（由调用方按批记失败）。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中无 JSON 对象")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("回复 JSON 不是对象")
    return data


def _as_confidence(value: Any) -> float:
    """置信度非法/缺失 → 0.0（进复核，不抛异常）。"""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return conf if conf == conf else 0.0  # NaN 不进自动合并


def _collect_groups(data: dict[str, Any], field: str, batch_names: list[str],
                    mapping: dict[str, dict[str, str]],
                    review: list[dict[str, Any]]) -> int:
    """校验并收集一批的合并组；返回实际采纳的组数。

    模型可能编造名称——凡不属于本批名称集合的一律丢弃（防幻觉污染映射）。
    """
    groups = data.get("groups")
    if not isinstance(groups, list):
        return 0
    conf = data.get("confidence")
    if not isinstance(conf, dict):
        conf = {}
    valid = set(batch_names)
    table = mapping[field]
    merged = 0
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        cleaned = list(dict.fromkeys(str(x).strip() for x in group))
        if len(cleaned) < 2:
            continue
        canonical = cleaned[0]
        if canonical not in valid:
            logger.debug("归一丢弃：标准名不在本批 field=%s canonical=%s", field, canonical)
            continue
        merged += 1
        for alias in cleaned[1:]:
            if not alias or alias == canonical or alias not in valid:
                continue
            if alias in table:  # 重复组：先到先得，避免同一别名两套标准名
                continue
            confidence = _as_confidence(conf.get(alias))
            if confidence >= MERGE_CONFIDENCE:
                table[alias] = canonical
            else:
                review.append({"field": field, "alias": alias,
                               "canonical": canonical, "confidence": confidence})
    return merged


def _collapse(mapping: dict[str, dict[str, str]]) -> None:
    """链式映射收敛（A→B、B→C ⇒ A→C/C），带环保护并清掉自映射。"""
    for table in mapping.values():
        for alias in list(table):
            target = table[alias]
            seen = {alias}
            while target in table and target not in seen:
                seen.add(target)
                target = table[target]
            table[alias] = target
        for alias in [a for a, target in table.items() if a == target]:
            del table[alias]


def normalize_entities(entities: dict[str, list[str]], chat: ChatClient | None,
                       *, batch_size: int = NORMALIZE_BATCH_NAMES
                       ) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]],
                                  dict[str, Any]]:
    """LLM 归一 → (字段作用域映射, 复核清单, 统计)。

    无 LLM：不做合并（恒等映射，零复核项）——诚实降级。
    每批独立调用/解析/校验：单批失败只记 warning + stats，其余批继续生效。
    置信度 <0.85 的候选进复核清单（不自动合并）。
    """
    mapping: dict[str, dict[str, str]] = {f: {} for f in entities}
    review: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "calls": 0, "groups": 0, "failed_batches": [], "fields": {},
    }
    if chat is None:
        return mapping, review, stats

    for field, names in entities.items():
        batches = list(_batch_names(names, max_names=batch_size)) if len(names) >= 2 else []
        stats["fields"][field] = {"names": len(names), "batches": len(batches)}
        for index, batch in enumerate(batches, 1):
            stats["calls"] += 1
            try:
                raw = chat.chat([
                    {"role": "system", "content": _NORMALIZE_PROMPT},
                    {"role": "user",
                     "content": f"字段：{_FIELD_HINTS.get(field, field)}\n"
                                + json.dumps(batch, ensure_ascii=False)},
                ], temperature=0.0)
                data = _parse_reply(raw)
            except Exception as exc:  # 单批失败隔离：不吞全局、不中断后续批
                logger.warning("实体归一失败 field=%s batch=%d/%d names=%d: %s",
                               field, index, len(batches), len(batch), exc)
                stats["failed_batches"].append({
                    "field": field, "batch": index,
                    "names": len(batch), "error": str(exc)[:200],
                })
                continue
            stats["groups"] += _collect_groups(data, field, batch, mapping, review)

    _collapse(mapping)
    return mapping, review, stats


def write_review_csv(review: list[dict[str, Any]], batch: str) -> Path | None:
    if not review:
        return None
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"graph_merge_review_{batch}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["field", "alias", "canonical", "confidence"])
        writer.writeheader()
        writer.writerows(review)
    return path


def write_mapping_json(mapping: dict[str, dict[str, str]], stats: dict[str, Any],
                       batch: str) -> Path:
    """归一映射固定落盘：重建时可直接 --mapping-file 复用，复核回填也改这份文件。"""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / f"graph_alias_mapping_{batch}.json"
    payload = {
        "version": 1,
        "batch": batch,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mapping": mapping,
        "stats": stats,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def load_mapping_file(path: str | Path) -> dict[str, dict[str, str]]:
    """读取归一映射 JSON（校验字段名/字符串值，忽略未知字段）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("映射文件缺少 mapping 对象")
    mapping: dict[str, dict[str, str]] = {f: {} for f in ENTITY_FIELDS}
    for field, table in raw.items():
        if field not in ENTITY_FIELDS:
            logger.warning("映射文件含未知字段 %s，已忽略", field)
            continue
        if not isinstance(table, dict):
            raise ValueError(f"映射文件字段 {field} 不是对象")
        for alias, canonical in table.items():
            if not isinstance(alias, str) or not isinstance(canonical, str) \
                    or not alias.strip() or not canonical.strip():
                raise ValueError(f"映射文件字段 {field} 含非字符串/空映射项")
            mapping[field][alias] = canonical
    _collapse(mapping)
    return mapping


def _apply_mapping(rows: list[dict[str, Any]],
                   mapping: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """行内按字段改写：映射是字段作用域的，同文本在不同栏位互不串用。"""
    if not any(mapping.values()):
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        for field in ENTITY_FIELDS:
            value = row.get(field)
            table = mapping.get(field) or {}
            if isinstance(value, str) and value in table:
                row[field] = table[value]
        out.append(row)
    return out


def _mapping_items(mapping: dict[str, dict[str, str]]) -> int:
    return sum(len(table) for table in mapping.values())


def build_graph(pg: PgConn, neo: NeoClient, chat: ChatClient | None = None,
                batch: str = "default", dry_run: bool = False,
                clear: bool = False, *, normalize: bool = False,
                mapping_file: str | Path | None = None,
                batch_size: int = NORMALIZE_BATCH_NAMES) -> dict[str, Any]:
    """业务库全量行 → 图谱。

    映射来源优先级：mapping_file（零模型调用）> normalize+chat（付费分批归一）
    > 恒等映射（默认）。dry_run：打条数+抽样+不写库；normalize 的产物照常落盘。
    """
    rows = pg.connection().execute(
        "SELECT project_no, project_name, subject_matter, winner, purchaser, "
        "agency, region, award_amount, publish_date FROM bidding_procurement"
    ).fetchall()
    entities = extract_entities(rows)

    review: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"calls": 0, "groups": 0, "failed_batches": [], "fields": {}}
    review_path: Path | None = None
    mapping_path: Path | None = None
    mapping_source = "identity"

    if mapping_file is not None:
        mapping = load_mapping_file(mapping_file)
        mapping_source = str(mapping_file)
    elif normalize and chat is not None:
        mapping, review, stats = normalize_entities(entities, chat, batch_size=batch_size)
        mapping_source = "llm"
        # 先落盘再动 Neo4j：重建失败也不丢这次付费归一的结果
        mapping_path = write_mapping_json(mapping, stats, batch)
        review_path = write_review_csv(review, batch)
    elif normalize:
        logger.warning("normalize=True 但主模型未配置：本次按恒等映射建图")
        mapping = {f: {} for f in ENTITY_FIELDS}
        mapping_source = "identity(no_chat)"
    else:
        mapping = {f: {} for f in ENTITY_FIELDS}

    rows = _apply_mapping(rows, mapping)
    merged_items = _mapping_items(mapping)

    if dry_run:
        print(f"[dry-run] 将写入 {len(rows)} 个 Project（batch={batch}）；"
              f"实体组: { {k: len(v) for k, v in entities.items()} }；"
              f"自动归一 {merged_items} 项（来源={mapping_source}）；"
              f"待人工复核 {len(review)} 项；失败批次 {len(stats['failed_batches'])}")
        for row in rows[:5]:
            print("  ", {k: row.get(k) for k in
                         ("project_no", "subject_matter", "winner", "region")})
        for field in ENTITY_FIELDS:
            for alias, canonical in list(mapping.get(field, {}).items())[:20]:
                print(f"  合并样例 {field}: {alias} -> {canonical}")
        return {"rows": len(rows), "dry_run": True, "mapping": mapping,
                "mapping_items": merged_items, "review": review,
                "review_items": len(review), "mapping_source": mapping_source,
                "normalize_calls": stats["calls"],
                "failed_batches": stats["failed_batches"],
                "mapping_json": str(mapping_path) if mapping_path else None,
                "review_csv": str(review_path) if review_path else None}

    neo.ensure_constraints()  # 约束先行（重跑必重复的已知坑，docs/01 §4.2）
    if clear:
        neo.clear_all()
    for row in rows:
        neo.upsert_project(row)
    return {"rows": len(rows), "mapping": mapping,
            "mapping_items": merged_items, "review": review,
            "review_items": len(review), "mapping_source": mapping_source,
            "normalize_calls": stats["calls"],
            "failed_batches": stats["failed_batches"],
            "mapping_json": str(mapping_path) if mapping_path else None,
            "review_csv": str(review_path) if review_path else None,
            "counts": neo.counts()}
