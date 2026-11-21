"""F2 PDF 解析（页码契约/MinerU 兜底链）+ F3 增强切分（边界/成本公式/批摘要）。"""

from __future__ import annotations

import json
import re

import pytest

from bidqa.ingest.enhance_split import (
    Block,
    enhance_blocks,
    estimate_cost,
    split_markdown,
    split_text,
    to_ingest_blocks,
)
from bidqa.ingest.pdf_parse import parse_pdf, split_pages


# ── 最小合法 PDF 构造器（含 xref，pdfminer 可解析）──
def _build_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode()
    return bytes(out)


# ── F2 ──
def test_pdfplumber_parse_page_contract(tmp_path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_build_pdf("Hello BidQA tender law"))
    result = parse_pdf(str(path))
    assert result["engine"] == "pdfplumber"
    assert "<!-- page:1 -->" in result["markdown"]
    assert "Hello BidQA tender law" in result["markdown"]


def test_mineru_failure_degrades_to_pdfplumber(tmp_path, monkeypatch) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_build_pdf("fallback text"))
    monkeypatch.setattr("bidqa.ingest.pdf_parse._mineru_parse",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("MinerU 上传失败")))
    result = parse_pdf(str(path), api_key="sk-fake")
    assert result["engine"] == "pdfplumber"
    assert result["degraded"].startswith("mineru_fallback")


def test_split_pages_contract() -> None:
    md = "<!-- page:1 -->\n第一页内容\n\n<!-- page:2 -->\n第二页内容"
    assert split_pages(md) == [(1, "第一页内容"), (2, "第二页内容")]
    assert split_pages("无标记文本") == []  # 无标记=无契约页（MinerU 兜底由 parse 层补）


# ── F3 ──
def test_split_text_clause_boundaries() -> None:
    text = "第一章 总则\n第一条 为了规范招标投标活动。\n第十条 本法所称招标。"
    pieces = split_text(text)
    assert len(pieces) == 3
    assert pieces[1].startswith("第一条")
    assert pieces[2].startswith("第十条")


# NIGHT-024 伪条头契约：行文引用（本办法第X条规定的 / 第X条第二款）不得成为条头块。
# 三行素材逐一取自 data/raw/regulations_20261121 真实法规（87号令/613号令/招标投标法），
# 内联固化防止 data 目录变更影响断言（NIGHT-023 正在并发清洗该目录）。
_REG_REAL = (
    "第三章 采购程序\n"
    "第六十五条 评标委员会发现招标文件存在歧义、重大缺陷导致评标工作无法进行，应当停止评标工作。\n"
    "（八）核对评标结果，有本办法第六十四条规定情形的，要求评标委员会复核或者书面说明理由，"
    "评标委员会拒绝的，应予记录并向本级财政部门报告；\n"
    "\u3000\u3000第四十二条\u3000使用通过受让或者租借等方式获取的资格、资质证书投标的，"
    "属于招标投标法第三十三条规定的以他人名义投标。\n"
    "\u3000\u3000投标邀请书应当载明本法第十六条第二款规定的事项。\n"
)

_FAKE_HEAD_RE = re.compile(r"^第[一二三四五六七八九十百零\d]+条(?=\s|$)")


def test_split_text_midline_refs_not_clause_boundaries() -> None:
    """行文引用不得切开成「第X条…」伪条头块；真条头（含全角缩进条头）仍逐条成块。"""
    pieces = split_text(_REG_REAL)
    fake_heads = [p[:14] for p in pieces
                  if p.startswith("第六十四条") or p.startswith("第三十三条")
                  or p.startswith("第十六条")]
    assert fake_heads == []                       # 伪条头块=0（改前此处为伪块列表）
    assert "有本办法第六十四条规定情形" in "\n".join(pieces)   # 引用句保持完整单块
    assert "招标投标法第三十三条规定的以他人名义投标" in "\n".join(pieces)
    assert "载明本法第十六条第二款规定的事项" in "\n".join(pieces)
    heads = [p for p in pieces if p.startswith("第六十五条") or p.startswith("第四十二条")]
    assert len(heads) == 2                        # 真条头不丢（含全角缩进条头）


