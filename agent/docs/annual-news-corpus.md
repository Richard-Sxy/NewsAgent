# 年度真实新闻语料与人工相关性审核

后续进展：已完成隔离新闻入库、真实 BGE 实验索引与可保存的人工标注工作台，见 [年度召回标注工作台](annual-retrieval-lab.md)。下文保留首批采集阶段的快照和当时待办。

本轮按用户确认的分工实施：系统主动搜索事件、收集多篇真实新闻和保存来源证据；“某查询的哪些结果算相关”由人工标注。自动发现、正文相同、事件归属提示、模型评分和向量相似度均不构成人工金标。本工具不产生 qrels，也不计算召回准确率。

## 首批采集交付（2026-10-27）

采集窗口为 2025-10-27 至 2026-10-27 的当前采集时刻，覆盖科技、AI、财经、股票、汽车、数码、国际、军事、体育、游戏。不是全网新闻全集，也不是均衡抽样或真实用户查询分布。

- 发现清单：82 个待核查事件分组、9 个专题合集；共 783 个候选链接。`discovery_real_event_count` 是早期字段名，仅统计非专题的发现分组，不表示事实或事件归属已验证。
- 域名规则与 URL 归一化后实际采集 775 条：559 条通过正文与日期基础检查，186 条仅元数据，19 条未取得页面日期，11 条采集失败。失败和缺口均保留，未伪造正文补齐。
- 559 条基本可浏览正文中有 14 组正文完全相同的来源。所有来源均保留；这只是确定性哈希建议，不等于完成语义上的同事实去重。
- 91 个发现分组（含专题）中，31 个尚不足两篇基础检查通过的正文，其中 14 个尚无此类正文。发现清单有多篇链接不保证均可成功抓取。
- 559 条 `relevance_label` 均为 null、`human_labels` 均为空，相关性与事件归属均为 `pending_human`；`retrieval_accuracy` 为 null。

| 板块 | 通过基础检查的来源数 |
| --- | ---: |
| 科技 | 43 |
| AI | 75 |
| 财经 | 35 |
| 股票 | 9 |
| 汽车 | 35 |
| 数码 | 19 |
| 国际 | 25 |
| 军事 | 22 |
| 体育 | 281 |
| 游戏 | 15 |

体育来源明显较多，股票与部分月份仍需补采，不能用当前分布宣称年度覆盖充分。`body_ready` 只表示取得标题、页面日期和至少 200 字符的可解析正文，不表示人工认证全文完整、新闻事实正确或与查询相关。图集、视频配文和转载仍可能需要进一步核查。

## 产物位置

路径均相对于仓库根目录。

- `var/news-events-annual-20261027/discovery/tech-events.json`：科技、AI、汽车、数码、游戏事件发现候选。
- `var/news-events-annual-20261027/discovery/world-events.json`：财经、股票、国际、军事事件发现候选。
- `var/news-events-annual-20261027/discovery/topic-seeds.json`：9 个明确选择的公开专题入口。
- `var/news-events-annual-20261027/discovery/topic-links.json` 与 `topic-html/`：专题实际链接和专题页面快照。
- `var/news-events-annual-20261027/corpus-v1/report.json`、`progress.json`、`raw/`、`articles/`：采集结果、失败状态、原始页面与解析记录。
- `output/news-events-annual-20261027/review-v1/index.html`：本地可搜索人工浏览清单，含来源、页面日期、正文、哈希和发现分组；同一来源跨分组可能重复展示。当前页面是只读浏览器，不包含标注保存后端。
- 同目录 `review-records.jsonl`：559 条带证据的人工核查输入，人工标签为空。
- 同目录 `content-candidates.jsonl`：559 条统一新闻内容包候选，保留来源证据，不是已发布数据库或索引。
- 同目录 `summary.json`：逐板块、逐月、逐分组覆盖、失败来源及正文哈希相同分组。

## 完整调用链

1. 上游为人工检索确认的公开事件链接清单，以及明确选择的专题页面。`discover_annual_event_links.main → discover → article_links` 读取专题 HTML 中实际存在的文章链接，限制来源域名、单专题数量、请求大小和时间窗口。专题关联与 URL 日期只存为发现提示；`published_at` 留空，URL 日期不会充当页面发布日期。
2. `collect_annual_event_news.main → merge_manifests` 合并发现文件、归一化 URL、去除重复链接并保留多个事件和板块提示。采集器按审核过的公开域名请求页面，约束重定向、公共 IP、超时和响应大小，不访问企业接口或数据库。
3. 页面解析从实际新闻正文容器取文，从页面 meta、JSON-LD 或标题附近的时间证据识别发布日期。缺证据或越界的记录单独列出。原始 HTML、解析 JSON、正文及报告中的 SHA-256 形成可追溯快照。缓存续跑重新检查哈希和解析证据。
4. `prepare_annual_news_review.main → verify_records` 校验报告引用的解析文件、原始页面和正文哈希，只导出基础检查通过的记录。按正文哈希提出完全相同来源组，生成安全转义的本地 HTML、核查 JSONL、统一内容候选和覆盖报告。外部正文仅为数据，不能改变工具行为。
5. 下游目前止于人工浏览清单和内容候选。已用现有 `app.modules.news_storage.model.build_package` 在隔离容器内对 559 条候选完成内容契约校验；仅在内存构包，没有写库、发布内容版本、调用 Embedding 或切换热／温／冷索引。

