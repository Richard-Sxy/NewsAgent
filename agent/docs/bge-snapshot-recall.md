# BGE 新闻快照接入

实际启动、部署和聊天卡片展示请先按 [独立 BGE 演示部署教程](bge-demo-deployment.md) 操作。

`var/knowledge-snapshots/rag-recall-demo/` 的向量库已作为 NewsAgent 原生只读检索 backend 接入。
热点榜单继续由企业/合成数仓的聚合指标和 Python 热度规则产生，快照负责主题检索和
热点上榜之后的相关新闻召回。它没有实时热度或日期筛选能力。

## 包内文件与存储

| 文件 | 内容与用途 |
| --- | --- |
| `var/knowledge-snapshots/rag-recall-demo/experiments/query_bge_snapshot.py` | 原离线查询参考：固定 BGE query instruction、归一化、HNSW IP、按文章去重；应用没有将该脚本作为运行时依赖 |
| `data/experiments/offline_recall_fg/chunks.jsonl` | 36,379 个新闻片段，12,187 个不同 `news_id`；HNSW label 是文件行号 |
| `data/experiments/offline_recall_fg/embeddings.npy` | `(36379, 1024)` float32 归一化向量；与 JSONL 行一一对应 |
| `data/experiments/offline_recall_fg/index.bin` | 同版本 HNSW 内积索引 |
| `data/models/bge-large-zh-v1.5/` | 本地模型、tokenizer、Pooling 与 Normalize 配置 |
| `SHA256SUMS` | 模型和语料的哈希清单，用于完整性验证和检索身份绑定 |

路径以 `var/knowledge-snapshots/rag-recall-demo/` 为起点。当前清单 SHA-256 为
`e68c268dadcf434d81c5e397e578321b1c614215d42a81a5ae82848ba4901dab`。
查询 Embedding 版本是 `bge-large-zh-v1.5:e68c268dadcf434d`，切片 ID 使用完整清单摘要和行号。
`(news_id, chunk_index)` 有重复，不能当作唯一切片键。

这份快照所有结构化 `url` 和 `publish_time` 都为空；80 篇新闻没有可提取的 Markdown 标题。
候选标题只从已有 `# ` 首行提取，缺失时明确显示新闻 ID。链接和时间不会从片段文字推测。
当前仓库也没有可直接用于补齐的真实新闻元数据台账。

## 完整调用链

热点链：

```text
热点 API / 聊天 query_hot_news / Temporal Schedule
→ 原 Text2SQL / 企业聚合取数 → Python 指标、基线、热度排行
→ HotNewsEnrichmentService.enrich
→ 按 news_id 读取源正文，title + summary 转 RelatedNewsSearchQuery
→ build_native_knowledge_search → TieredVectorKnowledgeSearchClient
→ LocalBgeQueryEmbedder：固定中文 instruction + 本地 BGE + 归一化 1024 维
→ BgeSnapshotVectorIndex.search：HNSW IP → 排除源新闻 → 按 news_id 去重
→ 候选不足时有界扩展切片池 → 单层 RRF
→ 原正文库尝试补齐 → RelatedNewsReranker 业务重排
→ HotNewsAnalysisInputBuilder 裁剪正文与证据白名单
→ 原生模型 Port → 既有 Schema / Validator → 已保存报告 / 查询 API
```

`app/model_runtime/knowledge_factory.py` 从选中的索引取配套 `query_embedder`，因此不会用
模型运行时的本地 16 维测试 Embedding 或另一种企业 Embedding 查询这份 1024 维索引。
正常模型推理、知识 API 入库和 PostgreSQL 台账继续使用原来的 Port；这份快照不能写入或删除。

当前演示 Worker `examples/native_hot_news_e2e_worker.py` 同样通过索引工厂装配。
配置外部索引时，`ProfileScopedContentRepository` 只把合成数仓目录中的候选 ID 交给
SQL 正文库，其他候选保留快照片段和缺失元数据。底层 SQL 的租户/ID 校验仍会执行。

聊天链：

```text
/chat → conversations API → ConversationAgentService
→ ConversationTools.search_knowledge → 同一个索引配置与 BGE query_embedder
→ 去重、片段裁剪、安全链接 → PostgreSQL 工具检查点 → SSE / 文本答复
```

本地规划器的明确追问“检索第一条的相关新闻”从当前会话已完成的热点结果取标题和
`news_id`，再调用 `search_knowledge` 并排除源新闻。无上下文或知识权限时不猜来源。
普通“解释第一条”继续读取原热点报告。真实模型使用同一个带可选 `source_news_id` 的工具合同。
聊天结果携带离线覆盖限制和快照/Embedding 版本，正文始终作为不可信证据。

### 热点对话自动检索与前端片段

不需要用户再发送一次“搜索相关文章”。读取已完成热点运行后，
`ConversationTools.read_hot_news` 在原有知识权限开启时，自动为返回结果的前 3 条新闻
各召回最多 3 篇相关文章。`query_hot_news` 完成查询后复用这一读取路径，选择单条排名
时只召回该新闻。源新闻标题缺失时明确提示，不使用新闻 ID 猜测主题。

