# AI 交接文档

> **给接手的 AI 助手 / 未来会话**：项目现在什么状态、还差什么、怎么验证。
> 分工：规则与红线在 `AGENTS.md`；命令与排障在 `docs/部署手册.md`；图解走读在 `docs/04`；设计细节在 `docs/01-03`。
> 本文只放**易变的状态**：现状快照、未完成清单、环境速览、检查清单。更新：2026-11-07。

## 1. 一分钟上手（下一任先做这四件事）

1. 读 `AGENTS.md`（硬约束 10 条 + 预算红线）→ `docs/04` §6（一次提问的代码级走读）
2. 拉起服务：`bash deploy/start.sh`（沙盒会话里可能 0xC0000142，见部署手册 §6）→ **立刻** `curl 127.0.0.1:8000/api/health` 六组件全 `ok`
3. 跑验证三连：`cd backend && uv run pytest -q` / `cd frontend && npx tsc --noEmit` / `bash deploy/e2e-chat.sh`
4. 需要评测时：先确认 PG/Neo4j 在线再跑（门禁会拦）；判分槽位**收费**（红线见 AGENTS.md），真跑批前先算成本

## 2. 质量现状（快照，接手时按 §6 复核）

| 项 | 状态 | 复核命令 |
|---|---|---|
| 标签 | `v1.0` = `cac6a3c`（2026-11-08 filter-repo 清史后全仓哈希重写，本文与开发日志里的旧哈希均为清史前记录）；其后若干补丁提交 | `git log --oneline -6` |
| 后端测试 | 全量全绿（2026-11-09 快照：在线 367 过，控制台轮 +17、CORS 预检回归 +1） | `cd backend && uv run pytest -q` |
| 前端类型 | 绿 | `npx tsc --noEmit` |
| e2e 三走向 | 机器断言版就绪（2026-11-07，变异自检通过）；最近一次真实三走向 2026-11-08 全 PASS（含标的物维度图表；变异自检同日复验） | `bash deploy/e2e-chat.sh`（真跑调主模型）/ `--mutation-check`（免后端自证） |
| Agent 评测 | 13/13 工具选择 1.0 · hit_cap 0.0 · 澄清 1.0 | `backend/eval/results/agent_20261102_663479c_deepseek-flash.json` |
| RRF k 接线 | E1 三分类的 `rrf_k` 已消费：两路独立 `query_points` + `rag/rrf.py` 客户端 `Σ1/(k+rank)` 融合（旧 `FusionQuery` 固定 k=2 已废弃）；`sources[].score` 无精排时为 `null`、CRAG 不再误触发改写。smoke 复测 8/8、hit_rate 1.0、P95 519ms | `cd backend && uv run pytest -q tests/test_rrf_k_contract.py` |
| 文档审计 | doc_defects=0 | `bash .auto/measure.sh` |
| 产品定位 | **练习版**（老师评估 + 甲方验收双门槛），PRD 逐项对照 = docs/03 附录B | —— |

近期变更不在这里维护——看 `git log` 与 `docs/开发日志.md`（每个任务的提示词/踩坑/决策都在）。

## 3. 未完成 / 可选清单（诚实版）

