"""F2 纯文本直入（NIGHT-019）：parse_txt 契约单测——法规采集产物 cleaned.txt
复用 PDF 管道切分契约（markdown + `<!-- page:N -->` → split_markdown → to_ingest_blocks）。

覆盖：正常（头部两变体：横幅行可省）/ 空文 / 长文兜底分块与条级切分 /
元数据头剥离（标注行不得混入正文块）/ 无头直通 / 头部无分隔线不越正文 / CRLF。
落盘一律 tmp_path；零外部调用、零模型调用。tests/ 无 __init__.py，helper 全部内联。
"""

from __future__ import annotations

import pytest

from bidqa.database.qdrant import Chunk, validate_chunk
from bidqa.ingest.enhance_split import split_markdown, to_ingest_blocks
from bidqa.ingest.pdf_parse import parse_txt, strip_meta_header

_META_A = (
    "【语料元数据（本行为标注，非原文）】\n"
    "标题：政府采购货物和服务招标投标管理办法\n"
    "文号：财政部令第87号（2017-07-11公布，2017-10-01施行）\n"
    "来源URL：https://tfs.mof.gov.cn/example.htm\n"
    "说明：标注行，非原文。\n"
)
_META_B = "标题：政府采购质疑和投诉办法\n文号：财政部令第94号\n"  # 变体B：无横幅行
_SEP = "─" * 40 + "\n"
_BODY = (
    "第一章 总 则\n"
    "第一条 为了规范政府采购行为，制定本办法。\n"
    "第二条 本办法所称采购，是指采购人以合同形式有偿取得货物。\n"
)


def _write(tmp_path, text: str, name="cleaned.txt") -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_txt_normal_strips_header_and_blocks(tmp_path):
    doc = parse_txt(_write(tmp_path, _META_A + _SEP + _BODY))
    assert doc["engine"] == "txt"
    assert doc["markdown"].startswith("<!-- page:1 -->\n第一章")
    blocks = split_markdown(doc["markdown"])
    assert len(blocks) == 3                      # 章头 / 第一条 / 第二条
    assert blocks[0].chapter == "第一章"         # 章节归属照常抽取
    assert [b.ref for b in blocks if b.ref] == ["第一条", "第二条"]
    assert all(b.page == 1 for b in blocks)      # 整篇单页口径（同 MinerU 兜底）
    joined = "\n".join(b.text for b in blocks)
    for leak in ("语料元数据", "标题：", "文号：", "来源URL", "说明：", "─"):
        assert leak not in joined                # 标注行不得混入正文块
    assert "第一条 为了规范" in joined


def test_parse_txt_header_variant_without_banner(tmp_path):
    doc = parse_txt(_write(tmp_path, _META_B + _SEP + _BODY))
    joined = "\n".join(b.text for b in split_markdown(doc["markdown"]))
    assert "标题：" not in joined and "文号：" not in joined
    assert joined.startswith("第一章")


def test_parse_txt_empty_body_raises(tmp_path):
    """空文件/空白文件/头部剥完无正文 → ValueError（调用方按解析失败拒收）。"""
    for content in ("", "   \n\n", _META_A + _SEP, _META_A + _SEP + "  \n\t"):
        with pytest.raises(ValueError):
            parse_txt(_write(tmp_path, content, name=f"empty_{len(content)}.txt"))


def test_parse_txt_long_text_fallback_chunks(tmp_path):
    """无章条结构的长文 → split_text 兜底段落（~500 字）分块，契约复用。"""
    body = "本说明段落无章节结构。" * 100  # 1100 字 → ≥3 个兜底块
    doc = parse_txt(_write(tmp_path, body))
    blocks = split_markdown(doc["markdown"])
    assert len(blocks) >= 3
    assert all(b.page == 1 and not b.ref for b in blocks)
    assert "".join(b.text for b in blocks).startswith("本说明段落")


def test_parse_txt_many_clauses_one_block_each(tmp_path):
    """条级切分契约复用：每「第X条」一块，ref 逐条有序（正文内不出现条号字样）。"""
    body = _META_B + _SEP + "第一章 总 则\n" + "".join(
        f"第{n}条 关于{n}情形的具体规定，内容与条文一一对应。\n"
        for n in ("一", "二", "三", "四", "五"))
    doc = parse_txt(_write(tmp_path, body))
    refs = [b.ref for b in split_markdown(doc["markdown"]) if b.ref]
    assert refs == [f"第{n}条" for n in ("一", "二", "三", "四", "五")]


def test_parse_txt_no_header_passthrough(tmp_path):
    doc = parse_txt(_write(tmp_path, _BODY, name="plain.txt"))
    assert doc["markdown"].startswith("<!-- page:1 -->\n第一章")


def test_parse_txt_header_without_separator_stops_at_body(tmp_path):
    """头部无分隔线的退化变体：只剥标注行本身，首个非标注行（正文）起保留。"""
    text = _META_B + "中华人民共和国财政部令第94号\n" + _BODY
    assert strip_meta_header(text).startswith("中华人民共和国财政部令第94号")


def test_parse_txt_crlf(tmp_path):
    """Windows CRLF 落盘同样成立：universal newlines 归一，无 \r 残留。"""
    p = tmp_path / "crlf.txt"
    p.write_bytes((_META_B + _SEP + _BODY).replace("\n", "\r\n").encode("utf-8"))
    blocks = split_markdown(parse_txt(str(p))["markdown"])
    assert len(blocks) == 3
    assert "\r" not in "\n".join(b.text for b in blocks)


def test_parse_txt_payloads_validate(tmp_path):
    """契约串联：parse_txt 产物经切分→payload→Chunk 全部过校验（断线①②）。"""
    doc = parse_txt(_write(tmp_path, _META_A + _SEP + _BODY))
    payloads = to_ingest_blocks(split_markdown(doc["markdown"]),
                                source_type="法规", title="87号令",
                                batch_id="reg-20261121", url="https://t")
    chunks = [Chunk(**p) for p in payloads]
    assert chunks and all(validate_chunk(c) is None for c in chunks)
