# 新闻入库链路的召回度量与优化方案

分析日期：2026-09-11
分析范围：`tencent-news-crawler`（新闻发现 → 抓取 → 清洗 → 去重 → FastGPT 入库）
证据来源：源码通读 + 对腾讯公开接口与 sitemap 的实测取样 + 现有评测产物（`data/*.json`）

本文只给出诊断、指标定义、改动点与验收标准。核心业务代码由用户亲自实现，
文中"骨架"是可直接照填的接口契约，不是已完成的实现。

---

## 一、现状链路

```text
① 发现
   scripts/ingest_daily.py  (DEFAULT_SOURCES 固定 6 个频道)
   └─ DiscoveryIngestService.discover
      └─ TencentTopicCrawler.get_news_indexes
         ├─ parse_news_indexes        静态 HTML 的 <a href="/rain/a/...">
         └─ fallback: fetch_feed      getPCList 接口，flush_num 翻页
      └─ filter_by_date                publish_time.startswith(target_date)

② 登记
   repository.mark_discovered()        逐条 INSERT ... ON CONFLICT

③ 入库 (串行, workers 默认 1)
   ingest_url()
   ├─ repository.get_by_url()          按 URL 幂等
   ├─ repository.mark_pending()
   ├─ ArticleCache.get_or_fetch() → TencentNewsCrawler.crawl()
   │     ├─ fetch_html()  (httpx, 无限速)
   │     ├─ parse()       正文容器选择器 → <p> 收集 → 噪声段过滤
   │     └─ ArticleCache.validate()   空标题/空正文/<50 字 → 抛错
   ├─ LTPTitleAnalyzer.analyze()      可选，写 news_title_* 两表
   ├─ format_article_text()           标题+URL+作者+时间+正文 拼成一段 markdown
   └─ FastGPTClient.create_news_collection()
         trainingType=chunk, chunkSettingMode=auto   ← 切片完全交给 FastGPT
   └─ repository.mark_success(collection_id)

④ QA 生成 → 第二个知识库（news_qa_service）
⑤ 评测   → evaluate_retrieval (recall@k / MRR)、evaluate_qa
⑥ 报告   → generate_daily_report
```

---

## 二、实测数据（2026-09-11 实跑腾讯公开接口）

| 观测项 | 实测结果 | 含义 |
|---|---|---|
| 分类页静态 HTML 内 `/rain/a/` 链接数 | **0 条**（`news.qq.com/ch/tech/`） | 每次都走 feed 接口，静态解析分支实际空跑 |
| `getPCList` 单页返回条数（`item_count=20 / 50`） | **25 / 55** | 单页真实上限高于 20，当前 `page_size` 被硬编码封顶 20 |
| `flush_num=0..4` 页间重叠 | **0** | 翻页有效，`max_pages` 是真实的召回杠杆 |
| 6 频道 × 3 页，当日非视频可用量 | tech 32 / sports 21 / finance 32 / ent 18 / auto 13 / edu 11 = **127** | 当前配置量级符合预期 |
| sitemap 子文件数 | **1000** | 全站索引规模 |
| 单个 sitemap 分片可解析文章 ID | **3306** 条，其中当日 1770 条 | 当日全站量级远大于 130 |
| `data/urls-2026-09-03-sitemap.txt`（仓库已有） | **54,206** 条当日唯一 URL | 可离线复算的"真值分母" |
| 其中图文 A / 视频 V | **27,425 / 26,781**（V 占 49%） | 视频接近一半，当前 100% 丢弃 |
| 当前每日实际入库上限 | 6 × 20 = **120** 篇 | — |

**结论：当日图文覆盖率 ≈ 120 / 27,425 ≈ 0.44%；含视频 ≈ 0.22%。**
这不是调参问题，而是"发现入口只覆盖 6 个频道标签页"的结构性缺口。

---

## 三、问题清单

### P0-1 视频新闻召回率为 0

`crawler/topic_crawler.py::parse_feed_indexes` 中：

```python
if article_type == "4":
    continue
```

腾讯当日视频约占一半（实测 sitemap 分片 V 占 49%）。README P1 已明确"不能完全忽略
视频"，当前实现等价于视频覆盖为零。

### P0-2 覆盖率没有分母，也没有指标

