# 本地行为标签与热点相关新闻检索

## 企业落地背景与数据边界

面向腾讯新闻内部内容运营：运营人员发现热点后，查看相关新闻、主题兴趣及受众预览，辅助选题与人工审核。主题兴趣描述读者的新闻消费偏好，不代表身份、职业、收入、政治倾向或健康状况。当前实现不发送新闻，不修改生产推荐策略。

用户确认多个数仓版本可以合并。本地输入包括 v7/v8 冻结 JSONL，以及 `classic-v1`、`enterprise-v1`、`enterprise-v2`、`public-headlines-v3`、`timeline-v4`、`timeline-v5`、`corpus-v6` 七个可复现生成器配置，共九个来源、两个隔离租户。v8 有 661 篇公开新闻；v6 有公开正文，v3 只有公开标题，其他源含合成新闻。栏目、指标和生命周期为模拟数据。数仓没有用户 ID、会话或个人访问明细，因此无法还原真实个人画像。本方案独立合成事件，取样分布参考合格的数仓聚合；生成后的事件聚合不保证与原数仓相等。

同一真实新闻 ID 去重，优先保留 v8 正文，保留所有 `source_refs`；相同 ID 存在不同已验证正文时拒绝合并。合成新闻按场景命名空间隔离。v8 撤回/受限及 tombstone 身份对旧版同一真实新闻统一生效，防止旧源重新引入失效内容。同一新闻、小时但不同 dataset/scenario/指标定义的观察分别保留，绝不相加；旧版缺失详情页等指标为 `null`，不是零。v3 指标日期早于真实标题发布时间，已排除这些发布前观察。

## 执行计划

1. 校验输入 Manifest 和表摘要；按最高窗口修订选择完整、质量通过、水位达标的小时，并应用内容发布及生命周期资格。
2. 建立两级主题目录，使用标题/正文关键词多标签；保存规则、命中词、位置和正文摘要。模拟栏目只能作为明确标记的兜底。
3. 从单篇新闻单小时指标生成可解释的行为标签，保留原指标、公式、分母门槛、阈值及基线版本；不跨小时累加 UV。
4. 生成固定种子的本地合成用户事件，覆盖低活跃、深读、通勤、跨主题、冷启动、兴趣迁移及负反馈场景；按截止时间计算每日兴趣快照。
5. 输出可校验的 JSONL 和 SQLite 索引，提供只读主题/行为检索，将聚合兴趣权重接入热点相关新闻重排，并把数据版本绑定到运行幂等键。
6. 构建实际数据包，执行边界和集成测试，保存结果及单页图解。

## 数据分层与规则

| 层 | 主键/范围 | 依据 | 用途 |
|---|---|---|---|
| 内容主题 | 租户、新闻 ID、正文哈希 | 版本化关键词及原文命中；多标签及父主题继承 | 主题过滤、相关新闻共享主题 |
| 新闻行为 | 租户、新闻 ID、合格小时窗口 | 曝光、点击、详情页、有效消费、互动、负反馈及同口径基线 | 热点解释、规则检索 |
| 合成个人事件 | synthetic_user_id、发生时间 | 固定种子、场景预设、数仓取样权重 | 仅用于本地画像构建和场景联调 |
| 合成兴趣画像 | 租户、synthetic_user_id、as_of | 仅从截止前合成事件计算、时间衰减 | 只在本地聚合后参与检索或人数预览 |

目录为草案扩展，不是腾讯官方分类规范。原确认主题为科技、财经、AI、军事、教育及科技→自然；其他细分均标为 `draft_extension`。没有文本证据的主题可以保持零覆盖，不能为展示凑齐军事、健康等分类。

新闻行为标签包含点击高低、深浅阅读、有效阅读率、互动/分享/收藏、负反馈、直达占比、重复访问、增长/降温、小样本、零曝光、缺失基线等。规则阈值是本地工程示例，不是线上策略参数。

画像不生成敏感人口属性。通知许可和频控是合成的资格示例，不代表真实订阅或送达。所有个人记录明确使用 `synthetic_user_id`，API 和 LLM 只接收主题、聚合人数和版本证据。

## 构建与查询入口

在仓库根目录，使用具备项目依赖的 Python：

```sh
cd agent
python -m app.behavior_tags.build \
  --source-dir ../output/warehouse/real-news-v8-20261014 \
  --source-dir ../output/warehouse/tencent-news-v7-20261014 \
  --source-dir profile:classic-v1 --source-dir profile:enterprise-v1 \
  --source-dir profile:enterprise-v2 --source-dir profile:public-headlines-v3 \
  --source-dir profile:timeline-v4 --source-dir profile:timeline-v5 \
  --source-dir profile:corpus-v6 \
  --output-dir ../output/behavior-tags/local-merged-v1-20261016 \
  --users-per-tenant 600 --seed 20261016 --days 31
python -m app.behavior_tags.query \
  --package ../output/behavior-tags/local-merged-v1-20261016 \
  --tenant-id 11111111-1111-4111-8111-111111111111 \
  --window-start 2026-10-13T00:00:00+08:00 \
  --window-end 2026-10-14T20:00:00+08:00 \
  --include tech --limit 10
```

