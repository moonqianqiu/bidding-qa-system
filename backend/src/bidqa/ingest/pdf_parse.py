"""F2 PDF 解析（docs/02）：MinerU 官方 API（云端，版面/表格/扫描件强）+ pdfplumber 本地兜底。

中间格式契约（MinerU→切分→payload.page 的数据契约）：Markdown + 页码标记 `<!-- page:N -->`。
MinerU 失败/未配置 → pdfplumber 兜底；两者皆败 → 异常（调用方把该文档入「拒收清单」）。
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from typing import Any

import httpx
import pdfplumber

PAGE_MARK = "<!-- page:{n} -->"
_PAGE_MARK_RE = re.compile(r"<!--\s*page:(\d+)\s*-->")

_MINERU_BASE = "https://mineru.net/api/v4"


def page_mark(n: int) -> str:
    return PAGE_MARK.format(n=n)


def split_pages(markdown: str) -> list[tuple[int, str]]:
    """契约解析：页码标记 → [(页码, 该页文本)]（切分层据此写 payload.page）。"""
    marks = list(_PAGE_MARK_RE.finditer(markdown))
    pages: list[tuple[int, str]] = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(markdown)
        pages.append((int(m.group(1)), markdown[start:end].strip()))
    return pages


# ── pdfplumber 兜底（纯 CPU 无模型）──
def _pdfplumber_parse(path: str) -> dict[str, Any]:
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            chunks: list[str] = [page.extract_text() or ""]
            for table in page.extract_tables() or []:
                rows = ["| " + " | ".join((cell or "").replace("\n", " ")
                                          for cell in row) + " |" for row in table]
                chunks.append("\n".join(rows))  # 表格按行保留
            body = "\n\n".join(c for c in chunks if c.strip())
            parts.append(f"{page_mark(i)}\n{body}")
    return {"markdown": "\n\n".join(parts), "engine": "pdfplumber"}


# ── MinerU 官方 API（申请上传链接 → PUT → 轮询 → zip 内 full.md）──
def _mineru_parse(path: str, api_key: str, timeout: float = 120.0,
                  poll_interval: float = 3.0) -> dict[str, Any]:
    file_name = path.replace("\\", "/").rsplit("/", 1)[-1]
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        resp = client.post(f"{_MINERU_BASE}/file-urls/batch", headers=headers, json={
            "enable_formula": True, "language": "ch",
            "files": [{"name": file_name}],
        })
        if resp.status_code != 200:
            raise RuntimeError(f"MinerU 申请上传链接失败: HTTP {resp.status_code}")
        data = resp.json()["data"]
        batch_id, upload_url = data["batch_id"], data["file_urls"][0]

        with open(path, "rb") as f:
            put = client.put(upload_url, content=f.read())
        if put.status_code not in (200, 201):
            raise RuntimeError(f"MinerU 上传失败: HTTP {put.status_code}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            poll = client.get(f"{_MINERU_BASE}/extract-results/batch/{batch_id}",
                              headers=headers)
            if poll.status_code == 200:
                item = poll.json()["data"]["extract_result"][0]
                if item.get("state") == "done":
                    zip_url = item["full_zip_url"]
                    break
                if item.get("state") == "failed":
                    raise RuntimeError(f"MinerU 解析失败: {item.get('err_msg')}")
            time.sleep(poll_interval)
        else:
            raise RuntimeError("MinerU 轮询超时")

        archive = client.get(zip_url)
        if archive.status_code != 200:
            raise RuntimeError(f"MinerU 结果下载失败: HTTP {archive.status_code}")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
            md_name = next(n for n in zf.namelist() if n.endswith(".md"))
            markdown = zf.read(md_name).decode("utf-8")
    # MinerU 的 md 无页码标记 → 由 content_list 补记不可行（zip 里无分页）；
    # 契约统一：整档按单页标记处理，页码由 content_list.json 的 page_idx 在此不可得时保持缺省。
    if not _PAGE_MARK_RE.search(markdown):
        markdown = f"{page_mark(1)}\n{markdown}"
    return {"markdown": markdown, "engine": "mineru"}


def parse_pdf(path: str, api_key: str | None = None) -> dict[str, Any]:
    """入口：MinerU 优先（配置 key 时），失败/未配置 → pdfplumber 兜底（如实记 engine）。"""
    if api_key:
        try:
            return _mineru_parse(path, api_key)
        except Exception as exc:
            result = _pdfplumber_parse(path)
            result["degraded"] = f"mineru_fallback: {exc}"
            return result
    return _pdfplumber_parse(path)
