"""M1-1/F4：Qdrant 仓库层 + payload 校验（local mode，无需服务）。"""

from typing import cast

import pytest
from qdrant_client import models

from bidqa.database.qdrant import (
    Chunk, QdrantRepo, validate_chunk,
)


def make_chunk(**kw) -> Chunk:
    base = dict(source_type="FAQ", title="测试问题", text="测试答案内容",
                ctx_summary="测试问题", batch_id="b1")
    base.update(kw)
    return Chunk(**base)


def test_validate_chunk_rejections():
    assert validate_chunk(make_chunk()) is None
    assert "source_type" in validate_chunk(make_chunk(source_type="新闻"))
    assert "title" in validate_chunk(make_chunk(title=" "))
    assert "text" in validate_chunk(make_chunk(text=""))
    assert "batch_id" in validate_chunk(make_chunk(batch_id=""))
    assert "page" in validate_chunk(make_chunk(page="3"))


def test_chunk_id_deterministic():
    c1, c2 = make_chunk(), make_chunk(text="不同内容")
    c3 = make_chunk()
    assert c1.id == c3.id and c1.id != c2.id  # 内容幂等 + 差异可辨


@pytest.fixture
def repo():
    r = QdrantRepo(url=":memory:", collection="test_knowledge_v1")
    r.ensure_collection()
    yield r
    r.close()


def test_upsert_bm25_only_and_query(repo):
    from bidqa.rag.bm25 import sparse_bm25_doc, sparse_query
    chunks = [make_chunk(title=f"问题{i}", text=f"答案内容{i}关于单一来源采购") for i in range(3)]
    n = repo.upsert(chunks, [None] * 3, [None] * 3,
                    [sparse_bm25_doc(f"{c.title}\n{c.text}", avgdl=20.0) for c in chunks])
    assert n == 3 and repo.count() == 3
    # BM25 灾备检索：文档侧烘焙权重、查询侧原始词频，同一 crc32 索引空间 → 必有交集
    points = repo.query_bm25(sparse_query("单一来源采购"), limit=2)
    assert len(points) == 2
    assert all(p.payload["source_type"] == "FAQ" for p in points)


def test_iter_points_full_scan_with_vectors(repo):
    from bidqa.rag.bm25 import sparse_bm25_doc
    chunks = [make_chunk(title=f"问题{i}", text=f"内容{i}") for i in range(2)]
    vec = sparse_bm25_doc("问题0\n内容0", avgdl=10.0)
    repo.upsert(chunks, [None] * 2, [None] * 2, [vec, vec])
    got = list(repo.iter_points(with_vectors=True))
    assert len(got) == 2
    ids = {str(pid) for pid, _, _ in got}
    assert ids == {c.id for c in chunks}
    for _pid, payload, vectors in got:
        assert payload["source_type"] == "FAQ"
        assert vectors is not None and vectors["sparse_bm25"].indices == vec.indices


def test_iter_points_payload_only(repo):
    repo.upsert([make_chunk()], [None], [None],
                [models.SparseVector(indices=[0], values=[1.0])])
    got = list(repo.iter_points())
    assert len(got) == 1 and got[0][2] is None  # with_vectors=False → vectors None


# ── 版本化重建：同客户端多集合切换 + 验证后删旧（docs/01 §4.1 流程，NIGHT-042） ──