def test_split_markdown_fake_clause_header_blocks_zero() -> None:
    """ref 命中但块首非真条头形态（条号后须接空白/结尾）的伪条头块=0；真条头 ref 逐条有序。"""
    blocks = split_markdown(f"<!-- page:1 -->\n{_REG_REAL}")
    fake = [(b.ref, b.text[:12]) for b in blocks
            if b.ref and not _FAKE_HEAD_RE.match(b.text)]
    assert fake == []                             # 伪条头块=0（NIGHT-024 自建口径）
    refs = [b.ref for b in blocks if b.ref]
    assert refs == ["第六十五条", "第四十二条"]     # 引用行不占 ref，真条头不丢
    ref_block = next(b for b in blocks if "本办法第六十四条规定情形" in b.text)
    assert ref_block.ref == "第六十五条"           # 引用句归属其所在条文块


def test_split_text_faq_pairs() -> None:
    text = "FAQ 汇总\n问：空调采购怎么立项？\n答：按流程申请。\n问：电梯年检谁负责？\n答：使用单位。"
    pieces = split_text(text)
    assert sum(p.startswith("问：") for p in pieces) == 2


def test_split_markdown_carries_page_chapter_ref() -> None:
    md = ("<!-- page:3 -->\n第三章 招标程序\n第九条 招标人应当有进行招标项目的相应资金。")
    blocks = split_markdown(md)
    assert len(blocks) == 2
    proc = next(b for b in blocks if b.text.startswith("第三章"))
    clause = next(b for b in blocks if b.text.startswith("第九条"))
    assert proc.page == 3 and proc.chapter == "第三章"
    assert clause.ref == "第九条" and clause.page == 3


def test_estimate_cost_matches_doc_formula() -> None:
    """docs/02 F3 口径：5000 块 ×（600 输入+100 输出）≈ 350 万 token。"""
    tin, tout, total = estimate_cost(5000)
    assert (tin, tout, total) == (3_000_000, 500_000, 3_500_000)


class FakeChat:
    """逐批应答：replies 列表按批弹出（批内编号 1..n）。"""

    def __init__(self, replies: list[str] | str | Exception):
        self.replies = [replies] if isinstance(replies, str) else (
            [replies] if isinstance(replies, Exception) else list(replies))
        self.batch_sizes: list[int] = []

    def chat(self, messages, temperature=0.3):
        self.batch_sizes.append(len(messages[-1]["content"].splitlines()))
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_enhance_blocks_batch_summaries() -> None:
    blocks = [Block(text=f"第{i}条 内容{i}") for i in range(5)]
    chat = FakeChat(["1. 摘要a\n2. 摘要b", "1. 摘要c\n2. 摘要d", "1. 摘要e"])
    out = enhance_blocks(blocks, chat, batch_size=2)
    assert [b.ctx_summary for b in out] == ["摘要a", "摘要b", "摘要c", "摘要d", "摘要e"]
    assert chat.batch_sizes == [2, 2, 1]  # 批处理


def test_enhance_blocks_failure_honest_empty() -> None:
    blocks = [Block(text="x"), Block(text="y")]
    chat = FakeChat(RuntimeError("模型超载"))
    out = enhance_blocks(blocks, chat)
    assert all(b.ctx_summary == "" for b in out)  # 失败留空不硬造


def test_enhance_no_chat_identity() -> None:
    blocks = [Block(text="x")]
    assert enhance_blocks(blocks, None) is blocks  # 无 LLM 不动原文（诚实降级）


def test_to_ingest_payload_schema() -> None:
    blocks = [Block(text="正文", page=2, chapter="第三章", ref="第九条", ctx_summary="摘要")]
    payloads = to_ingest_blocks(blocks, source_type="法规", title="招标投标法",
                                batch_id="b1", url="https://x")
    p = payloads[0]
    assert set(p) == {"source_type", "title", "text", "ctx_summary", "chapter",
                      "ref", "page", "url", "batch_id"}  # §4.1 payload schema
    assert p["page"] == 2 and p["batch_id"] == "b1" and json.dumps(p, ensure_ascii=False)
