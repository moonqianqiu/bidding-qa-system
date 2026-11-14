# AI 交接文档

> **给接手的 AI 助手 / 未来会话**：项目现在什么状态、还差什么、怎么验证。
> 分工：规则与红线在 `AGENTS.md`；命令与排障在 `docs/部署手册.md`；图解走读在 `docs/04`；设计细节在 `docs/01-03`。
> 本文只放**易变的状态**：现状快照、未完成清单、环境速览、检查清单。更新：2026-11-14（v2.0 重构收口）。

## 1. 一分钟上手（下一任先做这四件事）

1. 读 `AGENTS.md`（硬约束 10 条 + 预算红线）→ `docs/04` §6（一次提问的代码级走读，路径已对齐 v2.0 结构）
2. 结构速览：`docs/01` §1 分层树——**api=HTTP 适配壳、application=业务用例、工厂在各包 factory.py、组合根只有 container.build_agent_stack**；分层边界由 `tests/test_layering.py` AST 钉死（违规即红）
3. 拉起服务：`bash deploy/start.sh` → **立刻** `curl 127.0.0.1:8000/api/health` 六组件全 `ok`
4. 跑验证三连：`cd backend && uv run pytest -q`（459 例）/ `cd frontend && npm run build` / `bash deploy/e2e-chat.sh`（真实调用主模型，先看 §3 成本口径）

## 2. 质量现状（快照，接手时按 §6 复核）

| 项 | 状态 | 复核命令 |
|---|---|---|
| 标签 | `v1.0-baseline` → `v2.0-refactored` → `v2.1-kg-normalize` → `v2.2-structure-cleanup` → `v2.3-dual-vision` | `git tag -l && git log --oneline -12` |
| 后端测试 | **459 passed 全绿（含 Neo4j/PG 集成，服务需在线）**；测试目录=源码目录一条规则 | `cd backend && uv run pytest -q` |
| 分层守卫 | `tests/test_layering.py` **R1-R11** 全部空集（永久护栏）+ 每条规则「能红」自检 | `pytest tests/test_layering.py -q` |
| 类型检查 | pyrefly **49**（基线 56；bad-override ×5 + missing-attribute ×3 已清零，余为 psycopg 行类型噪音） | `uv run --with pyrefly pyrefly check src` |
| 前端类型 | 绿（`npm run build` 五路由全预渲染） | `cd frontend && npm run build` |
| e2e 三走向 | `--mutation-check` 免费自证 PASS；真跑调主模型（付费，跑前看 §3 成本） | `bash deploy/e2e-chat.sh --mutation-check` |
| Agent 评测 | 13/13 工具选择 1.0（快照见旧仓历史；新仓重跑见 §3 收费口径） | `uv run bidqa eval --golden ../eval/golden/agent_v0.jsonl --mode agent` |
| 图谱归一 | v2.1 完成：curated 映射 **9 项合并**（Purchaser 3504→3495，其余五类不变）；合并实体项目数守恒 PASS；分支/校区/附属误并候选全部未合并 | `python ../data/run/kg_acceptance.py` |
| 产品定位 | **练习版**（老师评估 + 甲方验收双门槛），PRD 逐项对照 = docs/03 附录B | —— |
| 旧仓 | `D:/.../bidding-qa-system` **冻结存档**（原地冻结+README 退役声明；数据副本保留），哈希只对应旧历史 | — |

近期变更不在这里维护——看 `git log`（C0-C9 每步一个绿 commit）与 `docs/开发日志.md` 2026-11-14 行。

## 3. 未完成 / 可选清单（诚实版）

| 项 | 性质 | 说明 |
|---|---|---|
| v2.3 双通道付费验收 | 付费动作（待用户） | 代码已全绿；`backend/.env` 待用户自填 `OCR_*` 三行（建议 `PaddlePaddle/PaddleOCR-VL`，SiliconFlow 有模型）→ 重启后端 → 表格截图应 `mode_used=ocr` 且答案引用图中数字、照片应 `mode_used=describe`；开发日志有验收清单 |
| CI / 私有远端 | P0 工程化 | 新仓 `git remote -v` 为空——建私有库 `git remote add origin <url> && git push origin main --tags` |
| `detail_search` 金额排序 | 功能排期 | 查询类型白名单 + order_by 列白名单 + 工具描述更新；**重构完成后另立一轮**（不在重构 commit 混功能） |
| 离群值溯源 | 数据排查 | 66 亿（涡阳 BZGY2023CG141）/ 23.7 亿 全局头部可疑值，源 xlsx 不可考——需与甲方核对源表 |
| embed_store 编码文本不一致 | 工程债（低优先） | dense+sparse 看 `ctx_summary+text`、bm25 词频通道看 `title+text`——改则两处对齐 + 重 ingest 全量；2026-11-10 起空摘要时仅 text，口径见旧记录 |
| clarify.py PROVINCES 词表副本 | 词表收口（C1 检查点已拍板待做） | 与 `ingest/regions.py` 双份会漂移——收口方向：clarify 导入 ingest/regions.PROVINCES（agent→ingest 允许） |
| DI 直调残留 | 测试隔离债（低） | `api/system/metrics.py` 的 `_online_stats`、`_qdrant_counts` 读配置走 get_settings()（仅读端点内部，无测试依赖真实 .env，危害低）；新代码一律 Depends |
| 真实 PDF 语料导入 | 功能候选 | `bidqa ingest pdf` 链路已通（MinerU 云端 + pdfplumber 兜底）；`data/raw/` 为空，语料未灌 |
| 在线评测开启 | 配置决策 | 默认关（`ONLINE_EVAL_ENABLED=false`）；判分是**收费中转**，何时开启由用户拍板 |