## 依赖与配置

发现和采集工具使用 Python 3.8+、标准库 HTTP/JSON/线程池和 BeautifulSoup4；审核导出仅使用 Python 标准库，无 Agent 或知识库框架运行时依赖。内容包契约校验使用项目现有 Python 3.11 环境。工具不读取 `.env`。

发现 CLI 提供 `--seeds`、`--output`、`--cache`、`--start-date`、`--end-date`、`--limit-per-topic`、`--timeout`；默认专题最多 100 条、固定 4 个发现线程，专题响应最多 3 MB。采集 CLI 提供可重复的 `--manifest`、`--output-dir`、`--workers`、`--timeout`、`--max-articles`、日期窗口，域名规则位于脚本静态审核列表。当前专题种子各最多 100 条，采集使用 6 个线程、12 秒超时、最多 1500 条。采集脚本的 `--discover-links` 是保留选项，报告明确标记其未实现递归发现；本轮实际发现由独立专题发现脚本完成。

审核 CLI 提供 `--corpus-dir`、可重复的 `--discovery`、`--output-dir`。输出中的来源证据使用仓库相对路径，方便校验和迁移。

## 复现命令

在仓库根目录的 Linux 环境运行；可续用已有缓存。重跑会更新生成时刻，公开页面也可能变化，应保留本轮快照，另建目录进行后续版本采集。

```bash
python3 news/experiments/discover_annual_event_links.py \
  --seeds var/news-events-annual-20261027/discovery/topic-seeds.json \
  --output var/news-events-annual-20261027/discovery/topic-links.json \
  --cache var/news-events-annual-20261027/discovery/topic-html --timeout 15

python3 news/experiments/collect_annual_event_news.py \
  --manifest var/news-events-annual-20261027/discovery/tech-events.json \
  --manifest var/news-events-annual-20261027/discovery/world-events.json \
  --manifest var/news-events-annual-20261027/discovery/topic-links.json \
  --output-dir var/news-events-annual-20261027/corpus-v1 \
  --workers 6 --timeout 12 --max-articles 1500

python3 agent/tools/prepare_annual_news_review.py \
  --corpus-dir var/news-events-annual-20261027/corpus-v1 \
  --discovery var/news-events-annual-20261027/discovery/tech-events.json \
  --discovery var/news-events-annual-20261027/discovery/world-events.json \
  --discovery var/news-events-annual-20261027/discovery/topic-links.json \
  --output-dir output/news-events-annual-20261027/review-v1
```

## 验证入口

```bash
(cd news && python3 -m unittest tests.test_discover_annual_event_links tests.test_collect_annual_event_news -q)
(cd agent && python3 -m unittest discover -s tests -p test_annual_news_review.py -q)
```

当前 34 项发现／采集测试和 16 项审核导出测试通过，共 50 项；覆盖来源约束、链接发现、日期证据、正文解析、缓存、哈希篡改、输出转义与人工标签留空。实际 559 条产物另做全量文件哈希核对和内容包契约校验。这些验证不等于模型质量或检索准确率验收。

## 首批采集阶段待办（进展见工作台说明）

1. 优先补齐股票、低覆盖月份及不足两篇正文的事件；继续系统发现和补采，人工不需要自行上网搜集素材。
2. 核查正文完整性与发布时间。559 条中仅 7 条具备明确时区及分钟／秒级时间；552 条的内容包 `publish_time` 留空并带质量标记，同时保留来源原始日期精度。不能将仅日期记录捏造成精确午夜，或直接宣称全部可进生命周期索引。
3. 将内容候选冻结为版本化内容包并对接已有新闻入库链路；目前统一新闻内容存储到热／温／冷索引的桥接仍需完成，未写入生产库。
4. 用现有新闻语义契约提出同事实去重和 follow_up、response、background、correction 等带原文证据的关系候选。保留多来源、后续新事实和更正，不能把同主题或时间先后当作因果。当前没有完成语义合并、逻辑链构建或多跳关系验收。
5. 使用真实企业 Embedding Port 建立隔离的热／温／冷实验索引，补充逐查询的路由、各层预算、候选、过滤、融合和最终结果展示。历史回放需要明确 `as_of` 过滤以避免未来信息泄漏；当前链路尚未完成此能力。
6. 人工负责查询与相关性标签，系统负责提供候选、来源和审核材料；建立明确的查询、news_id、相关等级、审核人／时间与版本契约后，再确定性计算 Precision@K、Recall@K、nDCG 等。未判定项需单独统计，不能默认为不相关；人工金标覆盖不足时不报整体准确率。

长期确认的分工记录在 `DemoMD/PROJECT_CONTEXT.md` 第 38 节；本轮采集数量和覆盖只是当前快照。