```text
/chat → POST /api/v1/conversations/{id}/messages[/stream]
→ ConversationAgentService → query_hot_news / read_hot_news
→ 已授权、已完成的热点运行 → news_id + title
→ ConversationRelatedArticles.recall → 配套 query_embedder 批量编码
→ TieredVectorKnowledgeSearchClient → 同一只读 BGE/HNSW 索引
→ 排除源新闻、按文章去重、正文裁剪到 600 字、安全链接与带时区时间校验
→ ToolTrace.result.related_articles → 原工具检查点和会话存储
→ 同步响应 / SSE done.turn.tools / GET 会话历史
→ ChatView 完成轮次 → projectRelatedArticles → RelatedArticlesPanel
→ 根据源新闻分组显示标题、前 300 字、展开到 600 字、可用的时间和原文链接
```

新增 `app/conversation/related_articles.py` 只依赖现有原生检索 Port 和 Python 标准库。
自动召回整个批次最多等待 5 秒；`ConversationAgentService` 传递可信的工具/整轮截止时间，
不足 1 秒余量时跳过召回，为热点工具返回和检查点留出时间。检索异常或自身超时返回提示，
不修改热点指标或评分，不重试召回；用户取消或整轮超时仍遵循原会话取消规则。
BGE 的后台线程不能随协程超时强行终止，正式并发容量仍需评测。

结果合同位于成功热点工具的 `result.related_articles`，每组包含：

| 字段 | 用途 |
| --- | --- |
| `source_run_id`、`source_news_id`、`source_title` | 绑定已授权热点运行和源新闻 |
| `status`、`notice` | 区分成功、空结果、无法召回和离线覆盖限制 |
| `items[].news_id`、`chunk_id`、`title`、`excerpt` | 相关文章身份与最多 600 字的正文片段 |
| `items[].source_url`、`publish_time` | 缺失时返回 `null`，不推测或补造 |
| `items[].embedding_version`、`snapshot_version`、`retrieval_backend` | 保存检索身份，供工具记录审计 |
| `evidence_is_untrusted` | 明确外部文本证据边界 |

前端只读取已保存的成功工具结果，并校验组的运行 ID 和源新闻 ID 对应热点列表。
`projectRelatedArticles` 还复用显式 `search_knowledge` 结果，按源新闻合并去重，最多显示
3 组、每组 3 篇。`RelatedArticlesPanel.vue` 用 Vue 文本插值显示不可信片段；链接只允许
无凭证且无控制字符的 HTTP(S)，发布时间和来源缺失时显示“未知”“来源链接未提供”。
展开和收起不请求全文；本次仅将有限片段传到浏览器。旧历史没有字段时不显示面板。

自动文章片段完整保存在工具检查点中，刷新历史和同 UUID 重放可以恢复，不再次检索。
SSE `tool_finished` 保持原有摘要；卡片在收到 `done` 后显示。新增自动片段不进入当前轮
规划模型输入或会话摘要预算，由 `_model_trace` 投影裁掉，指标和分析结果保持原样。
显式知识检索仍使用原来模型证据链。本次没有改 Prompt、业务 Schema、数据库迁移，
没有新增前端依赖或运行开关。

独立入口 `tools/query_news_recall.py`：参数/租户校验 → 工厂 → 上述原生召回 Port →
可选业务重排 → JSON → 关闭索引。`retrieval_score` 是归一化 RRF 排名分数；
`related_score` 是业务重排分数，两者都不是新闻热度或经过校准的相关概率。

## 配置与运行

`deploy/knowledge-search.bge-snapshot.yml` 声明 `backend: bge_snapshot`。
`package_dir` 相对 YAML 所在目录解析，默认指向仓库根下的包；无需复制大模型或索引。
配置项包括：`tenant_id`、`snapshot_version`、`device`、`chunk_pool`、`ef`、
`max_query_chars`、`model_batch_size`。包没有租户 ACL，因此示例只绑定本地演示租户
`11111111-1111-4111-8111-111111111111`；其他租户被拒绝或无结果。
`snapshot_version` 是配置审计名，实际模型/切片身份另外绑定清单哈希。

安装独立可选依赖，然后在 `agent/` 运行：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,bge-snapshot]'
USE_TF=0 USE_FLAX=0 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2 \
  .venv/bin/python tools/query_news_recall.py \
  --tenant-id 11111111-1111-4111-8111-111111111111 \
  --query 'WAIC 2026 智能体和机器人有哪些进展？' --limit 5
