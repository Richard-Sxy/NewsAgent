# 企业业务情形的本地合成数据模拟

这组数据用于模拟不同运营状态和接口边界。标题、新闻标识、聚合指标和基线由本地代码确定性生成，不包含企业新闻正文、原始用户行为或身份明细。运营场景使用真实 PostgreSQL 查询、热点 Workflow 和会话分析；独立契约演练核对缺失值、口径提示与拒绝行为。两者的验证范围不同。

2026-10-26 已按当前源码和模板核对本文。以下命令是现有模板的使用入口，本轮没有启动服务或重跑场景。
独立分析镜像仍有旧 COPY 路径，见文末待办。
历史测试数量与耗时保留原日期，不能作为当前工作区通过验收的结论。
本文 Bash 命令适用于 Linux/WSL；每个代码块均从仓库根目录开始，Python 使用已安装项目依赖的 3.11+ 环境。

## 数量扩容

`enterprise-v2` 使用独立冻结的 [news-warehouse-v2](sql-warehouse/sql-warehouse-schema-v2.md)，提供全部十种运营情形。`SQL_ASSISTANT_NEWS_PER_TENANT` 接受120、1200、12000，默认1200；`classic-v1`／`enterprise-v1` 继续使用各自的数据身份与冻结 v1 契约。

| 每租户新闻 | 两租户新闻总量 | 小时指标总行数 | 基线总行数 | 三表总行数 |
| --- | --- | --- | --- | --- |
| 120 | 240 | 5,760 | 5,760 | 11,760 |
| **1,200（默认）** | **2,400** | **57,600** | **57,600** | **117,600** |
| 12,000 | 24,000 | 576,000 | 576,000 | 1,176,000 |

默认新闻数为 `enterprise-v1` 的100倍。每篇覆盖2026-10-03的24个小时桶，包含图文／视频、四个栏目和两租户隔离；新闻ID与标题按固定规则生成。v2所有情形采用点击量Top100。仓库规模与单次分析范围分别披露：`read_hot_news`显示前5篇摘要，分析使用保存的完整候选榜单；两个窗口最多200行，独立分析服务限制输入和输出各256KiB。结果不能解释为全部1,200篇新闻的全量分析。

生成器逐行产生指标与基线，每批最多500行入库。独立行级规范化哈希协议绑定表名、顺序、值和行数；每次启动使用服务端游标逐行核对数据库全量指纹。小型manifest按不可变规模缓存，页面刷新不会重新生成全量数据。规模、数据版本／哈希和Schema版本进入查询预览及运行幂等身份；更换规模必须创建新隔离数据库，不覆盖已有数据或修复漂移。正文补齐只读取当前候选，知识索引按候选有界入库；再次准备／重启通过数据库元数据和完整分片核对跳过已索引内容。

独立分析服务模板为 [docker-compose.analysis-service.yml](../../deploy/docker-compose.analysis-service.yml)。
当前 `Dockerfile.analysis-service` 的部分 COPY 路径仍指向模块迁移前的位置，重新构建前需要修复；
以下为业务栈命令，前提是独立分析服务已经可用。命令在仓库根执行：

```bash
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
export SQL_ASSISTANT_NEWS_PER_TENANT=1200
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml build migrate
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml up -d \
  api hot-news-worker writing-worker outbox-worker data-loop-worker
```

业务栈通过 `migrate` 的 build 定义使用现有 `Dockerfile.e2e` 构建，其余应用角色复用同一镜像。
默认项目名为 `news-agent-enterprise-scale-demo`，API28030、PostgreSQL25462、Temporal27263、MinIO29300/29301，镜像标签 `enterprise-scale-demo`，使用项目独立数据卷。这些是模板端口，不表示当前已有实例在运行。
选择另一档规模时，为新项目设置独立名称、空数据卷和空闲端口；仅修改项目名不会改变固定宿主端口。
独立分析服务使用模板创建的 `newsagent-analysis-service_analysis-ingress` 外部网络，业务栈和分析服务须注入相同的专用本地测试 Token。

```bash
cd agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28030 \
VITE_DEV_PORT=5177 VITE_LOCAL_SIMULATION=1 npm run dev
```

