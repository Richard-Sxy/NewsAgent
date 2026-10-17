# 单新闻访客画像：输入、计算口径与本地聚合包

本文对应 [builder](../app/reader_profiles/build.py)、[本地客户端](../app/reader_profiles/local.py)、
[严格契约](../app/reader_profiles/contracts.py)、[服务门禁](../app/reader_profiles/service.py) 与
[装配工厂](../app/reader_profiles/factory.py) 的当前实现。它描述已经落地的本地合成链路，并给出企业端对齐接口的伪 SQL。
企业端表名与作业仅为映射示例，项目没有在本地建设企业行为数仓，也没有导入企业个人行为。

## 1. 数据从哪里来

```text
各版冻结模拟数仓 / 有界 profile 生成器
  → behavior_tags.merge.load_merged_source
  → 真实公开正文/公开标题/合成标题的内容标签
  → 独立生成的 synthetic_events（不是从聚合 UV 还原用户）
  → detail_view 的 tenant + news + 时间窗口内去重访客
  → 每名访客在窗口开始前的 30d / 24h 兴趣，排除目标新闻的全部历史贡献
  → 主题人数、画像覆盖、占比、同租户基准与 lift
  → 仅有聚合的 reader_profiles.sqlite
  → LocalNewsReaderProfileClient → ReaderProfileService → 热点分析/运营查询
```

本次已有输入目录为 `var/behavior-tags/local-merged-v1-20261016`。其 manifest 声明
2,474 条内容标签、308,091 条合成事件、1,200 个合成用户的初始化，模拟范围是
`2026-09-13T12:00:00Z` 至 `2026-10-14T12:00:00Z`。这些数量是该冻结本地包的规模，
不能作为企业规模、真实访问人数或查询延时的证明。

更早的 v7/v8 原始模拟数仓保留 `dim_news`、`news_metric_hourly`、
`news_metric_baseline_hourly`、`window_snapshot`、`content_tombstone` 等表。
它们提供新闻身份、标题/正文、发布时间、当前生命周期、小时聚合、基线样本数、
快照 revision、水位与质量状态。它们没有可供本项目使用的真实逐用户明细。
不同 scenario、dataset 或 metric policy 的观察不能相加为一个真实世界。

`behavior_tags.build._simulate_tenant` 根据已发生窗口的合成点击分布与预设 cohort
生成独立事件。`sampling_reference`、`source_dataset_version`、`scenario_id` 与
`metric_definition_id` 只说明合成采样参考，不能证明某个真实访客存在。

## 2. 实际使用的输入字段

新 builder 只读取并校验以下三个源产物，不读取原包 3.4GB 的 SQLite 或 2.7GB 的
`news_behavior_labels.jsonl`。它不会修改约 7GB 的源目录。

| 文件 | 使用字段/检查 |
|---|---|
| `manifest.json` | `schema_version`、`identity_sha256`、`package_version`、`generator_version`、`tenants`、`simulation_window`、`build_as_of`、`taxonomy_version`、`policy_version`、相关 `artifacts` 的 bytes/hash/rows |
| `policy.json` | 完整 `TopicPolicy`；校验 policy/taxonomy 版本与 manifest 一致，内容标签及事件 topic ID 必须属于启用目录 |
| `content_labels.jsonl` | `tenant_id`、`news_id`、`content_sha256`、`topic_ids`；身份唯一，并用于事件与输出的新闻绑定 |
| `synthetic_events.jsonl` | 下表所列实际事件字段；逐行有界读取，同时校验整个文件 SHA、行数及大小 |

| 事件字段 | 本链路含义 |
|---|---|
| `tenant_id` | 租户边界，不能跨租户合并用户或人数 |
| `synthetic_user_id` | 仅合成身份；源值以 `synthetic-` 开头，输出小包不保留此字段 |
| `event_id` | 去重身份；源文件出现重复即拒绝，不静默重复统计 |
| `occurred_at` | 有时区的事件时间；必须位于声明的模拟范围内 |
| `event_type` | `profile_initialized` / `impression` / `click` / `detail_view` / `interaction` / `negative_feedback` |
| `news_id`、`content_sha256`、`topic_ids` | 非初始化事件必须与同租户内容标签严格绑定；不能把旧正文的兴趣直接绑定到另一版本 |
| `duration_seconds`、`effective_read` | 详情阅读的确定性评分输入，负时长、非整数或非布尔标记拒绝 |
| `source_mode`、`generator_version` | 必须为支持的 synthetic 生成版本；不能把 enterprise 改名成本地合成源 |
| `not_reconstructed_from_aggregate_uv` | 必须为 `true`，显式保留“未从聚合 UV 重建个人”的边界 |

本包不使用合成事件中的 channel/device/notification flags 来推断投递资格；这些字段没有
被带入新包。新闻正文和标签中的文字也不会被当作程序指令执行。

