"""F1：Excel/FAQ 导入（docs/02 F1）——问题答案样例 xlsx → Chunk 列表。

统一试运行约定：--dry-run 打印条数 + 抽样 5 条 + 不写库。
列约定：表头行含「问」「答」；问=title（ctx_summary 前缀），答=text；source_type=FAQ。
可选来源 URL 列（NIGHT-027）：表头含 url/链接/link/网址 字样（大小写不敏感，取首个命中）
时取该列写入 Chunk.url；值须 http(s) 开头，空值/非 URL 文本一律留空不报错；
裸「来源」「来源站点」与「访问日期」类列不算 URL 列。
url 不参与 Chunk.id（id 只由 source_type|title|text 决定），有无该列 id 不变。

去重键（NIGHT-050）：问句先经 `_norm_key` 归一化（NFKC 全半角折叠——覆盖
`?？` `;；` `：:` `，,` 等全半角标点与全角字母数字/空格——再剥全部空白、转小写）
后判重，半角/全角混排与多打空格的同题不再漏判；判定重复时保留**答案更长**
（内容超集）的一条，答案等长保留首条——禁止 first-wins，防截断版挤掉完整版
（20261102 批次 rag_flow_002 教训，docs/02 F1）。保留行的 title/text 原样不动，
Chunk.id 仍由保留行内容决定。
"""

from __future__ import annotations

import unicodedata
from typing import Any

import openpyxl

from bidqa.database.qdrant import Chunk

_URL_HEADER_MARKS = ("url", "链接", "link", "网址")
_URL_SCHEMES = ("http://", "https://")


def _find_url_col(header: tuple[Any, ...]) -> int | None:
    """表头里找来源 URL 列（含 url/链接/link/网址 字样，首个命中）。

    只认 URL 类字样：裸「来源」「来源站点」是站点名、日期列是存档元数据，均不算。
    """
    for idx, h in enumerate(header):
        name = str(h).strip().lower() if h else ""
        if name and any(mark in name for mark in _URL_HEADER_MARKS):
            return idx
    return None


def _clean_url(value: Any) -> str:
    """URL 值清洗：仅接受 http(s) 开头；空值/非 URL 文本留空（不报错、不进 title/text）。"""
    s = str(value).strip() if value is not None else ""
    return s if s.lower().startswith(_URL_SCHEMES) else ""


def _norm_key(q: str) -> str:
    """问句去重键归一化（单入口）：NFKC 全半角折叠 + 剥全部空白 + 小写。

    NFKC 把全角标点/字母/数字折叠成半角（`？`→`?`、`；`→`;`、`：`→`:`、
    `，`→`,`、全角空格→空格等），空白逐字符剥除（中英文题面多打空格不影响
    判重）；仅作去重键，保留行的 title/text 原样不动（id 不受影响）。
    """
    return "".join(ch for ch in unicodedata.normalize("NFKC", q) if not ch.isspace()).lower()


def load_faq_chunks(path: str, batch_id: str, sheet: int = 0) -> tuple[list[Chunk], list[str]]:
    """返回 (chunks, errors)。归一化问句判重，重复时保留答案更长（超集）的一条。"""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[sheet]
    rows = [r for r in ws.iter_rows(values_only=True) if r and r[0] is not None]
    wb.close()
    if not rows:
        return [], ["空表"]

    # 跳过表头（首行含「问」或「答」字样）
    has_header = any(h and ("问" in str(h) or "答" in str(h)) for h in rows[0])
    url_idx = _find_url_col(rows[0]) if has_header else None
    if has_header:
        rows = rows[1:]

    chunks: list[Chunk] = []
    errors: list[str] = []
    # norm_q -> (首现行号, chunks 下标, 保留行答案长度)；重复且更长则原位替换
    seen_q: dict[str, tuple[int, int, int]] = {}
    for i, r in enumerate(rows, start=2):  # xlsx 行号从 2（含表头）
        q, a = (str(r[0]).strip() if r[0] else ""), (str(r[1]).strip() if len(r) > 1 and r[1] else "")
        if not q or not a:
            errors.append(f"第{i}行：问/答存在空值，跳过")
            continue
        url = _clean_url(r[url_idx]) if url_idx is not None and len(r) > url_idx else ""
        key = _norm_key(q)
        if key in seen_q:
            first_row, idx, kept_len = seen_q[key]
            if len(a) > kept_len:  # 本行答案更长（超集）：原位替换保留行，禁止 first-wins
                chunks[idx] = Chunk(
                    source_type="FAQ", title=q, text=a, ctx_summary=q, batch_id=batch_id, url=url,
                )
                seen_q[key] = (first_row, idx, len(a))
                errors.append(f"第{i}行：与第{first_row}行重复问题已去重，保留答案更长的本行「{q[:30]}」")
            else:
                errors.append(f"第{i}行：重复问题已去重「{q[:30]}」")
            continue
        chunks.append(Chunk(
            source_type="FAQ", title=q, text=a, ctx_summary=q, batch_id=batch_id, url=url,
        ))
        seen_q[key] = (i, len(chunks) - 1, len(a))
    return chunks, errors
