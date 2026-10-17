# 2026-10-14 新闻增量导入

已有可信公开标题目录的最新发布时间为北京时间 2026-10-06。本次补采
2026-10-07 至 2026-10-14，共 541 篇完整图文正文，保留来源、真实发布时间、原始
`news_id`、正文版本和文件 SHA-256。120 次公开栏目列表请求成功，2,908 个唯一候选
中实际尝试正文 590 篇；49 个无法提取完整正文的页面未纳入，其他候选达到每日配额后
跳过。公开推荐列表不构成腾讯新闻全量历史档案。

| 北京日期 | 完整正文 |
| --- | ---: |
| 2026-10-07 | 19 |
| 2026-10-08 | 28 |
| 2026-10-09 | 30 |
| 2026-10-10 | 26 |
| 2026-10-11 | 78 |
| 2026-10-12 | 120 |
| 2026-10-13 | 120 |
| 2026-10-14 | 120 |

冻结采集入口为 `var/news/data/incremental/2026-10-07_2026-10-14-v1/manifest.json`，
SHA-256 为 `13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee`。
正文位于同目录 `articles/`；列表原始响应、请求参数、响应摘要、候选和失败均有独立审计。
采集范围截至 10 月 14 日采集时点，不包括采集之后新发布的文章。

## 完整调用链

```text
collect_incremental_news CLI
→ 北京时间窗口 / 已冻结文件与请求摘要预检
→ discover / feed_request / fetch_feed
→ 腾讯公开 getPCList 六栏目，有限翻页
→ extract_candidates 校验图文、来源 ID 和候选时间
→ collect → collect_recall_articles.fetch_html / parse_article
→ 正文 meta 时间核验、清洗、ID / 内容去重、每栏目每日配额
→ articles/*.json + manifest.json + discovery_manifest.json + URL 清单
├→ import_news_cache.load_documents
│ → 文件摘要 / 来源 / 发布时间 / 版本校验 → 每批最多 20 文档
│ → loopback /api/v1/knowledge/documents
│ → KnowledgeDocumentPayload.as_document
│ → PostgresKnowledgeStore.upsert_documents
│ → split_text / EmbeddingPort → PostgreSQL 文档与版本化切片
│ → knowledge-report.json / 幂等重放回执
└→ build_incremental_news_index.load_articles / _verify_package
  → article_chunks → split_text 800 字、重叠 80 字
  → 本地 BGE 文档 Embedding（1024 维，归一化，不加查询前缀）
  → 保留原包正文和向量、追加新切片、重建 HNSW 行号标签
  → SHA256SUMS 校验、原子发布新包、build_report.json
  → 新检索 YAML / 本地 Compose overlay
  → API 与热点 Worker 的 build_knowledge_search_index
  → BgeSnapshotVectorIndex / 匹配的 LocalBgeQueryEmbedder
  → ConversationTools.search_knowledge / 热点关联召回
  → 保存的工具证据、聊天 SSE 与页面显示
```

模型依赖只来自已有本地 BGE 包；索引工具依赖 NumPy、SentenceTransformers、hnswlib。
采集依赖标准库和 BeautifulSoup。入库工具只使用标准库，凭据由指定环境变量注入；本次
实际使用仓库公开的本地演示凭据，未读取 `.env` 或企业密钥。新闻正文始终作为不可信证据。

PostgreSQL 入库仍调用现有 `native-local-embedding-v1` 测试 Port，只验证持久化链路；
语义召回使用单独生成的真实本地 BGE 向量。两种向量不混合。原 BGE 包、冻结 corpus-v6
目录、旧运行报告和数据库历史保留。SQL 热点候选仍使用原 v6 合成指标，新增正文没有真实
行为数据，因此不会据此生成最近八天的真实热度排行。

## 配置与复用

采集默认 6 栏目、每栏目 20 页、每页 50 条、正文最多 4 并发、每栏目每天最多 20 篇，
超时 15 秒、最多重试 2 次；Feed / 正文响应上限 4 / 2 MiB。时间以正文 HTML 元数据为准，
不从 ID 或模型输出推测。已有缓存哈希不匹配会在任何续跑写入前阻断。