现有 `scripts/evaluate_retrieval.py` 度量的是**检索召回**（已入库新闻能否被搜到），
而不是**采集召回**（当天真实发布的新闻有多少被入库）。更关键的是
`scripts/generate_retrieval_questions.py` 的取数条件：

```sql
JOIN news_ingest_records AS ingest ON ingest.url = qa.url
WHERE qa.status = 'submitted'
  AND ingest.status = 'success'
```

题目**只从已成功入库的记录反向生成** → 未入库的新闻永远不会进入评测集 →
漏采在指标上不可见。同时 `tests/retrieval_questions.json` 每题
`expected_collection_ids` 恒为 **1 个**（实测 min=max=1），使 `recall@5 = 1.0`
天然乐观。

### P0-3 sitemap 通道已实现但未接入每日任务

`crawler/tencent_sitemap.py` + `scripts/discover_sitemap.py` 已能拉到当日全站 URL
（实测可用），但 `scripts/ingest_daily.py` 只调用 `DiscoveryIngestService`，
sitemap 从未进入生产路径。这是把覆盖率从 0.4% 提到两位数的唯一现实手段。

### P1-1 逐条写库

`DiscoveryIngestService.discover_and_ingest`：

```python
for item in items:
    self.ingest_service.repository.mark_discovered(...)
```

而 `IngestRepository.mark_discovered_many()`（`executemany`，单事务）已实现却未被调用。

### P1-2 入库串行且正文抓取无限速

`discover_and_ingest` 调用 `ingest_urls(...)` 未传 `workers` → 默认 1。
另外 `crawler_request_interval = 0.5s` 只作用于 `TencentTopicCrawler.request()`，
而正文抓取走 `TencentNewsCrawler.fetch_html()` 的裸 `httpx.get`，**没有限速**。

### P1-3 质量拒绝与网络失败混为一类

`ArticleCache.validate()` 因"标题为空 / 正文为空 / 内容过短"抛出的 `ValueError`
会被 `ingest_url` 的通用 `except Exception` 捕获 → `mark_failed()` → `retry_count += 1`。
一条永远不合格的正文会被抓取 3 次才进 `retry_exhausted`，浪费 3 倍请求。

### P1-4 入库成功 ≠ 可召回

`mark_success(url, title, collection_id)` 只记录 `collection_id`。
FastGPT 返回 `insertLen: 0` 时无法区分"分片异步训练中"和"内容真的为空"。
README 也承认原文入库与知识索引耦合。检索召回会漏掉训练失败或卡住的 collection。

### P1-5 切片不受控（对检索召回影响最大的一处）

`format_article_text` 把元数据和正文拼成一段 markdown，然后
`chunkSettingMode: "auto"` 全权交给 FastGPT。
仓库中 `rag/chunker.py` 的 `chunk_text()` **从未被任何生产代码调用**。
切片粒度 / 是否带标题上下文，直接决定 `recall@k`，目前是黑盒且不可 A/B。

### P1-6 去重键仍是 URL，缺少 news_id 与内容版本

`news_ingest_records` 主键为 `url`，没有 `news_id`、`content_hash`、`content_version` 列。
`NewsArticle.news_id` 属性已实现但未落库。后果：
- 同一 article ID 若 URL 形式变化（http/https、query、频道参数）会重复入库；
- 没有"内容更正 / 撤稿"的版本概念，无法失效旧索引。

### P2-1 潜伏的静默漏采（当前未触发，但很危险）

`parse_news_indexes` 构造 `NewsIndex(title=..., url=..., topic=...)`，
**`publish_time` 恒为空字符串**（`topic_crawler.py` 第 199、210 行）。
而 `get_news_indexes` 结尾统一执行：

```python
indexes = self.filter_by_date(indexes=indexes, target_date=target_date)
```

`filter_by_date` 用 `item.publish_time.startswith(target_date)` 判断 →
`"".startswith("2026-09-11")` 为 `False` → **一旦某天分类页 HTML 里出现 `<a href="/rain/a/...">`，
该频道当天会被整体过滤为 0 条，而且不抛任何异常、不打日志**。

今天实测该页面 0 条 `/rain/a/` 链接，所以走的是 feed 分支，问题未暴露。但腾讯改版
随时会触发，属于必须加固的静默失败点。

