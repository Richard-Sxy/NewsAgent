# 热点新闻访问人群分析实现与运行

本功能服务腾讯新闻内部运营：找到热点新闻后，查看这篇新闻的实际访问群体偏好，再生成需要人工核查的推送主题方向。
本地访问行为全部是独立合成数据；企业接入时，个人明细、身份映射和大规模计算留在企业数仓，本项目只接收有界聚合。
热点分数继续由原 Python/SQL 链路计算。

## 执行结果

| 工作 | 当前结果 |
| --- | --- |
| 补充模拟数仓的访客明细语义 | 复用 308,091 条合成事件与 2,474 条内容绑定，不修改冻结源；共 1,200 个合成身份 |
| 按单篇新闻计算访问人群 | 生成 52,718 条新闻窗口聚合、1,488 个租户小时覆盖记录；新包只有聚合表 |
| 访问前兴趣与基准 | 分别计算最大回看 30 天与 24 小时兴趣，排除目标文章的全部历史贡献，披露历史截断、未知和冷启动 |
| 在线查询与延时约束 | 参数化索引批查，每批最多 20 篇；Top100 最多五批；超时、并发、缓存和响应体均有上限 |
| 热点 Agent | 聚合进入模型输入、推送策略输入、运行快照和榜单响应；新增独立 Prompt 版本 |
| 页面 | 展示该新闻读者数、长短期兴趣、标签中文名、覆盖、基线倍数与限制 |
| 企业接入 | 已实现固定 HTTPS 聚合 Adapter 与严格契约；真实接口、企业性能与模型质量仍需联调验收 |

详细字段、打分、去重、基准和企业 SQL 语义见[模拟数仓口径](news-reader-profile-warehouse.md)。
合成事件与原模拟数仓的小时 UV 是两套独立模拟，不能声称一一还原或强行对齐。

## 完整调用链

离线入口为 `python -m app.modules.reader_profiles.build`。`build._read_source` 检验 manifest 自校验与三份相关输入的 SHA、大小、行数及新闻绑定，
随后在窗口开始前维护长短期兴趣状态，按目标文章排除自身历史、计算访客与同租户基准，再写 `reader_profiles.sqlite`、`policy.json` 和 `manifest.json`。
未将个人事件、用户 ID 或逐人兴趣写入新包。

在线入口有三类：

1. `POST /api/v1/reader-profiles/query`：网关身份提供 tenant，body 只收新闻集合与有时区窗口；返回聚合快照。
2. 本地热点运行 API：`simulation.local_simulation` 冻结包/服务、租户与窗口身份，生成 `HotNewsRunRequest.reader_profile_context_sha256`。
3. 手工运行 CLI：`examples.start_hot_news_workflow` 使用同样的服务工厂生成上下文；Temporal 将冻结请求交给热点 Worker。
   此旧示例默认 classic 的全天窗口，与当前 1 小时包不兼容；不能直接用它代替上面的真实包 smoke。

`main.lifespan` 与 `app.hot_news_worker` 通过 `build_reader_profile_service(Settings)` 装配同一个不可变来源；本地 Worker 示例调用该工厂链路。
`ActiveProductionBundleHotNewsService.run` 校验上下文身份及当前 Bundle 的 Prompt 白名单，
`HotNewsEnrichmentService.enrich` 在原榜单确定后批量调用 `ReaderProfileService.fetch`。
服务按 tenant、排序后的 news_ids、UTC 窗口和来源 release 生成缓存键，合并相同请求，校验完整批次、版本、水位与因果截点。

本地 Port 为 `LocalNewsReaderProfileClient.fetch`：在 `asyncio.to_thread` 内用 SQLite 只读参数化索引读取。
企业 Port 为 `HttpNewsReaderProfileClient.fetch`：向固定 HTTPS 地址发送有界请求，流式限制响应大小，
拒绝重定向、原始个人字段、未知标签、过期/不完整数据及作用域不匹配。没有 HTTP 自动重试或合成数据回退。

