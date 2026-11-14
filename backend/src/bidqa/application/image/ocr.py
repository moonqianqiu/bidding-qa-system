"""OCR 通道（docs/01 §3.1，v2.3 双通道）：高分辨率原件 → 纯文本提取。

只做一次多模态调用；槽位选择/失败兜底/包装语义在 analyze.py。
"""

from __future__ import annotations

from bidqa.application.image import prompts


def ocr_extract(client, data_url: str) -> str:
    """OCR 槽位解析原件（temperature 0——提取要逐字稳定，不要创造性）。

    上游异常原样抛出（analyze 统一处理：auto 兜底 / 手动转 5031）。
    """
    return str(client.chat(prompts.ocr_messages(data_url), temperature=0.0)).strip()