### P2-2 `UTR` 假 ID 会造出 404 URL

实测热点聚合父节点的形态：

```json
{ "id": "UTR2026081421031200", "articletype": 525,
  "sub_item": [ { "id": "UTR2026081421031200", "articletype": 116,
                  "thing_info": { "focus_id": "20260911A08UWK00" } } ] }
```

`parse_feed_indexes` 取 `article_id = focus_id or item.get("id", "")`。
当 `thing_info` 缺失时回退到 `UTR...`，而 `ARTICLE_ID_PATTERN = ^[A-Za-z0-9]+$`
**能通过校验**，于是生成 `https://news.qq.com/rain/a/UTR2026081421031200` →
抓取必 404 → 消耗重试额度。

### P2-3 SQLite 并发与缺失索引

- `IngestRepository.connect()` 每次 `sqlite3.connect(self.db_path)`，无 `timeout`、
  无 WAL。`workers > 1` 时并发写 `mark_pending / mark_failed / save_title_analysis`
  容易 `database is locked`。
- `news_ingest_records` 无 `status`、`publish_time` 索引；
  `list_pending_qa` 是全表 `LEFT JOIN`。

---

## 四、指标定义（建议新增）

### 4.1 采集漏斗（每日 / 每频道）

| 指标 | 定义 | 数据来源 |
|---|---|---|
| `discovered_total` | 发现入口返回的候选数 | `discover()` 返回值 |
| `unique_url_total` | 去重后候选数 | `deduplicate()` 输出 |
| `fetched_ok` | 正文抓取成功数 | 缓存命中 + 新增 |
| `parse_ok` | 标题与正文解析通过数 | `ArticleCache.validate` 通过 |
| `quality_rejected` | 质量硬拒绝数（应为一次性终态） | 新增 `status='rejected'` |
| `dedup_skipped` | 已入库跳过数 | `status='skipped'` |
| `ingest_success` | collection 创建成功数 | `status='success'` |
| `index_ready` | 索引训练完成、可被检索数 | 新增 `index_status` |
| `ingest_success_rate` | `success / (success + failed + retry_exhausted)` | 派生 |

### 4.2 覆盖率召回（本项目最关键的新指标）

```text
coverage_recall        = |ground_truth ∩ ingested| / |ground_truth|
coverage_recall_video  = 同上，仅视频(V) URL
coverage_recall_article= 同上，仅图文(A) URL
coverage_lag_p95       = 从 publish_time 到 index_ready 的 P95 时延
```

`ground_truth` 来源：`TencentSitemapCrawler.discover(date)` 落盘的当日全站 URL 集合
（腾讯 sitemap 是唯一可稳定获取的"全站真值"）。仓库已有离线样本可直接复算：
`data/urls-2026-09-03-sitemap.txt` = 54,206 条。

### 4.3 内容质量

`content_len_p10 / p50`、`empty_content_rate`、`min_length_reject_rate`、
`author_missing_rate`、`publish_time_missing_rate`。

### 4.4 去重与身份

`url_dedup_rate`、`duplicate_news_id_rate`（同 `news_id` 对应多个 URL 的比例）、
`content_hash_changed_rate`（同一 news_id 内容变更率，用于更正/撤稿）。

### 4.5 检索召回（已有，需补偏差修正）

现有：`recall@k`、`MRR`。
建议补：`nDCG@k`、`hit_rate@k`、按 `topic` 分组 recall、按发布日期分组 recall、
负例题 `refusal_accuracy`、以及**未入库样本的漏采影响**（从 ground truth 抽样未入库新闻
构造题目，期望"这题本应能答"）。

### 4.6 稳定性

`retry_exhausted_rate`、HTTP 4xx/5xx 分类计数、`fastgpt_error_rate`、
`locked_db_error_count`。

---

## 五、优化方案

### 阶段一：先让召回可测量

#### 1. 新增 `scripts/build_coverage_ground_truth.py`

