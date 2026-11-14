"""POST /api/vision 契约测试（docs/01 §3.1）：4131/5031/4001/200 至少一例 + ref 缓存行为
+ v2.3 双通道行为矩阵（mode × OCR 槽位 × Qwen 路由，docs/01 §3.1 表）。

VISION/OCR 槽位客户端打桩（fake 返回固定输出），传输不触网、不读真实 .env；
体积校验先于能力校验（传输层错误优先，超限必 413 与配置无关）。
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from bidqa.api.ratelimit import get_limiter
from bidqa.application.image.refs import RefStore, get_ref_store  # C4：状态归应用层
from bidqa.application.image.refs import image_context_block
from bidqa.main import create_app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """vision 是限流端点：主桶进程内共享（ask/chat_stream 同桶），逐用例清空防合跑 429 串扰。"""
    get_limiter().reset()
    yield
    get_limiter().reset()

DESC = "图片为一份中标通知书扫描件，含项目编号 IT-2025-01 与金额 66 万元。"
OCR_TEXT = "第一条 项目概况 1.1 本项目为涡阳县道路改造工程。"
OCR_PREFIX = "【图片文字（OCR）】"


class FakeVision:
    """Qwen 路由+描述 fake：默认回合规 JSON；raw 注入劣化输出测容错；calls 计数。"""

    def __init__(self, route: str = "describe", raw: str | None = None):
        self.route = route
        self.raw = raw
        self.calls = 0

    def chat(self, messages, temperature=0.3) -> str:
        self.calls += 1
        user = messages[1]["content"]
        assert isinstance(user, list)  # 消息形态契约：多模态 list-content
        assert any(p.get("type") == "image_url" for p in user)
        if self.raw is not None:
            return self.raw
        return json.dumps({"route": self.route, "description": DESC}, ensure_ascii=False)


class FakeOCR:
    def __init__(self, text: str = OCR_TEXT, fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls = 0

    def chat(self, messages, temperature=0.3) -> str:
        self.calls += 1
        assert any(p.get("type") == "image_url" for p in messages[1]["content"])
        if self.fail:
            raise RuntimeError("ocr upstream down")
        return self.text


def _make_client(settings_factory, monkeypatch, *, vision: FakeVision | None = None,
                 ocr: FakeOCR | None = None, ocr_configured: bool = False) -> TestClient:
    """vision/ocr=None = 槽位未配置语义；ocr_configured 只喂 has_ocr（api 前置门）。"""
    import bidqa.application.image.analyze as analyze_mod

    extra: dict[str, str] = {}
    if vision is not None:
        extra.update({"VISION_BASE_URL": "http://vision.local/v1", "VISION_API_KEY": "k",
                      "VISION_MODEL": "test-vision"})
    if ocr_configured:
        extra.update({"OCR_BASE_URL": "http://ocr.local/v1", "OCR_API_KEY": "k",
                      "OCR_MODEL": "test-ocr"})
    app = create_app(settings_factory(**extra))
    monkeypatch.setattr(analyze_mod, "build_vision", lambda: vision)
    monkeypatch.setattr(analyze_mod, "build_ocr", lambda: ocr)
    return TestClient(app)


def _png_b64() -> str:
    return base64.b64encode(b"\x89PNG-fake-bytes-for-test").decode()


# ── 传输层校验（与能力配置无关）──────────────────────────────────────────

def test_413_oversize(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch)
    resp = client.post("/api/vision", json={"image": "A" * 4_000_001})
    assert resp.status_code == 413
    assert resp.json()["error_code"] == 4131


def test_413_oversize_original(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(),
                          ocr=FakeOCR(), ocr_configured=True)
    resp = client.post("/api/vision", json={"image": _png_b64(), "original": "A" * 4_000_001})
    assert resp.status_code == 413
    assert resp.json()["error_code"] == 4131


def test_4001_bad_base64(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision())
    resp = client.post("/api/vision", json={"image": "data:image/png;base64,@@@非法@@@"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


def test_4001_bad_base64_original(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(),
                          ocr=FakeOCR(), ocr_configured=True)
    resp = client.post("/api/vision",
                       json={"image": _png_b64(), "original": "@@@", "mode": "ocr"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


def test_400_mode_invalid(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision())
    resp = client.post("/api/vision", json={"image": _png_b64(), "mode": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


# ── 能力校验（5031）────────────────────────────────────────────────────

def test_5031_vision_not_configured(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch)
    resp = client.post("/api/vision", json={"image": _png_b64(), "filename": "a.png"})
    assert resp.status_code == 503
    assert resp.json()["error_code"] == 5031
    assert "vision" in resp.json()["detail"]["source"]


def test_5031_manual_ocr_without_ocr_slot(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(),
                          ocr_configured=False)
    resp = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64(), "mode": "ocr"})
    assert resp.status_code == 503
    assert resp.json()["error_code"] == 5031
    assert "ocr" in resp.json()["detail"]["source"]


def test_400_manual_ocr_requires_original(settings_factory, monkeypatch) -> None:
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(),
                          ocr_configured=True)
    resp = client.post("/api/vision", json={"image": _png_b64(), "mode": "ocr"})
    assert resp.status_code == 400
    assert resp.json()["error_code"] == 4001


# ── v2.3 双通道行为矩阵 ────────────────────────────────────────────────

def test_200_auto_no_ocr_slot_is_v22_behavior(settings_factory, monkeypatch) -> None:
    """auto + OCR 未配置 = v2.2 现状：Qwen 描述，route_by=unavailable，不算 degraded。"""
    vision = FakeVision()
    client = _make_client(settings_factory, monkeypatch, vision=vision)
    resp = client.post("/api/vision", json={"image": _png_b64(), "filename": "notice.png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["image_ref"].startswith("img_") and len(body["image_ref"]) == 16
    assert body["description"] == DESC
    assert body["filename"] == "notice.png"
    assert body["mode_used"] == "describe"
    assert body["route_by"] == "unavailable"
    assert body["degraded"] is False
    assert vision.calls == 1
    assert get_ref_store().get(body["image_ref"]) == DESC  # ref 立即可取


def test_200_auto_routed_to_ocr(settings_factory, monkeypatch) -> None:
    """auto + 路由 ocr + OCR 成功：Qwen 与 OCR 各调一次，描述=OCR 原文带前缀。"""
    vision = FakeVision(route="ocr")
    ocr = FakeOCR()
    client = _make_client(settings_factory, monkeypatch, vision=vision, ocr=ocr,
                          ocr_configured=True)
    resp = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64(), "filename": "doc.png"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["mode_used"] == "ocr"
    assert body["route_by"] == "auto"
    assert body["degraded"] is False
    assert body["description"] == f"{OCR_PREFIX}\n{OCR_TEXT}"
    assert vision.calls == 1 and ocr.calls == 1
    assert get_ref_store().get(body["image_ref"]) == f"{OCR_PREFIX}\n{OCR_TEXT}"


def test_200_auto_ocr_failure_degrades(settings_factory, monkeypatch) -> None:
    """auto + 路由 ocr + OCR 调用失败：Qwen 描述兜底，degraded=true，绝不失败。"""
    vision = FakeVision(route="ocr")
    ocr = FakeOCR(fail=True)
    client = _make_client(settings_factory, monkeypatch, vision=vision, ocr=ocr,
                          ocr_configured=True)
    body = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64()}).json()
    assert body["mode_used"] == "describe"
    assert body["route_by"] == "auto"
    assert body["degraded"] is True
    assert body["description"] == DESC


def test_200_auto_ocr_empty_degrades(settings_factory, monkeypatch) -> None:
    """auto + 路由 ocr + OCR 空文本：同样兜底（空描述绝不充数）。"""
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(route="ocr"),
                          ocr=FakeOCR(text=""), ocr_configured=True)
    body = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64()}).json()
    assert body["mode_used"] == "describe" and body["degraded"] is True


def test_200_auto_routed_ocr_without_original_degrades(settings_factory, monkeypatch) -> None:
    """auto + 路由 ocr 但没传原件：Qwen 描述兜底（前端恒传，此处为 API 健壮性）。"""
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(route="ocr"),
                          ocr=FakeOCR(), ocr_configured=True)
    body = client.post("/api/vision", json={"image": _png_b64()}).json()
    assert body["mode_used"] == "describe" and body["degraded"] is True
    assert body["description"] == DESC


def test_200_auto_describe_route(settings_factory, monkeypatch) -> None:
    """auto + 路由 describe：Qwen 描述，OCR 不被调用。"""
    vision = FakeVision(route="describe")
    ocr = FakeOCR()
    client = _make_client(settings_factory, monkeypatch, vision=vision, ocr=ocr,
                          ocr_configured=True)
    body = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64()}).json()
    assert body["mode_used"] == "describe" and body["route_by"] == "auto"
    assert body["degraded"] is False and body["description"] == DESC
    assert ocr.calls == 0


def test_200_manual_describe_ignores_route(settings_factory, monkeypatch) -> None:
    """mode=describe：强制通用描述，OCR 不参与。"""
    vision = FakeVision(route="ocr")  # 即便 Qwen 想走 OCR 也弃用其判断
    ocr = FakeOCR()
    client = _make_client(settings_factory, monkeypatch, vision=vision, ocr=ocr,
                          ocr_configured=True)
    body = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64(),
                             "mode": "describe"}).json()
    assert body["mode_used"] == "describe" and body["route_by"] == "manual"
    assert body["description"] == DESC
    assert ocr.calls == 0


def test_200_manual_ocr_skips_qwen(settings_factory, monkeypatch) -> None:
    """mode=ocr：跳过 Qwen 最省钱，直接 OCR 原件。"""
    vision = FakeVision()
    ocr = FakeOCR()
    client = _make_client(settings_factory, monkeypatch, vision=vision, ocr=ocr,
                          ocr_configured=True)
    body = client.post("/api/vision",
                       json={"image": _png_b64(), "original": _png_b64(),
                             "mode": "ocr"}).json()
    assert body["mode_used"] == "ocr" and body["route_by"] == "manual"
    assert body["description"] == f"{OCR_PREFIX}\n{OCR_TEXT}"
    assert vision.calls == 0 and ocr.calls == 1


# ── Qwen JSON 容错（解析失败=兜底为现状，绝不 5031）────────────────────

def test_200_qwen_nonjson_fallback(settings_factory, monkeypatch) -> None:
    raw = "这不是 JSON，是一段普通描述文字。"
    client = _make_client(settings_factory, monkeypatch,
                          vision=FakeVision(raw=raw))
    body = client.post("/api/vision", json={"image": _png_b64()}).json()
    assert body["mode_used"] == "describe"
    assert body["description"] == raw  # 整段兜底，请求不失败


def test_200_qwen_json_fenced(settings_factory, monkeypatch) -> None:
    raw = "```json\n" + json.dumps({"route": "describe", "description": DESC},
                                   ensure_ascii=False) + "\n```"
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(raw=raw))
    body = client.post("/api/vision", json={"image": _png_b64()}).json()
    assert body["description"] == DESC  # 代码栅栏剥掉后正常解析


def test_200_qwen_json_missing_description_fallback(settings_factory, monkeypatch) -> None:
    raw = json.dumps({"route": "ocr"}, ensure_ascii=False)  # 描述缺失=不可信输出
    client = _make_client(settings_factory, monkeypatch, vision=FakeVision(raw=raw))
    body = client.post("/api/vision", json={"image": _png_b64()}).json()
    assert body["description"] == raw and body["mode_used"] == "describe"


# ── RefStore / 上下文块（回归）─────────────────────────────────────────

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
