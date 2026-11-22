# AI 交接文档

> **给接手的 AI 助手 / 未来会话**：项目现在什么状态、还差什么、怎么验证。
> 分工：规则与红线在 `AGENTS.md`；命令与排障在 `docs/部署手册.md`；图解走读在 `docs/04`；设计细节在 `docs/01-03`。
> 本文只放**易变的状态**：现状快照、未完成清单、环境速览、检查清单。更新：2026-11-22（v2.5 定版前同步：P1-8 广播可达性修复+停机演练复验、ag-010 误澄清修复、OTel OTLP 实接实证、图谱 34,800 边逐项对账；Agent 能力批 A/B 已跑；**语料 10 法规+23 FAQ 已入库（718 块，Qdrant 793）**、ingest .txt 直入 c7e0590；白班轮 NIGHT-026/027/028/030/031/036：corpus_note 改运行时点数、快照命名加 golden 段、rag_law_v1 法规金集+BM25 零付费基线、faq_excel 可选 URL 列、stop.sh 双进程架构，测试 733）。

## 1. 一分钟上手（下一任先做这四件事）

1. 读 `AGENTS.md`（硬约束 10 条 + 预算红线）→ `docs/04` §6（一次提问的代码级走读，路径已对齐 v2.0 结构）
2. 结构速览：`docs/01` §1 分层树——**api=HTTP 适配壳、application=业务用例、工厂在各包 factory.py、组合根只有 container.build_agent_stack**；分层边界由 `tests/test_layering.py` AST 钉死（违规即红）
3. 拉起服务：`bash deploy/start.sh` → **立刻** `curl 127.0.0.1:8000/api/health` 六组件全 `ok`
4. 跑验证三连：`cd backend && uv run pytest -q`（733 例）/ `cd frontend && npm run build` / `bash deploy/e2e-chat.sh`（真实调用主模型，先看 §3 成本口径）

## 2. 质量现状（快照，接手时按 §6 复核）

