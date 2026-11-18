"""WP6：检索排序指标纯函数——精确值断言（变异可红）+ null≠0 口径 + 边界。"""

import pytest

from bidqa.eval.retrieval_metrics import ndcg_at_k, recall_at_k, reciprocal_rank

# nDCG 折扣常量（精确值断言用）：1/log2(3)（排名 2 位）、1/log2(4)（排名 3 位）
D2 = 0.6309297535714575
D3 = 0.5


def test_recall_full_hit_exact():
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0


def test_recall_second_position_and_truncation():
    assert recall_at_k(["x", "a"], {"a"}, 1) == 0.0  # 第 2 位命中，k=1 截断=0
    assert recall_at_k(["x", "a"], {"a"}, 2) == 1.0


def test_recall_all_miss_is_zero():
    assert recall_at_k(["x", "y"], {"a"}, 2) == 0.0  # 全不命中=真实 0 分


def test_recall_empty_relevant_is_none():
    """空 relevant=没测出 → None，绝不冒充 0（AGENTS 硬约束 1）。"""
    assert recall_at_k(["a", "b"], set(), 5) is None


def test_recall_empty_ranked_is_zero_real_signal():
    """检索为空（relevant 非空）=0 是真实失败信号，不是「没测出」。"""
    assert recall_at_k([], {"a"}, 5) == 0.0


def test_recall_k_beyond_list_is_fine():
    assert recall_at_k(["a"], {"a"}, 100) == 1.0


def test_recall_duplicate_ids_dedupe_keeps_order():
    """重复 id 去重保序：[a,a,b] 截 2 后仍含 b（不去重则 [a,a] → 0，断言可红）。"""
    assert recall_at_k(["a", "a", "b"], {"b"}, 2) == 1.0


def test_reciprocal_rank_exact():
    assert reciprocal_rank(["a", "b"], {"a"}) == 1.0
    assert reciprocal_rank(["x", "a", "b"], {"a"}) == 0.5  # 第 2 位命中
    assert reciprocal_rank(["x", "y"], {"a"}) == 0.0
    assert reciprocal_rank(["x", "y"], set()) is None  # 没测出≠0
    assert reciprocal_rank([], {"a"}) == 0.0  # 检索空=真实信号


def test_ndcg_perfect_and_miss_exact():
    assert ndcg_at_k(["a", "b", "c"], {"a", "b"}, 3) == 1.0  # 全命中=1
    assert ndcg_at_k(["x", "y"], {"a"}, 2) == 0.0
    assert ndcg_at_k(["x", "y"], set(), 2) is None  # 没测出≠0
    assert ndcg_at_k([], {"a"}, 2) == 0.0  # 检索空=真实信号


def test_ndcg_second_position_exact():
    """单期望第 2 位命中：DCG=D2，IDCG=1 → nDCG=D2。"""
    assert ndcg_at_k(["x", "a"], {"a"}, 2) == pytest.approx(D2)


def test_ndcg_third_position_truncation_exact():
    """k 截断：期望在第 3 位，k=2 → 0；k=3 → D3=0.5。"""
    ranked = ["a", "b", "c", "d"]
    assert ndcg_at_k(ranked, {"c"}, 2) == 0.0
    assert ndcg_at_k(ranked, {"c"}, 3) == pytest.approx(D3)


def test_ndcg_ideal_caps_at_relevant_count():
    """IDCG 按 min(|relevant|,k) 归一：2 期望只中 1（首位）→ 1/(1+D2)。"""
    got = ndcg_at_k(["a", "x"], {"a", "b"}, 5)
    assert got == pytest.approx(1.0 / (1.0 + D2))


def test_ndcg_multi_hit_two_relevant_exact():
    """[a,b,a] 去重后前 2 位全中 → nDCG=1（重复项不挤占名额）。"""
    assert ndcg_at_k(["a", "b", "a"], {"a", "b"}, 2) == pytest.approx(1.0)


@pytest.mark.parametrize("func", [recall_at_k, ndcg_at_k])
@pytest.mark.parametrize("bad_k", [0, -1, -7])
def test_k_not_positive_raises_value_error(func, bad_k):
    with pytest.raises(ValueError):
        func(["a"], {"a"}, bad_k)
