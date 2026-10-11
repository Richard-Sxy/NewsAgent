# NewsAgent

NewsAgent 是一套独立的 Python 新闻运营 Agent 系统：采集新闻内容，消费企业已有行为与
内容数据，确定性计算热点，组合可信证据，辅助研究写作，并通过反馈、评测与人工审批持续
改进。

项目不依赖第三方 Agent/知识库框架作为运行时。外部模型能力仅为企业内部的
OpenAI-compatible Chat Completions 与 Embeddings 接口；Agent 编排、Prompt 版本、结构化
校验、知识分块、索引、QA 与发布门禁均在本仓库实现。

## 仓库组成

- `agent/`：FastAPI、Temporal、PostgreSQL、Redis、MinIO、Python Agent、
  知识入库/检索、Data Loop 和 Vue 运营前端。
- `news/`：腾讯新闻发现、抓取、清洗、缓存、去重、标题 NLP、重试和
  NewsAgent 知识 API 客户端。
- `DemoMD/`：业务场景、长期架构上下文、演示脚本与设计参考。

## 核心链路

### 热点分析

```text
企业行为聚合 / 本地模拟数据
  → Python 指标、基线、热度和排行
  → news_id 精确正文 + 关联报道检索
  → 证据白名单
  → Python HotNews Agent
  → 企业推理接口（本地为确定性 Port 替身）
  → Pydantic + 业务 Validator
  → PostgreSQL/MinIO
  → API、SSE 与人工决策
```

### 研究写作

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
  → 人工 approve → 人工 activate / rollback
```

模型不能自行改变 Prompt、热度规则、模型版本或生产配置。

## 本地全链路启动

需要 Docker Desktop、Node.js 20.19+。

后端：

```bash
cd agent
docker compose -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
```

前端：

```bash
cd agent/frontend
npm ci
npm run dev
```

前端自动读取 `agent/frontend/dev-server/local.yml` 的端口、后端地址、
本地模拟和开发代理身份；修改后重启即可。`npm ci` 只需首次安装或锁文件变化后执行。
这份 YAML 仅供本地开发，生产构建和预览不加载本地身份。
配置调用链、覆盖规则与测试见 [前端说明](agent/frontend/README.md)。

访问 `http://127.0.0.1:5174/hot-news`；持续对话页面为 `http://127.0.0.1:5174/chat`。

持续对话第一版：消息与轮次保存在 PostgreSQL，支持同会话追问、刷新恢复、热点报告读取和
新闻知识检索。现已增加本地管理员的 `query_hot_news`：聊天问题直接进入原有热点 SQL
Workflow，读取模拟数仓并返回同一run的分析；不绕过 SQL 校验或写作人工审批。
聊天默认使用安全 SSE：处理与工具进度实时推送，答复校验、保存后分段返回；不是企业模型
原始 token 透传。[流式配置、断线恢复和验收](agent/docs/conversation-streaming.md)。
演示顺序：`我关注科技` → `记得上一轮我说了什么吗` → `查看最近热点` → `解释第一条新闻`。
配置、完整调用链、测试和后续阶段见 [持续对话说明](agent/docs/conversation-agent.md)。
合成数据现支持10组企业运营情形、20项数据契约演练及每租户120／1,200／12,000篇三个数量档；默认扩量实例每租户1,200篇、每次Top100。场景选择、独立实例启动、
分析链路与验收边界见 [企业情形合成演示](agent/docs/enterprise-synthetic-data.md)。
当前5177扩量环境的恢复命令、写作人工节点、反馈回流与Worker验收见
[本地项目模拟运行](agent/docs/local-project-simulation.md)。
真实兼容模型的 URL/模型/env-key 模板、同一链路接入与验收见
[聊天数仓与模型配置](agent/docs/conversation-warehouse-model.md)。默认仍是规则模型模拟，
模型模板仅显式启用后生效；企业数据只连接批准的内部端点。

本地环境使用与生产相同的 Python Port 和业务校验，只把企业模型与数据源替换成隔离的
确定性实现；不会连接企业地址或保存企业原始用户行为。

## 测试

后端完整回归：

```bash
cd agent
docker compose -f deploy/docker-compose.native-e2e.yml --profile test \
  run --rm test-runner python -m pytest -o addopts='' -q
```

爬虫：

```bash
cd news
pytest -q
```

前端：

```bash
cd agent/frontend
npm test
npm run lint
npm run build
```

## 企业配置

参考 `agent/deploy/model-runtime.enterprise.example.yml`。YAML 保存经审批的
接口 URL、模型路由、Prompt、Embedding 维度、超时和批量限制；密钥由
`ENTERPRISE_MODEL_API_KEY` 与 `ENTERPRISE_EMBEDDING_API_KEY` 注入。

生产上线前仍需完成企业模型合同测试、企业行为/内容 Adapter、持久化向量索引、Gateway/
IdP、网络与密钥治理、负载及故障恢复验证。当前长期状态以
[`DemoMD/PROJECT_CONTEXT.md`](DemoMD/PROJECT_CONTEXT.md) 为准。
