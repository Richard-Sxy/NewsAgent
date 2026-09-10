# NewsAgent 持久项目上下文

最后更新：2026-09-10

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
- 跨天 Memory 与版本台账约 15%。

完成“持续热点分析最小闭环”后，整体完成度预计可到约 60%。这些数字是架构里程碑
估算，不代表经过生产验证的测试覆盖率。

2026-09-10 复核：上述百分比是 2026-09-04 的历史基线。当前 Data Loop 按业务功能实现
约 95%；本地依赖 L2 闭环（正常批准、人工拒绝、下一次线上运行、显式回滚、授权故障恢复）
已经验收通过。按生产启用口径约 75%，差异主要来自真实 FastGPT App、企业数据 Adapter、
网关/IdP、Schedule、对象存储生产治理，以及 48 小时审批超时的 Temporal time-skipping CI
验收。全服务默认回归为 554 项通过、3 项 opt-in E2E 跳过；隔离 Compose 的完整 L2 为
3 项通过。尚不能据此描述为生产已上线。

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
模块。用户首先完成了业务校验器主体，并曾在该阶段单独授权 Codex 补齐其余主链路代码；
该一次性授权已经结束，后续代码实现遵循第 14 节的最新协作规则。Codex 仍需重点说明数据
如何经过 InputBuilder、Runner、FastGPTClient、Validator 和 Service。

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

2026-09-05 进展：热点 Temporal Activity 适配层已经归位并完成代码级闭环，新增最小
`analysis_runs` PostgreSQL 模型、幂等 `PostgresHotNewsRunStore`、Alembic 迁移和 Activity
测试。使用 `/tmp` 中的 macOS Python 3.11 临时环境运行热点 Activity、热点编排和大模型模块
测试最初共 25 项通过。随后已新增有边界的 `HotNewsMonitorWorkflow`，配置 Activity 心跳、
45 分钟单次超时、90 分钟总时限和最多一次自动重试；热点 Workflow、Activity、编排及大模型
模块测试现共 28 项通过。尚未在真实 PostgreSQL 执行迁移，也尚未注册热点 Worker 或创建
Temporal Schedule。

2026-09-10 里程碑：热点 Data Loop 的代码级业务闭环已经完成。线上运行会按租户读取一次
Active Production Bundle 快照，并将校验失败、低置信、检索异常、运营决策和聚合发布效果
统一转为 Feedback Case；强类型人工标签经独立权限审批后，可按 cutoff 冻结为不可变评测
Dataset。Workflow 在黄金集、窗口新鲜 bad case 和高风险回归集上并发回放候选、线上基线与
可选上一实验基线，由版本化确定性 Gate 判定，随后等待人工批准或拒绝；批准账本、激活、
失败恢复与显式回滚均使用稳定幂等键。生产 Bundle 运行时 Registry、端点级 RBAC、租户复合
外键、不可变触发器、S3 条件写与哈希核验已经补齐，Alembic 迁移头为 `20260910_0013`，
Data Loop 核心回归 140 项通过。生产环境迁移/并发演练、Object Lock、企业数据 RPC、
网关/IdP、热点 Worker 的真实 Adapter 装配/Schedule 与 FastGPT 真实 App 验收仍是上线前
必做项；FastGPT 不支持业务幂等键时，模型调用语义仍是有界 at-least-once，不能声称
exactly-once。

2026-09-10 L2 验收补充：新增隔离 Compose 栈，实际启动 PostgreSQL、Temporal、Redis、
MinIO、API、HotNews Worker、Data Loop Worker 和确定性 FastGPT HTTP 替身。黑盒验收已
实跑通过 `approve → activate → next run → rollback`、`REJECT → keep base active`，以及
`approve ledger committed → forced activation failure → bounded recovery → next run` 三条路径；
对象内容 SHA-256 与 Metadata、三层 Dataset、单一 Active Bundle、审批/激活账本唯一性均
进入自动断言。另已实际操作 stdin 人工 `REJECT` 停点。该结果只代表本地依赖闭环，L3
真实模型、企业 Gateway/IdP 和企业 Adapter 仍需发布前验收。

## 11. 后续阶段

以下是原规划顺序；其中第 4 项和第 5 项的候选评测、人工晋升、激活与回滚部分已于
2026-09-10 完成代码级闭环，尚待生产环境验收。Temporal Schedule、热点查询/SSE 和企业
训练平台仍未因此自动完成。

