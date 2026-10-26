# NewsAgent

NewsAgent 是一套独立的 Python 新闻运营 Agent 系统：采集新闻内容，读取行为聚合与内容数据，
确定性计算热点，组合可信证据，辅助运营分析与研究写作，并通过反馈、评测与人工审批改进。

当前优先完成本地架构与验收：使用公开新闻、合成行为聚合和可替换的模型/数据接口。
企业真实数据、模型和投递平台的接入是后续工作；本地运行成功不代表企业生产验收通过。

企业接入目标是腾讯新闻运营：企业侧根据每个用户访问的新闻主题与细分主题维护兴趣标签，
由既有推荐和推送平台服务用户。NewsAgent 连接热点研判与运营决策，在已完成新闻分析中
选择主题标签，通过聚合受众接口查询人数和通知资格，生成待人工决策的受众预览。企业用户标签
算法、接收者名单和真实投递保留在企业系统；本地使用相同接口的合成标签人群演示。
企业字段和聚合口径见[企业数仓合同](agent/docs/enterprise-tdsql-warehouse-contract.md)。
本地联调已支持多版本数仓统一装载、可解释新闻行为标签、合成兴趣画像与热点相关新闻重排，
当前正文、标签与读者聚合的关联见[数据库查询手册](agent/docs/news-storage-database-reference.md)。
项目定位、架构约束与后续方向见[长期项目背景](DemoMD/PROJECT_CONTEXT.md)。受众预估是确定性服务，
当前仅生成待人工评估的预览，不表示新增自主圈人 Agent 或已接通真实投递。

项目不依赖第三方 Agent/知识库框架作为运行时。企业模型适配采用 OpenAI-compatible
Chat Completions 与 Embeddings 接口；默认本地演示的生成模型使用确定性测试 Port，BGE
快照检索另用本地真实 Embedding。`Hunyuan7B/` 保存独立模型推理和评测实验，目录存在不表示
默认业务栈已切换到该模型。Agent 编排、Prompt 版本、结构化校验、知识分块、索引、QA 与发布门禁均在本仓库实现。

## 仓库组成

- `agent/`：FastAPI、Temporal、PostgreSQL、Redis、MinIO、Python Agent 与 Vue 运营前端。
  业务代码按 `app/modules/`（hot_news、writing、data_loop、memory、conversation、knowledge、
  audience、behavior_tags、reader_profiles、data_analysis、news_storage）组织，启动装配在 `app/bootstrap/`，
  本地模拟装配在 `simulation/`；旧导入路径保留薄兼容入口。
- `news/`：腾讯新闻发现、抓取、清洗、缓存、去重、标题 NLP、重试和
  NewsAgent 知识 API 客户端。
- `deploy/`：Compose、K8s、监控与模拟 CMS 等部署资产；后端本地测试和独立分析服务的
  Dockerfile 在 `agent/`。`agent/deploy` 是旧路径兼容链接，本文命令使用仓库根的真实目录。
- `var/`：本地运行数据根：行为标签/读者画像运行包、数仓数据包、只读知识快照与增量缓存；
  批量文件通常不纳入 Git，部分 manifest/合同文件纳入 Git；这与是否导入 PostgreSQL 是两回事。
  通用数仓工具使用 `WAREHOUSE_DATASET_DIR`，v8 Agent profile 使用 `REAL_NEWS_WAREHOUSE_DATASET`。
- `output/`：报告、图解、评测结果与验收证据。
- `Hunyuan7B/`：Hunyuan 权重、独立环境、推理脚本、微调数据与评测实验的统一入口；
  共享业务评测器以链接复用，说明见 [小模型工作区](Hunyuan7B/README.md)。
- `DemoMD/`：业务场景、长期架构上下文、演示脚本与设计参考。
- `项目架构.md`：完整目录树、模块内部结构与数据/版本边界。

## 常用资料