```python
"""落盘指定日期的全站新闻 URL 真值集，作为覆盖率召回的分母。"""

def main() -> int:
    # TODO 1: argparse --date YYYY-MM-DD（必填）、--workers 默认 8、--limit 可选
    # TODO 2: TencentSitemapCrawler(workers=args.workers).discover(args.date)
    # TODO 3: 落盘 data/ground_truth/urls-<date>.json
    #         {"date":..,"generated_at":..,"urls":[..],
    #          "article_count":..,"video_count":..,"other_count":..}
    # TODO 4: 用 SitemapDiscoveryResult.sitemap_count/failed_sitemaps 输出健康度
    # TODO 5: 退出码：全部 sitemap 失败 → 1；有部分失败 → 2；全部成功 → 0
    ...
```

执行与预期：

```bash
python -m scripts.build_coverage_ground_truth --date 2026-09-11
# 预期：data/ground_truth/urls-2026-09-11.json
#       article_count + video_count 合计量级 1e3 ~ 1e4
```

#### 2. 新增 `intelligence/ingest_metrics.py`

```python
from __future__ import annotations
from collections.abc import Iterable

def split_by_content_kind(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    """按 article id 第 9 位字符切分：A=图文 V=视频，其余归入 other。
    TODO 1: URL 先 rstrip('/').split('/')[-1] 取 id
    TODO 2: 长度不足 9 的 id 归入 other，不抛异常
    """

def compute_ingest_funnel(records: list[dict]) -> dict:
    """records: 目标日期下的 news_ingest_records 行（dict 列表）。
    TODO 1: 按 status 计数：discovered/pending/success/failed/
            retry_exhausted/skipped/rejected
    TODO 2: ingest_success_rate = success / (success + failed + retry_exhausted)
    TODO 3: 分母为 0 时返回 0.0，并置 insufficient_data=True
    """

def compute_coverage_recall(
    ground_truth_urls: list[str],
    ingested_urls: list[str],
) -> dict:
    """TODO 1: 两侧都用 TencentTopicCrawler.normalize_article_url 归一化后再比
    TODO 2: 分母 = len(ground_truth_urls)
    TODO 3: 返回 {denominator, numerator, recall,
                  denominator_article, recall_article,
                  denominator_video, recall_video}
    TODO 4: 分母为 0 → recall=0.0 且 insufficient_data=True（禁止 ZeroDivisionError）
    """

def compute_content_quality(articles: list[dict]) -> dict:
    """TODO 1: 正文长度分位数 p10/p50/p90
    TODO 2: author/publish_time 缺失率
    TODO 3: 空列表输入返回全 0，不抛异常
    """
```

#### 3. 扩展 `service/daily_report_service.py`

在现有 JSON/MD 输出中增加 `coverage` 与 `funnel` 两段；MD 报告输出一张漏斗表 +
一行 `coverage_recall`。不改现有字段名，保持向后兼容。

#### 4. 验收测试 `tests/test_ingest_metrics.py`

- 空 ground truth → `recall=0.0` 且 `insufficient_data=True`，不抛异常。
- A/V 拆分：构造 `20260903A...` / `20260903V...` / 短 ID 三种输入。
- 归一化：`https://new.qq.com/rain/a/X?a=1` 与 `https://news.qq.com/rain/a/X` 视为同一条。
- 用 `data/urls-2026-09-03-sitemap.txt` 当 fixture，断言分母为 **54206**。

### 阶段二：提升召回

#### 5. 接入 sitemap 通道路径

`service/discovery_ingest_service.py`

```python
class DiscoveryIngestService:
    def __init__(self, topic_crawler=None, ingest_service=None, sitemap_crawler=None):
        # TODO 1: 新增 self.sitemap_crawler = sitemap_crawler or TencentSitemapCrawler()

    def discover(
        self,
        sources: dict[str, str],
        limit_per_topic: int = 20,
        target_date: str | None = None,
        max_pages: int = 3,
        use_sitemap: bool = False,          # TODO 2
        sitemap_limit: int | None = None,   # TODO 3
    ) -> dict:
        # TODO 4: use_sitemap 时调用 self.sitemap_crawler.discover(target_date, sitemap_limit)
        # TODO 5: 合并顺序 = feed 结果在前（带 title/topic/publish_time），
        #         sitemap 结果在后（title="", topic="sitemap", publish_time=""）
        # TODO 6: 合并后仍走 TencentTopicCrawler.deduplicate()，URL 相同时保留 feed 那条
        # TODO 7: 返回值 sources 里追加一条 {"topic": "sitemap", "count": ...}
```

