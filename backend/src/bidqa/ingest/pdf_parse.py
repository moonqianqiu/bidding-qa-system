"""F2 PDF 解析（docs/02）：MinerU 官方 API（云端，版面/表格/扫描件强）+ pdfplumber 本地兜底。

中间格式契约（MinerU→切分→payload.page 的数据契约）：Markdown + 页码标记 `<!-- page:N -->`。
MinerU 失败/未配置 → pdfplumber 兜底；两者皆败 → 异常（调用方把该文档入「拒收清单」）。
另有纯文本直入 parse_txt：法规采集产物 cleaned.txt 复用同一契约（NIGHT-019）。
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


# ── 纯文本直入（法规采集产物 cleaned.txt；头部两种变体：横幅行可省）──
_META_BANNER_RE = re.compile(r"【语料元数据")
_META_KEY_RE = re.compile(r"^[^\s：:]{1,30}[：:]")  # 「键：值」标注行
_META_SEP_RE = re.compile(r"^[─━—=_\-]{5,}\s*$")  # 头部分隔线
_META_MAX_LINES = 32  # 头部行数上限：防退化文件把正文整段吃掉


def strip_meta_header(text: str) -> str:
    """剥离采集头部：从首个标注行到首个分隔线（含）。

    识别规则：首个非空行命中【语料元数据】横幅或「键：值」即进入头部模式，
    逐行吞掉标注行/空行，遇分隔线连它一起止；无分隔线则在首个非标注行止
    （不越过正文）。未命中头部特征 → 原样返回。
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not (_META_BANNER_RE.search(lines[i])
                               or _META_KEY_RE.match(lines[i])):
        return text
    start = i
    while i < len(lines) and i - start < _META_MAX_LINES:
        line = lines[i]
        if _META_SEP_RE.match(line):
            i += 1
            break
        if (_META_BANNER_RE.search(line) or _META_KEY_RE.match(line)
                or not line.strip()):
            i += 1
            continue
        break  # 非标注行：止步，正文自此处保留
    return "\n".join(lines[i:]).strip()


def parse_txt(path: str) -> dict[str, Any]:
    """纯文本直入：剥元数据头 → 整篇单页标记（NIGHT-019）。

    页码口径与 MinerU 兜底一致：txt 无真实分页，整档按 page:1 处理；
    块级切分（第X章/第X条/兜底段落）由 enhance_split.split_text 完成。
    正文剥空（空文件/仅剩元数据头）→ ValueError（调用方按解析失败拒收）。
    """
    with open(path, encoding="utf-8-sig") as f:
        body = strip_meta_header(f.read())
    if not body.strip():
        raise ValueError(f"纯文本无正文（空文件或仅剩元数据头）: {path}")
    return {"markdown": f"{page_mark(1)}\n{body}", "engine": "txt"}
