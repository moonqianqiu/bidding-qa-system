"""聊天槽位工厂：数据加工批量通道（SUBJECT_BATCH_*）与 OCR 槽位单一构造入口。"""

from __future__ import annotations

from bidqa.clients.chat_factory import build_ocr, build_subject_batch_chat
from bidqa.clients.llm import ChatClient


def test_subject_batch_chat_none_without_key(monkeypatch, settings_factory) -> None:
    monkeypatch.setattr("bidqa.clients.chat_factory.get_settings",
                        lambda: settings_factory())
    assert build_subject_batch_chat() is None  # 未配置=诚实拒绝，不回落主槽位


def test_subject_batch_chat_uses_batch_slot(monkeypatch, settings_factory) -> None:
    s = settings_factory(
        SUBJECT_BATCH_API_KEY="k-batch",
        SUBJECT_BATCH_BASE_URL="http://batch.local/v1",
        SUBJECT_BATCH_MODEL="qwen-batch",
    )
    monkeypatch.setattr("bidqa.clients.chat_factory.get_settings", lambda: s)
    client = build_subject_batch_chat()
    assert isinstance(client, ChatClient)
    assert client.model == "qwen-batch"
    # 批量模型思考开关随客户端走（关闭思考，防 reasoning 烧穿输出预算）
    assert client.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_build_ocr_none_without_config(monkeypatch, settings_factory) -> None:
    monkeypatch.setattr("bidqa.clients.chat_factory.get_settings",
                        lambda: settings_factory())
    assert build_ocr() is None  # OCR 槽位未配置 = None（能力型，不回落 VISION_*）


def test_build_ocr_uses_ocr_slot(monkeypatch, settings_factory) -> None:
    s = settings_factory(OCR_BASE_URL="http://ocr.local/v1", OCR_API_KEY="k-ocr",
                         OCR_MODEL="PaddlePaddle/PaddleOCR-VL")
    monkeypatch.setattr("bidqa.clients.chat_factory.get_settings", lambda: s)
    client = build_ocr()
    assert isinstance(client, ChatClient)
    assert client.model == "PaddlePaddle/PaddleOCR-VL"
    assert client.extra_body == {}  # OCR 槽位无批量通道的思考开关
