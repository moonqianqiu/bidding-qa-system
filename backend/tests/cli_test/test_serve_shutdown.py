"""回归钉子（审计 P1-8 广播可达性）：uvicorn 优雅停机时序中，活动 SSE 连接必须在
「连接收尾等待/超时 cancel」**之前**收到 error 5031，而不是被 cancel 成裸断连。

背景（uvicorn 0.54 server.py shutdown() 顺序）：关监听 → wait_for(连接收尾,
timeout_graceful_shutdown，超时对所有任务 cancel()) → **之后**才 lifespan.shutdown()。
修复前 broadcast_shutdown 挂在 lifespan 停机钩子（main.py 停机②）——执行时 SSE
响应任务早已被 cancel、通道经 finally 注销 → 广播 0 通道 → 客户端只见裸断连。

修复：cli/serve.BidqaServer 在 shutdown() **入口**先广播。本测试用真实 uvicorn
Server + 真实 SseChannel + 真实 TCP SSE 连接钉住该时序：timeout_graceful_shutdown=3s，
广播必须让 error 事件在 cancel 窗口（3s）内到达且连接干净收尾（EOF 而非挂死）；
error 若 ≥ grace 窗口才到达 = 走到了 cancel 路径 = 广播时序回归 → 红。
"""

from __future__ import annotations

import json
import threading
import time

import http.client
import uvicorn

from bidqa.cli.serve import BidqaServer

GRACE = 3.0  # 停机宽限秒数（cancel 阈值）：error 必须在此窗口内到达客户端


def _app():
    """最小 Starlette 应用：/sse 打开真实 SseChannel 并永不主动收尾（模拟进行中长流），
    首条事件与生产契约序一致（status*→…→done|error）。"""

    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from bidqa.api.chat.sse import SseChannel

    async def stream(request):
        ch = SseChannel()
        ch.emit("status", {"stage": "分析中"})
        return StreamingResponse(
            ch.lines(t0=0.0, build_done_event=lambda *a: {"schema_v": 1}),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return Starlette(routes=[Route("/sse", stream, methods=["POST"])])


def _read_lines(conn: http.client.HTTPConnection,
                resp: http.client.HTTPResponse, deadline: float,
                stop_at: bytes | None = None) -> list[bytes]:
    """读 SSE 行直到 EOF / deadline / 命中 stop_at。每次读前按剩余时间收紧
    socket 超时——阻塞式 readline 不看 deadline，一次性超时会让 socket 对象
    永久「poisoned」（后续读全部 OSError），必须逐读控制。"""
    out: list[bytes] = []
    sock = conn.sock
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            line = resp.fp.readline()
        except (TimeoutError, OSError) as exc:  # 服务端既不发事件也不关连接（回归形态）
            out.append(f"<<{type(exc).__name__}: {exc}>>".encode())
            break
        if not line:  # EOF：服务端干净关闭
            break
        out.append(line)
        if stop_at is not None and stop_at in line:
            break
    sock.settimeout(10.0)
    return out


def test_graceful_shutdown_broadcasts_5031_before_cancel() -> None:
    """真实 uvicorn 停机时序：活动 SSE 连接收到 error 5031 → 连接干净收尾。"""
    from bidqa.api.chat.sse import active_channel_count

    config = uvicorn.Config(app=_app(), host="127.0.0.1", port=0, log_level="warning",
                            lifespan="off", timeout_graceful_shutdown=int(GRACE))
    server = BidqaServer(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while (not server.started or not server.servers) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 10s 内未完成 startup"
    port = server.servers[0].sockets[0].getsockname()[1]

    channels_before = active_channel_count()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/sse", headers={"Accept": "text/event-stream"})
    resp = conn.getresponse()
    assert resp.status == 200
    first = _read_lines(conn, resp, time.monotonic() + 5, stop_at=b'"type": "status"')
    assert any(b'"type": "status"' in ln for ln in first), \
        f"首条 status 事件未到达（SSE 长连接未真正建立）: {first!r}"
    assert active_channel_count() >= channels_before + 1, "SSE 通道未进入活动注册表"

    t0 = time.monotonic()
    server.should_exit = True  # 与 SIGINT/SIGBREAK 优雅路径同入口（handle_exit 同款置位）
    rest = _read_lines(conn, resp, t0 + GRACE + 5)
    elapsed = time.monotonic() - t0

    thread.join(timeout=10)
    assert not thread.is_alive(), "uvicorn 10s 内未完成停机"

    text = b"".join(rest).decode("utf-8", errors="replace")
    error_lines = [ln for ln in text.splitlines() if '"type": "error"' in ln]
    assert error_lines, (
        f"优雅停机未向活动 SSE 连接投递 error 事件（{elapsed:.2f}s 收到 {len(rest)} 行）:"
        f" {text!r}")
    payload = json.loads(error_lines[-1].removeprefix("data: ").strip())
    assert payload["error_code"] == 5031, f"error 事件错误码不符: {payload}"

    # 核心时序口径：error 必须在 cancel（grace 窗口）之前触达——
    # 若 ≥ GRACE 说明 uvicorn 等待超时走了任务 cancel（广播迟到/未达）→ 回归。
    assert elapsed < GRACE, (
        f"error 在 {elapsed:.2f}s ≥ grace {GRACE}s 才收尾：走到了 cancel 路径，"
        "广播未在取消前触达通道")
    assert active_channel_count() == channels_before, "停机后通道未从注册表注销"