| 项 | 状态 | 复核命令 |
|---|---|---|
| 标签 | `v1.0-baseline` → `v2.0-refactored` → `v2.1-kg-normalize` → `v2.2-structure-cleanup` → `v2.3-dual-vision` → `v2.4-audit-repair` → **`v2.5`（2026-11-21 定版，用户已批准）** | `git tag -l && git log --oneline -12` |
| 后端测试 | **733 passed 全绿（含 Neo4j/PG 集成，服务需在线；2026-11-21 实跑：675→680 为 ag-010 修复 +5 契约例、680→682 为 P1-8 uvicorn+TCP SSE 回归 +2、682→706 为 NIGHT-008 level +17 与 NIGHT-014 退出码 +7、706→717 为 ingest .txt 直入 +11，语料入库轮收在 719；2026-11-22 白班轮 719→733 为 +14：corpus_note 注记 3、FAQ URL 列 5、dry-run url= 2、快照命名 golden 段 4——NIGHT-039 全量实跑复核）**；测试目录=源码目录一条规则 | `cd backend && uv run pytest -q` |
| 分层守卫 | `tests/test_layering.py` **R1-R11** 全部空集（永久护栏）+ 每条规则「能红」自检 | `pytest tests/test_layering.py -q` |
| 类型检查 | pyrefly **49 条存量冻结基线（pyrefly-baseline.json），0 new**（bad-override ×5 + missing-attribute ×3 已清零，余为 psycopg 行类型噪音；2026-11-21 复核 0 new） | `uv run --with pyrefly pyrefly check src` |
| 前端类型 | 绿（**Vite + React 19 + Tailwind 4**，`react-router-dom` 五路由；`npm run build` 通过；2026-11-21 typecheck+build 绿，主 chunk 501.62 kB 体积警告已登记 backlog，非门禁） | `cd frontend && npm run build` |
| 可观测性 | OTel span：`chat.turn → agent.round / llm.chat_stream / tool.<name>` + `rag.retrieve` / `persist`；**未配 `OTEL_EXPORTER_OTLP_ENDPOINT` = no-op**（SDK 惰性导入）；异常只记类型+栈帧（不落 message）；可接 Langfuse OTLP / Jaeger。**2026-11-21 实接实证**：本地最小 OTLP/HTTP receiver 收到真实后端 2 次 POST 共 27 span（业务 span 9：chat.turn×2 / agent.round×2 / llm.chat_stream×2 / rag.retrieve×2 / persist×1；FastAPI≥0.162 原生 HTTP 层 18：dependencies×5 / endpoint×5 / serialization×3 / 路由×5——仅 path/route/status 属性）；问题与答案文本在 span 原始 payload grep 0 命中（隐私白名单成立）；tool.* 本轮 0（dummy 上游无 tool_calls，如实缺口） | 见 docs/部署手册 §7 |
| 检索指标 | `recall@k / MRR / nDCG@k`（`bidqa eval --mode retrieval`）；空 relevant=None（没测出≠0）。**2026-11-22 基线（语料入库后 793 向量，真实向量检索）**：recall@5/10=1.0、nDCG@5/10=0.954、MRR=0.938（快照 `retrieval_20261122_b417892_deepseek-flash.json`）；HNSW 仍未建（<indexing_threshold）。快照 `corpus_note` 已改为**运行时真实点数**（2026-11-22 NIGHT-026：CLI 读 Qdrant count 注入，`corpus_note(count)` 单入口收口；点数不可得/空集合记「未确认/为空」不冒充数字）；该日快照内旧「仅 75 向量」note 属历史事实，不回改。**BM25 灾备零付费基线（同日，embedding=null，793 点）**：rag_v0 recall@5/@10=0.375；**新增法规金集 `eval/golden/rag_law_v1.jsonl`（12 例/10 部法规，ref 实库取证）recall@5=0.083/@10=0.25**（快照 `retrieval_20261122_da2329d_deepseek-flash.json`，12 例仅 3 题 ref 进 top10）——词法通道 sparse_bm25 仅存词频（无 IDF/长度归一），数字是**下限口径**，勿与 dense/混合通道对标；**NIGHT-042（同日）已按方案 B1 重建**：sparse_bm25 文档侧烘焙 BM25 权重（k1=1.5/b=0.75，avgdl=83.1）→ 新集合 bidqa_knowledge_v2（793 点、id/payload/dense/sparse 全量逐分量核验不变，旧 v1 保留回滚），BM25 灾备口径复测：rag_law_v1 recall@5/@10=1.0、MRR=0.8958；rag_v0 recall@5/@10=1.0、MRR=0.9375（快照 retrieval_20261122_*_353649a_deepseek-flash.json，embedding=null 零付费）；增量 ingest 后 avgdl 漂移跑通道重算归位（部署手册 §3）；**混合通道基线未跑**。同日 rag_v0 BM25 快照曾被 rag_law_v1 同名覆盖（rag_v0 数据备份于 `data/run/night031/…__rag_v0.json`）→ 快照命名加 golden 段：五段式 `{kind}_{日期}_{golden集名}_{git}_{judge}`（NIGHT-036；golden 不可得退回旧四段名，旧快照可读） | `uv run bidqa eval --golden ../eval/golden/rag_v0.jsonl --mode retrieval`（付费，先报价；rag_law_v1 金集同口径） |
| e2e 三走向 | `--mutation-check` 免费自证 PASS；真跑调主模型（付费，跑前看 §3 成本） | `bash deploy/e2e-chat.sh --mutation-check` |
| Agent 评测 | **40 例新基线：39/40=0.975，三项门禁全过**（2026-11-19 首跑，快照 `agent_20261119_12c4f93_deepseek-flash.json`，零判分调用）；hit_cap 0.0 / clarify 1.0 / p95 41.0s；plan_rate=0、reflected_count=0（基线批开关关闭口径自证）；**7 个新例 ag-034~040 全 PASS**（多步/对比/复合钉法在真实模型成立）。唯一 FAIL ag-010 **已修复**（c41d609，2026-11-21）：旧结论「合理但违规」作废——根因是模型在无限定条件排名题上误选澄清；已强化 search_database/graph 描述显式承接供应商 TopN 全库榜（真机探测 2 次调用命中 supplier_topn、ask_clarification 未出现；680 passed 含 5 新契约单测），**40 例批量复测待批付费（报价见 §3 增补行）**。历史：33/33=1.0（2026-11-15 三轮，快照 `agent_20261115_7c34b81_deepseek-flash.json`）。**能力批 A/B 已跑（2026-11-21，快照 `agent_20261121_1e94a51_deepseek-flash.json`）**：39/40=0.975、三门槛全过；plan_rate=0.175（7/40，与离线预估一致）、reflected_count=29；**ag-010 转 PASS**；**ag-016 新 FAIL 已收口**（仅用 search_graph，purchaser_dist 可答——口径拍板 1-A：golden 改 ANY-of，用该快照**离线重算 40/40**，零额外调用）；实际≈128 次调用、判分 0 | `uv run bidqa eval --golden ../eval/golden/agent_v0.jsonl --mode agent`（付费） |
| 图谱归一 | v2.1 完成：curated 映射 **9 项合并**（Purchaser 3504→3495，其余五类不变）；合并实体项目数守恒 PASS；分支/校区/附属误并候选全部未合并 | `python ../data/run/kg_acceptance.py` |
| 产品定位 | **练习版**（老师评估 + 甲方验收双门槛），PRD 逐项对照 = docs/03 附录B | —— |
| 旧仓 | `D:/.../bidding-qa-system` **冻结存档**（原地冻结+README 退役声明；数据副本保留），哈希只对应旧历史 | — |