```

`--news-id 20260719A04P4Y00` 可替换 `--query`，从快照取源标题和摘要并排除源新闻；
`--source-news-id` 可用于已有源标题的查询。`--related` 仅在有源 ID 和可靠、带时区的
发布时间时执行业务重排，调用者可显式提供 `--publish-time`；元数据缺失时返回语义召回及说明。

应用启动时设置 `KNOWLEDGE_SEARCH_CONFIG_PATH` 指向该 YAML。
聊天还需要原有 `CONVERSATION_ENABLED` 和 `CONVERSATION_KNOWLEDGE_ENABLED` 权限开关。
API 和 Worker 必须挂同一个配置、清单、模型和索引。模型只加载本地文件，禁止远程代码；
哈希、标签、行数、向量维度或版本错误直接失败，不回退另一个检索 backend。

`deploy/docker-compose.bge-snapshot.yml` 是原 native E2E 栈的可选 Overlay，包含
只读目录挂载、PyTorch/offline 环境和 E2E 镜像的可选依赖构建参数。
保存配置不会重启当前实例或修改已批准 Bundle。可先检查并构建隔离本地配置：

```bash
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.bge-snapshot.yml config --quiet
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.bge-snapshot.yml build migrate
```

镜像构建需要安装 hnswlib 的编译工具，配置使用 `Dockerfile.e2e` 的可选依赖参数。
新镜像的本地激活应明确使用独立 Compose 项目和端口，避免覆盖正在运行的演示；生产切换
仍需要批准的新 Bundle 和发布流程。2026-10-12 已在独立 `news-agent-bge-demo` 项目中
构建并启动 local 实例、迁移该实例数据库；未进行生产激活。见 [部署验收记录](local-bge-simulation.md)。

## 验证与后续项

快速验证入口：

本次相关回归 `185 passed, 3 skipped`：其中两项真实 BGE 测试默认需显式启用，另有一项
现有依赖型测试跳过。额外真实 BGE 验收 `2 passed`；Compose 合并后的构建参数与
API/Worker 的只读挂载校验通过；这些是初次接入阶段的测试记录，当时未执行容器构建。
同一临时隔离环境的快照、接线、CLI、正文目录适配和真实模型测试合计 `68 passed`；
额外运行原生 SQL、知识入库、模型 Port 与会话回归 `87 passed, 2 skipped`，与上述用例
有部分重叠，不累加成总数。

热点对话前端片段补充验收（与上面的回归重叠，不累加）：
后端相关回归 `325 passed, 1 skipped`；前端完整测试 `244 passed`，类型检查和生产构建通过。
真实 BGE 测试现为 `3 passed`，新增覆盖自动片段经工具检查点进入 SSE `done` 和历史读取、
指标保持原值、模型输入剔除自动片段以及 UUID 重放不重新编码。
另通过临时只读浏览器验收页，将真实对话保存结果交给当前生产构建的前端，确认源新闻分组、
真实标题/正文片段、300→600 字展开收起和缺失来源提示。页面指标使用明确标注的合成数据，
当时未连接运行中的服务，临时服务已关闭。2026-10-12 已另行完成真实 Docker、
PostgreSQL/Temporal、三轮 SSE 与浏览器卡片展开/刷新验收，详见上述部署记录。

新增专项测试入口：

```bash
python3.11 -m pytest -o addopts='' -q tests/test_conversation_article_delivery.py
cd frontend
node --test tests/related-articles.test.mjs
npm test
npm run build
```

```bash
python3.11 -m pytest -o addopts='' -q \
  tests/test_bge_snapshot_index.py tests/test_bge_snapshot_wiring.py \
  tests/test_news_recall_cli.py tests/test_conversation_related_news.py \
  tests/test_snapshot_fixture_content.py tests/test_milvus_search_config.py \
  tests/test_tiered_vector_retrieval.py tests/test_conversation_agent.py \
  tests/test_conversation_security.py tests/test_conversation_hot_news_query.py \
  tests/test_conversation_query_resolution.py tests/test_hot_news_enrichment.py \
  tests/test_related_news_reranker.py tests/test_native_hot_news_runtime.py \
  tests/test_native_hot_news_sql_support.py
```

真实本地模型验收需要显式启用：

```bash
RUN_BGE_SNAPSHOT_TESTS=1 USE_TF=0 USE_FLAX=0 HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2 \
  python3.11 -m pytest -o addopts='' -q tests/test_bge_snapshot_live.py
```

真实测试覆盖完整性、真实 BGE/HNSW 相关新闻排除和去重、聊天共享编码器，以及真实召回
经业务重排进入热点分析输入。其热点指标与源正文使用已有合成场景，不连接企业数仓，
不产生新 Workflow、不调用分析推理模型、不代表企业效果验收。
另已用包指定 `sentence-transformers==6.0.1` 在临时隔离环境实测 CLI。

尚待完成：权威时间/来源元数据补齐、企业语料授权、持续入库与快照重建、删除和更正策略、
带标注的召回质量和生产容量评测。真实 Temporal/API 的 local 部署验收已完成，
企业模型/数仓及生产环境仍须另外验收。
当前热点业务 Schema 不含检索快照版本，聊天和 CLI 已输出版本，切片 Port 已带完整清单
摘要；正式生产还需将该版本绑定到获批 Bundle 并补运行级检索审计，再通过人工发布流程。
