"""聊天槽位工厂：数据加工批量通道（SUBJECT_BATCH_*）单一构造入口。"""

from __future__ import annotations

from bidqa.clients.chat_factory import build_subject_batch_chat
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