近期变更不在这里维护——看 `git log`（C0-C9 每步一个绿 commit）与 `docs/开发日志.md` 2026-11-21 行。

## 3. 未完成 / 可选清单（诚实版）

| 项 | 性质 | 说明 |
|---|---|---|
| v2.3 双通道付费验收 | ✅ 2026-11-15 三 PASS | 文档图 auto→OCR（PaddleOCR-VL-1.5 原件逐字提取，编号/金额全对）、图表图 auto→describe（远端多模态 Qwen3.8-27B 结构化描述准确）、带图提问 3.8s 直答并引用 BZGY2023CG141 与 ¥12,500,000.00；过程与教训见开发日志 11-15 行。**vision 槽位现为用户定版远端多模态 Qwen3.8-27B（llama.cpp，.env 模型 id 未变）**；OCR=SiliconFlow PaddleOCR-VL-1.5 |
| 会话回放 + UI 输入条轮 | ✅ 2026-11-15 | 会话落库 **schema_v 2**：assistant item 附 done 收尾结构+ts，刷新后来源/工具/图表/耗时/trace 全回放（f36ea36；v1 旧行兼容=仅正文；load_history 投影二元防漏进上下文；真机三态验证过）。UI 输入条扁平化+多行自动长高封顶 128px+右侧按钮纵向叠放+退出登录明确按钮（0378692/c123eb3） |
| CI / 私有远端 | P0 工程化（用户拍板：以后再弄） | `ci.yml` 已扩为五 job（后端测试 / 分层守卫+pyrefly 基线 / 文档机械审计 / e2e 断言器变异 / 前端类型+构建）；**新仓 `git remote -v` 为空——Actions 从未触发**。建私有库后 `git remote add origin <url> && git push origin main --tags`，再点一次 CI |
| agent 评测快照重跑 | ✅ 2026-11-15 三轮收口 | ①扩例后首跑 30/33=0.909（`agent_20261115_fbecc16_Qwen3.8-27B.json`）；②graph 描述强化（82cc77f）后 33/33=1.0（`agent_20261115_82cc77f_deepseek-flash.json`）；③**审计修复轮**（澄清工具注册 f3442a0 + 图谱 Cypher 修复 17e595e）后重跑 **33/33=1.0**（`agent_20261115_7c34b81_deepseek-flash.json`）——澄清例首次真实走 ask_clarification 工具、图谱 entity 过滤/均价在真实图谱可用（免费探针验证：医院过滤/空调均价 21,184 元/180 项目）。三轮快照名均嵌 JUDGE_MODEL 指纹（用户改过判分模型名），agent 模式均未调判分 |
| `detail_search` 金额排序 | ✅ 2026-11-15 | `order_by`（amount_desc/asc + 中文别名容错归一）仅 detail_search 支持，`ORDER BY award_amount … NULLS LAST`（金额取不到沉底）；聚合/趋势固定口径拒绝（args + bp_query 双层）；真机三态核对 PASS |
| 会话回放两债 | ✅ 2026-11-15 两债全清 | ①降级直答落库（efbbb4e）：worker 异常分支对称持久化——游客进 guest_history、普通用户附 done 落 PG，刷新后降级回答不再整条消失；②反馈态回查（A7 债②）：`GET /api/conversations/{id}/feedback`（同问题取最新）+ 前端 openConversation 并联回查映射 message.feedback，👍/👎 刷新还原 |
| 离群值溯源 | 🟡 2026-11-16 数据侧定界，核源表待甲方 | 免费盘点（PG 实查）：8,489 行单批次 `sample-2023`，金额中位数 4,600 元、均值 251.2 万元；>1 亿 22 行（0.26%），其中 **BZGY2023CG141 涡阳托养服务 66 亿**为 2,630× 均值、项目性质与金额严重不符（疑源表多写零/单位错）；`content`/`source_url` 全空、`data/raw/` 无源 xlsx——**无法自证原值，须甲方核源表**；按红线不猜测不改写（保留原值并在答案中如实展示） |
| embed_store 编码文本不一致 | ✅ 2026-11-16 代码统一 / 🟡 存量待重 ingest | 三通道共用 `encode_text(chunk)` = `title`+可选`ctx_summary`+`text`（空段不空行）；新增一致性测试（dense 入参 == bm25 入参，变异可红）。**存量向量 dense/sparse 通道仍是旧口径**（NIGHT-042 重建已把 sparse_bm25 通道对齐统一口径，dense/sparse 未动）：重新 ingest 需原始 FAQ xlsx——原始 xlsx 仍未恢复（2026-11-21 采集的 `faq_topic_376023.jsonl` 为清洗产物，非原 xlsx），xlsx 恢复后执行 `bidqa ingest knowledge --input <xlsx>`（嵌入 API 付费，先报价确认） |
| clarify.py PROVINCES 词表副本 | ✅ 2026-11-15 收口 | clarify 改导入 `ingest/regions.PROVINCES`（agent→ingest 分层允许，layering 实测确认），历史副本删除；锁定测试 34 省逐一可匹配 + 同对象断言 |
| DI 直调残留 | 测试隔离债（低） | `api/system/metrics.py` 的 `_online_stats`、`_qdrant_counts` 读配置走 get_settings()（仅读端点内部，无测试依赖真实 .env，危害低）；新代码一律 Depends |
| 真实 PDF 语料导入 | 🟡 链路就绪（.txt 直入已支持）；PDF 原件仍缺 | `bidqa ingest pdf` 链路已通（MinerU 云端 + pdfplumber 兜底，有契约测试）；2026-11-21 起 .txt 法规采集产物可直入同一管道（c7e0590，见下行「语料素材」）；**PDF 原件仍缺**，命令：`uv run bidqa ingest pdf --input <x.pdf> --source-type 法规 --title "<标题>" --dry-run`（先 dry-run 后真跑；真跑嵌入付费） |
| **语料入库（2026-11-21 采集 → 2026-11-22 入库）** | ✅ 718 块写入，0 拒收 | 法规 10 篇 **695 块**（批次 `reg-20261121`：01 招标投标法实施条例·2019 三次修订、02 政采法实施条例 658 号、03 87 号令、04 94 号令、05 110 号令、06 101 号令、07 102 号令、08 74 号令、09 政府采购法·2014 修正、10 招标投标法·2017 修正）+ 徽采云 FAQ **23 条**（批次 `faq-zcy-376023`）；Qdrant 75 → **793**（旧 75 FAQ 点未动 → golden 现有 ref 仍有效）。来源追溯：法规走 Chunk.url；FAQ 侧 `faq_excel` 可选来源 URL 列已落地（NIGHT-027：表头含 url/链接/link/网址 字样、值须 http(s) 开头否则留空，url 不参与 Chunk.id——裸「来源」/「访问日期」列不误取），`ingest knowledge --dry-run` 抽样已打印 `url=`（NIGHT-030，付费嵌入前人工核验来源映射）；存量 23 条 FAQ 的 url 仍只靠 batch_id，回填需重 ingest（付费，待批）。素材在 `data/raw/`（gitignored）；版本口径详见各篇头部。**2026-11-22 入库后验证**：retrieval 基线 recall@5/10=1.0、nDCG=0.954、MRR=0.938；真机抽测 2 题 PASS（法规题命中政府采购法（2014 修正）第五十三条「7 个工作日」；FAQ 题命中订单支付时限「7 工作日/30 日」）；旧「全角/半角问号重复 FAQ」仍影响 rag_flow_002 排名（nDCG 0.63），语料清理后重测可改善 |
| 在线评测开启 | ✅ 2026-11-15 已开启（用户改 .env：`ONLINE_EVAL_ENABLED=true`、`ONLINE_EVAL_SAMPLE_RATE=0.03`） | 真机三验证 PASS：online jsonl 行为指标落盘（rounds/tool/elapsed 免费）、`GET /api/eval/runs` 200（新快照可查）、metrics `online_eval` 节 enabled/behavior=1/queued=0；判分 3% 抽样 + 点踩 100% 必评 |
| **审计修复 WP0-WP4** | ✅ 2026-11-15~16 阶段 0+1+2+3+4 全部收口（每轮独立 commit，`git log` 可数；审计报告本地归档 `data/run/archive_docs/AUDIT_2026-11-15.md`，不入库） | **已修**：P0-7 隔离库、P0-3 429 红线、P0-4 判分缓存、P0-8 部署断链、P1-1 归属校验、P1-2 fail-open、P1-3 live role、P1-4 compare_digest、P1-6 工厂门禁、P1-5 告警路径、P0-1+P1-9 Runner、P1-14/15 图谱 Cypher、**P0-2 澄清工具（付费重跑 33/33=1.0）**、P1-10/11 缓存、P1-12/13+P2-31/32、P1-17~19+P2-15~18 评测口径、**阶段 2**：P1-7 守护池、P1-8 lifespan 四步（2026-11-21 广播可达性修复+演练复验收口，见 §3 增补行）、P1-20+P2-34 双检锁、P1-21 SSE 异步化、P2-4/5/33/39、**阶段 3（前端）**：P0-5 单机死循环、P0-6 并发发送闸、P1-23 反馈错轮+回滚、P1-24 卸载 cleanup、P1-25 refreshList 合并、P1-26 await 切换守卫、P1-27 游客 provider 服务端门禁（变异自证）、A11 日志过滤 UI、A12 单组隐藏切换器；**阶段 4**：P2-1 tempdir 写探测、P2-2 measure.sh 入库+P2-47 文件面、P1-28 5001/5032 契约、子进程导入守卫样板、P1-29 workers 告警化、P1-30 联网名册留空、P1-31 CI 五 job+pyrefly 基线、P1-32/33/34+D1-D25 文档漂移、P2-42 e2e 断言收紧（S4/S5/S6 变异扩条）、P2-49 pycache 清理。**验证门：579 passed / layering 3 / pyrefly 0 new / tsc+build 绿 / measure.sh doc_defects=0 / 浏览器真机回归**。**阶段 5（可做部分已收）**：P1-16 batch 作用域（建图 `--scope-batch`、标的物写回 `--batch-id`，缺省全表时告警）、embed_store 三通道编码统一；数据侧：feedback 孤儿点踩 163 行已备份后清除、离群值定量定界待甲方核源表、PDF 语料缺原件（命令就绪）。**挂起待拍板**：P1-5 强制收口（需用户 .env 改 ADMIN_PASSWORD + `bidqa user reset`）、真实控制台停机演练复验 |
| **夜间能力包（2026-11-18/19）** | ✅ WP1-WP3 + WP6-WP7（连同 WP5 共 8 单收口） | 前端 **Vite + React 19**（5 路由/router/proxy/Tailwind4，Next 依赖清零）；**图谱关系可见化**（console `relationships` 5 类 + `relationship_total=34,800`，Neo4j Browser 验证查询入部署手册）；**OTel span**（默认 no-op、OTLP 可接 Langfuse；异常只记类型+栈帧）；**检索指标 + `--mode retrieval`**（75 向量局限声明随快照落档）；**选型与链路优化报告**（embedding 7 候选 / 向量库 4 候选 / 评测协议 / O1-O7 优化清单，零编造数字）。**验证**：675 passed / layering 3 / pyrefly 0 new / tsc+build 绿 / doc_defects=0 / e2e 变异 PASS。**未验证/待批**：Agent 40 例新基线付费 A/B 重跑、retrieval 真实基线（需语料+embedding 报价）、浏览器端「执行计划」真实问答验证（主模型端点未开）、OTel 真实 collector/Langfuse 对接、WP7 参数实测（语料到位后） |
| **WP5 Plan/Reflect 轨迹** | ✅ 2026-11-19（WP5a 能力层 + WP5b 契约/展示 + WP5c 评测口径） | WP5a：Plan-and-Solve（`AGENT_PLAN_ENABLED`）+ Reflection 证据自检（`AGENT_REFLECT_ENABLED`），`LoopResult.plan_steps/reflected`，失败一律降级零阻塞；WP5b：done 事件透传两字段并递增 **`schema_v=2`**（字段恒在位、空不省略、澄清轮置空），阶段条新增 `规划中`/`自检中`（仅实际发起调用前）；前端「执行计划」折叠列表 +「已补查」徽标（随落库 done 刷新回放，storedToMessages 整体透传 done 无需改动）。契约测试 5 例（assembly 3 + SSE 2）先红后绿。**WP5c**：黄金集 33→40 例（ag-034~040，含 note，澄清闸门双向对账钉进契约测试）；`run_agent_eval` 增 `plan_enabled/reflect_enabled` 透传，summary 增 `plan_rate`/`reflected_count`（观测指标，门禁三项不动）、cases 逐例落两字段、`env.agent_flags` 区分 A/B；CLI `--plan`/`--reflect`；**付费重跑（基线批+能力批）待用户批准** |
| **ag-010 误澄清修复** | ✅ 2026-11-21（c41d609） | 40 例基线唯一 FAIL 收口：旧结论「合理但违规」作废——根因是模型在**无限定条件**的排名题（「按中标总额排名供应商」）上误选 ask_clarification；修复=search_database/graph 工具描述显式承接「供应商 TopN 全库榜」（中性排名问题不再被当模糊问题澄清）。真机探测 2 次调用均命中 supplier_topn、ask_clarification 未出现；680 passed（含 5 新契约单测）。快照未重跑——批量复测属付费动作，待批（报价见下行） |
| **Agent 评测批量复测报价（待批）** | 🟡 付费待批 | ①40 例基线批复测（验证 ag-010 修复后门禁）：报价 **142~178 次主模型调用 / 17.5~22.0 分钟 / 判分 0**，待用户批准付费；②能力批 `--plan --reflect` **已跑**（2026-11-21，`agent_20261121_1e94a51_deepseek-flash.json`）：39/40、三门槛全过、plan_rate 0.175 / reflected_count 29；ag-016 按 ANY-of 口径离线重算 **40/40**；ag-010 转 PASS |
| **停机演练复验（NIGHT-002）** | ✅ 2026-11-21（②③④ 全 PASS；①已随 P1-8 收口） | ②强停后无残留进程、③重启后 /api/health 六组件全 ok、④数据面 PG 8,489 / 图谱节点 18,519 / 边 34,800 不变；①SSE 5031 广播原为**结构性失败**（非偶发）——根因与修复见下行 P1-8（uvicorn 0.54 停机时序：先等连接收尾/超时 cancel、之后才跑 lifespan，广播空投）。**遗留**：真控制台复验属用户侧（PTY 下 os.kill/广播式信号不可达，但 NIGHT-013 的定向组投递实测可达） |
| **P1-8 广播可达性修复** | ✅ 2026-11-21（af5031c；682 passed） | 方案=`cli/serve.BidqaServer.shutdown()` 入口**先 `broadcast_shutdown()` 再 super()**（主投递点前移到 uvicorn 停机等待之前），`main.py` 停机四步保留兜底（口径见 docs/01 §6）；端到端演练（进程内信号路径）：信号后 0.13s 客户端收到 error 5031、干净 EOF、退出码 3（uvicorn 重放 SIGBREAK 的环境默认，已对照解释）；新增真实 uvicorn+TCP SSE 回归测试（+2）。**遗留（已清）**：`deploy/stop.sh` 已改优雅先行（NIGHT-013，定向 CTRL_BREAK 实测送达）、退出码已归零（b5a23b9：优雅 0 / force 130） |
| **golden 修订（2026-11-21 用户拍板）** | ✅ 已修 | ①ag-016 改 ANY-of（`search_database`/`search_graph`，图谱 purchaser_dist 归一后口径亦合法）——能力批快照离线重算 **40/40**；②ag-002 `ref_sources` 由「民营博物馆」错指改为 `260593c0`（问题逐字精确命中）；③ag-001 `ref_sources` 置空并加 note（原指向「异议被驳回」FAQ 与问题无文本交集，疑似编写错位；待语料重 ingest 后按新 chunk 补齐）。检索类指标在语料到位前按「ref 全集」口径解读 |

