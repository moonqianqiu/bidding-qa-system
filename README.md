# 招投标智能问答机器人（bidding-qa-system）

> 可自托管的招投标领域 RAG + Agent 问答系统：用户用自然语言提问，系统自动判断去
> **知识库 / 知识图谱 / 业务数据库 / 联网** 取数，给出**带来源引用**的答案，数字类问题自动配图表。
> 状态：**v2.0 重构版 + v2.1 图谱归一完成**（2026-11-14，curated 9 项合并）——后端测试 435 例、分层守卫+变异验证通过。
> 本文边界：只回答「这是什么、怎么跑起来、下一份文档读哪个」。

## 1. 能力与边界

> **定位**：练习版（PRD 衍生单机问答实现）；验收门槛 = 老师评估 + 甲方验收，PRD 逐项对照见 [docs/03 附录B](./docs/03_项目规划.md)。

| 能力 | 说明 |
|---|---|
| 知识库问答 | 法规/流程/FAQ/招标文件解析文本；混合检索（dense + sparse 两路独立召回 + 客户端 RRF 融合，k 按问题分类注入 + 重排精排；BM25 为嵌入灾备单路、不参与融合），答案带来源条目与相关度 |
| 业务数据查询 | PostgreSQL 明细表 6 类查询（关键词/过滤/聚合/TopN/分组/时间范围），字段白名单防注入，自动出图 |
| 图谱查询 | Neo4j 标的物-采购人-供应商-代理机构-地区关系网络，6 类查询，自动出图 |
| PDF 招标文件解析 | MinerU 云端解析入库，pdfplumber 本地兜底 |
| 联网检索 | Tavily；默认关、显式开启，带缓存与降级话术 |
| 澄清追问 | 仅首轮、机制级防连问；地区/时间/标的物任一限定即放行检索 |
| 多轮对话 | 超窗旧对话自动摘要压缩为「前情提要」 |
| 流式输出 | SSE 七事件协议（ping/seq/schema_v），工具调用文本泄漏防护 |
| 反馈闭环 | 点赞/点踩落库，点踩触发判分 |
| 双评测体系 | 离线黄金集跑批 + 在线评测（行为指标全量免费、判分采样默认 0、点踩必评） |

**边界（明确不做什么）**：不生成招投标文件（项目 2「智标」的职责）· 不做投标决策/报价建议 ·
不提供法律意见 · 不做多租户 · 不做实时数据同步（离线导入快照）· 知识库没覆盖的直说「未包含」。

## 2. 架构与技术栈

```
frontend/  Next.js 16 + React 19 + Tailwind 4      app 薄壳 + features/chat · features/console · lib
backend/   FastAPI（Python 3.14 + uv，src/bidqa）   api · application · agent · tools · rag · clients · database · auth · ingest · eval · obs · cli
services/  绿色安装数据服务                          Qdrant / PostgreSQL / Neo4j 官方 zip，数据在 data/db/
deploy/    启停编排脚本                              start / stop / status / e2e / 服务安装
eval/      黄金集与评测产物                          golden/（用例，入 git）
```

- **模型面全 API 化**：本地零 GPU 零模型。主模型 / 视觉 / 判分**三槽位**各自一份
  `{base_url, api_key, model}` 配置，同一 **OpenAI-compatible** 适配器覆盖，无预置厂商枚举；
  嵌入/重排走 SiliconFlow（bge-m3 双路向量，本地 jieba BM25 灾备通道）。
- 分层职责、接口契约、数据模型、配置三列表、进程模型的**权威定义**：
  [docs/01_系统框架设计.md](./docs/01_系统框架设计.md)；总体图解：[docs/04_项目结构流程图.md](./docs/04_项目结构流程图.md)。

## 3. 快速开始

```bash
bash deploy/download-services.sh && bash deploy/install-services.sh   # 首次：绿色安装数据服务
cd backend && uv sync && cp .env.example .env                         # 填三槽位等配置（docs/01 §5）
cd frontend && npm install
bash deploy/start.sh                                                  # PG → Qdrant → Neo4j → 后端
cd frontend && npm run dev                                            # http://127.0.0.1:3000
```