- [新闻与用户标签字段手册](output/pdf/NewsAgent_当前新闻与用户标签_字段手册.pdf)：生成时的字段快照；后续变化以数据库查询手册为准。
- [统一数据库查询手册](agent/docs/news-storage-database-reference.md)：正文、新闻标签、读者聚合的物理表与关联。
- [文档入口](agent/docs/README.md)：本地运行、企业模拟、配置、恢复与监控的 7 份主要说明。
- [本地启动](agent/docs/local-bge-simulation.md)：保留运行和测试所需的本地模型、数仓替身。
- [小模型工作区](Hunyuan7B/README.md)：训练与评测资产入口。

日常阅读以以上入口为准；运行时冻结 Schema、数据包、数据库迁移与验收摘要继续保留。

## 核心链路

### 热点分析

```text
企业行为聚合 / 本地模拟数据
  → Python 指标、基线、热度和排行
  → news_id 精确正文 + 关联报道检索
  → 证据白名单
  → Python HotNews Agent
  → InferencePort（默认本地测试实现；企业实现需显式配置）
  → Pydantic + 业务 Validator
  → PostgreSQL/MinIO
  → API、SSE 与人工决策
```

### 研究写作

研究写作由运营按需启动，不是每次受众预览的必经流程。

```text
Research → 人工确认研究包 → Outline → 人工确认提纲
  → 分章节写作 → 组装 → Reviewer
  → 定点返工（有上限）→ 人工确认终稿 → CMS
```

### 知识闭环

```text
爬虫 / 视频文本化 / QA
  → NewsAgent 知识 API
  → 规范化文档与版本校验
  → 确定性切片
  → EmbeddingPort
  → PostgreSQL 文档/索引台账 + 向量索引投影
  → 分层召回、租户过滤、确定性重排
```

### Data Loop

```text
运行结果与人工反馈
  → 脱敏反馈 Case
  → Golden / Fresh bad case / High-risk 数据集
  → 当前与候选 Bundle 离线回放
  → 确定性 Evaluation Gate
  → 人工发布审批 → 既有 Workflow 按批准结果激活 / 显式回滚
```

模型不能自行改变 Prompt、热度规则、模型版本或生产配置。

## 本地全链路启动

完整本地模拟（查询和分析均使用本地测试 Port，BGE 使用真实快照）可直接启动：

```bash
python3 agent/tools/start_local_demo.py
```

入口会检查 Docker、Compose、磁盘与已有 BGE 快照，再构建并启动前端、API、热点/写作/
Outbox/Data Loop Worker 及基础依赖；不会下载缺失的快照。无需企业模型密钥或单独运行 Vite，
默认前端为 `http://127.0.0.1:5178`，API 为 `http://127.0.0.1:28060`。
预检、恢复、停止与业务验证见 [完整本地模拟启动说明](agent/docs/local-bge-simulation.md)。

独立的 `real-news-v8` 演示使用固定历史包：8,120 篇公开新闻，其中 661 篇关联模拟行为指标。
它不是当前统一新闻数据库的总量，也不随 `current-selection.json` 自动切换。
备齐 `var/warehouse/real-news-v8-20261014` 数据包后，可使用：

```bash
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.real-news-v8-agent.yml up -d --build
cd agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:38000 VITE_DEV_PORT=5274 npm run dev
```

访问 `http://127.0.0.1:5274/chat` 和 `/hot-news`。7,459 篇内容-only 文章没有行为指标，
不会进入热度排行；它们只作为真实内容和派生特征存在。详见
[真实新闻数仓 v8 DDL](agent/docs/sql/real-news-warehouse-v8.sql)。

需要沿用企业查询理解模型展示热点对话与 BGE 相关新闻时，使用以下部署入口：

```bash
python3 agent/tools/deploy_bge_demo.py --check
python3 agent/tools/deploy_bge_demo.py
```

该入口部署前端、API、Worker 和基础依赖，沿用已有企业查询理解配置，聊天入口为
`http://127.0.0.1:5178/chat`。准备环境、每个阶段的作用、更新、停止及排查步骤见
[本地 BGE 模拟运行说明](agent/docs/local-bge-simulation.md)。以下是原生基础演示的手工入口。

