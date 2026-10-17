# 每日千篇新闻导入

2026-10-15 用户要求将上一轮的小批采集扩充到每天千篇。当前回补范围仍为北京时间
2026-10-07 至 2026-10-14，目标为每天 1,000 篇经过正文验证和去重的新闻，共 8,000 篇。
原 v1 的 541 篇保留，并计入对应日期的目标。候选网址、抓取尝试、重试和切片均不计为
成功新闻。实际完成状态以 `output/news-bulk-import-2026-10-15/` 的回执为准。

## 本次已完成的数据与索引

| 北京时间日期 | 保留原有 | 本次新增 | 实际总数 |
| --- | ---: | ---: | ---: |
| 2026-10-07 | 19 | 981 | 1,000 |
| 2026-10-08 | 28 | 972 | 1,000 |
| 2026-10-09 | 30 | 970 | 1,000 |
| 2026-10-10 | 26 | 974 | 1,000 |
| 2026-10-11 | 78 | 922 | 1,000 |
| 2026-10-12 | 120 | 880 | 1,000 |
| 2026-10-13 | 120 | 880 | 1,000 |
| 2026-10-14 | 120 | 880 | 1,000 |
| 合计 | 541 | 7,459 | 8,000 |

知识 API 回执为 7,459 created、541 skipped、0 errors。数据库按完整 news_id 清单核验
8,000 篇标题与正文指纹、实际发布时间全部一致，原生切片共 13,873 条。冻结 v2 新闻
Manifest SHA-256 为 `f1262e6f4a8a212b35233363eb91df58f34d9407a45b1f541736729c4bd9dac4`。

BGE v2 在原 v1 的 12,728 篇 / 38,026 片基础上新增 7,459 篇 / 12,226 片，最终为
20,187 篇 / 50,252 片。独立验收逐篇覆盖本窗口的全部 8,000 篇 / 13,873 片，确认完整
正文切片、元数据、精确 base∪source 集合和行数、全部正式资产 SHA 均匹配；旧包正文原
字节和向量前缀保留。新包 `SHA256SUMS` 摘要为
`c59a3095657711fb0213b66bf7e7e775c63840961cbad4ba96fdbb9de6bd9138`。

本地 API 与热点 Worker 已通过同一部署类生成的 Compose 命令启用 v2 配置，使用
`up -d --no-deps --no-build --pull never api hot-news-worker`。只读预检采用 2.5 GiB 本地
磁盘预算，API readiness、挂载配置摘要和 Temporal 队列均通过。SQL 仍为 corpus-v6。
完整回执为 `knowledge-report.json`、`database-verification.json`、`index-verification.json`、
`bge-verification.json` 与 `deployment-verification.json`，均在本次输出目录。

