# NewsAgent 持久项目上下文

最后更新：2026-09-04

本文是用户要求跨后续项目会话复用的长期上下文。它保存已经确认的目标、架构判断、
当前进度和工程约束，不保存密码、Token、`.env` 内容或企业原始用户数据。

## 1. 项目目标

NewsAgent 的长期目标不是单纯的新闻写作机器人，而是一套消费企业已有数据、持续发现
新闻热点、组合可信证据、辅助研究写作，并通过 Data Loop 与 Model Loop 持续改进的
新闻运营 Agent 系统。

统一关联键为 `news_id`。行为指标回答“什么正在变热、哪些群体在关注”，内容库和
知识库回答“新闻讲了什么、有哪些相关报道”，Agent 负责规划查询、解释证据和组织
表达，不负责采集企业原始行为，也不自行计算权威指标。

## 2. 仓库组成

### `tencent-news-crawler`

负责腾讯新闻发现、抓取、清洗、缓存、SQLite 去重、失败重试、FastGPT 知识入库、QA
生成和基础评测。它是内容接入与知识库实验链路。

### `writing-agent-service`

Python 3.11、FastAPI、Temporal、PostgreSQL、Redis、S3/MinIO 构成的研究与辅助写作
后端。现有主流程为：

```text
创建任务
→ Research
→ 人工确认研究包
→ Outline
→ 人工确认提纲
→ 分章节写作与组装
→ Reviewer
→ 定点章节返工（最多三轮）
→ 人工确认终稿
→ CMS 发布
```

已有任务幂等、状态机、Checkpoint、不可变 Artifact、失败恢复、Outbox、Redis SSE、
健康检查和人工审批能力。

### `writing-agent-service/app/analytics`

已有 `BehaviorRecord`、`BehaviorDataSource`、指标聚合、历史基线、热度评分、排行、新闻
内容精确查询、FastGPT 关联新闻检索、规则重排和热点富化链路。

### `FastGPT`

完整引入的上游 FastGPT 工程，是知识库和模型应用基础设施，不属于 NewsAgent 自主实现
的业务能力。

## 3. 当前完成度判断

以“可持续运行、可反馈、自进化的新闻运营 Agent”为目标，2026-09-04 的估算为：

- 整体约 40%，尚缺约 60%。
- 腾讯新闻抓取与 FastGPT 入库约 65%。
- 新闻研究与辅助写作工作流约 80%。
- 行为指标与热点计算约 60%。
- 热点内容检索与重排约 55%。
- 热点分析 Agent 主链路约 65%。
- 持续调度与热点事件生命周期约 5%。
- 热点结果持久化、API 和运营界面约 10%。
- Data Loop 约 5%。
- Model Loop 尚未开始。
- 跨天 Memory 与版本台账约 5%。

完成“持续热点分析最小闭环”后，整体完成度预计可到约 60%。这些数字是架构里程碑
估算，不代表经过生产验证的测试覆盖率。

## 4. 已确认的目标架构

系统分成三个闭环，不采用一个无限运行、自行修改生产代码的“大 Agent”。

### Online Insight Loop

由 Temporal Schedule 或业务事件持续创建有边界的工作流实例。建议每个运行实例只处理
一个 `[window_start, window_end)` 窗口，并使用下列幂等维度：

```text
tenant_id + window_start + window_end + production_bundle_version
```

目标流程：

```text
检查数据 watermark 和质量
→ 查询企业行为数据
→ 聚合指标与历史基线
→ 热度评分和排行
→ 热点事件去重/合并
→ 按 news_id 精确读取内容
→ FastGPT 召回关联新闻
→ 规则重排
→ 构造 EvidencePacket
→ 热点分析 Agent
→ 数字、引用和因果表述校验
→ 保存报告 Artifact
→ 运营人工决策
→ 可选触发现有研究/写作流程
```

Temporal Worker 和 Outbox Worker 可以常驻；业务 Workflow 不应使用无边界的 `while`
循环。长时间训练或评测应使用 Temporal Child Workflow、Timer 或 Signal。

### Data Loop

系统校验失败、低置信结果、运营拒绝、人工纠错、误报/漏报、检索错误以及后续效果进入
Feedback Case。经人工修正的数据形成按日期和版本冻结的 Evaluation Dataset。

不保存企业原始用户行为明细，只保存必要的聚合指标、匿名化错误特征和证据引用。

### Model Loop

当前仓库没有企业训练平台，第一阶段不做自动训练大模型。先迭代四类可控资产：

1. 分析 Prompt。
2. 热度权重和阈值配置。
3. 关联新闻重排参数。
4. 输出 Schema 和校验规则。

Agent 只能生成候选版本与 Diff。候选版本需同时在固定黄金集、当日新鲜 bad case、历史
高风险回归集上评测，并与线上版本和上一实验版本双基准比较。Prompt、模型、热度规则
和线上发布均需人工批准；超时默认不晋升、不发布。