手工演示需要可用的本地 Docker Engine（可由 Docker Desktop 提供）、Docker Compose 和
Node.js 20.19+。下列后端、前端命令分别从仓库根目录执行。

后端：

```bash
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
```

前端：

```bash
cd agent/frontend
npm ci
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28000 npm run dev
```

前端自动读取 `agent/frontend/dev-server/local.yml` 的端口、后端地址、
本地模拟和开发代理身份；修改后重启即可。`npm ci` 只需首次安装或锁文件变化后执行。
这份 YAML 仅供本地开发，生产构建和预览不加载本地身份。
配置调用链、覆盖规则与测试见 [前端说明](agent/frontend/README.md)。

注意端口对应关系：`local.yml` 默认代理到 `http://127.0.0.1:28070`（真实新闻演示栈）；
上面的 native-e2e 栈暴露 `28000`，因此前端命令已显式覆盖代理地址。

访问 `http://127.0.0.1:5174/hot-news`；持续对话页面为 `http://127.0.0.1:5174/chat`。
本地演示数据位于租户 `11111111-1111-4111-8111-111111111111`。

持续对话将消息与轮次保存在 PostgreSQL，支持同会话追问、刷新恢复、热点报告读取和
新闻知识检索。本地管理员可通过 `query_hot_news` 让聊天问题进入热点 SQL
Workflow，读取模拟数仓并返回同一run的分析；不绕过 SQL 校验或写作人工审批。
聊天默认使用安全 SSE：处理与工具进度实时推送，答复校验、保存后分段返回；不是企业模型
原始 token 透传。配置与测试见[聊天数仓与模型说明](agent/docs/conversation-warehouse-model.md)。
演示顺序：`我关注科技` → `记得上一轮我说了什么吗` → `查看最近热点` → `解释第一条新闻`。
对话取数与模型配置、调用链和测试见 [聊天数仓与模型说明](agent/docs/conversation-warehouse-model.md)；后续验收事项见 [TODO](TODO.md)。
真实兼容模型的 URL/模型/env-key 模板、同一链路接入与验收见
[聊天数仓与模型配置](agent/docs/conversation-warehouse-model.md)。默认仍是规则模型模拟，
模型模板仅显式启用后生效；企业数据只连接批准的内部端点。

默认本地模拟与企业适配共用 Python Port 和业务校验；生成模型与行为数据使用隔离测试实现。
显式启用兼容模型模式后会调用配置的端点；企业数据只允许发送到已批准的内部地址。

## 测试

后端完整回归（容器内）：

```bash
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml --profile test \
  run --rm test-runner python -m pytest -o addopts='' -q
```

后端源码模式（从仓库根进入 `agent/`，先用 Python 3.11+ 安装项目及 dev 依赖）：

```bash
cd agent
uv sync --extra dev
.venv/bin/python -m pytest
```

爬虫（从仓库根开始，使用已安装爬虫测试依赖的 Python 环境）：

```bash
cd news
pytest -q
```

前端（从仓库根开始）：

```bash
cd agent/frontend
npm test
npm run lint
npm run build
```

## 企业配置

参考 `deploy/model-runtime.enterprise.example.yml`。模板中的接口 URL、模型路由和 Prompt
需要替换为企业批准的值，并通过 `MODEL_RUNTIME_CONFIG_PATH` 加载实际配置。
YAML 同时声明 Embedding 维度、超时和批量限制；密钥由
`ENTERPRISE_MODEL_API_KEY` 与 `ENTERPRISE_EMBEDDING_API_KEY` 注入。

生产上线前仍需完成企业模型合同测试、企业行为/内容 Adapter、持久化向量索引、Gateway/
IdP、网络与密钥治理、负载及故障恢复验证。当前长期状态以
[`DemoMD/PROJECT_CONTEXT.md`](DemoMD/PROJECT_CONTEXT.md) 为准。
