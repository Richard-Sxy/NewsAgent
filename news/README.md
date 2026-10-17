# 腾讯新闻采集与 NewsAgent 知识入库

该子项目负责分类发现、正文抓取、清洗、URL 幂等、缓存、标题 NLP、失败重试和每日任务。
合格内容通过 NewsAgent 自有知识 API 入库；切片、Embedding、索引与 QA 由
`agent/` 的 Python 实现完成。

## 调用链

```text
腾讯新闻分类页/详情页
  → TencentNewsCrawler
  → ArticleCache
  → NewsIngestService
  → 可选 LTPTitleAnalyzer
  → NewsAgentKnowledgeClient
  → POST /api/v1/knowledge/documents
  → Python 分块 + EmbeddingPort + 版本化索引
```

QA 链路使用同一正文缓存，通过 `POST /api/v1/knowledge/qa` 创建问答式知识文档。
SQLite 只保存本地幂等、重试、标题分析和文档 ID，不作为主服务的知识事实库。

## 安装与配置

```bash
cd news
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

必须配置：

```dotenv
NEWSAGENT_BASE_URL=http://127.0.0.1:28000
NEWSAGENT_KNOWLEDGE_TOKEN=replace-with-knowledge-ingest-token
NEWSAGENT_TENANT_ID=11111111-1111-4111-8111-111111111111
NEWSAGENT_TIMEOUT=120
```

令牌必须与主服务的 `KNOWLEDGE_INGEST_TOKEN` 一致。不要将真实令牌提交到仓库。

## 使用

单篇入库：

```bash
python -m scripts.ingest_one "https://news.qq.com/rain/a/NEWS_ID"
```

批量 URL：

```bash
python -m scripts.ingest_batch urls.txt --workers 4
```

发现并入库：

```bash
python -m scripts.discover_and_ingest --topic tech --max-pages 1 --limit 5
```

执行每日任务：

```bash
python -m scripts.ingest_daily
```

为已入库新闻生成 QA 文档：

```bash
python -m scripts.generate_qa --limit 20
```

只做发现、不写数据库和知识库时，使用 `scripts.discover_sitemap`。定时执行可使用
`scripts/run_daily.sh`，部署时应先确认路径、日志目录、限流和运行用户。

## 标题 NLP

标题 NLP 默认关闭。启用前先下载模型：

```bash
python -m scripts.download_title_nlp_model
```

然后设置 `TITLE_NLP_ENABLED=true`。`TITLE_NLP_REQUIRED=false` 表示抽取故障会单独记录，
但不阻断正文入库；设为 `true` 后它成为数据质量硬门槛。

## 幂等与失败

- URL 成功记录会被跳过，失败记录按 `INGEST_MAX_RETRY_COUNT` 限制重试。
- 正文缓存避免重复请求来源站点；删除缓存后需要正文时会重新抓取。
- 主服务返回的 `created/updated/skipped/failed` 是索引任务的权威状态。
- `401/403` 优先检查知识令牌和租户头；连接失败优先检查主服务 `/ready`。
- 新闻正文和页面字段均视为不可信输入，不允许它们改变抓取或索引策略。

## 测试

```bash
pytest -q
```

测试使用假的抓取器和知识客户端，不访问外部新闻站点或企业模型接口。真实联调前还需确认
抓取合规、请求频率、来源站点变化、企业 Embedding 配额和跨租户隔离。