`HotNewsAnalysisInputBuilder` 将本篇 `ReaderProfileSummary` 复制到输入；
`NativeHotNewsAnalysisRunner` 使用版本化 Prompt 交给企业模型接口或本地确定性 Port，
由原 `HotNewsAnalysisValidator` 校验报告。
`PushStrategyInputBuilder` 复用同一聚合证据；模型只解释人数和标签，不能改热度或推算点击增量。
`AnalyzedHotNews` 保存 summary 和独立 trace，原运行仓储序列化后，
`HotNewsQueryService` 将快照投影到榜单 API，`NewsReaderProfilePanel.vue` 显示中文标签与覆盖。
中文名称来自运行时冻结目录，旧报告不会随最新目录改名。

summary 与 `reader_profile_trace` 共同绑定 release、query fingerprint、snapshot、revision、租户、窗口、主题目录和因果截点。
不同来源或窗口进入不同运行身份，未启用时保留原运行幂等键。旧快照缺少画像字段仍可读取。

## 本地构建和复跑

在仓库根目录运行：

```bash
PYTHONPATH=agent python -m app.modules.reader_profiles.build \
  --behavior-package var/behavior-tags/local-merged-v1-20261016 \
  --output-dir var/reader-profiles/local-readers-v1-20261017 \
  --window-hours 1 --top-k 20
```

已有同名包不会覆盖。当前包只支持完整整小时窗口，范围为 2026-09-13 20:00 至 2026-10-14 20:00（北京时间）。
24 小时口径需要另建 `--window-hours 24` 包，并按包中覆盖窗口查询；不能将 24 个小时 UV 相加。
读取来源 SHA 为 manifest 原始字节 SHA，区别于 manifest 内自校验 `identity_sha256`。

在仓库根目录复跑真实包查询与本地模型链路：

```bash
PYTHONPATH=agent python -m examples.reader_profile_query_smoke \
  --package var/reader-profiles/local-readers-v1-20261017 \
  --output var/reader-profiles/local-readers-v1-20261017/query-smoke.json

PYTHONPATH=agent python -m examples.reader_profile_hot_news_smoke \
  --output var/reader-profiles/local-readers-v1-20261017-smoke.json
```

前者使用真实包、缓存与进程内鉴权 HTTP 路由，检查索引计划和无个人明细输出；不启动监听端口。
后者校验 v8 正文、指标、窗口文件哈希，选择两条正文 SHA 一致的新闻，经过实际 enrichment、输入构建器、原生本地模型 Port 和报告校验。
当前两条样例的访问人数各为 1，属于低支持样例；不同主题建议仅证明证据到达模型，不能证明推荐质量。

本地 Compose 运行时装配模板为 `deploy/docker-compose.reader-profiles.local.yml`，
与 `deploy/docker-compose.native-e2e.yml` 合用，并使用新的 Compose 项目及独立数据库。
它只读挂载聚合包与 `model-runtime.reader-profiles.local.yml`，API 和 Worker 使用相同包与 v3 Prompt。
固定端口沿用基础栈，启动前需要按已有环境选择可用端口。
模板明确使用 `corpus-v6`、120 篇新闻及对应场景，挂载冻结 data/docs，避开 classic 数据与合并包不同的合成命名空间。
corpus-v6 的 120 篇新闻与本包的 ID/正文 SHA 均已逐篇核对相符。
可选演示窗口为北京时间 **2026-09-22 10:00 至 11:00**，问题为“查询9月22日上午10点到11点点击量最高的前100条新闻”。
该小时 corpus 中有 95 篇新闻存在合成读者，点击 Top100 中有 83 篇非空；Top1 `20260812A0BLWD00` 有 5 位合成读者，
已通过真实 Service → Enrichment → InputBuilder 的定点验证。
v8 尚未注册在线 SQL profile。当前任务检查 Compose 配置和本地链路，没有启动完整页面栈或激活旧数据库中的 Bundle；页面端到端验收仍需实际启动确认。

## 配置和外部依赖