构建器拒绝覆盖已有目录。`--help` 为实际参数的权威入口。输入文件保持冻结，派生包单独保存；Manifest 绑定输入身份、规则、生成器模块与实际 catalog 文件摘要、seed、时间范围、行数和产物 SHA-256。加载前校验 Manifest 自摘要与产物摘要，SQLite 使用只读连接与参数化查询。生成器采用 120 篇/租户的有界配置，classic/enterprise-v1 为 12 篇/租户，不代表历史最大规模数据全量装载。

`--dataset-version`、`--scenario-id` 可在 CLI 中限定来源；不指定时分别返回各来源的新闻窗口记录。时间范围是各源覆盖的外包络，具体已验证小时记录见 Manifest 的 `source_coverage_by_scenario`，不能据此声称所有源连续覆盖全部日期。只读标签查询可以覆盖早期数仓窗口；兴趣重排必须具有不晚于请求窗口结束的画像快照。

API 入口为 `GET /api/v1/behavior-tags/topics` 和 `POST /api/v1/behavior-tags/search`，要求现有网关 `hot-news:read` 权限，租户从可信 principal 获取，请求体不能指定租户或用户。查询字段包括 `window_start/window_end/include_topic_ids/exclude_topic_ids/behavior_label_ids/match/dataset_version/scenario_id/limit`，仅返回新闻主题、窗口指标、标签依据和来源，无个人明细。

## 完整调用链与配置

离线入口 `app.behavior_tags.build` → `merge.load_merged_source` 分源适配，v8 调用 `source.load_warehouse_source` 校验与门禁 → 内容去重、最新生命周期拦截、场景保留 → `tagging.tag_article/behavior_labels` 生成可解释标签 → 合成事件和按日画像 → JSONL/`label_index.sqlite`/Manifest。外部依赖仅为本地冻结数仓、已有版本化生成器、Python、PyYAML/Pydantic/SQLite；不调用模型、网络或企业个人行为服务。

每个模拟日只使用当天开始前已经闭合的源指标分布；没有历史时记录 `unconditioned_synthetic_prior`。每条取样引用早于事件时点。曝光记录渠道、设备和发生小时，画像由事件计算渠道/设备计数、活跃时段、点击/阅读/互动/负反馈计数，兴趣分按三天半衰期衰减后归一化。

事件集是用于构造交互场景的有界样本，不是平台全量曝光流。点击概率、渠道和设备比例为明确的合成场景参数，未按企业分布校准；不得用它们估算平台真实 CTR、通知送达率或商业效果。数仓分布目前用于历史内容取样权重，原聚合指标始终独立保留。

在线入口为现有热点运行 → `build_behavior_runtime` 读取包 → `context_for_window` 选择不晚于分析窗口结束的画像快照并聚合预置 cohort → 原向量召回、内容生命周期校验、原相关性重排 → `BehaviorRetrievalContext.rerank` 仅对已合格候选按共享主题最多加 0.08 → 分析输入/运行报告。已有主体冲突降权的候选不获得兴趣加分。榜单 `hot_score` 仍由现有 Python/SQL 计算。

运行参数绑定 `retrieval_context_sha256`，包含包摘要、租户、cohort、画像时点、重排版本及 bonus 配置，防止不同画像复用旧分析结果。每条分析保存 `retrieval_trace`，可查看候选前后顺序、基础分、兴趣分和版本；不保存个人标识。历史窗口超出包范围、缺失快照、快照滞后超过 48 小时均明确失败，不静默使用未来数据。

本地 API 与热点 Worker 必须配置同一个只读包：

```dotenv
BEHAVIOR_TAGS_ENABLED=true
BEHAVIOR_TAGS_PACKAGE_PATH=/absolute/path/to/local-merged-v1-20261016
BEHAVIOR_TAGS_COHORT_ID=all
BEHAVIOR_TAGS_MAX_BONUS=0.08
# 可选：让已有受众预览读取此包中的合成画像
AUDIENCE_ENABLED=true
AUDIENCE_BACKEND=behavior_package
```

禁用时保留原有检索和幂等行为。合成包仅允许 `local/development/test/e2e`，生产启动拒绝。`AUDIENCE_BACKEND=behavior_package` 自动使用包内目录，兴趣分 ≥0.15 视为命中，精确集合计算 any/all/exclude 及许可/频控人数；预览要求窗口结束与完整画像快照时点一致。原固定 hash 受众样例仍可单独使用 `synthetic` 后端。可在现有本地演示 Compose 命令最后追加 `-f deploy/docker-compose.behavior-tags.local.yml`；需先构建包并重建包含本次代码的镜像，本次未操作运行中的实例。