打开 [扩量聊天演示](http://127.0.0.1:5177/chat)，页面展示数据总体规模和Top100分析范围，准备所选情形后发送分析问题。

同一扩量模板也装配写作、Outbox和Data Loop Worker；本节实例由上述三层Compose管理。
[本地运行](local-bge-simulation.md) 描述的是另一个BGE快照实例，其启动脚本不管理本节的28030实例。
完整评测仍需要经过人工复核的标签、冻结数据集和已登记候选，启动Worker不代表评测或配置上线完成。

```bash
cd agent
python -m examples.data_analysis_demo --base-url http://127.0.0.1:28030 --list-scenarios
python -m examples.data_analysis_demo --base-url http://127.0.0.1:28030 --scenario ranking-churn
NEWSAGENT_SCALED_DEMO_BASE_URL=http://127.0.0.1:28030 \
python -m pytest tests/test_scaled_demo_http.py -s
SCALE_WAREHOUSE_INTEGRATION_URL=postgresql+psycopg://newsagent_native:newsagent_native@127.0.0.1:25462/newsagent_native \
python -m pytest tests/test_scaled_profiles.py -s
```

数量扩容启动链：Compose → `Settings`选择profile／规模 → `build_local_hot_news_sql_service` →
`scaled_profiles.iter_scaled_rows`生成确定性新闻、小时指标和基线 → `initialize_demo_warehouse`分批写入新库或复核已有库 → API发布`sql-config`规模目录。

请求链：页面／`data_analysis_demo`读取目录并提交问题 → `SqlAssistantService.preview`冻结格式／数据身份、Python编译受限SQL →
保存预览并绑定热点运行 → Temporal热点Workflow/Activity → `SqlAssistantHotNewsMetricSource`执行PostgreSQL Top100查询、补齐单小时指标与基线 →
`SqlHotNewsContentRepository`提供正文，按候选准备知识索引 → Python排行与模型分析／校验 → 保存不可变运行 →
会话授权来源 → `analyze_hot_news_data`有界投影 → 独立分析服务／固定worker精确计算 → 校验结果并保存工具轨迹 → CLI JSON和前端结果卡。
外部依赖为PostgreSQL、Temporal、Redis、MinIO与独立分析服务；模型与Embedding使用本地同Port替身。

配置为 `SQL_ASSISTANT_DATASET_PROFILE=enterprise-v2`、`SQL_ASSISTANT_NEWS_PER_TENANT`、`SQL_ASSISTANT_SCENARIOS_PATH=deploy/text2sql-scenes.enterprise-v2.yml`；API和热点worker必须一致。这里的相对配置路径在容器 `/app` 下解析，对应镜像中的 `/app/deploy/`；源码模板位于仓库根 `deploy/`。测试入口为 `test_scaled_profiles.py`、`test_scaled_sql_identity.py`、`test_scaled_demo_http.py`。较大规模的配置与流式约束不等于生产容量承诺。

历史本地验收记录（2026-10-06，未在本轮重跑）：默认1200档完整PostgreSQL计数／指纹／重复启动／拒绝漂移通过；十场景各完成两份100篇候选的合成热点运行来源与六项独立分析服务操作，共60项分析通过，Linux资源限制启用。12000档在单独 `newsagent_scale_12000` 数据库生成并核验1,176,000行，首次入库＋完整复核131.26秒，重复全量复核16.70秒，测试进程峰值91.36MiB（含pytest、SQLAlchemy和驱动）；这不是服务生产内存承诺。该库ID超过1000的正文关联、知识首次入库及再次运行／新store重启后零新增Embedding也通过。后端套件当时记录1897通过、63项依赖型测试跳过；真实PG与v2 HTTP验收另行显式执行。前端当时记录194通过，lint、typecheck及build通过。

较大档PG验收复用上述测试入口，显式设置 `SCALE_WAREHOUSE_NEWS_PER_TENANT=12000`，并让 `SCALE_WAREHOUSE_INTEGRATION_URL` 指向新的专用测试库；知识验收入口为 `tests/test_native_hot_news_sql_scaled.py`。不要在演示业务库里改Embedding版本或运行破坏性初始化。

## 运营情形

`enterprise-v1` 提供较小的兼容场景，需显式配置 `SQL_ASSISTANT_DATASET_PROFILE=enterprise-v1`；设置类的默认profile为 `classic-v1`，各Compose overlay可覆盖它。v1合成配置保留冻结 `news-warehouse-v1` 的日期、字段、约束与规模：两个租户、每租户12篇新闻×24小时，共24篇维度记录、576条小时指标和576条基线。配置版本与全量数据 SHA-256 独立记录，不修改冻结的 Schema 文档。

| 情形 ID | 参考／当前小时（北京时间2026年10月3日） | 核对内容 |
| --- | --- | --- |
| steady | 00／01 | 稳定运营，不能把少量波动断言为突发 |
| breaking | 02／03 | 少数热点突增，与其余新闻共同展示 |
| fatigue | 04／05 | 热度回落与点击下降 |
| funnel | 06／07 | 高曝光低CTR、小曝光高CTR及消费差异；整体CTR按次数加权 |
| content-mix | 08／09 | 图文／视频的流量结构和消费差异 |
| low-volume | 10／11 | 小样本、零曝光、零点击，有限样本不作稳定结论 |
| zero-baseline | 12／13 | **已知基线为0**，绝对变化可计算，相对变化未知 |
| ranking-churn | 14／15 | 同口径Top5新增和退出；趋势只计算共同新闻 |
| recovery-gap | 16／18 | 窗口之间有间隔，披露 gap，不能说持续增长 |
| precision | 20／21 | 合法大计数，Python精确计算，浏览器不对不安全整数重算 |

目录由后端 `dataset.enterprise_scenarios` 发布，包含固定问题、窗口、候选上限和观察点。观察点是情形说明，CLI 的数字证据取自实际计算报告；不将观察点直接冒充已通过验收。

企业配置只能在新隔离数据库初始化。已有经典样本不能被覆盖或按新配置解释；企业实例每次启动会核对 ledger 与完整样本指纹，配置、缺失行或数值漂移均拒绝启动。已有运行的窗口／Bundle幂等身份和保存快照继续保持不可变。

## 启动与页面

先确认 [独立分析服务](../../deploy/docker-compose.analysis-service.yml) 已可用（构建限制见上文）。在仓库根使用三层 Compose overlay 创建新的业务项目和数据卷：

```bash
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-analysis-demo.yml build migrate
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-analysis-demo.yml up -d api hot-news-worker
```

模板项目名为 `news-agent-enterprise-analysis-demo`，镜像为 `news-agent/writing-agent-service:enterprise-analysis-demo`；API28020、PostgreSQL25452、Temporal27253、MinIO29200/29201。端口28000对应基础本地E2E模板，28010对应经典合成分析模板；不能仅凭端口判断是否使用企业模型或服务是否已启动。当前使用 `Dockerfile.e2e` 构建，旧缓存 Dockerfile 已移除。

```bash
cd agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28020 \
VITE_DEV_PORT=5176 VITE_LOCAL_SIMULATION=1 npm run dev
```

打开 [企业情形聊天演示](http://127.0.0.1:5176/chat)。在“数据分析”区域选择情形，点击“准备所选情形”；系统实际创建或复用两次热点运行，新建独立会话并真实读取来源。随后选择六种分析操作并发送。切换情形会准备另一个会话，不修改原来的运行或分析结果。前端在提交前重新核对目录版本、数据哈希、实际 SQL 问题、窗口与候选上限。

## 可重复的命令行演示

```bash
cd agent
python -m examples.data_analysis_demo --base-url http://127.0.0.1:28020 --list-scenarios
python -m examples.data_analysis_demo --base-url http://127.0.0.1:28020 --scenario ranking-churn
python -m examples.data_analysis_demo --base-url http://127.0.0.1:28020 --scenario zero-baseline
```

`--prepare-only` 只准备两个运行和来源会话；普通模式还发送六种分析问题并验证真实结果。报告保留场景／数据身份、运行与会话ID、实际候选数、来源哈希、分析执行信息和计算内容。请求无自动重试，出现409时不改窗口或Bundle绕过冲突。模型必须为本地Port，不会调用企业模型。

## 数据契约演练

数仓v1要求计数关系合法且基线非空。完全缺失基线、重复点击高于曝光及不一致CTR不能直接灌入该数仓，因此使用固定聚合 Port／分析输入演练：

```bash
cd agent
python -m examples.enterprise_analysis_contracts --list
python -m examples.enterprise_analysis_contracts --scenario ctr-mismatch
python -m examples.enterprise_analysis_contracts --all
```

版本 `enterprise-analysis-contracts-v1`，20项：缺失／部分／零基线、全体零曝光、有曝光无点击、CTR舍入和不一致、重复点击、空快照、加权CTR、候选进出、非连续窗口，以及无交集、窗口重叠、不同长度／选取口径／Bundle／Workflow、未知字段与行数超限的拒绝。

调用真实 `AnalysisRunner`：12项实际启动固定隔离worker，8项在输入门禁被预期拒绝。CTR相关三项另经 `run_aggregate_acceptance`：不一致CTR可产生质量提示，同时取数Port验收拒绝；重复点击是口径核对提示，不直接认定采集错误。报告给出 `expected/actual/passed`、输入／输出哈希、实际后端、worker是否启动及OS限制。macOS process没有完整Linux资源限制，报告如实为false；预检拒绝的OS证据为null。该演练不创建假热点运行或会话轨迹。

## 完整调用链与验收边界

v1与上面的v2使用同一执行边界：启动时由 `synthetic_profiles`生成固定样本，
`initialize_demo_warehouse`仅向新库入库并核对全量指纹；页面／CLI随后读取场景目录并提交问题。
`SqlAssistantService.preview`生成并保存受限SQL计划，绑定热点运行后由Temporal Activity执行只读查询。
`native_hot_news_sql_support`将候选转换为单小时指标、基线和内容Port → Python排行、知识检索、模型分析与校验 →
保存不可变运行 → 会话`read_hot_news`授权来源 → `analyze_hot_news_data`白名单投影 →
独立分析服务／worker的Python Decimal计算与完整结果复核 → PostgreSQL工具轨迹、CLI JSON和前端结果卡。

外部依赖为原有 PostgreSQL、Temporal、Redis、MinIO及独立分析服务；模型和Embedding均使用隔离本地Port。配置入口为 `SQL_ASSISTANT_DATASET_PROFILE`、原有`CONVERSATION_*`与`DATA_ANALYSIS_*`、三层Compose、前端显式代理端口。独立契约CLI仅接受固定场景和 `--backend process|docker`，不读取`.env`或任意输入文件。

测试入口：`tests/test_synthetic_profiles.py`、`tests/test_data_analysis_demo.py`、`tests/test_enterprise_analysis_contracts.py`与前端`tests/analysis-demo.test.mjs`。真实企业profile数仓验收通过显式 `SQL_WAREHOUSE_ENTERPRISE_INTEGRATION_URL` 指向独立测试库。完整后端用`python -m pytest`，前端用`npm test`、`npm run lint`、`npm run build`。

10组运营情形的真实 HTTP 验收会逐项完成两次取数和六项分析，并独立核对漏斗、零基线、候选交集、窗口间隔及大数精度：

```bash
cd agent
NEWSAGENT_ENTERPRISE_DEMO_BASE_URL=http://127.0.0.1:28020 \
python -m pytest tests/test_enterprise_demo_http.py -s
```

当前源码待办：`Dockerfile.analysis-service` 仍复制 `app/data_analysis/engine.py` 等旧路径，
实际实现已迁移到 `app/modules/data_analysis/`；修复镜像打包前不能把这组命令当作已验证的新机启动流程。
`tests/test_scaled_profiles.py` 通过 `agent/deploy` 兼容符号链接读取根目录 `deploy/` 的场景模板；
应保留该链接并在Linux/WSL执行相关测试。Windows通过UNC读取该链接失败，不等于目标模板已经删除。
本文没有修改业务代码或测试。

企业真实Adapter、迟到／水位、真实历史基线估计、企业模型质量与生产网络隔离仍待验收。
v1的12篇样本用于兼容场景，v2扩量使用独立实例；本地数量扩展不代表生产并发压力或企业真实身份系统已验收。
