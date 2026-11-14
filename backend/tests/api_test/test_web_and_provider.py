"""M3-7 D4 联网工具（缓存键契约/TTL/只缓存成功）+ M3-8 C8 provider 命名档路由。"""

from __future__ import annotations

import time

from bidqa.clients.chat_factory import _profile_chats, build_chat_for
from bidqa.clients.tavily import WebCache
from bidqa.config import Settings
from bidqa.tools.web import SearchWebTool

CORE = {
    "CHAT_BASE_URL": "https://api.example.com/v1", "CHAT_API_KEY": "sk-main",
    "CHAT_MODEL": "main-model", "QDRANT_URL": "http://127.0.0.1:6333",
}


class FakeTavily:
    def __init__(self, results=None, fail=False):
        self.results = results if results is not None else [
            {"title": "省财政厅公告", "url": "https://example.gov.cn/a", "content": "公开公告原文"}]
        self.fail = fail
        self.calls = 0

    def search(self, query, depth="basic", max_results=5):
        self.calls += 1
        if self.fail:
            from bidqa.clients.siliconflow import DataSourceError
            raise DataSourceError("web_search", "429")
        return self.results[:max_results]


# ── D4 工具 ──
def test_web_tool_results_and_sources() -> None:
    tool = SearchWebTool({"tavily": FakeTavily()})
    out = tool.run(tool.args_model.model_validate({"query": "安徽省财政厅 公告"}))
    assert out["empty"] is False
    assert out["sources"][0]["source_type"] == "web"
    assert out["sources"][0]["url"].startswith("https://")
    assert "省财政厅公告" in out["text"]


def test_web_cache_key_and_ttl() -> None:
    """缓存键 ("web", query, provider, depth)；命中不再请求；TTL 过期后重新请求。"""
    fake = FakeTavily()
    cache = WebCache()
    cache.TTL = 0.05  # 测试用短 TTL
    tool = SearchWebTool({"tavily": fake}, cache=cache)
    tool.run(tool.args_model.model_validate({"query": "同一查询"}))
    tool.run(tool.args_model.model_validate({"query": "同一查询"}))
    assert fake.calls == 1  # 第二次命中缓存
    time.sleep(0.06)
    tool.run(tool.args_model.model_validate({"query": "同一查询"}))
    assert fake.calls == 2  # TTL 过期重新请求


def test_web_failure_not_cached_and_degrades() -> None:
    fake = FakeTavily(fail=True)
    tool = SearchWebTool({"tavily": fake})
    out = tool.run(tool.args_model.model_validate({"query": "会失败的查询"}))
    assert out["empty"] is True
    assert "暂不可用" in out["text"]
    out2 = tool.run(tool.args_model.model_validate({"query": "会失败的查询"}))
    assert out2["empty"] is True and fake.calls == 2  # 失败结果不缓存


def test_web_deep_copy_from_cache() -> None:
    fake = FakeTavily()
    tool = SearchWebTool({"tavily": fake})
    first = tool.run(tool.args_model.model_validate({"query": "深拷贝"}))
    first["sources"][0]["title"] = "被改掉"
    second = tool.run(tool.args_model.model_validate({"query": "深拷贝"}))
    assert second["sources"][0]["title"] == "省财政厅公告"  # 缓存返回深拷贝


# ── 联网重排（参考 §15 吸收：分数 min-max 归一写回；失败/缺失=诚实降级不记 0）──
class FakeReranker:
    def __init__(self, scores=None, fail=False):
        self.scores = scores if scores is not None else [(0, 0.9), (1, 0.5)]
        self.fail = fail
        self.calls = []

    def rerank(self, query, documents, top_n):
        self.calls.append({"query": query, "documents": documents, "top_n": top_n})
        if self.fail:
            from bidqa.clients.siliconflow import DataSourceError
            raise DataSourceError("rerank", "500")
        return self.scores


