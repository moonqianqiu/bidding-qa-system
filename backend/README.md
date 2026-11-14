# bidqa 后端

招投标智能问答机器人 FastAPI 后端。分层架构与接口契约见 [`docs/01_系统框架设计.md`](../docs/01_系统框架设计.md)。

## 快速开始

```bash
# 1) 安装依赖（Python 3.14 + uv）
uv sync

# 2) 配置：复制模板并填写三槽位（核心槽位缺失 = 启动失败）
cp .env.example .env

# 3) 配置体检（核心缺失/能力降级一览）
uv run bidqa doctor

# 4) 启动（workers=1 硬性约定，docs/01 §6）
uv run bidqa serve    # http://127.0.0.1:8000/api/health

# 测试
uv run pytest
```

## 目录结构（Python 工程自包含在本目录）

```
backend/
├─ pyproject.toml / uv.lock   uv 工程定义与锁文件
├─ .venv/                     虚拟环境（uv 自动创建，点前缀隐藏目录；IDE 解释器选 .venv/Scripts/python.exe）
├─ src/bidqa/                 源码（src layout，包名 bidqa）
└─ tests/                     pytest
```

## 说明

- `src/bidqa/` 按 api / agent / rag / tools / clients / database / ingest / eval / obs / skills 分层（docs/01 §1）
- 所有 `uv` / `pytest` 命令在本目录（backend/）执行；仓库根目录**没有** Python 工程，别在根目录找 src/ 或 .venv
- 数据服务（Qdrant/PostgreSQL/Neo4j）绿色安装在仓库根 `deploy/`，见 `../README.md` §3.4
