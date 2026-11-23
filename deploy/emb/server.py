"""自建 bge-m3 嵌入服务（SLali / L20）：OpenAI 兼容 /embeddings，dense + 学习型 sparse。

响应形状与 `clients/siliconflow.py:_embed` 的解析口径**逐字段对齐**：

    data[i] = {"object": "embedding", "index": i,
               "embedding": [1024 floats],
               "sparse_embedding": {"indices": [int...], "values": [float...]}}

缺 `sparse_embedding` 会被客户端按 None 静默降级（NIGHT-044 口径债），故本服务恒定返回；
sparse 全零也照实返回空数组（客户端据此如实降级，不伪造）。

鉴权：`Authorization: Bearer <key>`，key 取 `EMB_API_KEY` 或 `EMB_API_KEY_FILE` 首个有效行。
单卡串行：encode 走进程内锁（一次一个 batch），避免并发抢显存。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent.parent
MODEL_PATH = os.environ.get("EMB_MODEL_PATH", str(BASE / "models/bge-m3"))
DEVICE = os.environ.get("EMB_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
MAX_LENGTH = int(os.environ.get("EMB_MAX_LENGTH", "8192"))
BATCH_SIZE = int(os.environ.get("EMB_BATCH", "32"))
MAX_INPUTS = int(os.environ.get("EMB_MAX_INPUTS", "256"))


def _load_api_key() -> str:
    key = os.environ.get("EMB_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(os.environ.get("EMB_API_KEY_FILE", str(BASE / "run/api-keys.txt")))
    if not key_file.is_file():
        raise RuntimeError(f"EMB_API_KEY / {key_file} 均未提供密钥")
    for line in key_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise RuntimeError(f"{key_file} 里没有有效密钥行")


API_KEY = _load_api_key()

from FlagEmbedding import BGEM3FlagModel  # noqa: E402  （密钥校验失败就不该加载模型）

model = BGEM3FlagModel(MODEL_PATH, use_fp16=True, device=DEVICE)
_encode_lock = threading.Lock()
_started_at = time.time()

app = FastAPI(title="bge-m3 embeddings", version="1.0")


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: str | list[str]
    encoding_format: str | None = "float"
    dimensions: int | None = None
    max_length: int | None = None


def _require(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": "missing bearer token"})
    if authorization[7:].strip() != API_KEY:
        raise HTTPException(status_code=401, detail={"error": "invalid api key"})


def _sparse_items(lexical: dict[str, float]) -> tuple[list[int], list[float]]:
    """词表 id -> (indices, values)；滤掉非正权重，按 id 升序（确定性输出）。"""
    pairs = []
    for tok, weight in lexical.items():
        w = float(weight)
        if w > 0.0 and np.isfinite(w):
            pairs.append((int(tok), w))
    pairs.sort(key=lambda p: p[0])
    if not pairs:
        return [], []
    idx, val = zip(*pairs)
    return list(idx), list(val)


def _encode(texts: list[str], max_length: int) -> list[dict[str, Any]]:
    with _encode_lock:
        out = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            max_length=max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
    dense = np.asarray(out["dense_vecs"], dtype=np.float32)
    if dense.ndim == 1:
        dense = dense.reshape(1, -1)
    # L2 归一（幂等：已归一时不变；不归一余弦分数不可比）
    norm = np.linalg.norm(dense, axis=1, keepdims=True)
    norm[norm == 0] = 1.0
    dense = dense / norm
    lexical = out["lexical_weights"]
    items = []
    for i in range(dense.shape[0]):
        idx, val = _sparse_items(lexical[i])
        items.append({
            "object": "embedding",
            "index": i,
            "embedding": [float(x) for x in dense[i].tolist()],
            "sparse_embedding": {"indices": idx, "values": val},
        })
    return items


def _token_count(texts: list[str]) -> int:
    try:
        return int(sum(len(model.tokenizer.encode(t, add_special_tokens=False)) for t in texts))
    except Exception:  # 计数只用于 usage 字段，失败不拖垮请求
        return sum(len(t) for t in texts)


@app.post("/v1/embeddings")
@app.post("/embeddings")
def create_embeddings(req: EmbeddingRequest, authorization: str | None = Header(default=None)):
    _require(authorization)
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    texts = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts:
        raise HTTPException(status_code=400, detail={"error": "input is empty"})
    if len(texts) > MAX_INPUTS:
        raise HTTPException(status_code=400,
                            detail={"error": f"input too large: {len(texts)} > {MAX_INPUTS}"})
    if req.encoding_format not in (None, "float"):
        raise HTTPException(status_code=400, detail={"error": "only encoding_format=float"})
    max_length = min(req.max_length or MAX_LENGTH, MAX_LENGTH)
    try:
        items = _encode(texts, max_length)
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(status_code=503, detail={"error": f"cuda oom: {exc}"}) from exc
    n_tokens = _token_count(texts)
    return {
        "object": "list",
        "model": req.model or "BAAI/bge-m3",
        "data": items,
        "usage": {"prompt_tokens": n_tokens, "total_tokens": n_tokens},
    }


@app.get("/v1/models")
@app.get("/models")
def list_models(authorization: str | None = Header(default=None)):
    _require(authorization)
    return {"object": "list",
            "data": [{"id": "BAAI/bge-m3", "object": "model", "owned_by": "self-hosted"}]}


@app.get("/health")
def health():
    info: dict[str, Any] = {
        "status": "ok",
        "model": MODEL_PATH,
        "device": DEVICE,
        "dim": 1024,
        "sparse": "always",
        "max_length": MAX_LENGTH,
        "uptime_s": round(time.time() - _started_at, 1),
    }
    if torch.cuda.is_available():
        info["gpu_mem_allocated_mib"] = round(torch.cuda.memory_allocated() / 2**20, 1)
        info["gpu_mem_reserved_mib"] = round(torch.cuda.memory_reserved() / 2**20, 1)
    return info
