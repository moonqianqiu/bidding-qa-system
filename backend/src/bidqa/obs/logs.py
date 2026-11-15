"""观测状态（docs/01 §2 obs 层；docs/01 §6 单进程约定）：请求计数 + 内存日志环。

从 api/system.py 归位（重构 C1 pilot）：观测状态曾被业务路由反向依赖
（ask/vision/chat_stream 从管理路由模块 import note_request——边界病灶的实证）。
归位后方向单向：API 调用 obs，obs 不 import API（tests/test_layering.py R2/R4 钉死）。

脱敏先具体后通用（AGENTS 硬约束 6）：先 API key 形态，其余规则按需追加。
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque

# 进程内请求计数（note_request 写入；metrics 端点读取）。单进程假定（workers=1）。
request_stats: dict[str, dict[str, int]] = {}

# 内存环形日志缓冲（MemoryLogHandler 写入；/api/system/logs 读取）。
log_buffer: deque[dict[str, str]] = deque(maxlen=500)

_KEY_RE = re.compile(r"(sk-[A-Za-z0-9]{4})[A-Za-z0-9-]+([A-Za-z0-9]{4})")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-.]{8,}")
_KV_RE = re.compile(
    r"((?:api[_-]?key|access[_-]?token|password)['\"]?\s*[:=]\s*)['\"]?[A-Za-z0-9_\-]{6,}",
    re.IGNORECASE)
_URL_CRED_RE = re.compile(r"((?:https?|wss?)://)([^\s/@:]+):([^\s/@]+)@")


def mask_secrets(text: str) -> str:
    """脱敏先具体后通用（docs/02 H 域；审计 P2-39：原只有 sk- 一条具体规则——
    Bearer token / api_key= / URL 内嵌凭据曾裸进日志并经 /api/system/logs 回显）。"""
    text = _URL_CRED_RE.sub(r"\1***@", text)
    text = _KV_RE.sub(r"\1****", text)
    text = _BEARER_RE.sub(r"\1****", text)
    text = _KEY_RE.sub(r"\1****\2", text)
    return text


def note_request(path: str, status_code: int) -> None:
    """请求计数（H2 行为指标：限流/4091 提前返回不计入，调用方自行把握时机）。"""
    entry = request_stats.setdefault(path, {"count": 0, "errors": 0})
    entry["count"] += 1
    if status_code >= 500:
        entry["errors"] += 1


class MemoryLogHandler(logging.Handler):
    """内存环形日志缓冲（单进程）；写入前脱敏。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = mask_secrets(self.format(record))
        except Exception:
            return
        log_buffer.append({"ts": time.strftime("%H:%M:%S"),
                           "level": record.levelname,
                           "name": record.name, "message": message[:500]})


class MaskedStreamHandler(logging.StreamHandler):
    """stderr handler（backend.log 的落盘出口）——**先脱敏后写**（审计 P2-4：
    原 StreamHandler 裸写，sk- 密钥直接入 backend.log；硬约束 6 曾只覆盖内存环）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = mask_secrets(self.format(record))
            self.stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def setup_log_buffer(level: int = logging.INFO) -> None:
    # 幂等（审计 P2-4：root 的 MemoryLogHandler 曾无条件叠加——3× create_app
    # 每条日志重复 3 份入环）
    root = logging.getLogger()
    if not any(isinstance(h, MemoryLogHandler) for h in root.handlers):
        handler = MemoryLogHandler()
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    # 应用级日志同步落 stderr（start.sh 2>&1 → backend.log）。此前根 logger 只有内存环，
    # 「上游流式响应为空」等 warning 在 backend.log 里永远看不到（2026-11-08 实证修复）。
    # uvicorn 自有 handler 不传播到 root，不会重复输出。幂等：重复 setup 不叠 handler。
    bidqa = logging.getLogger("bidqa")
    if not any(isinstance(h, MaskedStreamHandler) for h in bidqa.handlers):
        stream = MaskedStreamHandler()  # 默认 stderr；先脱敏后写（P2-4）
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        bidqa.addHandler(stream)
    bidqa.setLevel(level)
