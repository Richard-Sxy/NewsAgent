# 腾讯新闻榜单：表结构与查询接口

状态：本地模拟已实现；双来源统一接口为待实现方案。以下企业字段是建议映射，不代表腾讯内部真实表名。
已有[冻结数仓格式](sql-warehouse-schema-v1.md)、[场景模板](../deploy/text2sql-scenes.example.yml)和[本地演示说明](text2sql-local-demo.md)。
实时数仓的项目背景、当前 v1/v2 实际规模、约束 SQL 与企业对接边界见
[实时数仓背景与接入说明](realtime-warehouse-background.md)。
近期公开标题与独立 v3 模拟数仓见
[公开标题演示说明](public-headlines-demo.md)；该版本的行为指标仍为合成数据。

## 1. 查询哪些表

计算榜使用现有三张表组成的只读视图；企业原榜需要额外提供榜单快照。

| 查询对象 | 数据来源 | 当前状态 |
| --- | --- | --- |
| 新闻基本信息 | `dw.dim_news` | 已有合成表 |
| 新闻小时指标 | `dw.news_metric_hourly` | 已有合成表 |
| 历史参考基线 | `dw.news_metric_baseline_hourly` | 已有合成表 |
| 计算榜的取数入口 | `dw.news_behavior_aggregate` | 已有18字段只读视图 |
| 企业原始榜单 | 企业榜单服务或快照表，名称由企业提供 | 尚未接入 |

### 新闻表：dw.dim_news

一篇新闻一行；主键 `(tenant_id, news_id)`。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| tenant_id | uuid | 租户/业务域，经企业身份映射 |
| news_id | text | 关联正文、指标和榜单的统一新闻ID |
| title | text | 新闻标题 |
| content_type | text | article图文 / video视频 |
| category | text | 栏目；模拟支持科技、财经、体育、社会 |
| source | text | 来源名称 |
| publish_time | timestamptz | 带时区发布时间 |

### 小时指标表：dw.news_metric_hourly

一篇新闻一个小时一行；主键 `(tenant_id, news_id, event_time)`。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| tenant_id / news_id | uuid / text | 与新闻表关联 |
| event_time | timestamptz | 小时桶起点 |
| impressions | bigint | 曝光次数 |
| clicks | bigint | 点击次数 |
| unique_users | bigint | 单小时去重人数 |
| total_duration_seconds | numeric(20,2) | 总消费秒数 |
| effective_consumptions | bigint | 有效阅读/播放次数 |
| interactions | bigint | 互动事件次数，非互动人数 |

行数据大致如下，全部为合成示例，省略共同的tenant_id：

| news_id | event_time | impressions | clicks | unique_users | total_duration_seconds | effective_consumptions | interactions |
| --- | --- | --- | --- | --- | --- | --- | --- |
| demo-news-001 | 2026-10-03 10:00+08:00 | 10000 | 800 | 600 | 24000.00 | 500 | 120 |
| demo-news-002 | 2026-10-03 10:00+08:00 | 8000 | 400 | 300 | 12000.00 | 260 | 60 |

次数非负，点击不超过曝光，有效消费和小时UV不超过点击。CTR由次数计算：
`clicks / impressions`，示例分别为0.08和0.05；零曝光按批准口径处理。

### 基线表：dw.news_metric_baseline_hourly

主键同小时指标表，以下四个numeric(20,2)字段表示历史可比小时均值：

| 字段 | 含义 |
| --- | --- |
| tenant_id / news_id / event_time | 与当前小时指标对齐 |
| baseline_impressions | 历史曝光均值 |
| baseline_clicks | 历史点击均值 |
| baseline_effective_consumptions | 历史有效消费均值 |
| baseline_interactions | 历史互动均值 |

本地基线是合成参考，不包含真实历史样本数、基线UV和基线时长；缺失不得补造成0。

### 只读视图：dw.news_behavior_aggregate

按租户、新闻和小时连接上述表，提供18列：

```text
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions
```

这是当前Text2SQL唯一允许的查询对象；物理表不对模型开放。视图使用INNER JOIN，
缺基线的指标会被排除，企业适配需要明确处理。样本固定在北京时间2026-10-03，
热点模拟仅支持一个完整小时；小时UV不能跨小时直接相加。当前指标快照要求整数秒，
企业小数时长需确认转换口径。冻结v1不改，新字段另建版本。

## 2. 企业原榜表的大致形式

