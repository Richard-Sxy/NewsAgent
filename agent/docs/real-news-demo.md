# 具体新闻案例演示（corpus-v6）

2026-10-14：将本地演示的合成标题和正文替换为仓库已有的具体新闻。新闻内容真实来自提供的缓存，运营指标、基线、板块分类和模型推理仍为本地演示；不能据此判断新闻的真实热度或企业模型质量。

## 数据来源与版本

- 原文入口：`news/data/local_recall/articles-v1/*.json`，共 441 篇缓存。保留原 `news_id`、标题、完整正文、内容版本、HTTPS 来源链接和发布时间。
- 关联检索入口：只读 `var/knowledge-snapshots/rag-recall-demo` BGE 快照，共 12,187 个新闻 ID、36,379 个片段，向量维度 1024。原文缓存与快照交集 393 篇；正文至少 300 字并排除 7 个标题/时间不一致的 ID 后有 319 篇，按 ID 排序冻结其中 120 篇。
- 冻结产物：`agent/data/corpus-news-v6/catalog.json`，SHA-256 `bb6e550a3881091355dcc7e5e866a184f62f77b41f0100b1f9bdfeb56c49bc3d`。每篇保留原文件路径和 SHA-256，可追溯到缓存。运行期不再依赖可变原缓存。
- 数据集：`corpus-v6` / `news-corpus-real-content-120-v1`，数据身份 SHA-256 `0d89cc86cb2b6fa195334acdf2ecfd3efebf2f174bfeb23afe709c2159f1b8ff`。
- 数仓契约：`agent/docs/sql-warehouse-schema-v6.md`，版本 `news-warehouse-v6`，SHA-256 `bb4fb8fb45113b250ac953beb6c7789fe9a519d98c130d78c50b4277802480b7`。旧版本及旧演示历史保留。
- 机器来源字段为 `news_content_source=tencent-news-cache`；展示来源为“腾讯新闻公开页面缓存”，不将腾讯网当作所有文章的原发布者。

所有新闻均为图文缓存。发布时间直接取缓存字段；无时区的时间按已有缓存适配器语义解释为 Asia/Shanghai，不从新闻 ID 或正文推测时间。栏目按固定标题关键词分为科技、财经、体育、社会，是演示分类。

行为指标保持确定性模拟：每租户 120 篇、720 个小时，覆盖 `2026-09-10 00:00` 至 `2026-10-10 00:00`（Asia/Shanghai，结束不含）。新闻的历史发布日期与这个行为窗口独立。每次分析只能查询一个完整小时，禁止将跨小时 UV 相加。不存在视频样本，查询视频不会生成虚构视频正文。

## 完整调用链

```text
Vue ChatView / 本地 HotNewsQueryTools
→ 网关注入租户、用户和权限
→ 查询理解 + 批准场景 text2sql-scenes.corpus-v6.yml
→ Python 编译受限参数化 SQL，冻结 v6 契约与 dataset 身份
→ PostgreSQL dw.news_behavior_aggregate 只读候选查询
→ SQL 预览/执行与同一热点 run_key 原子绑定
→ Temporal HotNewsMonitorWorkflow / run_hot_news_window Activity
→ SqlAssistantHotNewsMetricSource 补充同小时 UV、时长与基线
→ Python HotNewsRanker 确定性计算权威热度
→ SqlHotNewsContentRepository 按相同 news_id 获取冻结原文
→ PostgresKnowledgeStore 原文台账/版本化切片 + 已有只读 BGE 关联召回
→ HotNewsAnalysisInput.related_news 证据白名单 → NativeHotNewsAnalysisRunner / 本地 InferencePort
→ Schema / Validator → PostgreSQL analysis_runs.result_payload 保存报告和 analysis_input
→ HotNewsQueryService 读取保存快照 → 查询 API / SSE
→ Vue 榜单、正文片段、关联新闻与运营分析
```

2026-10-15 链路复核：热点成功快照当前写入 PostgreSQL，没有每次报告双写 MinIO/S3 的实现；
`EvidencePacket` 是早期概念称呼，实际代码载体为 `HotNewsAnalysisInput`。v8新数仓仍未注册此链路，
具体查询和适配边界见 [SQL与热点全链路说明](hot-news-sql-chain.md)。

### 入口、数据转换与核心实现