| 项 | 性质 | 说明 |
|---|---|---|
| CI / 私有远端 | P0 工程化 | `.github/workflows/ci.yml` 已有，但 `git remote -v` 为空。**历史已清（2026-11-08）**：`招投标智能问答机器人/` 归档（约 20MB 课程 PDF/训练数据）已用 git filter-repo 从全部历史剥离（.git 24MB→650KB，45 提交/4 标签重写保留，清史前完整备份在 `D:/Temp/UserTemp/bidqa-backup-20261108.bundle`）——**现在只差建私有远端 push** |
| 图片解析 `/api/vision` + 前端上传 | **已建成（2026-11-08）** | 端点（4131/5031/4001/200 契约测试齐）+ `image_refs` worker 注入 + 前端压缩上传（长边≤1024/JPEG 0.85/白底）；VISION 槽位 `Qwen/Qwen3.5-4B`（**用户定版勿改**）；真实解析效果待下次起服务用真图核一次 |
| 英文界面下英文回答 | **✅ 已建成（2026-11-08）** | 请求 schema 加 `lang`(zh/en, 缺省 zh)→`AgentLoop.run`→system prompt 语言节；澄清/空答案兜底话术英文变体；降级路径同步；前端 `bidqa.lang` 随 payload 外发；来源标题/引文保持原文 |
| 前端打字机平滑 | **✅ 已建成（2026-11-08）** | 先量现状：后端 token 到达 p50=75ms 本身平滑，卡顿主因是每 token 全量渲染+localStorage 写 50 会话；改法：token/thinking 入缓冲 rAF 合帧(~30fps) flush + 持久化只在收尾/beforeunload 兜底（`patchLastAssistantIn` 纯函数共用） |
| 联网重排 + skills + 前端小件 | **✅ 已建成（2026-11-08）** | 联网重排：`SearchWebTool` 接 `RERANK_*`（query 重排/归一标注/失败保 None；**Tavily key 未配，代码休眠**）；skills C9：`agent/skills/*.md` 两个技能（bid-analysis/debug-answer）零往返注入；前端小件：复制回答/消息时间戳/会话重命名（PATCH `/api/conversations/{id}`）/导出 Markdown |
| 联网双供应商配置化（D4/D5） | **✅ 已建成（2026-11-08）** | `WEB_SEARCH_PROVIDERS` 名册（tavily/exa 可多个，**留空=关，前端不显示联网按钮**）；Exa REST 直调（原 spec MCP SDK 偏差已记 docs/02 D5）；单工具多供应商、组合缓存键、部分失败不缓存；**Tavily/Exa key 用户自填后激活** |
| A13 会话搜索 | **✅ 已建成（2026-11-08）** | 侧边栏搜索框按标题+消息关键词本地过滤+空态提示（docs/02 A13 收口） |
| 用户体系（三角色） | **✅ 已建成（2026-11-08）** | admin/user/guest 三角色：`/api/auth/*` 四端点 + 角色化中间件（metrics/logs 仅 admin；vision/conversations 拒游客）+ 会话隔离（conversations.user_id，NULL=机器域，越权 404）+ 游客严格档（不落库/禁导出/禁传图/锁档/独立限流 10/60s/进程内历史 TTL 1h）+ `/login` 页与服务端主会话反转；**本机默认启用，首启种子 admin/admin123（doctor 告警改密）**；管理员建号本轮走 `bidqa user` CLI，控制台界面随下轮；USERS_ENABLED=false 可退回旧单机模式 |
| 控制台 /console | **✅ 已建成（2026-11-09）** | **KPI 仪表盘布局**：KPI 行 7 卡 + 双栏（服务健康｜运行时 + 数据规模｜日志 5s 轮询）+ 管理员动作区三块（**用户管理 GUI（建号/重置吊销会话/停用·启用）** / **游客开关** / **会话管理全库版（多选批删 + purge-empty 两步式预览→执行，10 分钟保护窗）**）+ 深链行；端点 6 组全 admin-only（`ADMIN_ONLY_PATHS`→前缀匹配）；聚合端点任一库挂记「不可用」绝不记 0；**全库管理走独立旁路端点，聊天页可见域三元组零改动**；重置密码=同事务吊销该用户全部会话（CLI/API 单一入口）；自停防呆 4001；4091 扩义承载重名建号 |
| skills 第二层匹配 | **✅ 已建成（2026-11-08）** | 指代词（该技能/这个技能/上述技能）回看最近一条助手回复提到的技能；一层点名优先（参考三层匹配第二层） |
| 应用日志落盘 | **✅ 已修复（2026-11-08）** | bidqa logger 加 stderr StreamHandler（幂等）——backend.log 现在能看到「上游流式响应为空」等应用级行；内存环 /api/system/logs 照旧 |
| `subject_matter` | **✅ 已回填（2026-11-08）** | 原全 NULL（8,489 行）→ `subject-batch` 管道跑完 **7,949 去重名全部提取成功**，写回覆盖全表（现 8,489 行全有值，2,188 个不同标的物；此前记的 8,492 含 3 行测试残留，见下两行），成本 ≈¥2.3（SF GLM-5.3-Flash，reasoning_effort=low + few-shot）；`subject_agg` 图表与图谱维度解锁；硬名字（多段复合）提取粒度偏粗是 Flash 级模型已知局限 |
| 测试污染开发数据 | **✅ 已根治（2026-11-08）** | 两件连带破案：① `test_subject_writeback` 收尾不清理 → 开发库留 3 行 it-sm 残留（**「+3 行之谜」8,489→8,492 的来源**；region 裸值 安徽/合肥/芜湖 也来自它）；② `test_graph` 模块夹具 teardown `clear_all()` → 2026-11-08 全量测试跑完**真实图谱被清空**（当天上午「六类吻合」随即失效）。均改前缀 scoped 清理（IT-G*/it-sm try/finally），跑完 pytest PG/图谱计数不变 |
| `region` 脏值 | **✅ 已清洗（2026-11-08）** | 3 行省列错配改回安徽省（江西省合肥市/浙江省合肥市/江苏省芜湖市；证据：同前缀编号对照 20:1/15:1/1:1 + location/项目名——无为市=芜湖代管、乐桥镇=庐江县合肥、华亿科学园=合肥高新区）。**源 xlsx 省列仍错**：重导入会带回脏值，复修 SQL 见部署手册 §6；直辖市双写（北京市北京市）与外省样本行为源数据原样，保留 |
| `capabilities.chat` 口径 | **✅ 已修（2026-11-08）** | 新增 `has_chat`（主槽位三件套或命名档齐备）→ health capabilities 与其它能力同口径；qdrant 恒 True 属设计（本地绿装恒配置，可达性看 components 探测） |
| 在线评测开启 | 配置决策 | 默认关（`ONLINE_EVAL_ENABLED=false`，采样 0）；判分是**收费中转**，何时开启由用户拍板；成本口径见部署手册 §6 评测坑表（离线单题 3 次二值调用、agent 模式 0 次） |
| 灾备通道编码文本不一致（低优先级） | 工程债 | `ingest/embed_store.py` 两处编码起点不同：dense + bge-m3 稀疏看 `ctx_summary+text`（:41-44），`sparse_bm25` 词频通道看 `title+text`（:53）；灾备切换时同一 chunk 分数会跳。用户定优先级低、暂不改（改则两处须对齐 + 重 ingest 全量 75 条）；2026-11-09 代码审计发现，作为 rag 三处之后的第四处；2026-11-10 起 `ctx_summary` 为空时 dense/sparse 编码输入仅 `text`，本条口径不变 |
| F2/F3 生产入口 | **✅ 已接线（2026-11-10）** | `bidqa ingest pdf` 把 `parse_pdf→split_markdown→enhance_blocks→to_ingest_blocks→Chunk(**p)→ingest_chunks` 串成 CLI 入口；`ctx_summary` 改可选（docs/01 §4.1），修掉 dict/Chunk 与「空摘要被 validate_chunk 拒收」两根断线；`bidqa doctor` 增加 source_type 分布（取不到记「不可用」）。本轮 `chat=None` 零收费；缺陷 2（摘要只喂 400 字）、缺陷 3（空 except 无日志）、LLM 摘要槽位、MinerU 云端通道、F8 manifest、H6 一键汇总均在后续一轮。真实 PDF 语料未导入（`data/raw/` 为空，历史归档已剥离），当前库仍只有 FAQ |