完整安装 / 启停 / 数据初始化 / 日常运维命令见 [docs/部署手册.md](./docs/部署手册.md)。

## 4. 阅读路径（按目的选入口，从左到右）

| 你的目的 | 路径 |
|---|---|
| **跑起来用** | 本文 §3 → [部署手册](./docs/部署手册.md) |
| **看懂原理** | [01](./docs/01_系统框架设计.md) §1-§2 → [04 图解与走读](./docs/04_项目结构流程图.md) → [核心算法](./docs/核心算法.md) |
| **改代码** | [AGENTS.md](./AGENTS.md) → [01](./docs/01_系统框架设计.md) §3 契约 → 对应模块 → 改完跑 [03 §0.1 DoD](./docs/03_项目规划.md) + [部署手册 §4](./docs/部署手册.md) |
| **接手 / 看现状** | [AI交接文档](./docs/AI交接文档.md) → [AGENTS.md](./AGENTS.md) |
| **查问题** | [部署手册 §6 坑表](./docs/部署手册.md) → [开发日志](./docs/开发日志.md) |

## 5. 文档地图（每份文档只回答一个问题）

| 文档 | 回答什么 | 不放什么 |
|---|---|---|
| [AGENTS.md](./AGENTS.md) | 改代码的规则：硬约束 / 红线 / 契约流程 / 改动剧本 | 系统介绍 |
| [docs/01_系统框架设计.md](./docs/01_系统框架设计.md) | 设计权威：模块职责 / 接口契约 / 数据模型 / 配置 / 进程模型 | 过程与状态 |
| [docs/02_功能结构细化.md](./docs/02_功能结构细化.md) | 做什么、做到什么程度：8 大功能域 · 71 个功能点 · 量化验收指标 | 设计细节 |
| [docs/03_项目规划.md](./docs/03_项目规划.md) | 为什么这么做：定位 / 决策 / DoD / 风险登记 / PRD 对照 | 已收官的任务明细 |
| [docs/04_项目结构流程图.md](./docs/04_项目结构流程图.md) | 系统长什么样：图解 / 代码级走读 / 概念词典 / 代码地图 | 参数表 |
| [docs/核心算法.md](./docs/核心算法.md) | 怎么算的：检索链路 / 工具循环 / 归一入口 / 组件机制 | 接口契约 |
| [docs/部署手册.md](./docs/部署手册.md) | 怎么跑、怎么修：安装 / 启停 / 运维日历 / 排障坑表 | 设计 |
| [docs/AI交接文档.md](./docs/AI交接文档.md) | 现在什么状态：质量现状 / 未完成清单 / 环境速览 | 规则 |
| [docs/开发日志.md](./docs/开发日志.md) | 怎么走过来的：提示词 / 踩坑 / 决策 / 演练记录（只追加） | 现状 |
| 参考资料-招投标智能问答应用系统/ | 参考项目本地归档（gitignore，不入库）；入口见其 `00_项目总览/` | — |

## 6. 质量与验收

```bash
cd backend && uv run pytest -q        # 全量全绿（服务离线时集成例 skip 属预期；当前 435 例）
cd frontend && npx tsc --noEmit
bash deploy/e2e-chat.sh               # SSE 三走向（机器断言版；真实调用主模型）
cd backend && uv run bidqa eval --golden ../eval/golden/agent_v0.jsonl --mode agent
```

- 量化验收线（[docs/02 §0.1](./docs/02_功能结构细化.md)）：工具选择 ≥90% · hit_cap <10% · 澄清 100% 触发 · 检索 P95 ≤1.5s
- 版本标签：`v0.1-scaffold`（M0 奠基）→ `v0.2-m2`（Agent+多源）→ `v0.3-m3`（体验+质量件）→ `v1.0`（收口）
- 文档机械审计：`bash .auto/measure.sh`（doc_defects 应为 0）