## 3. 谁算这篇新闻的访客

精确集合定义：

```text
R(tenant, news, start, end)
  = distinct synthetic_user_id
    where event_type = detail_view
      and tenant_id = tenant and news_id = news
      and start <= occurred_at < end
```

同一人在一个窗口内重复进入详情页只计一次；只曝光、只 click 而没有 detail_view 不计入
新闻访客。分区身份是 `(tenant_id, synthetic_user_id)`，不能只按 user 字符串跨租户去重。

`reader_count = |R|`。这与原数仓的 click-UV、detail visitors、PV 或有效阅读人数有不同口径。
尤其不能把 24 个小时的去重读者人数相加得到日 UV；同一用户可能在多个小时访问。
24h 包会在完整 24h 的事件集合上重新 distinct。

## 4. 访问前兴趣，避免用本新闻证明本新闻读者感兴趣

画像时间固定为 `profile_cutoff = window_start`，不是窗口结束、接口请求时间或报告生成时间。
兴趣事件范围分别为 `[window_start-720h, window_start)` 和
`[window_start-24h, window_start)`。边界时刻与窗口内事件不会倒灌到画像特征。

对目标新闻 `N`，从每位用户的历史中排除所有 `news_id=N` 的贡献，包括更早窗口的阅读、
点击、互动与负反馈。目标新闻的 `content_topic_ids` 独立保留，不能代替读者兴趣标签。

评分复用 `behavior_tags.build._event_score`：

| 事件 | 原始分数 |
|---|---:|
| impression / profile_initialized | 0 |
| click | 1 |
| detail_view | `min(duration_seconds/60, 3) + (1 if effective_read else 0)` |
| interaction | 2 |
| negative_feedback | -3 |

对每个事件已有的多级 topic 分别计分，乘以三天半衰期衰减
`0.5 ** (age_seconds / 259200)`。汇总后仅保留正分，以该用户最高正分归一化；
归一化权重至少 0.2 的主题构成该用户在该期限内的兴趣集合。该版本为
`prewindow-exclude-trigger-interest-v1`，阈值、半衰期、回看期限和评分源码 SHA 写入新包
`policy.json`；它不复用旧日画像快照的未来信息。

`_RollingState` 使用同一指数因子的等价变换维护可加的分数，归一化时公共因子相消。
每个期限的状态只增量加入窗口前事件并移除过期事件，不对每篇新闻重扫历史。

**历史覆盖限制：** 30d/24h 是最大回看期限。源事件只有声明的 simulation 范围，
起点附近窗口实际缺少此前历史，builder 不会伪造 warmup。客户端在回看越过包覆盖起点时
添加 truncated-history limitation；不能把早窗解释为拥有完整 30 天的企业用户历史。

## 5. 已有画像、未知与冷启动

长期和短期分别计算，不共享一个含糊的 coverage 分母：

| 状态 | 定义 |
|---|---|
| profiled | 初始化在 `window_start` 已可见，且排除目标新闻后有正兴趣 |
| unknown | 不满足上述条件；包括未初始化、没有可用兴趣、负分抵消等 |
| cold start | 已初始化，但当前期限内没有非目标新闻的有效评分事件；是 unknown 的子集 |

每个期限都有 `profiled_reader_count + unknown_reader_count = reader_count`，
`cold_start_reader_count <= unknown_reader_count`。画像覆盖率可由
`profiled_reader_count / reader_count` 得到；空访客没有可解释的覆盖率。

## 6. 标签占比与基准 lift

一个读者可以属于多个主题，父主题和细分主题也可能同时出现，各标签占比不要求相加为 100%。
先对该新闻全部去重读者、全部标签进行局部完整统计，再按人数降序/ID 稳定排序截取 Top K，
默认 20；不会先取少量用户或少量标签再估分母。

| 输出字段 | 定义 |
|---|---|
| `ReaderTopic.reader_count` | 目标新闻读者中属于该兴趣标签的人数 |
| `share_all` | 标签人数 / 全部去重读者，未知画像仍在分母 |
| `share_profiled` | 标签人数 / 该期限内已有画像读者 |
| `baseline_profiled_count` | 同租户、同 `window_start` 的全体合成用户中，排除目标新闻后仍有画像的人数 |
| `baseline_reader_count` | 上述基准画像人群中属于该标签的人数；字段名沿用严格接口契约，并非目标新闻访问人数 |
| `baseline_share` | 基准标签人数 / 基准画像人数 |
| `lift` | `share_profiled / baseline_share`；基准率为 0 或支持不足时为 null |
| `low_sample` | 画像分母、基准分母、标签人数或基准标签人数有任一低于最小支持数时为 true |

