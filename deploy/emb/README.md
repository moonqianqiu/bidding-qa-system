# 自建 bge-m3 嵌入服务（deploy/emb）

> 目的：让 bge-m3 的**学习型稀疏**真正交付（第三方 `/embeddings` 端点普遍不返回 `sparse_embedding`），
> 使检索的三条向量通道（dense / sparse / sparse_bm25）全量可用。设计口径见 `docs/01` §4.1，
> 运维步骤见 `docs/部署手册.md` §3.1。

## 组成

| 文件 | 作用 |
|---|---|
| `install.sh` | venv + torch + FlagEmbedding（版本成组钉死，见脚本注释） |
| `fetch-weights.sh` | 从 `hf-mirror.com` 拉 `BAAI/bge-m3`（`pytorch_model.bin` + `sparse_linear.pt`/`colbert_linear.pt` 线性头——**缺 sparse_linear.pt 就没有学习型稀疏**） |
| `server.py` | FastAPI：`POST /v1/embeddings` 返回 `embedding[1024]` + `sparse_embedding{indices,values}`；`GET /v1/models`、`GET /health` |
| `emb-start.sh` / `emb-stop.sh` | 用户级 nohup 起停（不依赖 root/systemd），pid 文件 + 日志 |

## 部署（实测机器：Ubuntu 24.04 + L20 48G）

```bash
export EMB_BASE=$HOME/group_4/z/emb          # 默认即此路径
mkdir -p "$EMB_BASE"/{models,logs,run,app}
cp server.py "$EMB_BASE/app/"
bash install.sh
bash fetch-weights.sh
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "$EMB_BASE/run/api-keys.txt"
chmod 600 "$EMB_BASE/run/api-keys.txt"
bash emb-start.sh
curl -s http://127.0.0.1:8002/health
```

客户端（`backend/.env`）：

```
EMBEDDING_BASE_URL=http://<host>:8002/v1
EMBEDDING_API_KEY=<run/api-keys.txt 内容>
EMBEDDING_MODEL=BAAI/bge-m3
```

然后全量重灌并切换集合（见部署手册 §3/§4；`QDRANT_COLLECTION` 指向新集合，旧集合保留作回滚口）。

## 注意

- **密钥不入库**：`run/api-keys.txt` 只在服务器上生成；`.env` 被 `.gitignore` 排除。
- 单卡串行：进程内锁，一次处理一个 batch；显存静态 ≈1.5 GiB（fp16）。
- 换回第三方端点无需改代码：客户端对缺 `sparse_embedding` 的响应按 None 降级并打一次 WARNING，
  hybrid 自动退化为 dense 单路。
