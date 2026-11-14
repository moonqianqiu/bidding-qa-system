"""M1-5/E1：问题分类与 rrf_k（docs/01 §4.1 参数表）。"""

from bidqa.rag.classify import classify


def test_keyword_only():
    kind, k = classify("安徽省最近有哪些安装类项目中标")
    assert kind == "keyword" and k == 30


def test_concept_only():
    kind, k = classify("供应商提出的异议应当在几日内答复")
    assert kind == "concept" and k == 90


def test_mixed():
    kind, k = classify("单一来源采购的项目金额是多少")
    assert kind == "mixed" and k == 60


def test_default_keyword():
    kind, k = classify("今天天气怎么样")
    assert kind == "keyword" and k == 30
