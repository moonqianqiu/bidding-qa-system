"""SSE 传输机制单测：停机广播（P1-8 停机②）+ 通道注销。"""

from __future__ import annotations


def test_broadcast_shutdown_reaches_active_channel() -> None:
    """P1-8（审计）：停机时活动 SSE 通道必须收到 error 5031（docs/01 §6 四步②），
    而非被 uvicorn 强杀断连——客户端据此知道是服务端维护而非网络故障。"""
    from bidqa.api.chat.sse import SseChannel, active_channel_count, broadcast_shutdown

    before = active_channel_count()
    ch = SseChannel()
    assert active_channel_count() == before + 1  # 建通道即注册
    n = broadcast_shutdown()
    assert n >= 1  # 至少广播给本用例的活动通道
    lines = list(ch.lines(t0=0.0, build_done_event=lambda *a: {}))
    assert len(lines) == 1 and '"type": "error"' in lines[0] and "5031" in lines[0]
    assert active_channel_count() == before  # 生成器结束 → 通道注销


def test_channel_unregister_on_result_path() -> None:
    """正常收尾（_result→done）同样注销——注册表只含活动通道。"""
    from bidqa.api.chat.sse import SseChannel, active_channel_count

    before = active_channel_count()
    ch = SseChannel()
    ch.emit("_result", object())
    list(ch.lines(t0=0.0, build_done_event=lambda *a: {"schema_v": 1}))
    assert active_channel_count() == before