def _two_results() -> list[dict]:
    return [{"title": "公告一", "url": "https://example.gov.cn/1", "content": "内容一"},
            {"title": "公告二", "url": "https://example.gov.cn/2", "content": "内容二"}]


def test_web_rerank_scores_normalized_and_persisted() -> None:
    rr = FakeReranker(scores=[(0, 0.9), (1, 0.5)])
    tool = SearchWebTool({"tavily": FakeTavily(results=_two_results())}, reranker=rr)
    out = tool.run(tool.args_model.model_validate({"query": "公告"}))
    assert out["sources"][0]["score"] == 1.0    # min-max：最高分 → 1
    assert out["sources"][1]["score"] == 0.0    # 最低分 → 0
    assert rr.calls[0]["query"] == "公告"        # 按检索 query 重排（BaseTool 签名不动）
    assert len(rr.calls[0]["documents"]) == 2   # 文档与来源对位
    headers = [l for l in out["text"].splitlines() if l.startswith("[")]
    assert headers == ["[1] 公告一", "[2] 公告二"]  # 只标注不重排序


def test_web_rerank_results_cached_with_scores() -> None:
    """归一后缓存：同 query 二次调用 Tavily 与 rerank 都不再执行。"""
    rr = FakeReranker(scores=[(0, 0.9), (1, 0.5)])
    tool = SearchWebTool({"tavily": FakeTavily(results=_two_results())}, reranker=rr)
    first = tool.run(tool.args_model.model_validate({"query": "同一查询"}))
    second = tool.run(tool.args_model.model_validate({"query": "同一查询"}))
    assert len(rr.calls) == 1
    assert second["sources"][0]["score"] == first["sources"][0]["score"] == 1.0


def test_web_rerank_failure_keeps_none() -> None:
    """重排失败 = 引擎原序 + score None（诚实降级，绝不记 0 假装测过）。"""
    tool = SearchWebTool({"tavily": FakeTavily()}, reranker=FakeReranker(fail=True))
    out = tool.run(tool.args_model.model_validate({"query": "公告"}))
    assert out["empty"] is False
    assert out["sources"][0]["score"] is None


def test_web_no_reranker_unchanged() -> None:
    """未配置重排（RERANK_* 缺失）= 现状不变。"""
    tool = SearchWebTool({"tavily": FakeTavily()})
    out = tool.run(tool.args_model.model_validate({"query": "公告"}))
    assert out["sources"][0]["score"] is None


# ── D5 多供应商（Tavily/Exa 名册；合并去重/组合缓存键/部分失败诚实降级）──
class FakeExa:
    """鸭子类型第二供应商（与 ExaClient.search 同签名）。"""

    def __init__(self, results=None, fail=False):
        self.results = results if results is not None else [
            {"title": "Exa 结果", "url": "https://exa.dev/e", "content": "英文页面"}]
        self.fail = fail
        self.calls = 0

    def search(self, query, depth="basic", max_results=5):
        self.calls += 1
        if self.fail:
            from bidqa.clients.siliconflow import DataSourceError
            raise DataSourceError("web_search", "500")
        return self.results[:max_results]


def test_web_two_providers_merge_and_dedupe() -> None:
    """双供应商：按配置序合并、URL 去重 keep-first；text 与 sources 对位。"""
    tav = FakeTavily(results=[
        {"title": "公告一", "url": "https://gov.cn/1", "content": "内容一"},
        {"title": "两边都有", "url": "https://gov.cn/same", "content": "重合条目"}])
    exa = FakeExa(results=[
        {"title": "Exa 独有", "url": "https://exa.dev/2", "content": "内容二"},
        {"title": "重复条目", "url": "https://gov.cn/same", "content": "重复"}])
    tool = SearchWebTool({"tavily": tav, "exa": exa})
    out = tool.run(tool.args_model.model_validate({"query": "公告"}))
    assert [s["url"] for s in out["sources"]] == [
        "https://gov.cn/1", "https://gov.cn/same", "https://exa.dev/2"]  # 配置序 keep-first
    assert "Exa 独有" in out["text"]