def test_collection_version_switch_and_drop_old():
    """切换演练：v1 导入 → v2 复制 → 验证（点数/可查/旧集合原样）→ 才删旧。

    回滚语义：删旧之前 v1 全程可用——删除动作必须放在全部验证断言之后。
    """
    from bidqa.rag.bm25 import sparse_bm25_doc, sparse_query
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    old = QdrantRepo(url="", collection="t_switch_v1", client=client)
    old.ensure_collection()
    chunks = [make_chunk(title=f"问题{i}", text=f"答案{i}关于单一来源采购") for i in range(3)]
    old.upsert(chunks, [None] * 3, [None] * 3,
               [sparse_bm25_doc(f"{c.title}\n{c.text}", avgdl=15.0) for c in chunks])

    new = QdrantRepo(url="", collection="t_switch_v2", client=client)
    new.ensure_collection()
    copied = 0
    for pid, payload, vectors in old.iter_points(with_vectors=True):
        vecs = cast(dict, vectors)  # qdrant-client vector 联合类型，测试收窄
        copied += new.upsert_raw([models.PointStruct(
            id=pid, vector={"sparse_bm25": vecs["sparse_bm25"]}, payload=payload)])

    # 验证阶段：点数一致、新集合可查、旧集合原样可查（回滚窗口未关闭）
    assert copied == 3 and new.count() == old.count() == 3
    assert len(new.query_bm25(sparse_query("单一来源采购"), limit=3)) == 3
    assert len(old.query_bm25(sparse_query("单一来源采购"), limit=3)) == 3

    # 全部验证通过后才允许删旧
    old.drop_collection()
    assert not client.collection_exists("t_switch_v1")
    assert new.count() == 3


def test_existing_ids(repo):
    chunks = [make_chunk(title=f"问题{i}", text=f"内容{i}") for i in range(2)]
    repo.upsert(chunks, [None] * 2, [None] * 2,
                [models.SparseVector(indices=[0], values=[1.0])] * 2)
    got = repo.existing_ids([chunks[0].id, "nonexistent"])
    assert got == {chunks[0].id}


def test_source_type_counts_mixed(repo):
    chunks = [make_chunk(source_type="法规", title=f"法{i}", text=f"条{i}") for i in range(2)]
    chunks += [make_chunk(source_type="FAQ", title="问1", text="答1")]
    repo.upsert(chunks, [None] * 3, [None] * 3,
                [models.SparseVector(indices=[0], values=[1.0])] * 3)
    assert repo.source_type_counts() == {"法规": 2, "FAQ": 1}


def test_source_type_counts_empty(repo):
    assert repo.source_type_counts() == {}


# ── E3：两路独立 query_points + 客户端 RRF ──

def _seed_hybrid(repo):
    chunks = [make_chunk(title=f"问题{i}", text=f"答案{i}") for i in range(3)]
    dense = [[1.0] + [0.0] * 1023, [0.0, 1.0] + [0.0] * 1022,
             [0.0, 0.0, 1.0] + [0.0] * 1021]
    sparse = [models.SparseVector(indices=[1, 2], values=[1.0, 0.5]),
              models.SparseVector(indices=[1], values=[0.9]),
              models.SparseVector(indices=[2], values=[0.8])]
    repo.upsert(chunks, dense, sparse,
                [models.SparseVector(indices=[3], values=[1.0])] * 3)
    return chunks


def test_query_hybrid_two_way_prefetch(repo):
    chunks = _seed_hybrid(repo)
    points = repo.query_hybrid([1.0] + [0.0] * 1023,
                               models.SparseVector(indices=[1], values=[1.0]),
                               rrf_k=60, prefetch_size=30, limit=15)
    assert points and len(points) <= 15
    assert {str(p.id) for p in points} <= {str(c.id) for c in chunks}
    assert all(p.score > 0 for p in points)


def test_query_hybrid_rrf_k_affects_top_score(repo):
    _seed_hybrid(repo)
    q_vec = [1.0] + [0.0] * 1023
    q_sparse = models.SparseVector(indices=[1], values=[1.0])
    low = repo.query_hybrid(q_vec, q_sparse, rrf_k=2, limit=3)
    high = repo.query_hybrid(q_vec, q_sparse, rrf_k=90, limit=3)
    assert low[0].score > high[0].score  # 1/(k+rank)：k 越大融合分越小
    assert high[0].score < 0.05           # k=90 时首位约 0.02 量级


def test_query_hybrid_no_branch_returns_empty(repo):
    assert repo.query_hybrid(None, None, rrf_k=30) == []
