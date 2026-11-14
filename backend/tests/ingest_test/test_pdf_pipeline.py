"""F2/F3 全链路串测：parse_pdf → split_markdown → enhance_blocks →
to_ingest_blocks → Chunk(**p) → ingest_chunks（:memory: 集合）。

本次接线的核心验收：库里不再清一色 FAQ（source_type_counts 出现「法规」）。
落盘一律 tmp_path；parse_pdf 不带 key 直接走 pdfplumber 本地兜底、零外部调用；
摘要用 None 或内联 fake，不触网、不调模型。tests/ 无 __init__.py，helper 全部内联。
"""

from __future__ import annotations

from bidqa.database.qdrant import Chunk, QdrantRepo, validate_chunk
from bidqa.ingest.embed_store import ingest_chunks
from bidqa.ingest.enhance_split import enhance_blocks, split_markdown, to_ingest_blocks
from bidqa.ingest.pdf_parse import parse_pdf


def _build_pdf(text: str) -> bytes:
    """最小合法 PDF（含 xref，pdfminer 可解析）——内联，tests/ 无 __init__.py。"""
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


class _FakeChat:
    """按批内编号返回摘要，不触网（覆盖「有摘要」路径）。"""

    def chat(self, messages, temperature=0.3):
        n = len(messages[-1]["content"].splitlines())
        return "\n".join(f"{i + 1}. 摘要{i + 1}" for i in range(n))


def test_pdf_pipeline_end_to_end_writes_non_faq_source_type(tmp_path):
    path = tmp_path / "law.pdf"
    path.write_bytes(_build_pdf("Article 1 General provisions for tender validity."))

    doc = parse_pdf(str(path))
    assert doc["engine"] == "pdfplumber"          # 无 key → 本地兜底，零外部调用
    assert "<!-- page:1 -->" in doc["markdown"]

    blocks = enhance_blocks(split_markdown(doc["markdown"]), None)  # 本轮 chat=None
    assert blocks
    payloads = to_ingest_blocks(blocks, source_type="法规", title="招标投标法",
                                batch_id="b1", url="")
    chunks = [Chunk(**p) for p in payloads]       # 断线①适配（与 CLI 分支同一写法）
    assert all(validate_chunk(c) is None for c in chunks)  # 断线②：空摘要不再被拒收

    repo = QdrantRepo(url=":memory:", collection="test_pdf_pipeline_v1")
    repo.ensure_collection()
    try:
        report = ingest_chunks(repo, chunks, embedding=None)  # 灾备单路，零嵌入调用
        assert report.rejected == [] and report.upserted == len(chunks)
        dist = repo.source_type_counts()
    finally:
        repo.close()

    assert dist.get("法规") == len(chunks)
    assert "FAQ" not in dist                     # 库里不再清一色 FAQ


def test_pipeline_with_summaries_still_validates():
    md = ("<!-- page:1 -->\n第一章 总则\n第一条 为了规范招标投标活动。\n"
          "第二条 本法所称招标，是指招标人公开邀请不特定法人投标。")
    blocks = enhance_blocks(split_markdown(md), _FakeChat())
    assert blocks and all(b.ctx_summary for b in blocks)
    payloads = to_ingest_blocks(blocks, source_type="法规", title="招标投标法",
                                batch_id="b1")
    assert all(validate_chunk(Chunk(**p)) is None for p in payloads)