```bash
# 从仓库根目录执行；已冻结/启用的版本不继续扩充，后续增量请选择新目录版本。
PYTHONPATH=news python3.11 -m experiments.collect_incremental_news \
  --start-date 2026-10-07 --end-date 2026-10-14 \
  --output-dir var/news/data/incremental/2026-10-07_2026-10-14-v1

# NEWSAGENT_KNOWLEDGE_TOKEN 由本地安全环境设置，不在命令或报告中输出。
python3.11 agent/tools/import_news_cache.py \
  --manifest var/news/data/incremental/2026-10-07_2026-10-14-v1/manifest.json \
  --base-url http://127.0.0.1:28070 \
  --tenant-id 11111111-1111-4111-8111-111111111111 \
  --report output/news-import-2026-10-14/knowledge-report.json

# 新包拒绝覆盖；失败可用完全相同参数加 --resume 继续。
python3.11 agent/tools/build_incremental_news_index.py \
  --articles-dir var/news/data/incremental/2026-10-07_2026-10-14-v1/articles \
  --base-package var/knowledge-snapshots/rag-recall-demo \
  --output-package var/agent/data/news-bge-incremental-20261014-v1 \
  --snapshot-version news-bge-incremental-20261014-v1 \
  --threads 4 --batch-size 16 --device mps

# 不具备 MPS 的设备选 --device cpu；本地服务的查询模型继续使用 CPU。
python3.11 agent/tools/start_real_news_demo.py --incremental-news-index --check --skip-build --min-free-gib 4
python3.11 agent/tools/start_real_news_demo.py --incremental-news-index --skip-build --min-free-gib 4
```

检索配置为 `deploy/knowledge-search.incremental-20261014-v1.yml`，包目录
`../data/news-bge-incremental-20261014-v1`，租户为已有本地演示租户；API 和热点 Worker
通过 `deploy/docker-compose.incremental-news-demo.yml` 同时选择这份只读配置。
`start_real_news_demo.py` 的 `--incremental-news-index` 将新 overlay 放在最后，并在启动前
检查新包存在。默认命令仍对应原冻结演示，重新部署新增召回时必须带此参数。
前端入口为 `http://127.0.0.1:5179/chat`，已有开发入口 5174 代理同一 API 28070。
生成的模型/向量大文件由 `.gitignore` 排除，重建来源和回执仍保存在工作区。
本次已有镜像，无需重新构建；磁盘余量为 9.2 GiB，启动预检显式选择已有参数
`--min-free-gib 4`。启动器的默认余量门槛仍为 10 GiB。

## 测试入口

```bash
cd news
python3.11 -m pytest -o addopts='' -q \
  tests/test_collect_incremental_news.py tests/test_collect_recall_articles.py
cd ../agent
python3.11 -m pytest -o addopts='' -q \
  tests/test_import_news_cache.py tests/test_incremental_news_index.py \
  tests/test_real_news_demo_deploy.py tests/test_bge_snapshot_index.py \
  tests/test_bge_snapshot_wiring.py tests/test_knowledge_embedding_dimensions.py
```

离线检查覆盖身份/时间/摘要、续跑篡改阻断、畸形 API 回执、同版本幂等、原索引保留、
新增小索引召回与部署配置顺序。实际 541 篇文件全部经过正文和文件哈希预检；真实数据库、
模型索引、API/Worker 加载与聊天召回另以 `output/news-import-2026-10-14/` 的验收回执为证。

本次相关离线测试共 122 项通过（采集 29，Agent/入库/索引/部署 93）。实际入库
`created=541`，同一 Manifest 重放 `skipped=541`，541 篇数据库正文摘要全部与采集文档
摘要一致。新包新增 1,647 切片，总计 12,728 篇、38,026 切片，维度 1024；原包摘要保持
不变。新包 Manifest SHA-256 为
`edf9d5f6cd9b05f8d1c5eeece8c47424bcfdeaf6d569b02f4d5619a042b27733`，版本
`news-bge-incremental-20261014-v1`。

API 与热点 Worker 已加载新版本；`/ready` 三项均为 `ok`，Temporal 热点、写作、评测
队列均有实际 Workflow/Activity poller。经现有前端网关创建验收会话，检索
“一个月降价两次惹怒车主？特斯拉降价玩法怎么了？”与“你投入真心的AI伴侣，生杀大权
在谁手里？”，分别命中 10 月 7 日与14日的新增原文；返回的标题、来源、日期、ID、索引
版本均核对通过。验收链接：
`http://127.0.0.1:5179/chat?conversation=4d4e5954-4314-452a-843a-1eeedeefac5c`。

详细回执：`knowledge-report.json`、`knowledge-replay-report.json`、
`data-verification.json`、`live-chat-verification.json`，均位于上述验收输出目录。

## 未完成 TODO

- 49 个没有可解析完整正文的页面没有导入；公开推荐接口仍不能保证每日全量覆盖。
- 原快照已有 ID 的更正需要单独版本化更新流程，本构建工具保留原版本并跳过重复 ID。
- 最近新闻尚无企业真实聚合指标和基线；SQL 热点窗口仍对应原演示数据。
- 企业推理/Embedding 接口、正式语料权限及生产持续导入仍待独立联调和审批。
