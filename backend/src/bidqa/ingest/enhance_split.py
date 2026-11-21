"""F3 上下文增强切分（docs/02）：条款/章节边界切分 + 每块前缀 LLM 摘要（廉价模型+批处理）。

切分优先级：第X章 → 第X条 → FAQ 问答对 → 兜底段落（~500 字）。
成本口径（docs/02 F3）：块数 ×（约 600 输入 + 100 输出 token）；estimate_cost 单一入口。
摘要失败/无 LLM → ctx_summary 留空（诚实降级，不影响入库）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bidqa.clients.llm import ChatClient
from bidqa.ingest.pdf_parse import split_pages

# 成本公式常量（docs/02 F3：5000 块 ≈ 350 万 token/全量重建）
COST_TOKENS_IN_PER_BLOCK = 600
COST_TOKENS_OUT_PER_BLOCK = 100

_CHAPTER_RE = re.compile(r"(?=第[一二三四五六七八九十百零\d]+章)")
# 条头判定（NIGHT-024）：仅「行首（允许全/半角空格缩进）+ 条号后接空白或结尾」的「第X条」算条头。
# 行文中引用（本办法第X条规定的／第X条第二款）不切开、不赋 ref；章头口径不变（本次只收条头）。
_CLAUSE_RE = re.compile(r"(?m)^[\t 　]*(?=第[一二三四五六七八九十百零\d]+条(?:\s|$))")
_FAQ_Q_RE = re.compile(r"^\s*(?:问|Q)[:：]", re.M)
_CHUNK_FALLBACK = 500
_SUMMARY_PROMPT = (
    "你是检索索引增强器。为每个编号的文本块写 1-2 句上下文摘要前缀（说明该块讲什么、"
    "适用什么情形），不重复原文词句堆砌。按编号逐行输出：`n. 摘要`。"
)


@dataclass
class Block:
    text: str
    page: int | None = None
    chapter: str = ""
    ref: str = ""  # 条款号（如 第九条）
    ctx_summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def estimate_cost(blocks: int) -> tuple[int, int, int]:
    """按 03 §A3/docs/02 F3 公式：(块数×600输入, 块数×100输出, 合计 token)。"""
    tin = blocks * COST_TOKENS_IN_PER_BLOCK
    tout = blocks * COST_TOKENS_OUT_PER_BLOCK
    return tin, tout, tin + tout


def _ref_of(text: str) -> str:
    """块首条号 → ref（与 _CLAUSE_RE 同一判定：条号后须接空白/结尾，防引用块误标，NIGHT-024）。"""
    m = re.match(r"\s*(第[一二三四五六七八九十百零\d]+条)(?=\s|$)", text)
    return m.group(1) if m else ""


def split_text(text: str) -> list[str]:
    """单页文本 → 块序列（章节 > 条款 > FAQ > 兜底段落）。"""
    chapters = [c for c in _CHAPTER_RE.split(text) if c.strip()]
    pieces: list[str] = []
    for chapter in chapters or [text]:
        clauses = [c for c in _CLAUSE_RE.split(chapter) if c.strip()]
        if len(clauses) > 1:
            pieces.extend(clauses)
            continue
        if _FAQ_Q_RE.search(chapter):
            faqs = _FAQ_Q_RE.split(chapter)
            head = faqs[0].strip()
            if head:
                pieces.append(head)
            pieces.extend(f"问：{q.strip()}" for q in faqs[1:] if q.strip())
            continue
        for i in range(0, len(chapter), _CHUNK_FALLBACK):
            seg = chapter[i:i + _CHUNK_FALLBACK].strip()
            if seg:
                pieces.append(seg)
    return pieces


def split_markdown(markdown: str) -> list[Block]:
    """契约输入（`<!-- page:N -->`）→ Block 列表（带页码/章节/条款号）。"""
    blocks: list[Block] = []
    for page_no, page_text in split_pages(markdown):
        for piece in split_text(page_text):
            chapter_m = re.search(r"(第[一二三四五六七八九十百零\d]+章)[^\n]*", piece)
            blocks.append(Block(text=piece.strip(), page=page_no,
                                chapter=chapter_m.group(1) if chapter_m else "",
                                ref=_ref_of(piece)))
    return blocks


def enhance_blocks(blocks: list[Block], chat: ChatClient | None,
                   batch_size: int = 16) -> list[Block]:
    """批量生成块前缀摘要（廉价模型；失败逐批降级为空摘要）。"""
    if chat is None or not blocks:
        return blocks
    for start in range(0, len(blocks), batch_size):
        batch = blocks[start:start + batch_size]
        numbered = "\n".join(f"{i + 1}. {b.text[:400]}" for i, b in enumerate(batch))
        try:
            raw = chat.chat([
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": numbered},
            ], temperature=0.2)
            summaries: dict[int, str] = {}
            for line in raw.splitlines():
                m = re.match(r"\s*(\d+)[.、]\s*(.+)", line.strip())
                if m:
                    summaries[int(m.group(1))] = m.group(2).strip()
            for i, b in enumerate(batch):
                b.ctx_summary = summaries.get(i + 1, "")
        except Exception:
            for b in batch:  # 诚实降级：摘要留空
                b.ctx_summary = ""
    return blocks


def to_ingest_blocks(blocks: list[Block], *, source_type: str, title: str,
                     batch_id: str, url: str = "") -> list[dict[str, Any]]:
    """→ F4 payload（docs/01 §4.1 schema；ctx_summary 参与编码由 embed 侧拼装）。"""
    payloads = []
    for b in blocks:
        payloads.append({
            "source_type": source_type, "title": title,
            "text": b.text, "ctx_summary": b.ctx_summary,
            "chapter": b.chapter, "ref": b.ref, "page": b.page,
            "url": url, "batch_id": batch_id,
        })
    return payloads