## 4. 环境与数据速览

- **配置**：`backend/.env`（**用户自己管**，别代改；2026-11-14 已从旧仓逐字节复制，md5 一致）。三槽位 `CHAT_/VISION_/JUDGE_*`；v2.3 增 `OCR_*`（能力型，未配置=图片自动路由退化为 Qwen 通用描述）；
  `SUBJECT_BATCH_*`=数据加工批量通道（subject-batch 与图谱归一默认通道）；
  `EMBEDDING_/RERANK_`=SiliconFlow；`WEB_SEARCH_PROVIDERS`=联网名册（留空=关）；`CHAT_PROFILES`=命名档；
  `CHAT_MAX_TOKENS`=8192（思考型上游先烧 reasoning，预算不足正文空）；`USERS_ENABLED`=true（admin/admin123 种子，**建议改密**）。
  配置语义以 `docs/01` §5 为准
- **服务**：8000 后端 / 3000 前端 dev / 5432 PG / 6333 Qdrant / 7474+7687 Neo4j（当前从**新仓**跑：C9 已切换）
- **数据规模**：PG 8,489 行样例中标数据（subject_matter 全量回填）、Neo4j 六类 **18,519 节点**（v2.1 归一后 Purchaser 3495，其余五类同 v1.0）、FAQ 75 向量
- **数据位置**：`data/db/`（库文件）· `data/run/`（日志/pid）· `backend/eval/results/`（快照+判分缓存，**gitignore**）· `deploy/diagnosis/`（机器级排障归档）
- **前端状态**：localStorage `bidqa.theme/lang/provider/token/user`；会话服务端主（conversations.user_id 归属）
- **结构地图**：`docs/01` §1 分层树 + `docs/04` §8 代码地图（v2.0 已对齐；旧路径 chat_stream.py/auth_core.py/service.py/postgres.py 均已不存在）

## 5. 下任的第一个建议动作

- **要收工程债**：按 §3 前四行做（远端 push → 金额排序 → 离群值 → 词表收口）
- **要补功能**：真实 PDF 语料导入（`bidqa ingest pdf --input x.pdf --source-type 法规 --title "..."`，先 dry-run）
- **要调质量**：先 `eval agent` 留基线快照（付费，跑前算成本），再改工具描述/上下文，对比前后快照
- **要改代码**：先跑 `pytest tests/test_layering.py` 理解边界——新增依赖若红，说明越层了，回 `docs/01` §3.1 对表

## 6. 交接检查清单（接手时逐项打勾）

- [ ] `git status` 干净；`git log --oneline -12` 覆盖 C0-C9（基线→重构逐 commit 绿）
- [ ] `bash deploy/start.sh` 后 `/api/health` 六组件全 ok
- [ ] `cd backend && uv run pytest -q` → 459 passed（服务在线；离线时 Neo4j/PG 集成 skip 属预期并如实记录）
- [ ] `cd frontend && npm run build` 绿（五路由预渲染）
- [ ] `bash deploy/e2e-chat.sh --mutation-check` PASS（免费）；真跑三走向属付费动作（跑前告知用户）
- [ ] `pytest tests/test_layering.py` 3 passed（分层守卫全绿；allowlist 应全空集）
- [ ] 读 `docs/04` §6（走读，路径 v2.0 口径）与 `docs/部署手册.md` §6（排障坑表，排障工具在 deploy/diagnosis/）
- [ ] 确认未动 `.env` 的 VISION / JUDGE 红线（AGENTS.md），`ONLINE_EVAL_ENABLED=false`
- [ ] 确认旧仓未被改动（`git -C ../bidding-qa-system status` 应干净且停在基线提交）
