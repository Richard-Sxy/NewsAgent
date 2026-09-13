# 腾讯新闻向量入库

本文只讲一件事：**新闻正文如何进入 FastGPT 知识库并形成可检索的语义索引**。
企业级容量与架构推演见 [`../knowledge-base-scale-design.md`](../knowledge-base-scale-design.md)；
运维命令、cron、报告等见上级目录的 `../../README.md`。

## 一、一句话理解

向量入库 = 把「已抓取清洗的新闻」交给 FastGPT，由 FastGPT 完成**分片 + 向量化 +
索引**，我们只负责内容、元数据和状态记录。

```text
腾讯新闻 URL
  → 抓取正文（TencentNewsCrawler）
  → 本地 JSON 缓存（ArticleCache）
  → 组装文本 + metadata
  → FastGPT 创建 collection（异步训练/向量化）
  → SQLite 记录 collection_id 与状态
```

## 二、两条入库链路

代码在 `service/fastgpt_client.py`，对应两个 FastGPT dataset。

### 1. 原文知识库（raw dataset）

`create_news_collection(article, text, extra_metadata)`

- 接口：`POST /api/core/dataset/collection/create/text`
- 关键参数：
  - `trainingType: "chunk"` —— 按文本分片。
  - `chunkSettingMode: "auto"` —— **分片交给 FastGPT**，本地不切。
  - `dataEnhanceCollectionName: true` —— 用内容增强 collection 名称。
- 用途：保存新闻全文，支持按语义召回相关报道与背景。

### 2. QA 知识库（qa dataset）

`create_news_qa_collection(article, text, extra_metadata)`

- 同样走 `create/text`，但：
  - `trainingType: "qa"`；
  - `qaPrompt` 来自 `prompts/news_qa_generation.txt`；
  - dataset 用 `FASTGPT_QA_DATASET_ID`。
- 用途：让 FastGPT 依据正文**生成问答对**再向量化，提升问答式检索质量。

两条链路共用同一份正文缓存和同一套 metadata，一篇新闻通常只请求腾讯一次。

## 三、文本与元数据（检索质量的关键）

### 文本

`service/news_ingest_service.py:format_article_text` 把结构化对象拼成带标题、来源地址、
作者、发布时间和正文的纯文本，再交给 FastGPT。

### metadata

`create_news_collection` 写入的字段：

```text
source            = tencent_news / tencent_news_qa
news_id           = 从 URL 路径末段提取（与主服务统一关联键）
source_url
original_title
author
publish_time
+ 标题 NLP 实体（启用 TITLE_NLP 时）：
  title_persons / title_organizations / title_proper_nouns ...
```

为什么 metadata 重要：

- `news_id` 是主服务 `FastGPTKnowledgeSearchClient` 把检索结果映射回新闻的唯一依据
  （读取 `metadata.news_id`，或从 `sourceName` 的 `tencent-news-{id}.txt` 兼容解析）。
- 标题实体用于后续事件聚类、实体热度与检索过滤。
- collection 命名：原文 `tencent-news-{news_id}`，QA `tencent-news-{news_id}-qa`。

## 四、分片策略

- **生产入库由 FastGPT 的 `chunkSettingMode=auto` 分片**，本地不参与。
- `rag/chunker.py` 是学习/实验用的固定窗口 + overlap 实现，**不接生产链路**。
  原因：分片与向量化在 FastGPT 内耦合，本地重复实现容易与线上检索效果不一致。

## 五、状态、幂等与重试

SQLite（`data/news_ingest.db`）承担去重与状态：

- `news_ingest_records`：原文入库，URL 为主键，记录 `collection_id`、`status`、
  `retry_count`、`error_message`。
- `news_qa_records`：QA 生成，URL 为主键，记录 `qa_collection_id` 与状态。
- `news_title_analyses` / `news_title_entities`：标题分词与实体。

幂等规则：

- URL 已 `success` → 再次运行返回 `skipped`，不重复入库。
- 失败累加 `retry_count`，达到 `INGEST_MAX_RETRY_COUNT` / `QA_MAX_RETRY_COUNT` 后自动
  任务不再请求；修复外部问题后用 `scripts/reset_retry.py` 人工重置。
- 正文缓存：`data/articles/<URL 的 SHA-256>.json`，缓存损坏时重新抓取并原子替换。

## 六、异步性与训练状态

创建 collection 只返回 `collectionId`；**分片与向量化是异步的**，所以：

- 返回的 `insertLen` 可能为 0，不代表失败。
- 用 `service/fastgpt_training_service.py` 检查：
  - `getDatasetTrainingQueue` → `trainingCount` / `rebuildingCount`
  - `hasDatasetTrainingError` → `hasError`
  - `check_all()` 聚合原文库与 QA 库状态。
- `scripts/check_training_status.py` 退出码：`ready=0`、`training=2`、`error=1`、`failed=1`。

## 七、检索侧如何衔接

主服务（`writing-agent-service`）的 `FastGPTKnowledgeSearchClient` 调
`/api/core/dataset/searchTest`，按切片结果归并到 collection，并用 `news_id` 与本地
内容库、行为指标关联。也就是说：

> 入库阶段写入 `news_id`，决定了后续「热点 → 关联报道 → 证据」能否闭环。

## 八、评测与优化

- 评测题集：`tests/evaluation_questions_v2.json`。
- 脚本：`scripts/evaluate_qa.py`、`scripts/evaluate_retrieval.py`。
- 召回优化记录：`docs/news-ingest-recall-optimization.md`。
- 标题 NLP：`docs/title-nlp-ingest.md`。

## 九、已知边界（待办）

- 入库与知识索引目前**同步耦合**，尚未拆成「保存 → Outbox → 异步索引 Worker」。
- 新闻更正、撤稿、版本更新时的**索引失效/删除**尚未实现。
- 视频字幕/ASR 文本尚未接入统一入库链路。
- QA 生成质量与检索召回需要持续评测，并把高频别名补进实体词典。

## 十、关键代码位置

| 关注点 | 文件 |
| --- | --- |
| 单篇/批量入库 | `service/news_ingest_service.py` |
| QA 生成 | `service/news_qa_service.py` |
| FastGPT 写入 | `service/fastgpt_client.py` |
| 训练状态 | `service/fastgpt_training_service.py` |
| 正文缓存 | `service/article_cache.py` |
| 状态/去重 | `service/ingest_repository.py`、`service/qa_repository.py` |
| 发现并入库 | `service/discovery_ingest_service.py` |
| 分片（实验） | `rag/chunker.py` |
