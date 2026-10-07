# writing-agent-service

NewsAgent 的独立 Python 后端，负责热点分析、研究写作、知识入库与检索、Data Loop、
配置晋升和跨运行 Memory。技术栈为 Python 3.11、FastAPI、Temporal、PostgreSQL、Redis
与 S3/MinIO。

模型侧只调用企业内部 OpenAI-compatible Chat Completions 与 Embeddings 原始接口；
Prompt、Agent 编排、结构化解析、业务校验、知识分块、索引和发布门禁均由本项目实现。

## 设计原则

- Temporal 负责有界控制流；Python/SQL 负责指标、基线、热度、排行和门禁。
- LLM 只做开放文本理解与表达，不生成权威数字。
- 新闻正文、检索结果和模型输出均为不可信输入。
- PostgreSQL 保存状态、版本和审计；MinIO 保存不可变 Artifact；向量索引是可重建投影。
- Prompt、模型路由、Schema、规则和生产 Bundle 必须版本化，高风险变更必须人工审批。
- 不保存企业原始用户行为明细，只保存必要的聚合快照和证据引用。

## 目录

| 目录 | 职责 |
| --- | --- |
| `app/model_runtime/` | YAML、Prompt Registry、推理/Embedding Port、HTTP Adapter、本地替身 |
| `app/knowledge/` | 文档规范化、切片、Embedding、索引台账和 QA |
| `app/services/agents/`、`app/agents/` | 写作与热点 Agent Runner |
| `app/analytics/` | 指标、基线、热度、排行与确定性重排 |
| `app/workflows/`、`app/activities/` | Temporal Workflow 与 Activity |
| `app/api/` | 查询、人工 Gate、知识入库和本地模拟 API |
| `app/models/`、`app/repositories/`、`alembic/` | ORM、仓储和数据库迁移 |
| `app/retrieval/` | 分层向量召回、过滤和证据约束 |
| `app/sql_assistant/` | 本地 Text2SQL 意图编译、严格 SQL 护栏、只读数仓与查询快照 |

## 本地全链路

```bash
docker compose -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
docker compose -f deploy/docker-compose.native-e2e.yml --profile test \
  run --rm test-runner
```

该栈启动 PostgreSQL、Redis、Temporal、MinIO、API、写作 Worker、热点 Worker 与 Outbox
Worker。本地模型实现与生产实现使用相同 Port；它用于验证控制流和数据契约，不代表企业
模型质量。

本地 [热点 Agent SQL 工具演示](docs/text2sql-local-demo.md)：页面 `/hot-news#query-tools`
一键完成问题 → SQL 取数 → Python 热度排序 → 知识检索与分析；页面只投影同一次运行。
旧 `/sql-assistant` 重定向到热点页面，不再暴露独立查询业务入口。
聊天数据分析支持榜单概况、分布、指标比较、质量检查、保存基线比较与可比窗口趋势，
由固定隔离 worker 计算，并可接入独立 HTTPS 分析服务；
[调用链、执行边界、配置、企业聚合合同与测试](docs/data-analysis.md)。本地 Compose
显式开启，企业部署默认关闭，不支持执行模型生成代码。
可按[聊天数据分析演示](docs/data-analysis-demo.md)启动独立分析服务与本地 Overlay，在
独立 `5175/chat`／API `28010` 准备两个固定小时窗口，读取来源后逐项发送六种操作；
也提供同链路 CLI，保留 `28000` 上的既有演示。
数据库 [v1格式文档](docs/sql-warehouse-schema-v1.md) 为只读并绑定内容哈希；
可填写 [场景 YAML 模板](deploy/text2sql-scenes.example.yml)，不修改冻结格式。

企业配置参考 `deploy/model-runtime.enterprise.example.yml`，本地配置参考
`deploy/model-runtime.local.yml`。密钥由环境变量注入，不得写入 YAML 或提交仓库。

## 测试与质量门槛

```bash
python -m pytest -q
python -m evaluation.run_hot_news_eval \
  --runtime-config deploy/model-runtime.local.yml
python -m evaluation.run_fault_drills
```

生产上线前还需完成企业模型合同测试、企业行为/内容 Adapter 联调、Gateway/IdP、网络与
密钥治理、长期负载和故障恢复验证。长期架构与当前里程碑见
[`PROJECT_CONTEXT.md`](../DemoMD/PROJECT_CONTEXT.md) 和
[`TAOTIAN.md`](../DemoMD/TAOTIAN.md)。
