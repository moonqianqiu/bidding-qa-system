"""POST /api/vision 契约测试（docs/01 §3.1）：4131/5031/4001/200 至少一例 + ref 缓存行为。

VISION 槽位客户端打桩（fake.chat 返回固定描述），传输不触网；
体积校验先于能力校验（传输层错误优先，超限必 413 与配置无关）。
"""

from __future__ import annotations

import base64
import time

from fastapi.testclient import TestClient

from bidqa.application.vision import image_context_block
from bidqa.application.vision import RefStore, get_ref_store  # C4：状态归应用层
from bidqa.main import create_app


class FakeVision:
    def chat(self, messages, temperature=0.3) -> str:
        # 断言多模态消息形态：user.content 是 list 且含 image_url
        user = messages[1]["content"]
        assert isinstance(user, list)
        assert any(p.get("type") == "image_url" for p in user)
        return "图片为一份中标通知书扫描件，含项目编号 IT-2025-01 与金额 66 万元。"


def _make_client(settings_factory, monkeypatch, vision_configured: bool) -> TestClient:
    import bidqa.application.vision as app_vision  # C4：build_vision 缝在应用层

    extra = ({"VISION_BASE_URL": "http://vision.local/v1", "VISION_API_KEY": "k",
              "VISION_MODEL": "test-vision"} if vision_configured else {})
    app = create_app(settings_factory(**extra))
    if vision_configured:
        monkeypatch.setattr(app_vision, "build_vision", lambda: FakeVision())
    return TestClient(app)


def _png_b64() -> str:
    return base64.b64encode(b"\x89PNG-fake-bytes-for-test").decode()


def test_413_oversize(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision_configured=False)
    resp = client.post("/api/vision", json={"image": "A" * 4_000_001})
    assert resp.status_code == 413
    assert resp.json()["error_code"] == 4131


def test_5031_vision_not_configured(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision_configured=False)
    resp = client.post("/api/vision", json={"image": _png_b64(), "filename": "a.png"})
    assert resp.status_code == 503
    assert resp.json()["error_code"] == 5031
    assert "vision" in resp.json()["detail"]["source"]


def test_4001_bad_base64(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision_configured=True)
    resp = client.post("/api/vision", json={"image": "data:image/png;base64,@@@非法@@@"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


def test_200_happy_path_and_ref_roundtrip(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision_configured=True)
    resp = client.post("/api/vision", json={"image": _png_b64(), "filename": "notice.png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_ref"].startswith("img_") and len(body["image_ref"]) == 16
    assert "中标通知书" in body["description"]
    assert body["filename"] == "notice.png"
    assert "中标通知书" in (get_ref_store().get(body["image_ref"]) or "")  # ref 立即可取


def test_ref_store_ttl_expiry() -> None:
    store = RefStore(ttl=0.05)
    ref = store.put("描述")
    assert store.get(ref) == "描述"
    time.sleep(0.08)
    assert store.get(ref) is None  # 过期即 None，绝不返回旧描述


def test_ref_store_capacity_evicts_oldest() -> None:
    store = RefStore(max_entries=2)
    first = store.put("第一张")
    second = store.put("第二张")
    store.put("第三张")
    assert store.get(first) is None  # 超容量丢最旧
    assert store.get(second) is not None


def test_image_context_block_injection() -> None:
    ref = get_ref_store().put("招标文件第3页保证金表格")
    block = image_context_block([ref, "img_不存在0000"])
    assert block.startswith("【图片内容 1】招标文件第3页保证金表格")
    assert image_context_block([]) == ""
    assert image_context_block(["img_不存在0000"]) == ""  # 未知 ref 静默跳过