聊天网关已从 8 天各选一篇本次新增新闻检索，8/8 全部通过。每轮均核对完成状态、
search_knowledge 轨迹、目标 ID、标题、原文 URL、实际发布时间、v2 快照、Embedding
版本及不可信证据标记；未更换样本。回执为 `live-chat-verification.json` 和
`live-chat-turns/`，总验收为 `data-verification.json`。可打开
[本地验收会话](http://127.0.0.1:5179/chat?conversation=1e5929a0-2097-4e6a-9597-ce282ef6bd3e)。
本次聊天验收脚本入口为 `output/news-bulk-import-2026-10-15/verify_live_chat.py --execute`；
已有回执时加 `--resume` 复用同一会话与请求编号。

## 完整调用链

```text
discover_sitemap_window CLI
→ 固定 HTTPS news.qq.com sitemap 索引 / 子 sitemap
→ XML 大小 / 路径 / 重定向 / 实体限制、原始响应 SHA 校验
→ 日期提示只用于发现和调度，不作为发表时间证据
→ 有界公开候选 + 原始 XML + cache/source/discovery Manifest
→ collect_incremental_news CLI
→ 验证原 v1 Manifest、原字节复制到独立 v2
→ load_external_discovery / collection_contract
→ 优先有缺口日期的候选，保留延后队列作回补
→ fetch_html / parse_article
→ 识别 p、qnt-p、rich_media_content 内叶子 section
→ 只提取正文容器，剔除顶部、问 AI、署名与推荐
→ 以正文 HTML metadata 核验北京时间日期
→ news_id / 标题与正文 SHA 去重，成功原文占用每日配额
→ journal + 每批 Manifest 检查点 / 逐日 deficit
→ import_news_cache.load_documents
→ 整批文件 / 日期 / 来源 / 版本预检
→ 每批 20 文档 POST /api/v1/knowledge/documents
→ KnowledgeDocumentPayload.as_document
→ PostgresKnowledgeStore.upsert_documents
→ 原生 split_text / EmbeddingPort → PostgreSQL 文档及版本化切片
→ build_incremental_news_index.load_articles / _verify_package
→ 全文 800 字切片、80 字重叠，保留旧包所有行和向量
→ 按文本长度分组调用已有本地 BGE；向量行号恢复原顺序
→ 每批 npy + 输入 SHA + 向量 SHA 检查点
→ 1024 维归一化 float32 向量 / HNSW / SHA256SUMS
→ 原子发布新 v2 包
→ verify_bulk_news_index 独立标准库验收
→ 源文件与完整切片摘要、元数据、base∪source 集合及精确行数、旧包前缀校验
→ 版本化 YAML + Compose overlay
→ API / 热点 Worker 加载同一 BgeSnapshotVectorIndex
→ 匹配的 LocalBgeQueryEmbedder
→ ConversationTools.search_knowledge / 热点关联召回
→ PostgreSQL 保存工具证据 → 聊天 API / SSE / 页面
```

新闻正文与检索内容仍为不可信证据，不参与权限或系统指令。只使用公开新闻，不读取
`.env`，不保存企业原始用户行为。企业推理、生产 Prompt、热度规则、审批和发布流程
均不由本次导入修改。

## 来源、配额与版本

公开站点地图已实际读取 40 个子分片，共发现 28,710 个唯一 A 类 ID 候选；包含前一日
10 月 6 日的 200 个候选，供发布时间跨日的新闻回补。10 月 7—14 日候选分别为
3,506 / 4,115 / 3,793 / 2,894 / 3,141 / 3,402 / 3,806 / 3,853。
这些数字仅表示发现数量；每篇最终必须由正文 metadata 单独验证。

冻结发现入口：
`var/news/data/incremental/2026-10-07_2026-10-14-v2/sitemap-discovery/discovery_manifest.json`，
SHA-256 `ef73ed136d5c91353b60d696913a6a9828ca841fd3ab48db8b3eb66100fc2b03`。
原 v1 Manifest 摘要仍为
`13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee`。

旧版每栏目每天 20 篇的限制不再作为默认容量。默认每日全局目标 1,000，默认 4 并发、
最多 8 并发；本次使用 8 并发、请求发起间隔 0.1 秒、有界重试。窗口、目标、栏目限制和
候选源摘要由 collection_contract 固定；续跑可调整并发、超时和限速，语义变化需新版本。
正文最长 200,000 字符、标题最长 500 字符并禁止换行，与知识 API/索引合同一致，超界页
记入失败，不能截短冒充完整正文。

## 复用命令

以下命令从仓库根目录运行。已经发布的版本不能继续扩充；新批次选择新目录和新快照名。

```bash
PYTHONPATH=news python3.11 -m experiments.discover_sitemap_window \
  --start-date 2026-10-07 --end-date 2026-10-14 \
  --output-dir var/news/data/incremental/2026-10-07_2026-10-14-v2/sitemap-discovery \
  --max-sitemaps 150 --workers 4 --target-per-day 2000

PYTHONPATH=news python3.11 -m experiments.collect_incremental_news \
  --start-date 2026-10-07 --end-date 2026-10-14 \
  --output-dir var/news/data/incremental/2026-10-07_2026-10-14-v2 \
  --seed-manifest var/news/data/incremental/2026-10-07_2026-10-14-v1/manifest.json \
  --seed-manifest-sha256 13ffd2d5a4145a4f68cd5ba25fead17340fc7d7a70e0efa02632e75ae58fdcee \
  --discovery-manifest var/news/data/incremental/2026-10-07_2026-10-14-v2/sitemap-discovery/discovery_manifest.json \
  --target-per-day 1000 --workers 8 --request-interval 0.1

# NEWSAGENT_KNOWLEDGE_TOKEN 由本地环境安全注入，不在命令、日志或回执中输出。
python3.11 agent/tools/import_news_cache.py \
  --manifest var/news/data/incremental/2026-10-07_2026-10-14-v2/manifest.json \
  --tenant-id 11111111-1111-4111-8111-111111111111 \
  --base-url http://127.0.0.1:28070 \
  --report output/news-bulk-import-2026-10-15/knowledge-report.json

python3.11 agent/tools/build_incremental_news_index.py \
  --articles-dir var/news/data/incremental/2026-10-07_2026-10-14-v2/articles \
  --base-package var/agent/data/news-bge-incremental-20261014-v1 \
  --output-package var/agent/data/news-bge-incremental-20261014-v2 \
  --snapshot-version news-bge-incremental-20261014-v2 \
  --expected-manifest-sha256 f1262e6f4a8a212b35233363eb91df58f34d9407a45b1f541736729c4bd9dac4 \
  --device mps --precision float16 --batch-size 8 --threads 4

python3.11 agent/tools/start_real_news_demo.py \
  --incremental-news-version v2 --skip-build --min-free-gib 4 --check
python3.11 agent/tools/start_real_news_demo.py \
  --incremental-news-version v2 --skip-build --min-free-gib 4
```

构建中断后，用完全相同参数加 `--resume`，已经提交的批次经摘要校验后复用。
本机 30 篇、86 全文切片实测，内存充足时 MPS/float32/batch16 比 batch32/64 和 float16
更快。实际全量构建时，16 GiB 主机同时运行现有演示，系统 swap 超过 11 GiB；因此完整
保留首轮 float32/batch16 的中断检查点，重新以 float16/batch8 降低权重和激活内存。
精度探针最低余弦相似度为 0.99999934；输出仍为重新归一化的 float32，并在构建身份和
报告中记录精度。默认已使用 SDPA，不以丢弃全文切片换速度。
无 MPS 的机器选择 `--device cpu`。推理只用本地
冻结模型，依赖 NumPy、SentenceTransformers、hnswlib；采集仍仅标准库和 BeautifulSoup。

新检索配置 `deploy/knowledge-search.incremental-20261014-v2.yml` 指向独立 v2 包；
`deploy/docker-compose.bulk-news-demo.yml` 同时配置 API 和热点 Worker。
启动器保留旧 `--incremental-news-index` 的 v1 行为，`--incremental-news-version v2`
显式选择新版。只有包完成构建后才部署，不让运行中 API 读取临时目录。

知识库入库仍使用现有本地确定性 Embedding Port 验证存储，语义检索单独使用真实本地
BGE/HNSW；两种向量不混用。当前 SQL 热点候选仍使用冻结 corpus-v6 的合成行为指标，
新增正文不会被伪造成新增的真实行为或热度。

## 验证入口

全包验收只使用标准库，不加载模型或反序列化向量：

```bash
python3.11 agent/tools/verify_bulk_news_index.py \
  --manifest var/news/data/incremental/2026-10-07_2026-10-14-v2/manifest.json \
  --package var/agent/data/news-bge-incremental-20261014-v2 \
  --base-package var/agent/data/news-bge-incremental-20261014-v1 \
  --expected-manifest-sha256 f1262e6f4a8a212b35233363eb91df58f34d9407a45b1f541736729c4bd9dac4 \
  --expected-base-manifest-sha256 edf9d5f6cd9b05f8d1c5eeece8c47424bcfdeaf6d569b02f4d5619a042b27733 \
  --receipt output/news-bulk-import-2026-10-15/index-verification.json
```

```bash
cd news
python3.11 -m pytest -o addopts='' -q \
  tests/test_collect_incremental_news.py tests/test_collect_recall_articles.py \
  tests/test_discover_sitemap_window.py
cd ../agent
python3.11 -m pytest -o addopts='' -q \
  tests/test_import_news_cache.py tests/test_incremental_news_index.py \
  tests/test_real_news_demo_deploy.py tests/test_verify_bulk_news_index.py tests/test_bge_snapshot_index.py \
  tests/test_bge_snapshot_wiring.py tests/test_knowledge_embedding_dimensions.py
```

相关离线测试 182 项通过（采集/解析/发现 65，入库/索引/部署 103，独立验收 14）。覆盖正文模板与噪声
过滤、真实日期与配额、seed/冻结摘要、恢复中断、语义参数漂移、API 畸形回执、索引批次
续跑与行序、精度和部署版本选择。逐篇数据库内容指纹、索引完整正文和 metadata 已全部
通过，8 天实际聊天抽验通过。单页 PDF 由 `evaluation/render_news_bulk_import_pdf.py`
读取冻结 Manifest 与总验收回执，使用 ReportLab 和嵌入式中文字体生成；交付前核对页数、
用 Poppler 渲染并检查排版。

## 剩余边界

- 公开 sitemap 是有限发现，不能声称覆盖腾讯新闻所有栏目或全部历史；访问失败、跨域
  重定向和未识别正文模板仍须审计，不能填充虚构正文补配额。
- 原索引已有 ID 的更正需要独立版本化流程；本轮保留原版本并跳过相同 ID。
- 本轮是有界回补，未创建自动定时任务或生产持续调度。
- 企业模型质量、真实聚合指标/基线、正式语料权限和生产发布仍待独立验收。
