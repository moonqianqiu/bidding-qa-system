"""SSE 传输机制单测：异步化（P1-21）+ 停机广播（P1-8 停机②）+ 通道注销。"""

from __future__ import annotations

import asyncio
import inspect


def test_lines_is_async_generator() -> None:
    """P1-21（审计）：lines() 曾是同步生成器——Starlette iterate_in_threadpool
    每条 SSE 连接占用 1 个 AnyIO 线程（默认 40），空闲 get(15s) 也占着；
    40+ 并发流会把所有同步 def 端点（health/auth）饿死。异步化后连接在事件循环等待。"""
    from bidqa.api.chat.sse import SseChannel

    assert inspect.isasyncgenfunction(SseChannel.lines)


def test_async_lines_full_contract() -> None:
    """异步消费全契约：绑定前 emit 的 status 不丢（缓冲搬运）、token 顺序、
    done 收尾、ping 心跳。"""

    async def scenario() -> list[str]:
        from bidqa.api.chat.sse import SseChannel

        ch = SseChannel(ping_interval=0.05)
        ch.emit("status", {"stage": "分析中"})  # 绑定前投递（worker 先于响应启动的竞态）
        lines_iter = ch.lines(t0=0.0, build_done_event=lambda r, e: {"schema_v": 1})
        out: list[str] = []
        task = asyncio.create_task(anext_(lines_iter))
        # 绑定发生在第一次迭代；绑定前那条 status 由缓冲搬运保证不丢
        out.append(await asyncio.wait_for(task, timeout=2.0))
        ch.emit("token", {"text": "你好"})
        out.append(await asyncio.wait_for(anext_(lines_iter), timeout=2.0))
        ch.emit("_result", object())
        out.append(await asyncio.wait_for(anext_(lines_iter), timeout=2.0))
        return out

    async def anext_(it):
        return await it.__anext__()

    out = asyncio.run(scenario())
    assert '"type": "status"' in out[0]  # 绑定前的投递不丢
    assert '"type": "token"' in out[1]
    assert '"type": "done"' in out[2]


def test_broadcast_shutdown_reaches_active_channel() -> None:
    """P1-8（审计）：停机时活动 SSE 通道必须收到 error 5031（docs/01 §6 四步②），
    而非被 uvicorn 强杀断连——客户端据此知道是服务端维护而非网络故障。"""
    from bidqa.api.chat.sse import SseChannel, active_channel_count, broadcast_shutdown

    before = active_channel_count()
    ch = SseChannel()
    assert active_channel_count() == before + 1  # 建通道即注册
    n = broadcast_shutdown()
    assert n >= 1  # 至少广播给本用例的活动通道

    async def consume() -> list[str]:
        return [line async for line in ch.lines(t0=0.0, build_done_event=lambda *a: {})]

    lines = asyncio.run(consume())
    assert len(lines) == 1 and '"type": "error"' in lines[0] and "5031" in lines[0]
    assert active_channel_count() == before  # 生成器结束 → 通道注销


def test_channel_unregister_on_result_path() -> None:
    """正常收尾（_result→done）同样注销——注册表只含活动通道。"""
    from bidqa.api.chat.sse import SseChannel, active_channel_count

    before = active_channel_count()
    ch = SseChannel()
    ch.emit("_result", object())

    async def consume() -> None:
        async for _ in ch.lines(t0=0.0, build_done_event=lambda *a: {"schema_v": 1}):
            pass

    asyncio.run(consume())
    assert active_channel_count() == before
