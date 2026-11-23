"""M1-2 灾备通道：jieba BM25 稳定稀疏向量（NIGHT-042 方案 B1 两侧语义）。

查询侧 sparse_query=原始词频；文档侧 sparse_bm25_doc=烘焙 BM25 权重
（tf 饱和 k1 + 长度归一 b，avgdl 显式传入）；两侧点积 ≈ 无 IDF 的 BM25 分数。
"""

import pytest

from bidqa.rag.bm25 import doc_weight, sparse_bm25_doc, sparse_query, tokenize


def test_tokenize_cjk():
    tokens = tokenize("单一来源采购的异议程序")
    assert "单一来源" in tokens or "采购" in tokens


# ── 查询侧：原始词频 ──

def test_query_side_stable_and_raw_tf():
    """确定性 + 索引唯一 + 值=原始词频（≥1 整数；方案 B1：权重烘焙在文档侧）。"""
    a = sparse_query("单一来源采购的异议程序")
    b = sparse_query("单一来源采购的异议程序")
    assert a.indices == b.indices and a.values == b.values  # 确定性
    assert len(a.indices) == len(set(a.indices))            # 索引唯一
    assert all(v >= 1.0 and float(v).is_integer() for v in a.values)  # 词频


def test_query_side_tf_counts_repeats():
    tf = dict(zip(sparse_query("保证金 估算 保证金").indices,
                  sparse_query("保证金 估算 保证金").values))
    assert len(tf) == 2
    assert 2.0 in tf.values() and 1.0 in tf.values()  # 重复词元 qtf=2


def test_query_different_texts_differ():
    assert sparse_query("中标通知书").indices != sparse_query("履约保证金").indices or \
        sparse_query("中标通知书").values != sparse_query("履约保证金").values


# ── 文档侧：烘焙 BM25 权重 ──

def test_doc_weight_formula_anchors():
    """烘焙公式手工值（k1=1.5、b=0.5；docs/01 §4.1 参数表）。"""
    assert doc_weight(1.0, 8, 8) == pytest.approx(1.0)  # dl=avgdl & tf=1 → 词频锚点
    assert doc_weight(10.0, 8, 8) == pytest.approx(10 * 2.5 / (10 + 1.5))   # tf 饱和
    assert doc_weight(1.0, 16, 8) == pytest.approx(2.5 / (1 + 1.5 * 1.5))   # 长块 dl=2·avgdl 被压（norm=1-0.5+0.5*2）
    assert doc_weight(1.0, 4, 8) == pytest.approx(2.5 / (1 + 1.5 * 0.75))   # 短块 dl=0.5·avgdl 被抬（norm=1-0.5+0.5*0.5）
    assert doc_weight(2.0, 3, 0) == pytest.approx(2 * 2.5 / (2 + 1.5))  # avgdl 退化（≤0）：norm=1 不除零


def test_doc_weight_tf_saturates():
    """tf 增量单调递减（饱和）：w(10)-w(9) < w(2)-w(1)。"""
    assert (doc_weight(10.0, 8, 8) - doc_weight(9.0, 8, 8)) < \
        (doc_weight(2.0, 8, 8) - doc_weight(1.0, 8, 8))


def test_doc_bake_idempotent():
    """同输入两次恒等（重算脚本幂等的函数级前提）。"""
    a = sparse_bm25_doc("单一来源采购的异议程序", avgdl=12.0)
    b = sparse_bm25_doc("单一来源采购的异议程序", avgdl=12.0)
    assert a.indices == b.indices and a.values == b.values
    assert len(a.indices) == len(set(a.indices))


def test_doc_bake_values_match_formula():
    """文档向量值 = doc_weight(qtf, dl, avgdl) 逐词元成立；indices 与查询侧同空间。"""
    text = "保证金 估算 保证金"
    q = sparse_query(text)
    dl = len(tokenize(text))
    doc = sparse_bm25_doc(text, avgdl=dl)  # dl=avgdl
    assert doc.indices == q.indices
    assert doc.values == pytest.approx([doc_weight(t, dl, dl) for t in q.values])
    assert 2.0 * 2.5 / (2.0 + 1.5) in doc.values  # tf=2、dl=avgdl → 1.4286（饱和 < 2）


def test_doc_bake_avgdl_normalizes_length():
    """长度归一：dl=avgdl → tf=1 权重恰 1.0；文档偏短（avgdl 大）→ 权重 >1。"""
    text = "保证金 估算"  # dl=2
    at_avg = sparse_bm25_doc(text, avgdl=2.0)
    short = sparse_bm25_doc(text, avgdl=100.0)
    assert all(v == pytest.approx(1.0) for v in at_avg.values)
    assert all(v > 1.0 for v in short.values)


# ── 两侧点积语义回归（NIGHT-037 决定性断言：自洽性） ──

def _dot(text: str, qtf: dict[int, float], avgdl: float) -> float:
    doc = sparse_bm25_doc(text, avgdl=avgdl)
    dmap = dict(zip(doc.indices, doc.values))
    return sum(t * dmap.get(i, 0.0) for i, t in qtf.items())


def _dot_legacy(text: str, qtf: dict[int, float]) -> float:
    """旧口径对照：查询词频 × 文档原始词频点积（NIGHT-042 之前的线上语义）。"""
    doc = sparse_query(text)
    dmap = dict(zip(doc.indices, doc.values))
    return sum(t * dmap.get(i, 0.0) for i, t in qtf.items())


def test_baked_dot_product_self_retrieval_beats_generic_long_doc():
    """自洽性回归（NIGHT-037 §2.1）：块用自身全文查询必须压过泛词高词频长块。

    旧词频点积（查询词频×文档词频）下，泛词「的/，」堆积的长块反超块本身
    （线上最差自召回 106/793）——本测试内嵌旧口径对照，证明断言真能红
    （变异纪律）；烘焙权重后目标块必须反超（新语义绿）。
    """
    target = "投标保证金应当以现金或者保函形式提交，估算金额为采购预算的百分之二"
    distractor = "的，" * 50 + "的，的，的，的，"  # 高词频泛词长块，与 target 仅共享泛词
    docs = [target, distractor]
    avgdl = sum(len(tokenize(d)) for d in docs) / len(docs)
    q = sparse_query(target)
    qtf = dict(zip(q.indices, q.values))
    # 变异对照：旧口径下长泛词块确实反超（断言方向相反 → 断言能红）
    assert _dot_legacy(distractor, qtf) > _dot_legacy(target, qtf)
    # 烘焙口径修复：自召回 rank=1 性质成立
    assert _dot(target, qtf, avgdl) > _dot(distractor, qtf, avgdl)