## 5. 目标数据与状态模型

热点运行不应塞进现有 `WritingJob`。建议独立增加：

```text
analysis_runs
news_metric_snapshots
news_metric_baselines
hot_events
hot_analysis_reports
analysis_feedback
evaluation_datasets
evaluation_runs
configuration_candidates
production_bundles
promotion_decisions
```

PostgreSQL 保存状态、聚合快照和版本元数据；S3/MinIO 保存不可变报告、评测数据快照和
实验产物；Redis 只负责缓存与事件推送；FastGPT 负责知识检索和模型应用，均不是业务
事实的唯一来源。

建议将现有只关联 `writing_jobs` 的 Outbox 逐步扩展为支持 `aggregate_type +
aggregate_id` 的通用领域事件 Outbox。

## 6. 热点分析报告契约

长期目标不是只返回几段文字，而是输出可验证的 Claim：

```text
HotNewsAnalysisReport
├── news_id
├── window
├── production_bundle_version
├── trend_summary
├── claims[]
│   ├── claim_type: metric | fact | hypothesis
│   ├── text
│   ├── metric_refs[]
│   ├── evidence_news_ids[]
│   ├── confidence
│   └── limitations[]
├── attention_reasons[]
├── operation_suggestions[]
└── validation_result
```

数字必须能反查 Metric Snapshot；事实必须绑定证据 `news_id`；假设必须显式标记，不能
写成已确认因果。

## 7. Harness 与自主权边界

- 数据同步和数据质量失败属于硬阻塞。
- 归因和长期 Memory 更新失败可以降级，不应阻塞已经可信的在线分析结果。
- 外部提交必须携带幂等键；有副作用操作最多自动重试一次，再失败转只读诊断。
- 数据读取、指标计算、报告生成和离线评测可以自动化。
- Prompt、规则、模型和生产发布只能生成候选，高风险动作必须人工审批。
- 自动修改线上配置、自动部署模型和自动发布新闻默认禁止。
- 新闻内容、检索结果和外部模型输出均可能包含 Prompt Injection，必须作为数据处理。
- 每次运行记录完整 Production Bundle：指标口径、热度配置、重排配置、Prompt、FastGPT
  App ID 和输出 Schema 版本。

## 8. 四层 Memory 映射

1. Schedule Payload：只包含租户、窗口、Workflow 版本和 Production Bundle ID。
2. Runbook：稳定流程、工具权限、失败策略和人工 Gate。
3. Operational Memory：数据库与 Artifact 中的热点历史、错误榜、评测、候选和审批记录。
4. Approved Experience：人工确认后的数据口径、例外规则和迭代经验。

生产 Memory 必须版本化、可审计、可回滚，不能让 Agent 随意覆盖一个 Markdown 文件作为
业务事实源。本文件只保存项目协作上下文，不替代生产 Memory。

## 9. 当前已知问题

- `writing-agent-service/app/agents/hot_news_analysis.py` 是用户当前正在修改的文件，存在
  多处未完成的运行逻辑，不能视为可用状态。
- `tencent-news-crawler/crawler/tencent_news.py` 在 `_parse_article_id` 附近存在缩进错误。
- `writing-agent-service/app/analytics.py/entities.py` 是疑似遗留目录和未完成代码，不被
  当前热点主链路引用。
- 仓库错误跟踪了 `.venv`、`__pycache__`、egg-info 和
  `writing-agent-service/.env`。如果其中存在真实凭据，需要移除并轮换。
- 已跟踪虚拟环境来自 Linux，在当前 Mac 上解释器链接和二进制扩展不可用，暂时不能得到
  可信的全量 pytest 结果。
- 根 README 对热点模块进展的描述落后于实际代码。

## 10. 今日任务：完成热点 Agent 的大模型接入模块

用户已明确：今日目标不是完成简单规则 Demo，而是完成可以真实调用大模型的热点分析
模块。用户首先完成了业务校验器主体，后续又明确授权 Codex 补齐其余主链路代码；
Codex 仍需在交付时重点说明数据如何经过 InputBuilder、Runner、FastGPTClient、Validator
和 Service。

模块目标调用链：

```text
EnrichedHotNews
→ 可信输入构造器
→ HotNewsAnalysisInput（Pydantic）
→ HotNewsAnalysisAgentRunner
→ FastGPTClient.run_structured
→ HotNewsAnalysisReport（Pydantic）
→ HotNewsAnalysisValidator
→ AgentResult[HotNewsAnalysisReport]
```

推荐新增或调整：

