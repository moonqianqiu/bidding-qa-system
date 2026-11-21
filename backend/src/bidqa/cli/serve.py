"""serve 子命令：uvicorn 启动（workers=1 硬性约定，docs/01 §6）。"""

from __future__ import annotations

import sys
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
    """

    async def shutdown(self, sockets=None) -> None:
        from bidqa.api.chat.sse import broadcast_shutdown  # cli 是入口层，允许 import api

        broadcast_shutdown()
        await super().shutdown(sockets)


def cmd_serve(args, settings: Any) -> int:
    try:
        settings.validate_core()
    except ConfigError as exc:
        print(f"[bidqa] 启动失败：{exc}", file=sys.stderr)
        return 2
    config = uvicorn.Config(
        "bidqa.main:app", host=args.host, port=args.port, workers=1,
        log_level="info",
        timeout_graceful_shutdown=10)  # 停机窗口：SSE 广播 error 后客户端有 10s 收尾（P1-8）
    BidqaServer(config).run()
    return 0