`scripts/ingest_daily.py` 增加 `--use-sitemap` 与 `--sitemap-limit` 两个开关。
注意：sitemap 条目没有标题和频道，`publish_time` 为空，因此**必须同时完成第 8 项
（`filter_by_date` 放行空时间）**，否则新增的 URL 会被静默丢光。

#### 6. 放宽发现上限

`crawler/topic_crawler.py::get_news_indexes`：

```python
# 现状
page_size = min(max(limit or 20, 10), 20)
# 建议
page_size = min(max(limit or 20, 20), 50)   # 实测 item_count=50 返回 55 条
```

#### 7. 视频分级接入（最小版本）

`crawler/topic_crawler.py::parse_feed_indexes`：不再 `continue` 丢弃 `articletype == "4"`，
改为在 `NewsIndex` 上增加 `is_video: bool = False` 标记后返回。
`NewsIngestService.ingest_url` 遇到 `is_video=True` 时只写
`news_ingest_records`（标题、`video_info` 中的时长/封面 URL），`status='video_pending'`，
**不抓正文、不调 FastGPT**。视频覆盖从 0 → 元数据级，成本接近 0。
后续再按 README P1 补字幕/ASR。

#### 8. 修复三个静默失败点

`crawler/topic_crawler.py`：

```python
# 8a. filter_by_date 放行空时间，避免静态解析结果被整体清零
@staticmethod
def filter_by_date(indexes, target_date):
    if not target_date:
        return indexes
    return [i for i in indexes if not i.publish_time or i.publish_time.startswith(target_date)]

# 8b. 排除 UTR 假 ID（无 focus_id 时的兜底）
if not focus_id and str(item.get("id", "")).startswith("UTR"):
    continue

# 8c. 日期过滤丢弃量显式计数，超过阈值 logger.warning
```

对应测试：

- `test_filter_by_date_passes_empty_publish_time`
- `test_feed_indexes_skip_utr_without_focus_id`
- `test_feed_indexes_keeps_video_with_flag`
- `test_page_size_caps_at_50`

#### 9. 质量拒绝独立终态

`service/article_cache.py` 新增异常类型：

```python
class ArticleQualityError(ValueError):
    """标题/正文不满足最低内容质量要求，属不可重试的终态失败。"""
```

`validate()` 改为抛 `ArticleQualityError`（保留 `ValueError` 兼容性）。
`service/news_ingest_service.py::ingest_url` 在通用 `except` 之前单独捕获：

```python
except ArticleQualityError as exc:
    self.repository.mark_rejected(url, str(exc))
    return {"url": url, "title": "", "collection_id": None,
            "insert_len": 0, "status": "rejected", "error": str(exc)}
```

`ingest_urls` 增加 `rejected` 计数；`IngestRepository` 增加 `mark_rejected()`
（写 `status='rejected'`，**不增加** `retry_count`）。

验收：同一篇正文过短的新闻重复运行 3 次，`retry_count` 恒为 0，`status` 恒为 `rejected`。

#### 10. Repository 加固

`service/ingest_repository.py`：

```python
# TODO 1: initialize 增加列 news_id / content_hash / content_version / is_video
#         / source_kind / index_status / index_error，migrate() 同步补列
# TODO 2: 建索引 idx_ingest_status_publish(status, publish_time)
# TODO 3: connect() → sqlite3.connect(db_path, timeout=30)
#         启动时执行一次 PRAGMA journal_mode=WAL
# TODO 4: 新增 mark_rejected(url, reason)
# TODO 5: 新增 mark_index_status(url, collection_id, index_status, error=None)
# TODO 6: 新增 list_by_date(target_date) 供指标计算
# TODO 7: 新增 count_duplicate_news_id() 供去重指标
```

`mark_discovered_many` 在 `discover_and_ingest` 中替换逐条循环。

#### 11. 入库并发与限速

- `discover_and_ingest` 调用 `ingest_urls(..., workers=settings.ingest_workers)`，
  新增配置 `ingest_workers: int = 4`。
- `TencentNewsCrawler.fetch_html` 复用与 `TencentTopicCrawler.request` 相同的
  限速与重试（抽一个共享的 `crawler/http.py`，或在 crawler 上挂 `wait_for_rate_limit`）。
  否则正文抓取并发一开就会打爆腾讯。

