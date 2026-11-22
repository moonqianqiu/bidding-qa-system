"""M1-3：Excel/FAQ 导入（表头跳过、去重、空值跳过）。"""

import openpyxl

from bidqa.ingest.faq_excel import load_faq_chunks


def make_xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_load_faq_chunks(tmp_path):
    x = tmp_path / "faq.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["单一来源可以联合体吗？", "一般不可以。"],
        ["单一来源可以联合体吗？", "重复问题应去重。"],
        ["只有问题没有答案", None],
        ["中标通知书效力如何？", "对招标人和中标人具有法律效力。"],
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 2
    assert chunks[0].source_type == "FAQ"
    assert chunks[0].title == "单一来源可以联合体吗？"
    assert chunks[0].ctx_summary == chunks[0].title
    assert chunks[0].batch_id == "b1"
    assert chunks[0].url == ""  # 无来源列：url 留空，行为与引入该特性前一致
    assert len(errors) == 2  # 1 去重 + 1 空值
    assert any("去重" in e for e in errors) and any("空值" in e for e in errors)


def test_load_faq_chunks_url_archive_layout(tmp_path):
    """存档 5 列布局（问/答/来源/来源URL/访问日期）：只取来源URL列。

    空值/纯空白/非 URL 文本不崩溃、url 留空；「来源」（站点名）与「访问日期」不误取。
    """
    x = tmp_path / "faq_archive.xlsx"
    make_xlsx(x, [
        ["问", "答", "来源", "来源URL", "访问日期"],
        ["单一来源可以联合体吗？", "一般不可以。", "某站点", "https://example.com/a", "2026-11-21"],
        ["发布商品链接怎么填？", "填商品链接。", "某站点", "  ", "2026-11-21"],
        ["Q3？", "A3。", "某站点", "不是链接的文字", "2026-11-21"],
        ["Q4？", "A4。", "某站点", None, "2026-11-21"],
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert errors == []
    assert len(chunks) == 4
    assert [c.url for c in chunks] == ["https://example.com/a", "", "", ""]
    assert chunks[0].title == "单一来源可以联合体吗？"
    assert chunks[0].text == "一般不可以。"


def test_load_faq_chunks_url_trap_headers_not_taken(tmp_path):
    """裸「来源」「来源站点」「访问日期」「采集日期」列不得误取为 URL（NIGHT-027 验收 2）。"""
    for i, header in enumerate(("来源", "来源站点", "访问日期", "采集日期")):
        x = tmp_path / f"trap_{i}.xlsx"
        make_xlsx(x, [
            ["问", "答", header],
            ["Q1？", "A1。", "政采云帮助中心（徽采云专区）"],
        ])
        chunks, _ = load_faq_chunks(str(x), batch_id="b1")
        assert len(chunks) == 1
        assert chunks[0].url == "", header


def test_load_faq_chunks_url_header_variants(tmp_path):
    """url/URL/Link/链接/网址 表头变体均识别（大小写不敏感），值前后空白剥离。"""
    for i, header in enumerate(("url", "URL", "Link", "来源链接", "来源网址")):
        x = tmp_path / f"variant_{i}.xlsx"
        make_xlsx(x, [
            ["问", "答", header],
            ["Q1？", "A1。", " https://example.com/v "],
        ])
        chunks, _ = load_faq_chunks(str(x), batch_id="b1")
        assert chunks[0].url == "https://example.com/v", header


def test_load_faq_chunks_url_id_unchanged(tmp_path):
    """加/不加来源URL列，Chunk.id 相同（id 只由 source_type|title|text 决定，NIGHT-027 验收 3）。"""
    with_url = tmp_path / "with_url.xlsx"
    make_xlsx(with_url, [
        ["问", "答", "来源URL"],
        ["Q1？", "A1。", "https://example.com/x"],
    ])
    without_url = tmp_path / "without_url.xlsx"
    make_xlsx(without_url, [
        ["问", "答"],
        ["Q1？", "A1。"],
    ])
    c1, _ = load_faq_chunks(str(with_url), batch_id="b1")
    c2, _ = load_faq_chunks(str(without_url), batch_id="b1")
    assert len(c1) == len(c2) == 1
    assert c1[0].id == c2[0].id
    assert c1[0].url == "https://example.com/x"
    assert c2[0].url == ""


def test_load_faq_chunks_url_row_shorter_than_header(tmp_path):
    """数据行比表头短（URL 单元格缺失）时不崩溃、url 留空。"""
    x = tmp_path / "short_row.xlsx"
    make_xlsx(x, [
        ["问", "答", "来源URL"],
        ["Q1？", "A1。"],  # 缺 URL 单元格
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert errors == []
    assert len(chunks) == 1
    assert chunks[0].url == ""


# ── 去重键归一化（NIGHT-050）：半角/全角与空白变体判重 + 保留超集 ──


def test_load_faq_chunks_norm_question_mark_variant(tmp_path):
    """问号半/全角（? 与 ？）视为同题（20261102 批次 rag_flow_002 教训）。"""
    x = tmp_path / "qmark.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["单一来源采购方式公示被“质疑”应该怎么办?", "完整答案含法条。"],
        ["单一来源采购方式公示被“质疑”应该怎么办？", "截断答案。"],
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 1
    assert chunks[0].text == "完整答案含法条。"  # 重复判定成立保留更长答案
    assert len(errors) == 1 and "去重" in errors[0]


def test_load_faq_chunks_norm_semicolon_variant(tmp_path):
    """分号半/全角（; 与 ；）混排问句判重。"""
    x = tmp_path / "semi.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["成交结果公告后;如何质疑？", "答案A较长版。"],
        ["成交结果公告后；如何质疑?", "答案B。"],
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 1
    assert chunks[0].text == "答案A较长版。"
    assert len(errors) == 1 and "去重" in errors[0]


def test_load_faq_chunks_norm_whitespace_variant(tmp_path):
    """空格/全角空格等空白变体判重（剥全部空白后同题）。"""
    x = tmp_path / "ws.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["中标通知书 效力如何？", "更长的完整答案。"],
        ["中标通知书效力　如何?", "短答案。"],
    ])
    chunks, _ = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 1
    assert chunks[0].text == "更长的完整答案。"


def test_load_faq_chunks_keep_superset_replaces_truncated_first(tmp_path):
    """截断版在前、完整版在后：保留超集（替换首条），禁止 first-wins。

    若简单保留首条，截断行入库后 Chunk.id 变化会挤掉 gold 引用
    （gold 5b98714c = 完整版内容寻址 id，NIGHT-048 §6 方案A）。
    """
    x = tmp_path / "truncated_first.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["质疑期限是多久?", "截断答案。"],                       # 半角问号 + 短答案（在前）
        ["质疑期限是多久？", "完整答案：" + "法条原文。" * 10],  # 全角问号 + 超集答案
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 1
    assert chunks[0].text == "完整答案：" + "法条原文。" * 10
    assert chunks[0].title == "质疑期限是多久？"  # 保留行内容原样（含全角问号）
    assert any("保留答案更长的本行" in e for e in errors)


def test_load_faq_chunks_keep_first_when_duplicate_shorter(tmp_path):
    """正常首次保留：重复行答案不更长（等长/更短）时保留首条。"""
    x = tmp_path / "first_kept.xlsx"
    make_xlsx(x, [
        ["问", "答"],
        ["如何申请非预算指标？", "首条完整答案。"],
        ["如何申请非预算指标?", "更短的答案。"],
        ["如何申请非预算指标？", "等长答案XX。"],
    ])
    chunks, errors = load_faq_chunks(str(x), batch_id="b1")
    assert len(chunks) == 1
    assert chunks[0].text == "首条完整答案。"
    assert chunks[0].title == "如何申请非预算指标？"
    assert len(errors) == 2  # 两条重复行都只记去重，无替换


def test_load_faq_chunks_dedup_id_unchanged(tmp_path):
    """去重/替换不改写保留行内容：kept id == 该行单独成表时的 Chunk.id。"""
    pair = tmp_path / "pair.xlsx"
    make_xlsx(pair, [
        ["问", "答"],
        ["公示被质疑怎么办?", "截断答案。"],
        ["公示被质疑怎么办？", "完整答案含法条依据原文。"],
    ])
    chunks, _ = load_faq_chunks(str(pair), batch_id="b1")
    single = tmp_path / "single.xlsx"
    make_xlsx(single, [
        ["问", "答"],
        ["公示被质疑怎么办？", "完整答案含法条依据原文。"],
    ])
    alone, _ = load_faq_chunks(str(single), batch_id="b1")
    assert len(chunks) == 1 and len(alone) == 1
    assert chunks[0].id == alone[0].id  # id 只由保留行 source_type|title|text 决定
    assert chunks[0].text == alone[0].text