原榜不必有完整行为指标，但要有榜单ID、快照和原始排名。可由企业提供现有SQL视图、RPC或HTTP接口，再映射为下面的字段；不要求企业重建表。

| 字段 | 建议类型 | 用途 |
| --- | --- | --- |
| tenant_id | 项目映射为uuid | 数据访问范围 |
| board_id | text | 榜单ID，例如科技榜，实际值待提供 |
| snapshot_id | text | 不可变榜单快照ID |
| snapshot_at | 带时区时间 | 快照生成/发布时间 |
| news_id | text | 上榜新闻ID |
| source_rank | integer | 企业原始名次，可并列或跳号 |
| source_position | integer / 稳定顺序键 | 企业条目顺序，保留并列顺序 |
| source_score | Decimal，可空 | 企业原分数，不换成项目热度 |
| board_policy_version | text，可明确未知 | 企业排行规则版本 |
| content_status | text | 发布、下架、受限等状态映射 |

合成示例，省略tenant_id、snapshot_at和策略版本：

| board_id | snapshot_id | news_id | source_rank | source_position | source_score |
| --- | --- | --- | --- | --- | --- |
| demo-tech-board | demo-snapshot-001 | demo-news-002 | 1 | 1 | 92.60 |
| demo-tech-board | demo-snapshot-001 | demo-news-001 | 2 | 2 | 88.30 |

新闻002可以在企业榜排第一，即使新闻001点击率更高；原榜仍保持企业顺序。
建议唯一键为 `(tenant_id, board_id, snapshot_id, news_id)`。若是事件榜，要另加
事件ID及新闻成员映射。分页固定同一快照；过滤下架/受限内容不重编号原始rank，
过滤后不足N可明确返回少于N。缺少指标时返回null，不影响保序读取企业全天榜。

## 3. 查询接口

### 现有入口与待实现入口

| 方法与路径 | 用途 | 状态 |
| --- | --- | --- |
| POST /api/v1/local-simulation/hot-news/run | 本地问题 → SQL取数 → 热点分析 | 已有，仅E2E |
| GET /api/v1/hot-news/runs | 查询本项目历史热点运行 | 已有 |
| GET /api/v1/hot-news/runs/{run_id} | 查询本项目榜单及分析详情 | 已有 |
| POST /api/v1/hot-news/ranking-queries | 统一发起双来源查询 | 拟议，未挂载 |
| GET /api/v1/hot-news/ranking-queries/{query_id} | 读取统一查询状态和结果 | 拟议，未挂载 |

现有本地POST请求使用 `question, scenario_id, window_start, window_end`；
不能把下述拟议请求直接交给它。正式HTTP/RPC协议以企业现有服务为准，
这里的POST/GET是NewsAgent面向页面、聊天的应用接口草案。

现有本地入口可使用以下请求（需隔离E2E租户及hot-news:admin授权）：

```json
{
  "question": "查询科技视频新闻点击率最高的前5条",
  "scenario_id": "news-ranking",
  "window_start": "2026-10-03T10:00:00+08:00",
  "window_end": "2026-10-03T11:00:00+08:00"
}
```

当前入口同步等待热点Workflow完成，最多120秒；返回 `workflow_id, run_id, status,
production_bundle_version, ranked_news_count, analyzed_news_count, sql_query_id`。
用返回的run_id查询已有详情接口即可读取榜单；下文202异步合同属于新接口方案。

### 查询企业原榜：请求示例

```json
{
  "request_id": "44444444-4444-4444-8444-444444444444",
  "source_kind": "enterprise_board",
  "board_id": "demo-tech-board",
  "snapshot_id": "demo-snapshot-001",
  "limit": 1,
  "include_analysis": false
}
```

board_id/snapshot_id均为占位。snapshot_id不传时选最新完整快照，并在首次请求绑定；
同request_id重试继续读取原快照。

### 计算项目榜：请求示例

```json
{
  "request_id": "55555555-5555-4555-8555-555555555555",
  "source_kind": "newsagent_computed",
  "window_start": "2026-10-03T10:00:00+08:00",
  "window_end": "2026-10-03T11:00:00+08:00",
  "content_type": "video",
  "category": "科技",
  "candidate_sort_by": "ctr",
  "candidate_limit": 20,
  "ranking_basis": "newsagent_hot_score",
  "limit": 5,
  "include_analysis": true
}
```