1. `app/schemas/hot_news.py`：定义强类型输入、指标、证据和输出 Claim Schema。
2. `app/services/agents/hot_news.py`：仿照 ResearchAgentRunner，实现 FastGPT Runner。
3. `app/services/hot_news_analysis.py`：组合输入构造、Runner 与业务校验，作为唯一调用入口。
4. `app/config.py` 与 `.env.example`：增加 `FASTGPT_HOT_NEWS_APP_ID`，不得写死或提交密钥。
5. `tests/test_hot_news_agent.py`：MockTransport/AsyncMock 验证请求与结构化输出。
6. `tests/test_hot_news_analysis_validator.py`：验证数字引用、证据白名单和可信上下文一致性。

现有 `FastGPTClient.run_structured` 已负责 HTTP 调用、错误分类、JSON 解包和 Pydantic 校验，
热点模块不得重复实现 HTTP 客户端。Runner 只负责选择 App、组装 payload、指定
`output_type` 和 `mode="hot_news_analysis"`。

业务校验必须保证：输出 `news_id` 与输入一致；指标引用只使用允许的指标键；证据
`news_id` 必须是输入候选集合的子集；无证据时必须输出 limitation；模型不能生成权威
指标或将假设写成确定因果。新闻正文和检索文本作为不可信 evidence 传入，不允许其中的
指令改变系统行为。

今日 Definition of Done：

- 存在真实的 `HotNewsAnalysisAgentRunner`，通过现有 FastGPT 客户端调用指定 App。
- 输入和输出均为严格 Pydantic Schema，不以自由文本作为成功结果。
- 合法模型响应可以转换为 `AgentResult[HotNewsAnalysisReport]`，保留 request ID、usage 和
  raw content。
- 非 JSON、Schema 不合法、新闻 ID 不一致、未知证据 ID 和无依据数字均被拒绝。
- 网络、超时、鉴权、限流和服务端异常沿用现有错误分类，不在 Runner 中自行反复重试。
- 单元测试使用 MockTransport 或 AsyncMock，不依赖真实 FastGPT。
- 使用独立的手工 smoke test 验证真实 FastGPT App；没有凭据时单元测试仍可运行。
- 不接数据库、Temporal Schedule、自动发布、Data Loop 或 Model Loop。

当前实现状态（2026-09-04）：

- `HotNewsAnalysisInputBuilder` 已将 `EnrichedHotNews` 转换为严格输入 Schema，分开
  权威指标、热度分量、最长 2000 字正文片段和最多 5 条白名单证据。
- `HotNewsAnalysisAgentRunner.run` 已复用 `FastGPTClient.run_structured`，固定
  `mode="hot_news_analysis"` 和 `HotNewsAnalysisReport` 输出类型，不在 Runner 内重试或降级。
- 用户编写的 `HotNewsAnalysisValidator` 主体已保留，同时修复了异常类名、
  Pydantic 指标键读取、证据对象字段路径和 `related_contexts` 字段兼容问题。
- `HotNewsAnalysisService.analyze` 现在是独立可调用的主链路入口。不访问真实网络的
  MockTransport 手工验证已通过，请求 ID 和 token usage 可保留。
- 仍需用可用的 macOS Python 3.11 开发环境执行完整 pytest；仓库跟踪的 Linux
  `.venv` 不可用，当前系统 Python 又没有 pytest。还需配置真实 FastGPT App 的
  System Prompt/输出约束并做一次携真实凭据的 smoke test。

## 11. 后续阶段

今日热点 Agent 稳定后，依次建设：

1. `AnalysisRun`、指标快照、热点事件和分析报告持久化。
2. `HotNewsMonitorWorkflow` 与 Temporal Schedule。
3. 热点查询 API、SSE 和运营人工决策入口。
4. Feedback/Label Data Loop。
5. 候选配置、回归评测、人工晋升与回滚 Model Loop。
6. 企业训练平台具备后，再考虑自动训练适配器。

## 12. 参考材料结论

用户提供了淘天电商 KIE Auto Research 实践作为参考。需要复用的是双 Loop、分级自主权、
跨天 Memory、幂等、失败分级、候选评测和人工上线 Gate；不应直接照搬其 ODPS、标注平台
或自动训练实现。NewsAgent 当前最合适的进化对象是 Prompt、热度配置、检索重排和校验
规则，训练平台属于后续可选集成。

## 13. 用户协作偏好

用户要求自己完成热点 Agent 的核心业务代码，Codex 可以搭建生产骨架、留下明确 TODO、
提供审查和排错建议。每次 Codex 新建或修改文件后，必须在交付中说明完整调用链，包括：

1. 谁调用新模块，以及调用入口。
2. 输入从哪里产生、经过哪些 Schema 和转换。
3. 哪些核心函数负责什么。
4. 调用了哪些外部服务或基础设施。
5. 输出返回给谁、后续流向哪里。
6. 配置项和依赖注入位置。
7. 对应测试文件、验证状态和剩余 TODO。

不能只汇报“新增了哪些文件”。