def test_web_cache_key_covers_provider_roster() -> None:
    """缓存键含供应商名册：名册变化即冷启动，供应商结果不串键。"""
    cache = WebCache()
    tav1 = FakeTavily()
    tool = SearchWebTool({"tavily": tav1}, cache=cache)
    args = tool.args_model.model_validate({"query": "同一查询"})
    tool.run(args)
    assert cache.get(("web", "同一查询", "tavily", "basic")) is not None  # 单供应商键落位
    tav2, exa = FakeTavily(), FakeExa()
    SearchWebTool({"tavily": tav2, "exa": exa}, cache=cache).run(args)
    assert tav2.calls == 1 and exa.calls == 1  # 名册变键变，各自冷跑
    assert cache.get(("web", "同一查询", "tavily,exa", "basic")) is not None


def test_web_partial_provider_failure_not_cached() -> None:
    """部分供应商失败：用成功者继续（部分结果诚实返回）但不缓存。"""
    tav, exa = FakeTavily(), FakeExa(fail=True)
    tool = SearchWebTool({"tavily": tav, "exa": exa})
    args = tool.args_model.model_validate({"query": "部分失败"})
    out = tool.run(args)
    assert out["empty"] is False
    assert [s["url"] for s in out["sources"]] == ["https://example.gov.cn/a"]  # 仅成功者
    tool.run(args)
    assert tav.calls == 2  # 部分结果不缓存 → 重跑


def test_web_all_providers_fail_honest() -> None:
    """全败 = empty_result 诚实文案（与单供应商语义一致）。"""
    tool = SearchWebTool({"tavily": FakeTavily(fail=True), "exa": FakeExa(fail=True)})
    out = tool.run(tool.args_model.model_validate({"query": "全败"}))
    assert out["empty"] is True and "暂不可用" in out["text"]


def test_web_description_lists_providers() -> None:
    """描述动态列出供应商（描述质量影响工具选择率）。"""
    tool = SearchWebTool({"tavily": FakeTavily(), "exa": FakeExa()})
    assert "tavily/exa" in tool.description


# ── C8 provider 命名档 ──
def make_profile_settings(monkeypatch, profiles: str) -> Settings:
    monkeypatch.setenv("CHAT_PROFILES", profiles)
    monkeypatch.setenv("CHAT_FAST_BASE_URL", "https://fast.example.com/v1")
    monkeypatch.setenv("CHAT_FAST_API_KEY", "sk-fast")
    monkeypatch.setenv("CHAT_FAST_MODEL", "fast-model")
    monkeypatch.setenv("CHAT_BROKEN_BASE_URL", "https://broken.example.com/v1")
    # BROKEN 档缺 key/model → 应被跳过
    return Settings(_env_file=None, **CORE, CHAT_PROFILES=profiles)


def test_chat_profiles_parse(monkeypatch) -> None:
    s = make_profile_settings(monkeypatch, "fast,broken")
    profiles = s.chat_profiles()
    assert set(profiles) == {"fast"}  # 缺件档位跳过
    assert profiles["fast"]["model"] == "fast-model"


def test_chat_profiles_empty(monkeypatch) -> None:
    s = Settings(_env_file=None, **CORE)
    assert s.chat_profiles() == {}


def test_build_chat_for_routing(monkeypatch) -> None:
    """指定已配置 provider → 走命名档；未指定/未知 → 主槽位。"""
    s = make_profile_settings(monkeypatch, "fast")
    monkeypatch.setattr("bidqa.clients.chat_factory.get_settings", lambda: s)
    _profile_chats.clear()

    fast = build_chat_for("fast")
    assert fast is not None and fast.model == "fast-model"
    assert build_chat_for("fast") is fast  # 复用同实例
    main = build_chat_for(None)
    assert main.model == "main-model"
    assert build_chat_for("unknown") is main  # 未知名回落主槽位
