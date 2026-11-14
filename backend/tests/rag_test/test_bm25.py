"""M1-2 灾备通道：jieba BM25 稳定稀疏向量。"""

from bidqa.rag.bm25 import sparse_bm25, tokenize


def test_tokenize_cjk():
    tokens = tokenize("单一来源采购的异议程序")
    assert "单一来源" in tokens or "采购" in tokens


def test_sparse_stable_and_informative():
    a = sparse_bm25("单一来源采购的异议程序")
    b = sparse_bm25("单一来源采购的异议程序")
    assert a.indices == b.indices and a.values == b.values  # 确定性
    assert len(a.indices) == len(set(a.indices))            # 索引唯一
    assert all(v >= 1.0 for v in a.values)                  # 词频 ≥1


def test_sparse_different_texts_differ():
    assert sparse_bm25("中标通知书").indices != sparse_bm25("履约保证金").indices or \
        sparse_bm25("中标通知书").values != sparse_bm25("履约保证金").values