## 4. 环境与数据速览

- **配置**：`backend/.env`（**用户自己管**，别代改；2026-11-14 从旧仓逐字节复制，但 11-15 起用户已自行改过判分模型名、`ONLINE_EVAL_*`、`SUBJECT_BATCH_BASE_URL`——md5 已不再与旧仓一致，以文件实读为准）。三槽位 `CHAT_/VISION_/JUDGE_*`；v2.3 增 `OCR_*`（能力型，未配置=图片自动路由退化为 Qwen 通用描述）；
  `SUBJECT_BATCH_*`=数据加工批量通道（subject-batch 与图谱归一默认通道）；
  `EMBEDDING_/RERANK_`=SiliconFlow；`WEB_SEARCH_PROVIDERS`=联网名册（留空=关）；`CHAT_PROFILES`=命名档；
  `CHAT_MAX_TOKENS`=8192（思考型上游先烧 reasoning，预算不足正文空）；`USERS_ENABLED`=true（admin/admin123 种子，**建议改密**）。
  配置语义以 `docs/01` §5 为准
- **服务**：8000 后端 / 3000 前端 dev / 5432 PG / 6333 Qdrant / 7474+7687 Neo4j（当前从**新仓**跑：C9 已切换）
- **数据规模**：PG 8,489 行样例中标数据（subject_matter 全量回填；agency 非空 844 行，其余 7,645 行为空——源数据如此，非建图缺陷）、Neo4j 六类 **18,519 节点**（Project 8489 / Supplier 4001 / Purchaser 3495 / SubjectMatter 2188 / Agency 306 / Location 40）与 **34,800 边**（FOR/PROCURED/WON/IN 各 8,489 + AGENTED 844；2026-11-21 只读对账与 PG 逐项相等，0 孤立/0 自环/0 重复边）、Qdrant **793 点**（法规 695 块 + FAQ 23 条 + 旧 FAQ 75 点，2026-11-22 入库后；HNSW 未建）
- **数据位置**：`data/db/`（库文件）· `data/run/`（日志/pid）· `backend/eval/results/`（快照+判分缓存，**gitignore**）· `deploy/diagnosis/`（机器级排障归档）
- **前端状态**：localStorage `bidqa.theme/lang/provider/token/user`；会话服务端主（conversations.user_id 归属）
- **结构地图**：`docs/01` §1 分层树 + `docs/04` §8 代码地图（v2.0 已对齐；旧路径 chat_stream.py/auth_core.py/service.py/postgres.py 均已不存在）

