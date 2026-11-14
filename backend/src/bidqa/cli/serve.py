"""serve 子命令：uvicorn 启动（workers=1 硬性约定，docs/01 §6）。"""

from __future__ import annotations

import sys
from typing import Any

from bidqa.config import ConfigError


def cmd_serve(args, settings: Any) -> int:
    try:
        settings.validate_core()
    except ConfigError as exc:
        print(f"[bidqa] 启动失败：{exc}", file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run("bidqa.main:app", host=args.host, port=args.port, workers=1, log_level="info")
    return 0