1. `AnalysisRun`、指标快照、热点事件和分析报告持久化。
2. `HotNewsMonitorWorkflow` 与 Temporal Schedule。
3. 热点查询 API、SSE 和运营人工决策入口。
4. Feedback/Label Data Loop。
5. 候选配置、回归评测、人工晋升与回滚 Model Loop。
6. 企业训练平台具备后，再考虑自动训练适配器。

## 12. `TAOTIAN.md` 长期架构参考

用户明确要求后续 NewsAgent 的 Agent 设计参考仓库根目录的 [`TAOTIAN.md`](./TAOTIAN.md)，
并要求该决定可被后续会话继承。凡涉及 Agent 架构、持续运行、Data Loop、Model Loop、
Harness、自主权边界、评测晋升或跨天 Memory 的任务，开始设计前必须读取该文件原文。

需要吸收并映射到 NewsAgent 的工程原则包括：

1. 用 Online/Data/Model 等有边界的 Loop 组合系统，不构造一个无限运行且可自行修改生产
   系统的“大 Agent”。
2. Data Loop 负责把校验失败、低置信、人工纠错和线上反馈转化为版本化评测数据；Model
   Loop 负责归因、生成候选、离线评测和提交人工决策。
3. Harness 必须覆盖幂等、失败分级、有副作用操作的重试上限、环境与凭据收口、文件安全、
   人工 Gate、超时默认不发布，以及可追溯和可回滚。
4. Agent 自主权分级：允许自动读取、计算、归因、生成报告和候选；Prompt、规则、模型、
   生产配置、部署和发布等高风险变更必须人工审批。
5. Memory 按触发索引、稳定 Runbook、Operational Logs/状态台账、Approved Experience
   分层；跨天状态要记录趋势、持续错误、建议采纳情况、版本效果和双基准对比。
6. 评测同时覆盖固定黄金集、当日新鲜 bad case 和历史高风险回归集，并对比线上基线与
   上一实验基线，防止局部优化造成全局退化。

`TAOTIAN.md` 是设计参考而不是 NewsAgent 的业务事实源或可直接复制的实施方案。不得照搬
其中的电商 KIE 目标、ODPS、质检/标注平台、训练脚本、具体指标或权限设置，也不得把文中
展示的效果数字当作本项目结果。发生冲突时，以本文件定义的 NewsAgent 数据边界、安全
约束、当前阶段和人工审批规则为准。NewsAgent 当前最合适的进化对象仍是 Prompt、热度
配置、检索重排和校验规则；企业训练平台属于后续可选集成，当前热点 Agent 主链路的优先级
不因加入该参考文档而改变。

## 13. 运营团队 Memory 设计重点

用户明确要求后续重点深挖 Memory 层，尤其是运营团队如何积累热点判断、人工决策和发布
效果。Memory 不是聊天记录或一个可被 Agent 随意改写的 Markdown/向量库，而应是可审计、
可检索、可过期、可回滚的运营知识系统。
当前主线已进一步明确为“短期工作记忆 + 长期用户记忆”的分层设计，需重点定义
写入条件、作用域、权限、时效、冲突解决、晋升/遗忘机制，以及两层在运行时的组合方式。

2026-09-07 进展：短期记忆、长期候选、长期记忆、来源引用和解析结果的严格
Pydantic 契约已建立；纯领域晋升服务已覆盖审批命令、候选版本、有效期和替代关系；
Resolver 已实现租户/用户/任务/组织作用域过滤、显式短期偏好覆盖和稳定冲突排序。
2026-09-07 后续进展：`short_term_user_memories`、`long_term_memory_candidates`、
`long_term_user_memories`、`memory_promotion_requests` 四张 PostgreSQL 表的 ORM 与 `0009`
迁移已建立，包含租户内幂等、非空来源、时间窗口、版本、状态、active 长期记忆唯一性以及
候选/替代/结果的租户和用户复合外键约束；PostgreSQL 离线迁移 SQL 已验证可生成。
Repository、审批人 RBAC/ABAC 授权、API、审计事件、完整幂等事务和真实数据库迁移验证仍未
接入，不得将当前阶段描述为生产可用。

建议按“事实—决策—效果—经验”组织：

1. Observation Memory：保存指标快照、热点事件状态、分析报告、证据引用和校验结果；只保存
   聚合行为，不保存企业原始用户行为明细。