最小 lift 支持数默认 5。比例使用 `contracts.ratio_decimal` 保留八位小数、半偶舍入；lift
使用已经舍入的两个比例相除后再次保留八位。接口校验人数、分母、比例和 lift 一致。

基准不是“这篇新闻的栏目人数”，也不是另一个租户，更不是累计源 UV。排除新闻可能改变
某人的最高分、兴趣集合和画像可用性，因此不能简单从一个主题人数总表减去该新闻的点击数。
实现先复用全体用户的窗口状态，再仅重算对目标新闻有历史贡献的用户，以修正基准。

## 7. 企业端映射伪 SQL

下面的 `enterprise_event`、`initialized_profile`、`qualified_window` 是企业自行映射的假名，
不是本仓库已创建的数据库表。企业应在自己的数据域完成身份映射、同意/隐私策略、事件去重、
迟到修正与聚合；只通过 Port 返回严格聚合契约，不把个人明细导入本项目。

```sql
-- 先在企业端按事件身份选择截至 as_of 的最高有效 revision，
-- 然后做质量/水位门禁；不能因最新版待修正而回退旧完整结果。
WITH versioned AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY tenant_id, event_id ORDER BY source_revision DESC
  ) AS rn
  FROM enterprise_event
  WHERE ingestion_time <= :as_of AND tenant_id = :tenant_id
), readers AS (
  SELECT DISTINCT tenant_id, news_id, actor_key
  FROM versioned
  WHERE rn = 1 AND event_type = 'detail_view'
    AND event_time >= :window_start AND event_time < :window_end
    AND news_id IN (:requested_news_ids)
), profile_inputs AS (
  SELECT r.news_id AS trigger_news_id, h.actor_key, h.topic_id,
         SUM(score(h) * decay(:window_start - h.event_time)) AS topic_score
  FROM readers r
  JOIN qualified_history h
    ON h.tenant_id = r.tenant_id AND h.actor_key = r.actor_key
  WHERE h.event_time >= :window_start - :lookback
    AND h.event_time < :window_start
    AND h.news_id <> r.news_id
  GROUP BY r.news_id, h.actor_key, h.topic_id
)
-- 然后结合截至window_start已存在的初始化/可用性，计算正分归一化与标签集合；
-- 独立计算同tenant/window_start且同样排除trigger_news的基准人群。
-- 最后先COUNT DISTINCT actor_key与所有主题，再计算占比/lift，最后Top K。
SELECT /* strict ReaderProfileSummary aggregate fields only */;
```

上述关联用于解释语义，不建议在千万级线上请求时逐新闻展开完整事件 join。
企业端可按租户、用户、主题、小时及新闻贡献预聚合，在快照计算阶段复用窗口状态。
预聚合需要保留能正确合并的状态：sum/count 可加；UV 需要可合并的去重集合或适当草图，
不能相加已经最终化的小时 UV。迟到修正必须更新同窗口 revision，并撤销/替换旧贡献，
不能把新完整快照再次当增量加到旧值上。

若企业返回近似去重，须使用 `enterprise_approx_distinct` 并附 `error_bound`。
本地计数始终为 `exact_synthetic_distinct`。接口不以 HLL 的近似计数代替可投递用户集合，
也不会据聚合画像生成真实收件人。

## 8. 输出与“空”边界

新目录原子发布，只包含：

```text
manifest.json
policy.json
reader_profiles.sqlite
```

SQLite 只有 `metadata`、`contents`、`coverage`、`reader_profiles` 四张表。
其中没有 synthetic_user_id、逐人分数、事件或收件人表。`reader_profiles` 主键为
`(tenant_id, news_id, window_start, window_end)`，仅为实际出现 detail_view 的新闻/窗口写行。
`coverage` 显式记录已完整统计的租户窗口，`contents` 记录合法新闻及内容 SHA/主题绑定。

三种返回语义不能混淆：

1. 已绑定新闻 + 已完整统计窗口 + 有访客行：返回 `status='complete'` 的严格聚合。
2. 已绑定新闻 + 已完整统计窗口 + 无访客行：返回 `status='empty'`、零访客和空 topics。
   此时 baseline_count 用契约值 0，并明确 `empty reader cohort; baseline not computed`；
   不把这个 0 描述成真实基准人群为零。
3. 未绑定新闻、未知租户、未覆盖窗口、未支持的窗口宽度或 source release 不符：
   返回 `invalid_query`，不伪装成零人。

`LocalNewsReaderProfileClient.fetch` 使用参数化 SQL 按租户/新闻/窗口批量查索引，
通过 `asyncio.to_thread` 执行 SQLite 读取；不在线扫描个人历史，也不把新闻字符串拼接成 SQL。
`close()` 后拒绝请求。合成客户端只允许 local/development/test/e2e 环境。

## 9. 时间窗口、版本与发布绑定

