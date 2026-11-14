"""F1：Excel/FAQ 导入（docs/02 F1）——问题答案样例 xlsx → Chunk 列表。

统一试运行约定：--dry-run 打印条数 + 抽样 5 条 + 不写库。
列约定：表头行含「问」「答」；问=title（ctx_summary 前缀），答=text；source_type=FAQ。
"""

from __future__ import annotations

import openpyxl

from bidqa.database.qdrant import Chunk


def load_faq_chunks(path: str, batch_id: str, sheet: int = 0) -> tuple[list[Chunk], list[str]]:
    """返回 (chunks, errors)。重复问题去重（保留首个）。"""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.worksheets[sheet]
    rows = [r for r in ws.iter_rows(values_only=True) if r and r[0] is not None]
    wb.close()
    if not rows:
        return [], ["空表"]

    # 跳过表头（首行含「问」或「答」字样）
    if any(h and ("问" in str(h) or "答" in str(h)) for h in rows[0]):
        rows = rows[1:]

    chunks: list[Chunk] = []
    errors: list[str] = []
    seen_q: set[str] = set()
    for i, r in enumerate(rows, start=2):  # xlsx 行号从 2（含表头）
        q, a = (str(r[0]).strip() if r[0] else ""), (str(r[1]).strip() if len(r) > 1 and r[1] else "")
        if not q or not a:
            errors.append(f"第{i}行：问/答存在空值，跳过")
            continue
        if q in seen_q:
            errors.append(f"第{i}行：重复问题已去重「{q[:30]}」")
            continue
        seen_q.add(q)
        chunks.append(Chunk(
            source_type="FAQ", title=q, text=a, ctx_summary=q, batch_id=batch_id,
        ))
    return chunks, errors