`app/sql_assistant/corpus_profiles.py` 的 `corpus_catalog()` 每次读取验证有界 JSON、文件哈希、严格字段、来源 URL、唯一 ID 和时间元数据；缺失或篡改时直接阻断，不回退到合成正文。`corpus_article()` 和 `corpus_news_ids()` 提供原文查询，`iter_corpus_rows()` 生成真实新闻维表，并把既有确定性小时指标和基线的新闻 ID 映射到原始 ID。`corpus_manifest()` 将内容摘要、表摘要和模拟来源标识共同纳入数据身份。

`app/config.py`、`synthetic_profiles.py` 和 `scenarios.py` 接受新 profile/契约。`warehouse.py` 的 `warehouse_contract()`、`get_text2sql_schema()`、`demo_dataset_info()`、流式装载与数据预检使用 v6，并明确标题/来源是真实缓存，栏目和指标为演示。固定数量 120，不能只改环境变量扩展冻结样本。`hot_news_binding.py` 校验 profile、数量、schema、dataset 身份，防止复用旧数据集运行。

`examples/native_hot_news_e2e_worker.py` 将 corpus-v6 纳入同一流式候选链。`native_hot_news_sql_support.py` 的 `SqlHotNewsContentRepository.batch_get_by_news_ids()` 在 SQL 元数据与 catalog 的 ID、标题、来源、栏目、类型和时间完全一致后，返回完整原文、前 300 字摘要和原 URL。`seed_native_sql_knowledge()` 将原文交给 Python 知识台账，`text_source=real-news-cache-v6`；原缓存整数版本在知识 metadata 边界转换成字符串，持久化版本仍由已有入库代码处理。

知识台账使用本地 Embedding Port 完成入库协议验证；关联召回仍使用已提供 BGE 模型、1024 维索引和匹配的查询 Embedding，不把本地测试向量送入 BGE 索引。原 BGE 文件保持只读。本次替换没有重新生成该向量包。其部分关联片段缺少结构化 URL/发布时间，页面继续显示未知；不会把片段里的文字当作可信元数据。

`app/model_runtime/local_inference.py` 仍提供确定性测试推理，保留所有模型质量限制说明；仅将固定建议文案中的“模拟新闻”改为“新闻”，避免把真实原文误标为合成内容。它不推断真实新闻热度或发布建议。

`app/schemas/hot_news_api.py` 增加可选、有界 `content_excerpt` 展示字段，核心热点分析 Schema 保持兼容。`HotNewsQueryService._index_news_content_excerpts()` 从本次已保存 `analysis_input` 投影最多 600 字，校验新闻 ID 一致；旧记录无片段时为空，不查询更新后的原文替换历史证据。

### 前端与下游输出

- `HotNewsRankTable.vue` 展开“用于本次分析的正文片段”，按普通文本渲染不执行原文 HTML。`HotNewsView.vue` 依据本次已保存 v6 schema 标明真实正文、模拟指标和演示栏目。
- `examples/conversation_hot_news.py` 向运行配置公开 corpus-v6 来源与模拟标识；`api/conversations.ts` 和 `ChatView.vue` 展示“真实新闻 · 模拟指标 · 本地模型”。
- `api/sqlAssistant.ts`、`utils/analysisDemo.ts`、`AnalysisDemoPanel.vue` 和 `HotNewsQueryTools.vue` 识别 v6 数量、窗口、schema、内容摘要及来源标识，将其纳入场景准备和身份刷新校验。`examples/data_analysis_demo.py` 的 CLI 场景目录同样验证这份身份。
- `api/types.ts` 传递只读正文片段。页面读取报告、SQL 轨迹、关联证据和原有分析结果；运营决策、写作转交、长期记忆晋升及配置发布继续使用已有人工审批节点。

## 本地配置与启动

独立 Compose project 为 `news-agent-real-news-demo`，使用新的 PostgreSQL/Temporal/MinIO 数据卷。旧 native/BGE 项目不删除、不迁移历史。API/Worker 使用已存在本地镜像和只读当前源码挂载，前端从本地 build 产物离线组装；不会读取 `.env` 或请求企业模型密钥。

```bash
cd agent/frontend
npm run build
cd ../..
python3 agent/tools/start_real_news_demo.py --check
python3 agent/tools/start_real_news_demo.py
# 查看状态、停止项目（保留数据卷）
python3 agent/tools/start_real_news_demo.py --status
python3 agent/tools/start_real_news_demo.py --stop
```