| 配置 | 默认或约束 |
| --- | --- |
| `READER_PROFILE_ENABLED` | false，显式启用 |
| `READER_PROFILE_BACKEND` | `enterprise_http` 或 `local_package` |
| `READER_PROFILE_PACKAGE_PATH` | 本地不可变聚合包；仅 local/development/test/e2e |
| `READER_PROFILE_ENDPOINT_URL`、`READER_PROFILE_API_TOKEN` | 企业固定 HTTPS 与注入的 SecretStr Token |
| `READER_PROFILE_TAXONOMY_PATH` | 企业必须是 approved 目录 |
| `READER_PROFILE_SOURCE_RELEASE_SHA256` | 企业必填的不可变聚合发布 SHA；本地可选严格绑定 |
| `READER_PROFILE_POLICY_VERSION` | 默认 `prewindow-exclude-trigger-interest-v1`；与来源严格一致 |
| `READER_PROFILE_ALLOWED_ANALYSIS_PROMPT_VERSIONS` | 默认只允许 `native-e2e-prompt-v3-reader-profiles`；企业设置已批准版本数组 |
| `READER_PROFILE_TIMEOUT_SECONDS` | 每批与等待默认 3 秒 |
| `READER_PROFILE_RUN_TIMEOUT_SECONDS` | Top100 全部批次的共同默认 5 秒期限 |
| `READER_PROFILE_CACHE_TTL_SECONDS` | 默认 30 秒，0 禁用；企业有效期更早时取更早值 |
| `READER_PROFILE_MAX_CACHE_ENTRIES`、`READER_PROFILE_MAX_INFLIGHT` | 默认 256 项与 16 个不同在途请求，超限直接 busy |
| `READER_PROFILE_MAX_RESPONSE_BYTES` | 默认 524288 字节 |

不复用 `BEHAVIOR_TAGS_ENABLED`。原功能描述全租户兴趣对相关新闻的排序，新功能描述本新闻访问者画像，二者证据分开。
本地新增 Prompt YAML 保留旧 Prompt 内容，既有 Bundle 不自动晋升。
企业环境模板见 `deploy/reader-profiles.enterprise.env.example`，目录、聚合发布与模型 Prompt 均需企业显式审批。

依赖为 Python 3.11、Pydantic、FastAPI、httpx、标准库 SQLite/asyncio；运行持久化继续使用现有 PostgreSQL、Temporal、Redis 和 Artifact 服务。
未引入外部 Agent/知识库运行框架，也没有新增数据库迁移。

## 验证与边界

专项入口为 `tests/test_reader_profile_*.py`，覆盖：访问前截点、排除当前文章、负反馈、UV 去重、
长短期分离、冷启动、unknown 分母、基准与 lift、先统计再 Top K、源哈希、租户/窗口/正文绑定、
未覆盖不能视作零、企业 Schema/身份版本、HTTP 大小和去敏、缓存/并发/取消/超时、鉴权与 Prompt 门禁。
同时回归旧热点编排、原生模型、榜单、Temporal 与行为标签接口。
前端入口为 `tests/news-reader-profile.test.mjs`，检查渲染内容、准确百分比、低样本、空样本与名称转义；执行 lint/typecheck/build。

2026-10-17 本次最终相关后端回归 **550 passed**，前端相关 **24 passed**，lint、typecheck、build 与 Compose 配置检查通过。
后端测试仅有现有 Starlette 与 OpenTelemetry 依赖弃用提示；该结果不替代企业接口、模型质量或整栈页面验收。

30 次、20 篇一批的本机功能探针：未经过服务缓存的聚合 Port P95 为 3.588ms，校验后缓存命中 P95 为 2.627ms。
SQLite 使用主键 SEARCH。该数字排除启动的文件哈希校验，操作系统缓存状态未控制，
也未包含企业网络、模型、并发压测和千万级原始数据，因此不构成生产 SLA。

## 企业上线前剩余工作

- 企业在自己的数据域实现事件去重、身份与隐私策略、增量画像和新闻人群聚合，并对接本项目严格 Port。
- 聚合 release 必须不可变；迟到修正发布新 release/revision，并重新绑定运行身份，不能用旧缓存身份替换数据。
- 实测企业查询 P95/P99、并发、超时与失败恢复；对企业模型开展独立质量验收。
- v8 的完整在线页面演示仍需注册对应 SQL 场景、生命周期与内容 Adapter；不能将合并后的合成命名空间强行别名为原 profile 新闻 ID。
- 真实可触达人数仍由独立受众资格接口校验；不能由重叠标签相加或兴趣人数推出。真实推送继续人工审批。
- 动态 Temporal Schedule 尚需在每个窗口派发前冻结 reader context；当前缺少摘要会被门禁拒绝，不自动取最新包补齐。
  开启画像前应暂停既有 Schedule；当前 CLI 拒绝该组合的 ensure/unpause，避免持续派发失败任务。

以上内容不授予本地系统执行真实推送、发布或修改企业数仓的权限。