#### 12. 索引与入库解耦（README P1 落地）

```text
mark_success（同事务写 news_index_tasks: status='pending'）
  → KnowledgeIndexWorker 异步消费
  → 调 FastGPTClient.create_news_collection
  → 回写 collection_id + index_status='ready' / 'failed' + retry_count
  → 新版本索引 ready 后再使旧版本失效
```

最小实现可以先用 SQLite 一张 `news_index_tasks` 表 + 一个独立脚本
`scripts/run_index_worker.py` 轮询，不必马上引入 Temporal。

#### 13. 阶段二验收

```bash
python -m scripts.build_coverage_ground_truth --date 2026-09-11
python -m scripts.ingest_daily --date 2026-09-11 --use-sitemap --sitemap-limit 2000
python -m scripts.generate_daily_report --date 2026-09-11
```

报告中出现 `coverage_recall`、`coverage_recall_article`、`coverage_recall_video`
三个数；`quality_rejected` 与 `retry_exhausted` 分离；`video_pending` 计数 > 0。

### 阶段三：检索召回优化

1. **切片可控化**：把 `format_article_text` 拆成"元数据头 + 正文"，
   用 `chunkSettingMode: "custom"` 指定 `chunkSize` / `chunkSplitter`，
   并让每片携带标题作为上下文（contextual chunking）。
   用现有 `evaluate_retrieval.py` 做 A/B，对比 `recall@1 / recall@5 / MRR`。
2. **评测集去偏**：
   - `generate_retrieval_questions.py` 的 JOIN 条件不能再限定
     `ingest.status='success'`，否则漏采永远不可测；
   - 补**负例题**（知识库中不存在，期望拒答，对齐 `should_refuse`）；
   - 补**多正例题**（同一事件多篇报道，`expected_collection_ids` ≥ 2），
     单正例下 `recall@k` 的区分度不足；
   - 补**未入库题**：从 ground truth 抽样当期未成功入库的新闻构造题目，
     量化"漏采对下游可用性的影响"。
3. **指标扩展**：`nDCG@k`、`hit_rate@k`、按 `topic` / 发布日期分组 recall。

---

## 六、预期收益

| 指标 | 现状 | 阶段二完成后 |
|---|---|---|
| 当日图文覆盖率 | ~0.44% | 5% ~ 15%（取决于 sitemap 取量与抓取预算） |
| 当日视频覆盖率 | 0% | 元数据级，接近当日 feed 可见量 |
| 单频道单页上限 | 20 条 | 50 条 |
| 入库吞吐 | ~1 篇/秒（串行 + 部分限速） | 4 ~ 8 倍 |
| 质量硬拒绝的重试浪费 | 3 次/篇 | 0 |
| `retry_exhausted` 误计 | 质量问题混入 | 与真实网络失败分离 |
| 静默漏采风险 | 2 处（P2-1 / P2-2） | 消除并带告警 |
| 检索 recall@1 / MRR | 0.92 / 0.9533（50 题，样本有偏） | 先修评测集偏差，再谈数值提升 |

---

## 七、执行顺序建议

1. 阶段一 1-4 项：**先拿到 `coverage_recall` 这个数**。没有分母，后面所有优化都无法验证。
2. 阶段二 8 项：修三个静默失败点（改动小、风险低、收益高）。
3. 阶段二 5-7 项：接 sitemap、放宽上限、视频元数据接入（真正的召回提升）。
4. 阶段二 9-11 项：质量终态、Repository 加固、并发限速（稳定性与吞吐）。
5. 阶段二 12 项 + 阶段三：索引解耦与切片 A/B（影响检索召回）。

## 八、本方案的边界

- 本文不修改任何业务实现文件，不含生产配置变更。
- 覆盖率分母取自腾讯 sitemap，它是"腾讯愿意公开的全站 URL 集合"，
  不等于"腾讯实际发布总量"；该指标应表述为
  **"sitemap 口径覆盖率"**，不能表述为"全站覆盖率"。
- 视频接入的第一版只到元数据层，不包含字幕/ASR/多模态，不能表述为
  "视频内容已入库"。
- 所有百分比均为量级估算，需在阶段一指标落地后以实测数值替换。
