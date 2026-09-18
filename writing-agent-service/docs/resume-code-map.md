# NewsAgent 简历能力与代码映射

最后更新：2026-09-18

本文把面向 Agent 开发岗的简历主线映射到当前仓库。它只用于规划与核对，
不代表所有简历能力已经在企业环境落地。

状态约定：

- **已有代码**：存在业务实现与对应测试，可继续做真实环境验证。
- **部分完成**：本地领域逻辑或适配接口已存在，但企业 RPC、持久化、API 或端到端链路缺失。
- **预设**：只有规划目录和说明，没有可执行实现，简历中不能写成“已构建”。

## 1. 数仓行为数据接入、指标聚合与热点排行

**简历能力**：对接行为数据服务，按 `news_id` 和时间窗口统一曝光、点击、
有效消费、互动等指标，计算历史基线、热度分量与热点排行。

**当前对应代码**：

- `app/analytics/data_source.py`：数据源协议、查询条件和本地内存适配器。
- `app/analytics/entities.py`：行为领域对象和事件类型。
- `app/analytics/metrics.py`：事件去重及窗口指标聚合。
- `app/analytics/baseline.py`：历史窗口基线计算。
- `app/analytics/hot_score.py`：确定性热度分数与分量。
- `app/analytics/ranking.py`：热点排序。
- `tests/test_analytics_data_source.py`、`test_news_metric_calculator.py`、
  `test_baseline.py`、`test_hot_score.py`、`test_ranking.py`：本地验证。

**状态：仓库级核心完成，企业联调待完成。** 已实现异步行为/基线 RPC 契约、watermark、
分页、枚举防腐映射、总 deadline、版本漂移与数据质量校验；真实企业 SDK、字段表和授权
环境仍需接入，入口为 `app/clients/enterprise/`。

## 2. 新闻内容查询、关联报道召回与规则重排

**简历能力**：根据统一 `news_id` 查询正文，召回关联报道，并结合核心主体、
事件类型、标题关键词、发布时间和基础相关度完成重排与过滤。

**当前对应代码**：

- `app/analytics/news_content.py`：内容仓储协议及本地缓存适配器。
- `app/clients/knowledge_base.py`：当前实验检索客户端。
- `app/analytics/hot_news_enrichment.py`：排行、正文和关联报道富化。
- `app/retrieval/news_features.py`：主体、事件和关键词特征。
- `app/retrieval/related_news_reranker.py`：多特征重排、主体冲突降权及原因输出。
- `tests/test_news_content_repository.py`、`test_hot_news_enrichment.py`、
  `test_related_news_reranker.py`：本地验证。

**状态：仓库级核心完成，企业联调待完成。** 已实现内容批量 RPC、响应完整性校验、
关联报道批量召回、索引/策略版本校验、去重、自身排除、元数据补全和本地确定性重排；
当前 FastGPT/本地缓存仍只视为实验适配器，真实服务 Client 待企业环境联调。

## 3. 结构化热点分析 Agent 与可信输出校验

**简历能力**：把指标、热度分量、正文和证据转换为严格上下文，调用模型服务，
并校验新闻身份、指标引用、证据白名单和因果表述。

**当前对应代码**：

- `app/schemas/hot_news.py`：强类型输入和报告 Schema。
- `app/services/agents/hot_news.py`：热点分析 Agent Runner。
- `app/services/hot_news_analysis.py`：输入构造、模型调用、业务校验及统一入口。
- `app/clients/fastgpt.py`：当前实验模型服务客户端。
- `tests/test_hot_news_analysis.py`、`test_hot_news_agent_module.py`、
  `test_fastgpt_client.py`：结构化调用与失败路径验证。

**状态：仓库级主链路完成，真实服务验收待完成。** 严格 Schema、FastGPT 结构化 Runner、
业务 Validator 和统一 Service 已实现；企业真实 Prompt/App、鉴权、质量评测和生产网络联调
尚未完成。FastGPT 是外部模型应用基础设施，不应描述为本项目自研模型平台。

## 4. 有界、幂等且可恢复的热点运行

**简历能力**：将单次热点分析限制在固定窗口内，使用稳定幂等键、有限重试、
心跳和结果快照支持故障恢复与追溯。

**当前对应代码**：