## 4. 环境与数据速览

- **配置**：`backend/.env`（**用户自己管**，别代改）。三槽位 `CHAT_/VISION_/JUDGE_*`；`EMBEDDING_/RERANK_`=SiliconFlow；
  `WEB_SEARCH_PROVIDERS`=联网名册（tavily/exa，留空=关，前端按钮随其显隐）；
  `CHAT_PROFILES`=命名档（前端「模型」下拉）；`CHAT_MAX_TOKENS`=主槽位输出预算（2026-11-11 设 8192：思考型上游先烧 reasoning，预算不足时正文空，见开发日志）；`ONLINE_EVAL_*`=在线评测开关；
  用户体系 `USERS_ENABLED/ADMIN_USERNAME/ADMIN_PASSWORD/SESSION_TTL_HOURS/GUEST_RATE_LIMIT`（本机默认启用，见 docs/01 §5）。配置语义以 `docs/01` §5 为准
- **服务**：8000 后端 / 3000 前端 dev / 5432 PG / 6333 Qdrant / 7474+7687 Neo4j
- **数据规模**：PG 8,489 行样例中标数据（subject_matter 已全量回填；2026-11-08 曾现 8,492 = 3 行测试残留已删，见 §3）、Neo4j 图谱 2026-11-08 重建（干净版：Project 8,489 / SubjectMatter 2,188 / Supplier 4,001 / Purchaser 3,504 / Agency 306 / Location 40，六类与 PG 实体组逐一吻合；重建约 20 分钟）、FAQ 语料（入库结果以 `ingest` 输出为准）
- **数据坑**：`region` 市级粒度（合肥 3471 / 芜湖 1123 / 亳州 1037 / 滁州 529）
- **数据位置**：`data/db/`（库文件）· `data/run/`（日志/pid）· `backend/eval/results/`（快照+判分缓存），详见部署手册 §5
- **前端状态**：localStorage `bidqa.theme/lang/provider/token/user`（用户体系后会话改**服务端主**，
  `bidqa.conversations.v1` 已废弃不再读写）；登录凭证 `bidqa.token`+`bidqa.user`（角色门控读后者）
