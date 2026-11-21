"""serve 子命令：uvicorn 启动（workers=1 硬性约定，docs/01 §6）。"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import uvicorn

from bidqa.config import ConfigError


class BidqaServer(uvicorn.Server):
    """在 uvicorn 停机时序**最前端**广播 SSE error 5031（审计 P1-8 广播可达性）。

    uvicorn 0.54 shutdown() 顺序：关监听 → wait_for(连接收尾, timeout_graceful_shutdown，
    超时对所有任务 cancel()) → **之后**才 lifespan.shutdown()。broadcast_shutdown 若只挂
    lifespan 钩子（main.py 停机②），执行时进行中 SSE 响应任务早已被 cancel、通道经
    lines() 的 finally 注销 → 广播 0 通道 → 客户端只看到裸断连。本子类把广播提到
    shutdown() 入口——先于等待/cancel 窗口：SSE 生成器收到 _error 即 yield error 事件
    并正常 return，连接在宽限期内干净收尾（不发 RST/不挂死）。main.py 停机②保留为
    兜底（嵌入式/非 uvicorn 场景），停机四步不变。

    NIGHT-014：优雅停机完成后进程必须以 exit 0 退出。uvicorn>=0.30 的
    capture_signals() 在停机收尾会把捕获到的信号用**恢复后的原 handler** 重放
    （0.54 server.py：restore 后 raise_signal）——SIGBREAK/SIGTERM 落回默认处置
    直接终止进程：Windows rc=3、Unix rc=128+sig，部署侧按 exit 0 判停机成功被误判。
    本子类抑制该重放（见 capture_signals），退出码由 cmd_serve 显式区分。
    """

    async def shutdown(self, sockets=None) -> None:
        from bidqa.api.chat.sse import broadcast_shutdown  # cli 是入口层，允许 import api

        broadcast_shutdown()
        await super().shutdown(sockets)

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """抑制 uvicorn「停机完成后重放捕获信号」（NIGHT-014：优雅停机 → exit 0）。

        父实现（uvicorn 0.54 server.py capture_signals）在 with 退出时先恢复原
        handler，再把捕获到的信号 raise_signal 重放——默认处置终止进程，停机本体
        早已完成却拿到 rc=3/128+sig。本包装在父实现恢复 handler **之前**清空
        _captured_signals，重放循环空转；handler 安装/恢复语义完全继承父实现
        （含非主线程直通、Ctrl+Break 等三信号同装）。finally 清空兼管异常路径：
        停机期异常不再被重放掩盖成 rc=3，真实异常退出码与 traceback 原样向上。
        兼容口径：uvicorn>=0.30（锁定 0.54.0）的 Server._captured_signals。
        """
        with super().capture_signals():
            try:
                yield
            finally:
                self._captured_signals.clear()


def cmd_serve(args, settings: Any, *, app: Any = "bidqa.main:app") -> int:
    """app 仅限测试注入最小 ASGI 对象（免拉起真实 bidqa.main 的外部服务依赖）。"""
    try:
        settings.validate_core()
    except ConfigError as exc:
        print(f"[bidqa] 启动失败：{exc}", file=sys.stderr)
        return 2
    config = uvicorn.Config(
        app, host=args.host, port=args.port, workers=1,
        log_level="info",
        timeout_graceful_shutdown=10)  # 停机窗口：SSE 广播 error 后客户端有 10s 收尾（P1-8）
    server = BidqaServer(config)
    server.run()
    if server.force_exit:
        # SIGINT 连发（Ctrl+C 强杀，handle_exit 置位）：修复前由信号重放天然产生
        # 非零退出码，重放被抑制后在此显式保持——130=128+SIGINT 惯例。
        return 130
    return 0  # 优雅停机（含无信号主动停机）——部署侧按 exit 0 判定停机成功