## 5. 下任的第一个建议动作

- **要收工程债**：按 §3 未 ✅ 行做（私有远端 push → 真实 PDF 语料导入 → 离群值溯源需甲方 → embed_store 口径重 ingest）
- **要补功能**：真实 PDF 语料导入（`bidqa ingest pdf --input x.pdf --source-type 法规 --title "..."`，先 dry-run）
- **要调质量**：先 `eval agent` 留基线快照（付费，跑前算成本），再改工具描述/上下文，对比前后快照
- **要改代码**：先跑 `pytest tests/test_layering.py` 理解边界——新增依赖若红，说明越层了，回 `docs/01` §3.1 对表

## 6. 交接检查清单（接手时逐项打勾）

- [ ] `git status` 干净；`git log --oneline -12` 覆盖审计修复 WP0-WP4 轮次（每轮独立 commit）
- [ ] `bash deploy/start.sh` 后 `/api/health` 六组件全 ok
- [ ] `cd backend && uv run pytest -q` → 733 passed（服务在线；离线时 Neo4j/PG 集成 skip 属预期并如实记录）
- [ ] `cd frontend && npm run build` 绿（五路由预渲染）
- [ ] `bash deploy/e2e-chat.sh --mutation-check` PASS（免费）；真跑三走向属付费动作（跑前告知用户）
- [ ] `pytest tests/test_layering.py` 3 passed（分层守卫全绿；allowlist 应全空集）
- [ ] 读 `docs/04` §6（走读，路径 v2.0 口径）与 `docs/部署手册.md` §6（排障坑表，排障工具在 deploy/diagnosis/）
- [ ] 确认未动 `.env` 的 VISION / JUDGE 红线（AGENTS.md）；`ONLINE_EVAL_ENABLED` 与 §3 一致（当前 `true`、采样 0.03）
- [ ] 确认旧仓未被改动（`git -C ../bidding-qa-system status` 应干净且停在基线提交）
