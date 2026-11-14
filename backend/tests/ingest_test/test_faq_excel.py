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
    assert len(errors) == 2  # 1 去重 + 1 空值
    assert any("去重" in e for e in errors) and any("空值" in e for e in errors)