2. Decision Memory：保存运营的采用、拒绝、改写、合并事件、暂缓和发布决策，并记录操作者、
   理由、作用范围、引用版本和时间。
3. Outcome Memory：保存决策后的曝光、点击、停留、转化、投诉、纠错和风险结果，用于建立
   “建议—动作—效果”闭环；不得把相关性自动解释为因果。
4. Approved Experience：从重复 case 中生成候选经验，经人工审批后成为有适用范围、置信度、
   生效时间和失效时间的运营 Playbook；模型生成的总结不得自动晋升为规则。

检索时必须区分硬约束与软经验：品牌、安全、合规和人工禁用项优先作为确定性策略执行；
相似事件、受众偏好、渠道经验和历史效果只作为有来源的参考上下文。每条记忆至少携带
`tenant_id`、事件/新闻关联键、时间窗口、来源引用、Production Bundle 版本、记忆类型、
置信度、审批状态、适用范围和过期/替代关系。还应同时记录 event time、recorded time 和
valid time，防止未来信息泄漏、旧经验误用和跨租户污染。

存储职责沿用现有边界：PostgreSQL 保存结构化事实、决策、版本和关系；S3/MinIO 保存不可变
报告与评测产物；全文/向量索引只是可重建的检索投影；Redis 只做缓存和事件推送；FastGPT
不是运营事实的唯一来源。现有 `WritingArtifact`、`AgentRun` 和 Outbox 可复用其不可变、审计
和事件发布模式，但热点 Memory 应使用独立聚合根，不强行挂到 `WritingJob`。

首个最小闭环应优先实现：热点运行与报告持久化 → 运营反馈/决策 → 发布后聚合效果 → 周期性
经验候选 → 人工批准进入 Playbook。暂不先做“Agent 自动写长期记忆”或仅依赖向量相似度的
黑盒记忆。

运营 Memory 还必须支持组织和用户作用域。推荐使用统一 Memory 模型，通过
`tenant_id + team_id + section_id + role_id + user_id` 表达租户、团队、业务板块、岗位和个人
层级，而不是为每个用户建立彼此隔离且无法治理的独立记忆库。运行时由 Memory Context
Resolver 在权限范围内组合“租户硬规则 → 团队 Playbook → 板块经验 → 角色默认值 → 个人
偏好 → 当前任务上下文”；更具体的偏好可以覆盖一般偏好，但个人记忆永远不能覆盖合规、
品牌、安全等上层硬约束。

个人记忆应区分显式偏好、角色职责、历史决策和系统推断。显式偏好可由本人维护；团队或
板块经验需负责人审批；系统推断只能作为有置信度和过期时间的候选，不能直接变成硬规则。
记忆读取和写入均需 RBAC/ABAC、审计和来源记录。人员调岗或离职时，个人偏好停止生效，
但其代表组织作出的业务决策应保留在原团队/板块作用域并完成责任转移，避免业务知识随账号
一起丢失或错误地跟随个人。

## 14. 用户协作偏好

最新协作规则（2026-09-05）：项目代码默认由用户亲自实现。Codex 应先给出实现顺序、设计
思路、前后接口参数、代码骨架或核心函数 TODO，以及验收测试；用户完成代码后，Codex 负责
审查、排错和验证。除非用户明确要求“直接帮我修改”或“直接帮我实现”，Codex 不得编辑
业务实现文件。此前针对热点 Agent 主链路的一次性直接实现授权不延续到后续任务。

指导粒度偏好：不能只给“新增配置”“注册 Worker”等概括性建议。每一步应明确文件路径、
定位位置、需要输入或修改的具体名称、字段、函数签名、参数来源、依赖关系、执行命令、预期
输出和常见失败。用户要求亲自实现时，可以给出足以照着填写的骨架和 TODO，但不要替用户
直接完成核心业务实现。

每次 Codex 新建或修改文件后，必须在交付中说明完整调用链，包括：

1. 谁调用新模块，以及调用入口。
2. 输入从哪里产生、经过哪些 Schema 和转换。
3. 哪些核心函数负责什么。
4. 调用了哪些外部服务或基础设施。
5. 输出返回给谁、后续流向哪里。
6. 配置项和依赖注入位置。
7. 对应测试文件、验证状态和剩余 TODO。

不能只汇报“新增了哪些文件”。