`--check` 仅做文件摘要、端口、本机 Docker、资源和本地镜像预检。启动时还校验镜像依赖、初始化基础设施、迁移独立数据库，并检查 API、前端代理和三个实际 Worker 队列的 workflow/activity poller。支持 `--api-port` / `--web-port`；默认 28070 / 5179。依赖本机 Docker Compose >= 2.24.4、至少 6 GiB Docker 内存、默认 10 GiB 磁盘余量、已有 BGE 包和本地镜像。

核心配置在 `deploy/docker-compose.real-news-demo.yml`：

| 配置 | 值/用途 |
| --- | --- |
| SQL_ASSISTANT_DATASET_PROFILE | corpus-v6 |
| SQL_ASSISTANT_NEWS_PER_TENANT | 120 |
| SQL_ASSISTANT_SCENARIOS_PATH | /app/deploy/text2sql-scenes.corpus-v6.yml |
| MODEL_RUNTIME_CONFIG_PATH | /app/deploy/conversation-hooks.model-runtime.local.yml |
| 知识检索 | 继承 knowledge-search.bge-snapshot.yml 的只读本地 BGE |
| 前端开发代理 | frontend/dev-server/local.yml → http://127.0.0.1:28070 |

稳定演示入口：<http://127.0.0.1:5179/chat>；开发窗口：<http://127.0.0.1:5174/chat>（需 `npm run dev`）。可输入“查询科技点击量最高的前3条新闻并分析原因”，或使用场景 YAML 的示例。默认选择模拟数据的最后一天第一个整点小时；“今天”不会被偷换为历史模拟日期。

## 验证入口与结果

```bash
cd agent
PYTHONPYCACHEPREFIX=/tmp/newsagent-pycache python3.11 -m pytest -o addopts='' -q \
  tests/test_corpus_profiles.py tests/test_real_news_demo_deploy.py tests/test_real_news_wiring.py \
  tests/test_sql_assistant_warehouse.py tests/test_public_headlines.py tests/test_timeline_v5_profiles.py \
  tests/test_hot_news_sql_binding.py tests/test_native_hot_news_sql_support.py tests/test_native_hot_news_sql_scaled.py \
  tests/test_sql_assistant_service.py tests/test_conversation_hot_news_query.py \
  tests/test_hot_news_query_diagnostics.py tests/test_hot_news_query_windows.py
cd frontend
npm test
npm run build
npm run lint
```

本次相关后端测试 245 passed、3 skipped（需专用 PostgreSQL 测试环境的用例）；本地推理、原生热点运行和分析重试的补充测试 16 passed；前端全量 338 passed；类型检查、构建和 lint 通过。新增覆盖原缓存/BGE交集、内容哈希、数据身份、元数据漂移阻断、知识版本类型、可执行示例问题、保存快照片段和部署命令边界。

实际浏览器查询已完成运行 `0c2802a3-874f-4faf-8206-f118f061531d`，返回“智谱和 MiniMax，把大模型做成了两种生意”“WorkBuddy这一年：腾讯AI实践最真实的25个判断”“WorkBuddy开放平台上线，AI办公迎来硬件入口之争”。三篇 API 标题与 catalog 完全一致，三份 600 字正文片段均来自对应原文；BGE 每篇召回三份相关片段。指标与分析来自本地模拟，仅用于链路验证。

工作台示例按钮也已执行成功：`b9261b72-110d-4438-a5a0-90efc2eca636`，5 篇上榜并完成分析。5 个页面标题均与对应 ID 的原缓存一致，展开的宇树机器人正文片段匹配原文。1360px 与 390px 检查的页面宽度均等于视口，手机榜单仅在自身容器内横向滚动。截图与逐条核对结果位于 `output/real-news-demo-2026-10-14/`，其中 `verification.json` 来自可见页面 DOM 与冻结 catalog 的比较，不包含企业行为明细。

## 未完成 TODO

1. 接入企业聚合行为指标和基线后，才能判断真实热度；跨小时去重需要企业数仓支持。
2. 演示板块改为可信业务栏目映射；新闻扩容、缓存刷新和 BGE 元数据补齐需发布新 catalog/契约版本，不能修改已有冻结版本绕过摘要。
3. 企业推理/Embedding 接口与模型质量验收仍需独立完成；本地确定性模型不作为质量结论。
4. 当前替换覆盖本地演示入口及其完整热点链；生产接入、持续调度与发布依旧遵循既有版本化和人工审批要求。
