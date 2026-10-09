# 30天模拟数仓与热点数据集

新增隔离 profile `timeline-v4`，覆盖北京时间2026-09-10 00:00至2026-10-10 00:00（左闭右开）。
默认每租户1,200篇合成新闻，共两个租户；全库维表2,400行、小时指标1,728,000行、基线1,728,000行。
支持120/1,200/12,000篇档位，每篇720小时。48类主题覆盖四栏目与图文/视频，新闻带合成标记。
运营指标包含日增长、周末差异、每日轮换的热点群体；保留十项边界情形，增加跨日、跨周、月内首尾三个对比场景。

## 调用链与配置

- 上游：`deploy/docker-compose.native-e2e.yml`叠加`deploy/docker-compose.timeline-demo.yml`；API和热点Worker分别启动原有E2E服务。
- 配置：`SQL_ASSISTANT_DATASET_PROFILE=timeline-v4`、`SQL_ASSISTANT_NEWS_PER_TENANT=1200`、`SQL_ASSISTANT_SCENARIOS_PATH=deploy/text2sql-scenes.timeline-v4.yml`。规模首次初始化后冻结，改变规模须另建隔离项目。
- 数据生成：`iter_timeline_rows`生成新闻维表、精确整数/Decimal小时指标与基线；`timeline_manifest`绑定窗口、数量、场景、表hash与行数。`dataset_window`按profile返回范围，v1/v2/v3仍采用旧范围。
- 入库：`build_local_hot_news_sql_service` → `initialize_demo_warehouse` → `_initialize_scaled_warehouse` → PostgreSQL事务锁 → 每批500行写入 → `_verify_scaled_rows`完整重算实际hash → 登记不可变数据身份。数据不依赖外部网络。
- 窗口入口：热点工作台`HotNewsQueryTools`从`sql-config`读取范围和记录数，提供最新/1天前/7天前/29天前小时选择并校验边界；`AnalysisDemoPanel`通过`enterpriseDemoCatalog`读取13个固定对比场景。
- 热点执行：`execute_local_hot_news_query` → `SqlAssistantService.preview` → 原生模型意图 → SQL Guard → 绑定v4 Schema、数据hash和窗口到运行 → Temporal Workflow → `SqlHotNewsMetricSource`与`SqlHotNewsDetailRepository` → SQL只读候选及同小时补全 → Python热度排行 → 正文/知识Port → 模型解释与校验 → PostgreSQL/MinIO报告 → API与窗口展示。
- 会话：`LocalConversationHotNewsQuery`默认使用最后一个样本日的配置小时，仍经现有问题解析、身份权限和相同热点命令；不会自动扩展成跨日UV聚合。
- 外部依赖：本地PostgreSQL、Redis、Temporal、MinIO；模型和Embedding均为现有本地隔离Port，未连接企业接口。
- 下游：版本化运行、SQL审计、榜单、分析与证据；本地验收清单位于`data/timeline-v4/manifest.json`与`verification.json`。

## 启动与测试

从`agent/`执行（已构建timeline-demo镜像）：

```sh
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.timeline-demo.yml up -d --no-build
```

API为`http://127.0.0.1:28050`。前端从`agent/frontend/`启动：

```sh
VITE_DEV_PORT=5175 VITE_DEV_PROXY_TARGET=http://127.0.0.1:28050 npm run dev
```

访问`http://127.0.0.1:5175/hot-news`。旧5174窗口仍连接旧实例。

前端启动后，可从`agent/`执行`python tools/verify_timeline_demo.py`，读取配置并运行最新、7天前、首日的三个热点窗口，将清单和验收摘要写入`data/timeline-v4/`。仅通过本地代理使用已有演示身份，不读取.env。

测试入口：`tests/test_timeline_profiles.py`（计数约束、小时覆盖、跨日轮换、清单不可变、场景范围、SQL身份隔离）；
相关兼容回归为SQL数仓/服务/绑定、scaled/public profiles、热点SQL适配器及`test_data_analysis_demo.py`。
前端`node --test tests/analysis-demo.test.mjs`覆盖新清单与越界拒绝；`npm run build`与修改文件ESLint验证类型和构建。

## TODO与限制

合成新闻不是扩充后的真实新闻语料。公开原标题仍由v3的冻结目录提供，需要更多真实标题时应采集并另建目录版本。
企业行为/模型质量、真实历史基线、跨小时去重UV、实时增量均未验收。单次热点取数保持一个完整小时，跨日场景只是两个小时窗口的对比。
实际验收：首日、7天前、最新日各完成5条榜单与5篇分析；最终镜像额外验证10月8日窗口，同样完成5条榜单与5篇分析。旧日期回放的热点事件生命周期更新会降级（既有时序约束），不影响已保存榜单和分析；乱序事件生命周期回放仍为TODO。
30天大数据首次入库及启动全量hash核验需要时间；12,000篇档位尚未做实际数据库容量验收。
