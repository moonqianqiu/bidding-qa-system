#!/usr/bin/env bash
# BAAI/bge-m3 权重下载（huggingface.co 不通，走 hf-mirror）
# 该仓库无 model.safetensors，权重是 pytorch_model.bin；flag 模型还需 sparse/colbert 线性头。
set -euo pipefail
D="${EMB_BASE:-$HOME/group_4/z/emb}/models/bge-m3"
R=https://hf-mirror.com/BAAI/bge-m3/resolve/main
mkdir -p "$D/1_Pooling"

fetch() {
  local name="$1"
  if ! curl -fsSL --retry 5 --retry-delay 2 -o "$D/$name" "$R/$name"; then
    echo "FAILED: $name" >&2
    exit 1
  fi
  echo "ok $name ($(stat -c%s "$D/$name") bytes)"
}

for f in config.json config_sentence_transformers.json modules.json sentence_bert_config.json \
         tokenizer.json tokenizer_config.json special_tokens_map.json sentencepiece.bpe.model \
         sparse_linear.pt colbert_linear.pt; do
  fetch "$f"
done
fetch "1_Pooling/config.json"
fetch "pytorch_model.bin"
echo "WEIGHTS-DONE"
ls -lh "$D"
