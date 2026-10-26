# NewsAgent Agent 服务（`agent/`）

NewsAgent 的独立 Python 后端，负责热点分析、研究写作、知识入库与检索、Data Loop、
配置晋升和跨运行 Memory。技术栈为 Python 3.11、FastAPI、Temporal、PostgreSQL、Redis
与 S3/MinIO。当前优先验证公开新闻与合成行为数据的本地链路，企业生产接口仍需联调。

企业模型通过 OpenAI-compatible Chat Completions 与 Embeddings 接口适配；默认本地栈
使用相同 Port 的确定性测试实现，BGE 快照模式另使用本地真实 Embedding。
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
| `app/bootstrap/` | 启动装配：API lifespan、写作/热点 Worker 运行时、热点依赖工厂 |
| `app/modules/hot_news/` | 热点业务模块：contracts/persistence、scoring（指标基线热度排行）、analysis（富化/Runner/校验）、application（运行/查询/决定/转交）、adapters（RunStore）、workflows |
| `app/modules/{writing,data_loop,memory}/` | 写作（Workflow/Checkpoint/Gate/产物）、Data Loop（反馈/评测/数据集/Bundle/审批）、Memory（上下文/晋升/仓储） |
| `app/modules/{conversation,knowledge,audience,behavior_tags,reader_profiles,data_analysis}/` | 其余业务模块：对话、知识、受众预览、行为标签、读者画像与数据分析 |
| `simulation/` | 本地模拟装配：本地 API 工厂、E2E Worker、SQL/知识支持、场景工厂、模拟数仓与数据（examples/ 只保留纯演示脚本） |
| `../var/`（仓库根） | 本地运行数据：行为标签/读者画像运行包、数仓数据包与只读知识快照；部分清单纳入 Git，批量文件通常忽略，数据是否导入数据库另由导入流程决定 |
| `app/model_runtime/` | YAML、Prompt Registry、推理/Embedding Port、HTTP Adapter、本地替身 |
| `app/services/`、`app/agents/` | 旧导入路径兼容 shim、安全测试 harness 与热点分析 Agent 入口 |
| `app/analytics/` | 共享分析词汇表（entities/news_content/sql_guard）与旧路径兼容再导出 |
| `app/workflows/`、`app/activities/` | 通用工作流契约与知识生命周期 Workflow/Activity |
| `../deploy/`（仓库根） | Compose、K8s、监控与模拟 CMS 部署资产；后端 Dockerfile 位于 `agent/`，`agent/deploy` 保留兼容链接 |
| `app/api/` | 查询、人工 Gate、知识入库和本地模拟 API |
| `app/models/`、`app/repositories/`、`alembic/` | ORM 惰性聚合入口、对话仓储和数据库迁移 |
| `app/retrieval/` | 分层向量召回、过滤和证据约束 |
| `app/sql_assistant/` | 本地 Text2SQL 意图编译、严格 SQL 护栏、只读数仓与查询快照 |

## 本地运行

带前端与 BGE 快照的本地模拟入口是 `python3 agent/tools/start_local_demo.py`，
需要预先准备快照；配置、预检和停止方法见[本地运行说明](docs/local-bge-simulation.md)。
下面是较小的原生基础栈，API 使用 `28000`，前端需按仓库根 README 单独启动。

以下命令在仓库根执行：

```bash
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
docker compose --env-file /dev/null -f deploy/docker-compose.native-e2e.yml --profile test \
  run --rm test-runner
```

该栈启动 PostgreSQL、Redis、Temporal、MinIO、API、写作 Worker、热点 Worker 与 Outbox
Worker；基础栈不含前端和 Data Loop Worker。上面的 `test-runner` 默认只运行
`tests/test_native_hot_news_runtime.py`，不是完整回归。本地模型用于验证控制流和数据契约，
不代表企业模型质量。

本地热点 Agent SQL 工具：页面 `/hot-news#query-tools`
一键完成问题 → SQL 取数 → Python 热度排序 → 知识检索与分析；页面只投影同一次运行。
旧 `/sql-assistant` 重定向到热点页面，不再暴露独立查询业务入口。
聊天数据分析支持榜单概况、分布、指标比较、质量检查、保存基线比较与可比窗口趋势，
由固定隔离 worker 计算，并可接入独立 HTTPS 分析服务；
实现见[数据分析模块](app/modules/data_analysis/)，场景与测试见[企业模拟](docs/enterprise-synthetic-data.md)。本地 Compose
显式开启，企业部署默认关闭，不支持执行模型生成代码。
独立分析服务和本地 Overlay 模板位于仓库根 `deploy/`；
可通过[企业模拟说明](docs/enterprise-synthetic-data.md)查阅固定窗口、六种分析操作和 CLI。
数仓 [契约统一入口](docs/sql-warehouse/README.md) 中的各冻结版本 分别对应既有数据集，内容绑定哈希；
可填写 [场景 YAML 模板](../deploy/text2sql-scenes.example.yml)，不修改冻结格式。

企业配置参考 `deploy/model-runtime.enterprise.example.yml`，本地配置参考
`deploy/model-runtime.local.yml`。密钥由环境变量注入，不得写入 YAML 或提交仓库。

热点榜单支持单新闻访问人群与推送主题分析：
离线聚合该新闻的去重读者及其访问前长短期兴趣，在线只读取聚合，绑定到模型输入和报告。
本地已生成独立合成包；[数据库查询手册](docs/news-storage-database-reference.md)说明读者聚合字段、关联与口径。

日常文档统一从[文档入口](docs/README.md)查阅。

## 测试与质量门槛

以下命令从仓库根进入 `agent/` 后执行，需要已安装项目及 dev 依赖的 Python 3.11+ 环境。
需要真实数据库或服务的集成测试按各测试文件要求显式启用，跳过不等于验收通过。

```bash
cd agent
python -m pytest -q
# 只校验评测数据集；不执行模型推理。
python -m evaluation.run_hot_news_eval
# 显式执行本地测试模型回放；用于检查结构和控制流。
python -m evaluation.run_hot_news_eval --run-model \
  --runtime-config ../deploy/model-runtime.local.yml
python -m evaluation.run_fault_drills
```

生产上线前还需完成企业模型合同测试、企业行为/内容 Adapter 联调、Gateway/IdP、网络与
密钥治理、长期负载和故障恢复验证。长期架构与当前里程碑见
[`PROJECT_CONTEXT.md`](../DemoMD/PROJECT_CONTEXT.md) 和
[`TAOTIAN.md`](../DemoMD/TAOTIAN.md)。
