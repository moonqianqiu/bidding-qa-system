"""视觉解析提示词（docs/01 §3.1）：Qwen 路由通道与 OCR 通道的全部文案与消息组装。

v2.3 双通道核心取舍：Qwen 只调用一次，永远同时返回 route 判断与 description
（描述必填）——mode 只决定「用哪个输出」，不决定「调几次」。这样路由判到 OCR
但 OCR 槽位缺失/失败时，直接用手里的描述降级，零额外调用。
"""

from __future__ import annotations

ROUTE_DESC_SYSTEM = (
    "你是招投标文件图片解析助手。完成两件事，只输出一个 JSON 对象，不要输出任何其它文字：\n"
    '{"route": "describe 或 ocr", "description": "图片的结构化文字描述"}\n'
    "1. route：判断这张图是否属于「文档类」——合同、招标文件、营业执照、资质证书、报表、"
    "通知函等以文字信息为主的图片（文字密度高、需要精确引用编号/金额/日期/条款）选 ocr；"
    "照片、图表、软件界面截图等选 describe。\n"
    "2. description：永远必填。只描述图片中真实可见的内容（文字/表格/数字/布局），"
    "不推测、不编造；看不清的部分如实说明。")
ROUTE_DESC_USER = "请解析这张图片。"

OCR_SYSTEM = (
    "你是 OCR 文字提取助手。按阅读顺序提取图片中的全部文字，输出纯文本："
    "不要翻译、不要总结、不要添加任何解释或标注；表格按行输出，单元格之间用空格分隔；"
    "看不清的字用「□」占位，不要猜测。")
OCR_USER = "请提取这张图片中的全部文字。"


def _vision_messages(system: str, user_text: str, data_url: str) -> list[dict]:
    """多模态 list-content 消息（同一 OpenAI-compatible 适配器）。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]


def route_desc_messages(data_url: str) -> list[dict]:
    """Qwen 路由+描述消息（看压缩预览，不看原件）。"""
    return _vision_messages(ROUTE_DESC_SYSTEM, ROUTE_DESC_USER, data_url)


def ocr_messages(data_url: str) -> list[dict]:
    """OCR 通道消息（看高分辨率原件，不看压缩预览）。"""
    return _vision_messages(OCR_SYSTEM, OCR_USER, data_url)