candidate_sort_by决定先选哪些新闻，ranking_basis决定最终排序，limit决定返回几条。
当前SQL先选候选，再由Python热度重排；必须声明“候选内榜单”，不能当成全库TopN。
若用户要CTR榜，应保留CTR顺序，不悄悄切换热度。榜单candidate_limit是拟议新字段，
不同于现有编排策略中用于关联证据召回的同名属性。

### 返回示例

未完成的创建请求返回202，带 `query_id/status`，再用GET查询；完成后返回以下结构。
示例为企业原榜，所有ID和分数均为合成值：

```json
{
  "query_id": "66666666-6666-4666-8666-666666666666",
  "status": "completed",
  "source_kind": "enterprise_board",
  "synthetic": true,
  "board_id": "demo-tech-board",
  "snapshot_id": "demo-snapshot-001",
  "snapshot_at": "2026-10-03T10:05:00+08:00",
  "source_data_version": "demo-data-v1",
  "ranking_basis": "enterprise_original",
  "ranking_policy_version": "demo-board-policy-v1",
  "coverage_scope": "board_snapshot_top_n",
  "items": [
    {
      "news_id": "demo-news-002",
      "title": "合成科技新闻示例",
      "source_rank": 1,
      "source_position": 1,
      "source_score": "92.60",
      "computed_rank": null,
      "computed_score": null,
      "metrics": null
    }
  ],
  "limitations": ["合成数据示例；指标未提供，不补造数字"]
}
```

计算榜返回相同封套：source_kind为newsagent_computed，source_rank/source_score为空，
改填computed_rank/computed_score、实际指标、窗口、数据/规则/Bundle版本和候选范围。
企业原榜规则与项目规则分别记录；分数与比率使用十进制字符串避免精度变化。
企业未提供的数据版本、窗口或水位应明确缺失，不用响应时间代替。

身份由可信网关绑定，不接受请求正文指定租户；字段、榜单和窗口受批准范围约束。
同request_id改参数返回冲突；失败、水位不完整、超时或权限不足返回明确错误，
不以旧榜单冒充本次结果。模型只能解释，不能生成权威排名、分数或指标。

## 4. 如何接入项目

```text
页面 / 聊天 → 身份与请求校验 → RankingQueryService（待新增）
  ├─ enterprise_board → 企业Adapter → 固定快照，保留原始顺序
  └─ newsagent_computed → NewsMetricSource → 批准SQL/RPC → Python排序
→ 新闻正文与证据 → 可选模型分析/校验 → PostgreSQL/报告存储 → API/SSE
```

已有本地入口共用 `execute_local_hot_news_query → SqlAssistantService → 参数化SQL
→ Temporal热点Workflow → NewsMetricSnapshot → HotNewsRanker → NativeHotNewsAnalysisRunner`。
可复用[NewsMetricSource](../app/analytics/metric_source.py)和
[SqlWarehouseClient.execute](../app/clients/enterprise/sql_warehouse.py)；
原榜需要新Adapter及保序分析路径，不能直接塞入自动重排链。

你先补齐以下信息：

| 需要的信息 | 填写内容 |
| --- | --- |
| 榜单 | 实际board_id、名称、新闻榜还是事件榜 |
| 数据入口 | 表/视图结构或API/IDL、两条脱敏样例 |
| 排名与快照 | rank/score/并列规则、快照版本、刷新频率 |
| 指标口径 | 时间窗口、水位、UV、有效消费、互动、时长精度 |
| 调用条件 | SDK、鉴权获取方式、只读权限、超时与配额；不填写密钥值 |

后续实现：双来源Schema → 共享Service与原榜模拟 → 生产聚合装配/真实Adapter → 联调验收。
配置沿用 `SQL_ASSISTANT_SCENARIOS_PATH`、`MODEL_RUNTIME_CONFIG_PATH`、
`HOT_NEWS_DEPENDENCIES_FACTORY`、`TEXT2SQL_MAX_ROWS/TEXT2SQL_TIMEOUT_MS`；
企业榜单端点、身份、白名单和版本配置待补。外部依赖为数仓/榜单服务、PostgreSQL、
Temporal、模型接口与既有Redis/对象存储。

相关测试入口（在agent目录执行）：

```bash
python -m pytest tests/test_sql_assistant_warehouse.py tests/test_native_hot_news_sql_support.py -q
```

TODO：统一API/结果持久化、原榜Adapter与保序分析、生产指标源装配、企业合同与质量验收。
开发时重点验证来源区分、原榜保序、租户隔离、快照一致、UV口径、候选覆盖和幂等恢复。