| cohort_id | 设计场景 | 可观察差异 |
|---|---|---|
| focused | 单一主题偏好 | 高权重主题集中 |
| cross_topic | 跨主题浏览 | 多个主题有持续点击/消费 |
| deep_reader | 长时深读 | 高阅读时长及有效阅读比例 |
| skim_reader | 快览阅读 | 短停留，低有效阅读 |
| explorer | 探索阅读 | 弱主题偏好，分布较分散 |
| negative_feedback | 明确不感兴趣 | 负反馈降低相应主题权重 |
| interest_shift | 兴趣迁移 | 前后半程选择不同偏好主题 |
| cold_start | 冷启动/无行为 | 仅初始化、空兴趣，不捏造偏好 |
| notification_disabled | 通知未许可 | 阅读兴趣与通知资格分别计算 |
| frequency_limited | 达到频控示例 | 主题可命中，资格人数排除 |

cohort 是合成生成配置，profile 的实际深读、活跃、兴趣迁移等标签由事件计算，不保证每个样本都符合预设标签。若选定主题在真实内容中缺少覆盖，该主题偏好不会凭空进入最终画像。

## 验证与未完成事项

专项测试入口：`pytest tests/test_behavior_tag*.py tests/test_behavior_hot_news_integration.py tests/test_audience*.py`。覆盖输入摘要、修订门禁、跨租户、零分母/缺基线、关键词边界、多主题/继承、复现性、未来隔离、旧幂等兼容、真实重排调用及热度不变。属于功能与边界验证，不代表企业模型质量验收或推荐效果提升。

企业落地仍需：提供正式主题目录与规则审批、企业授权的去标识聚合画像 Port、在线数据 SLA/过期策略，以及离线标注集和线上受控实验。当前没有接入真实内部用户行为、真实企业画像或推送系统，也没有部署/重启已有服务。新闻生命周期采用构建截止时资格，属于保守排除，不应宣称完整恢复每个历史时点的内容状态。

Schedule 动态窗口仍缺少“执行时冻结画像上下文”的 Activity，因此行为开关开启时调度器拒绝 ensure/unpause，未绑定摘要的旧任务也会明确拒绝。当前支持本地页面的 SQL 热点入口与手工启动脚本；Workflow 内不读取 SQLite 或文件。前端全量测试另有既存公开端口配置断言不匹配及沙箱监听限制，相关热点/受众组件测试单独验收。

## 本次实际产物与验证（2026-10-16）

正式包：`output/behavior-tags/local-merged-v1-20261016`，约 6.6 GiB；大 JSONL 与 SQLite 已按可重建产物加入 Git ignore，Manifest 和紧凑报告可单独查看。目录为只读运行输入，请新建输出目录重新构建，不要就地编辑。

| 产物 | 实际数量 |
|---|---:|
| 内容主题记录（按租户） | 2,474 |
| 新闻小时行为记录（分来源/场景/口径） | 686,493 |
| 合成用户 | 1,200（600/租户） |
| 合成事件 | 308,091 |
| 每日画像快照 | 39,600 |
| 新闻行为标签种类 | 27 |

12 根主题与 72 个细分共 84 项，实际内容覆盖 83 项，`military.history` 无依据而保持空白。事件含 138,244 次曝光、76,267 次点击、76,267 次详情访问、12,531 次互动、3,582 次负反馈以及 1,200 次画像初始化；这些仅为合成事件数，不是数仓原人数或平台行为总量。

验证证据为 `output/behavior-tags/verification-20261016.json`，包含完整目录覆盖、行为标签公式样例、去个人标识的画像样例和多主题检索结果。`agent/tools/inspect_behavior_package.py` 是复查入口：加载并校验包 → SQLite 完整性检查 → 标签/来源分布 → 事件时序与历史取样引用 → 画像截止时点 → 参数化检索 → JSON 报告。该检查不调用模型或网络，原 UV 不参与加总。

主验证集合 393 项全部通过，见 `output/behavior-tags/tests-20261016.xml`；另补充本地启动/SQL/运行身份/Schedule 相关 34 项通过、1 项跳过。最新 runtime/API 26 项再次通过。前端 typecheck、ESLint、生产构建和相关组件 21 项通过；全量前端检查尚有两条既有端口断言（28000 与公开配置 28070）及一条沙箱监听 EPERM 未通过，不改这些独立配置。

单页 PDF 由 `output/diagrams/render_behavior_tags_20261016.py` 读取 Manifest 与验证报告生成；已确认一页并渲染检查中文与排版。当前企业正式目录、授权聚合画像接口、效果评测、Schedule 动态冻结仍是 TODO；既有实例没有重启。