- `app/services/hot_news_orchestration.py`：查询、聚合、排行、富化和分析编排。
- `app/workflows/hot_news.py`：有界 Temporal Workflow 及重试/超时策略。
- `app/activities/hot_news.py`：Activity、心跳、错误分类和幂等重放。
- `app/hot_news_bootstrap.py`、`app/hot_news_worker.py`：依赖装配与 Worker 注册。
- `app/services/hot_news_schedule.py`、`app/hot_news_scheduler.py`：窗口 Dispatcher 与
  Schedule 声明式管理。
- `app/models/hot_news.py`、`app/services/hot_news_run_store.py`：运行快照与幂等持久化。
- `app/api/hot_news.py`、`app/services/hot_news_event_stream.py`：查询、决策、转交和 SSE。
- `alembic/versions/20260905_0007_hot_news_analysis_runs.py`：数据库迁移。
- `tests/test_hot_news_orchestration.py`、`test_hot_news_activity.py`、
  `test_hot_news_workflow.py`：编排验证。

**状态：仓库级与本地依赖闭环完成，生产接入待完成。** Worker、窗口 Workflow、Schedule、
运行持久化、查询/决策 API 和 SSE 已存在，并通过隔离依赖栈做过本地验证；企业 SDK、目标
生产库迁移、真实 Schedule 注册、Gateway/IdP、监控告警和获授权环境 E2E 仍需完成。

## 5. 运营反馈 Data Loop

**简历能力**：统一回流校验失败、低置信结果、运营纠正、误报漏报、检索错误和
发布后聚合效果，生成可追溯的 `FeedbackCase` 并冻结版本化评测数据。

**当前对应代码**：

- `app/models/hot_news_decision.py`、`app/schemas/hot_news_decision.py`、
  `app/services/hot_news_decision.py`：接受、拒绝、暂缓和纠正决策。
- `alembic/versions/20260907_0008_hot_news_decisions.py`：运营决策迁移。
- `tests/test_hot_news_decision_service.py`：租户隔离、幂等和替代关系测试。
- `app/services/hot_news_run_store.py`：可供 Feedback Case 引用的可信分析快照。
- `app/services/data_loop/`：Feedback、标签审核、数据集冻结、评测、Gate、审批、激活和回滚。
- `app/api/data_loop.py`、`app/workflows/data_loop.py`：人工入口与有界 Temporal 编排。
- `app/models/analysis_feedback.py`、`evaluation_dataset.py`、`production_bundle.py`：持久化模型。

**状态：仓库级闭环完成，本地 L2 已验证。** 校验失败、低置信、检索异常、运营决策和
聚合效果可形成 Feedback Case；标签提交与独立二审、三层 Dataset 和不可变 Artifact 已有
实现。真实运营数据、企业身份、生产对象存储和长期效果口径仍待验收。

## 6. 错误归因 Agent、离线评测与受控晋升

**简历能力**：归因 Agent 分析指标、热度规则、检索、Prompt 和校验问题，生成候选
Diff 与回归用例；在黄金集、新鲜 Bad Case 和高风险回归集上执行双基准评测，
最终由人工决定晋升或回滚。

**当前对应代码**：

- `app/services/agents/error_attribution.py`：错误归因 Runner。
- `app/services/data_loop/`：候选生成、三层回放、确定性 Gate、审批、激活和回滚。
- `app/workflows/data_loop.py`：48 小时人工 Gate 与超时默认拒绝。
- `app/services/memory_context.py`、`memory_promotion.py`：作用域上下文和人工经验晋升。

**状态：受控配置迭代闭环已实现并完成本地 L2 验证。** 当前迭代对象是 Prompt、热度规则、
重排参数和输出 Schema，不包含大模型自动训练；真实 FastGPT、企业评测数据和生产环境
晋升仍未验收，不能描述为“生产模型自动训练或自动上线”。

## 简历用词边界

- 第 1～2 项可以描述为“完成仓库级核心链路与企业 RPC 防腐层”，但在真实联调前不能写
  “已接入腾讯数仓/内容中心”或生产效果数据。
- 第 3～4 项可以描述为“构建/实现核心链路”，企业模型 RPC 和生产上线仍按真实状态说明。
- 第 5～6 项可描述为“实现本地可回放的反馈、评测与受控晋升闭环”，但不能写成已接入
  企业生产数据、自动训练大模型或自动上线。
- FastGPT、爬虫和 QA 生成是本地实验基础设施，不应写成腾讯内部生产能力。
- 指标、基线、热度和评测统计由确定性代码或 SQL 计算；Agent 只负责解释、归因、
  模式发现和候选生成。
- Data Loop 不自动训练、修改线上配置或发布；高风险候选必须经过人工审批。