CLI 默认 `--window-hours 1 --top-k 20`。`window-hours` 只允许 1 或 24；每个包固定一种宽度。
builder 从 simulation 起点向上取整到完整小时，再按该宽度切窗；24h 不保证按北京时间零点
切分。只发布完全落在 simulation 范围内的窗口。客户端要求窗口与 `coverage` 的精确行匹配，
不会把多个小时聚合临时相加，也不自动转换任意滑动 24h 请求。

发布示例（输出目录必须尚不存在）：

```bash
PYTHONPATH=agent /opt/homebrew/bin/python3.11 -m app.modules.reader_profiles.build \
  --behavior-package var/behavior-tags/local-merged-v1-20261016 \
  --output-dir var/reader-profiles/local-readers-v1-20261017 \
  --window-hours 1 --top-k 20
```

新 manifest 记录源行为包的 `identity_sha256`、源 manifest 原始字节 SHA、三个相关源产物
的哈希，以及 taxonomy/profile/metric policy、窗口范围、水位、生成时间、revision、计数与
新产物的哈希。自身 `identity_sha256` 是排除此字段后的规范 JSON 哈希。

本地客户端的 `source_release_sha256` 是**新 reader manifest 原始字节**的 SHA，
与 manifest 的自哈希、原行为包 identity、客户端 identity 是不同字段。Query 和 Batch 都绑定
同一 release；同一发布不能静默替换 SQLite 或 policy。SQLite metadata 校验 manifest core，
避免把另一个包的数据库与当前 manifest 混用。

## 10. 装配、性能边界与测试

`main.py` lifespan → `factory.build_reader_profile_service` → 本地客户端/企业 HTTP Port →
`ReaderProfileService` → 热点批量 enrichment 与 `POST /api/v1/reader-profiles/query`。企业 backend 要求
approved taxonomy、明确 release 和对应 policy；没有生产环境的合成回退。

主要配置项位于 `app/config.py`：

| 配置 | 作用 |
|---|---|
| `READER_PROFILE_ENABLED` | 默认关闭；开启可选的读者画像分支 |
| `READER_PROFILE_BACKEND` | `local_package` 或 `enterprise_http` |
| `READER_PROFILE_PACKAGE_PATH` | 本地聚合小包路径 |
| `READER_PROFILE_ENDPOINT_URL` / `READER_PROFILE_API_TOKEN` | 企业聚合接口及凭证；文档不提供敏感值 |
| `READER_PROFILE_TAXONOMY_PATH` / `READER_PROFILE_POLICY_VERSION` | 企业主题目录及兴趣口径绑定 |
| `READER_PROFILE_SOURCE_RELEASE_SHA256` | 不可变数据发布绑定 |
| `READER_PROFILE_TIMEOUT_SECONDS` / `READER_PROFILE_RUN_TIMEOUT_SECONDS` | 单批及整次运行的预算 |
| `READER_PROFILE_CACHE_TTL_SECONDS` / `READER_PROFILE_MAX_CACHE_ENTRIES` | 有界聚合缓存 |
| `READER_PROFILE_MAX_INFLIGHT` / `READER_PROFILE_MAX_RESPONSE_BYTES` | 并发与响应体上限 |
| `READER_PROFILE_ALLOWED_ANALYSIS_PROMPT_VERSIONS` | 允许接收读者画像的显式 Prompt 版本 |

缓存由 query fingerprint 绑定租户、新闻集合、窗口和 release；命中后仍校验版本、完整性、
水位与期限。相同请求可共享在途任务；超时或画像不可用不会授权生成替代人数。

本地 builder 的明确上限是 1,000,000 事件、20,000 内容、5,000 合成用户、744 个完整窗口，
新单个产物最多 1GiB。计算复用窗口总状态与逐新闻贡献；复杂度仍取决于活跃用户、主题与
访问新闻分布。该实现和样本不构成百万/千万访客的吞吐、内存或 P95 验收，更不证明腾讯新闻
内部采用了某种存储或算法。企业规模应针对真实契约独立测量。

专项测试入口：

```bash
PYTHONPATH=agent /opt/homebrew/bin/python3.11 -m pytest \
  agent/tests/test_reader_profile_build.py \
  agent/tests/test_reader_profile_local.py -q
```

覆盖访问去重、1h/24h 重算、未来特征拒绝、目标新闻全部贡献排除、负贡献排除后兴趣恢复、
滚动期限边界、冷启动/未知、同租户基准与 lift、局部完整统计后 Top K、源哈希与合成来源、
源目录不变、输出无个人表、空与未覆盖/未绑定区别、SQL 参数化、线程读取和关闭语义。

当前边界：企业端真实身份、事件采集、数仓作业、授权与隐私策略由企业落地；本项目只消费
严格聚合 Port。本文没有为企业选择基础设施，也没有修改旧模拟数仓的冻结产物。