- **浏览器自动化**：`bsk` v0.4.1（`C:/Users/WindLight/.local/bin/bsk.exe`，扩展已配 Chromium/Helium）。用前 `bsk daemon start --foreground`，用完 `bsk session stop`；不需要时把守护进程也停掉

## 5. 下任的第一个建议动作

- **要收工程债**：按 §3 前三行做（重启可靠性 → e2e 断言 → CI/远端），三件都不改业务语义，风险最低
- **要上云**：~~清史~~已完成（2026-11-08 filter-repo，见 §3）→ 现在直接建私有库 `git remote add origin <url> && git push origin main --tags`
- **要补功能**：~~图片解析~~ / ~~英文回答~~ / ~~打字机平滑~~ / ~~联网重排+skills+前端小件~~ / ~~联网双供应商配置化~~ / ~~A13 会话搜索~~ / ~~skills 二层匹配~~ 均已建成（2026-11-08，见 §3）→ 剩余候选：代码高亮（需加依赖，本域回答极少出代码块）
- **要调质量**：先 `eval agent` 留基线快照，再改工具描述/上下文，对比前后快照
- **要开在线评测**：先告知判分是收费中转、估算每周成本，由用户改 `.env` 并设采样率

## 6. 交接检查清单（接手时逐项打勾）

- [ ] `git status` 只有用户自己的改动；`git log --oneline -3` 与本文 §2 一致
- [ ] `bash deploy/start.sh` 后 `/api/health` 六组件全 ok（按端口确认进程真活着）
- [ ] `cd backend && uv run pytest -q` 全量全绿（服务在线）
- [ ] `cd frontend && npx tsc --noEmit` 绿
- [ ] `bash deploy/e2e-chat.sh --mutation-check` 自证通过；真跑三走向断言全绿（调主模型）
- [ ] `bash .auto/measure.sh` → `doc_defects=0`
- [ ] 读 `docs/04` §6（走读）与 `docs/部署手册.md` §6（排障坑表）
- [ ] 确认未动 `.env` 的 VISION / JUDGE 红线（AGENTS.md），`ONLINE_EVAL_ENABLED=false`
