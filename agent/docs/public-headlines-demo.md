# 近期公开新闻标题与模拟运营数据

2026-10-07：按用户要求，将公开原标题用于一版新的本地演示数据，前端参考值限制显示精度。v1/v2已保存运行继续保持原版本；本版使用独立数据库、profile和Schema。

## 数据在哪里

- 公开标题目录：[catalog.json](../data/public-headlines/2026-10-07/catalog.json)。包含1,200个不同标题，发布日期均为北京时间2026-10-06；原标题、公开来源、原始文章ID和原文链接均保留。
- 采集器：[collect_public_headlines.py](../tools/collect_public_headlines.py)。通过公开腾讯新闻sitemap发现候选，访问有界HTTPS页面，仅保留标题元数据，不保存新闻正文或企业用户数据。原文发布时间来自网页meta，不用URL日期代替。
- 数据生成与严格目录校验：[public_headlines.py](../app/sql_assistant/public_headlines.py)。运行时不联网，目录字节摘要固定；缺失、漂移、重复标题/ID或数量不足时拒绝。
- 冻结合同：[news-warehouse-v3](sql-warehouse-schema-v3.md)，profile `public-headlines-v3`。独立本地数据库连接为`127.0.0.1:25472`、库`newsagent_native`，容器`news-agent-public-headlines-demo-postgres-1`。

两个租户复用同一1,200篇公开新闻目录：`dw.dim_news`有2,400行，`dw.news_metric_hourly`和`dw.news_metric_baseline_hourly`各57,600行。不能把两租户数据量描述为2,400篇不同的公开原文。

公开标题日期和模拟行为日期是独立时间轴：标题来自2026-10-06，行为仍使用固定2026-10-03的24小时情形。发布时间保留真实值；模拟数据不能作为新闻发布前发生真实行为的证据。正文Port仅提供标题引用、原文链接和模拟说明，不伪造原文内容。用户看到的主标签是标题，ID只作关联键或次级信息。

## 页面数值精度

参考值/均值最多2位小数，百分比2位，热度分最多4位，缺失值显示`—`。这只改变展示；数据库、Python/SQL运算、完整报告与审计值保持原精度。前端对权威十进制字符串使用精确文本舍入，超大整数不转成不安全的JavaScript Number。

## 启动与入口

先按[独立分析服务演示](data-analysis-demo.md)准备分析服务。使用下面三个Compose文件创建本版独立实例；它使用新的镜像、数据卷和端口。

```bash
cd agent
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.public-headlines-demo.yml up -d api hot-news-worker
```

公开token仅用于本地合成分析演示；生产凭证由秘密管理系统提供。本版API为`http://127.0.0.1:28040`，数据库25472，Temporal27273，MinIO29400/29401。Compose不读取项目`.env`。

前端启动：

```bash
cd agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28040 \
VITE_DEV_PORT=5178 VITE_LOCAL_SIMULATION=1 npm run dev
```

打开`http://127.0.0.1:5178/chat`，选择运营情形并准备运行；也可进入`/hot-news`查看同一运行的真实标题、模拟指标和参考值。新的标题版本不会更新旧运行中的标题快照。

镜像构建采用`Dockerfile.e2e`，或依[可信缓存构建说明](data-analysis-demo.md)使用`Dockerfile.e2e.cached`离线构建；两者均复制固定目录、冻结v3契约与场景，不在构建/运行时重新抓取新闻。

## 完整调用链与配置

公开sitemap → `collect_public_headlines.parse_headline`校验主机、大小、标题、来源和真实日期 → 冻结catalog及固定SHA → `public_headlines.public_catalog`/`iter_public_rows`转换为原标题新闻维度和确定性模拟指标 → `warehouse.initialize_demo_warehouse`只在新e2e库分批入库并完整核对行hash → `SqlAssistantService`冻结profile/规模/目录身份 → 模型Port返回受限意图 → Python `compile_query`/`SqlAssistantGuard` → PostgreSQL只读参数化Top100 → Temporal热点Workflow → 同小时UV、时长、基线核对 → Python排行 → 公开标题引用的内容/知识Port → 结构化分析与校验 → 不可变运行/会话来源 → 独立分析服务的确定性计算 → 前端精确格式化卡片。

配置为`SQL_ASSISTANT_DATASET_PROFILE=public-headlines-v3`、`SQL_ASSISTANT_NEWS_PER_TENANT=1200`、`SQL_ASSISTANT_SCENARIOS_PATH=deploy/text2sql-scenes.public-headlines-v3.yml`。仅120/1200档，不允许超过目录数量；API与热点worker必须一致。场景仍Top100、SQL10秒、模型最多2次各15秒、预览900秒。依赖为Python标准库采集器、PostgreSQL/SQLAlchemy/sqlglot/Pydantic/PyYAML、Temporal、Redis、MinIO、同Port的本地模型/Embedding及独立分析服务。

## 验证入口与限制

进入`agent/`后运行`python -m pytest tests/test_collect_public_headlines.py tests/test_public_headlines.py`；相关旧版回归包括`test_scaled_profiles.py`、`test_scaled_sql_identity.py`、`test_native_hot_news_sql_scaled.py`和SQL服务/绑定测试。前端使用`npm test`、`npm run typecheck`、`npm run lint`、`npm run build`（具体脚本以package.json为准）。实际数据可用[只读核对SQL](sql/news-warehouse-readonly-checks.sql)在新库检查。

本地采集取得1,200个唯一标题后停止，候选页面范围与选样偏向公开sitemap最新部分；四栏映射是演示分类，不代表全站或企业栏目分布。公开自媒体标题是外部不可信文本，标题来源可追溯不等于内容事实核验。

TODO：下一期公开目录需要新版本；原文事实与正文质量另行核验；企业真实聚合Adapter、水位/迟到/修正、真实基线、跨窗口UV、生产权限和容量验收仍未接入。此变化不修改生产Prompt/模型/规则，也不产生企业模型质量指标。
