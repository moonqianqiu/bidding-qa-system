#!/usr/bin/env bash
# 自建 bge-m3 嵌入服务：环境安装（SLali 实测可用的版本组合，2026-11-23 NIGHT-065）
#
# 依赖钉死的理由：transformers 5.x 删了 `transformers.modeling_attn_mask_utils`，
# FlagEmbedding 1.4.2 import 即崩；huggingface-hub / datasets / sentence-transformers
# 随之成组对齐（见 docs/部署手册.md §3.1）。
set -euo pipefail
BASE="${EMB_BASE:-$HOME/group_4/z/emb}"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
export PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_HOME="$BASE/models/.hf"

mkdir -p "$BASE"/{models,logs,run,app}
echo "[1/4] venv"
python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install -q -U pip wheel
echo "[2/4] torch（PyPI 默认 wheel 自带 cu12 运行时）"
"$BASE/venv/bin/pip" install -q torch
echo "[3/4] FlagEmbedding + fastapi（版本成组钉死）"
"$BASE/venv/bin/pip" install -q FlagEmbedding fastapi "uvicorn[standard]" \
  "transformers==4.57.6" "huggingface-hub==0.36.2" "datasets==3.6.0" "sentence-transformers==3.4.1"
echo "[4/4] 版本确认"
"$BASE/venv/bin/python" -c "import torch, transformers, FlagEmbedding; \
print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), 'tf', transformers.__version__)"
echo "INSTALL-DONE"
