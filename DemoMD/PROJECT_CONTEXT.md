# NewsAgent 持久项目上下文

最后更新：2026-10-24

本文保存已经确认、需要跨会话继承的项目事实、架构边界、当前状态和协作方式。它不保存
密码、Token、`.env` 内容、企业原始用户行为或未经确认的临时猜测。

## 1. 已确认架构

NewsAgent 是独立 Python Agent 项目。Agent 应用层、Prompt/模型版本、结构化解析、业务
校验、知识入库、确定性切片、Embedding 编排、索引、关联检索、QA 与索引状态均由本项目
实现，不依赖 FastGPT 运行时。

企业只提供 OpenAI-compatible Chat Completions 与 Embeddings 原始接口，以及已有业务
数据和基础中间件。本地模拟使用相同 Python Port 的隔离确定性实现，不连接企业服务。
历史框架客户端、协议替身、环境变量、Compose 和运行文档已于 2026-10-02 从活动代码删除；
旧数据库列通过 `20261002_0017` 无损迁移为供应商无关字段。旧 Alembic 迁移与本说明中的
历史名称保留用于已有数据库升级和决策追溯，不属于可执行依赖。

实现目录与运行入口见 [Agent 服务说明](../agent/README.md)；模型运行时源码位于
[`agent/app/model_runtime/`](../agent/app/model_runtime/)。原 `python-runtime.md` 已从工作区移除。

## 2. 项目目标与数据边界

NewsAgent 不是单纯写作机器人，而是新闻运营 Agent 系统：消费企业已有行为与内容数据，
持续发现热点，组合可信证据，辅助运营决策与研究写作，并通过反馈、评测和人工审批改进。

- 企业数据库提供 C 端行为数据；本项目不负责埋点、日志传输或建设行为数仓。
- 本地只保存必要聚合快照、版本、审计和证据引用，不保存企业原始用户行为明细。
- `news_id` 是行为指标、正文、知识、热点事件、报告和反馈的统一关联键。
- Python/SQL 计算指标、基线、热度、排行和门禁；LLM 只做文本理解与表达。
- 新闻正文、检索内容与模型输出均为不可信输入，不能改变系统指令或权限边界。
- Prompt、热度规则、模型版本和生产发布需要版本化；高风险变更必须人工审批。

## 3. 仓库组成

- `agent`：FastAPI、Temporal、PostgreSQL、Redis、S3/MinIO、Python Agent、
  Python 知识运行时、Data Loop、Memory、API 和 Vue 运营前端。
- `news`：腾讯新闻发现、抓取、清洗、缓存、去重、失败重试、标题特征、
  事件聚类和 NewsAgent 知识 API 客户端。
- `DemoMD`：业务场景、演示脚本、设计摘要和长期参考。

## 4. 当前业务链路

### Online Insight Loop

```text
Temporal Schedule / 业务事件
→ 按租户和窗口查询企业行为聚合数据
→ watermark 与数据质量校验
→ Python 指标、历史基线、热度和排行
→ 热点事件合并与生命周期
→ news_id 精确正文
→ Python 关联新闻召回与确定性重排
→ EvidencePacket 白名单
→ NativeHotNewsAnalysisRunner
→ PromptRegistry + InferencePort
→ HotNewsAnalysisReport + HotNewsAnalysisValidator
→ PostgreSQL/MinIO
→ 查询 API、SSE、运营决策与可选写作转交
```

主题受众预览是热点报告之后的可选分支：已完成分析 → 人工选择版本化主题组合 →
`AudiencePreviewService` → 企业聚合受众Port／隔离本地替身 → 身份、版本、水位和时效
校验 → 人工评估预览。该分支不调用LLM，不创建正式审批或投递任务；完整更新说明见
[`新闻运营架构与评测说明`](../agent/docs/news-operations-agent-design.md)。

每次 Workflow 只处理一个有界窗口，稳定幂等维度为：

```text
tenant_id + window_start + window_end + production_bundle_version
```

### Research & Writing Loop

```text
Research → 人工确认研究包 → Outline → 人工确认提纲
→ 分章节写作与组装 → Reviewer → 定点返工（最多三轮）
→ 人工确认终稿 → CMS 发布
```

本地链路使用 `StructuredAgentClient` 和 `LocalWritingInference`，每个阶段仍经过既有
Pydantic Schema、Checkpoint 与人工 Gate。

### Knowledge Loop

```text
爬虫 / 图文 / 视频文本化 / QA
→ KnowledgeDocument
→ NewsAgent 知识 API / ContentIngestService
→ PostgresKnowledgeStore
→ split_text 确定性切片
→ EmbeddingPort
→ 文档台账 + 版本化向量索引
→ TieredVectorKnowledgeSearchClient
→ 租户过滤、去重、业务重排和证据白名单
```

### 本地交互式 Text2SQL 演示

```text
热点 Agent 页面 → 网关身份与单小时窗口 → 冻结数仓格式/场景 YAML
→ NativeStructuredAgentClient / InferencePort → 受限 SqlAssistantIntent
→ Python compile_query → SQL AST/绑定参数护栏 → PostgreSQL 预览快照
→ SQL计划原子绑定到热点幂等run_key → Temporal热点 Workflow / Activity
→ run-scoped NewsMetricSource → claim/身份/冻结配置/SQL/哈希校验
→ PostgreSQL READ ONLY 候选查询 → 同小时UV/时长/基线补充
→ NewsMetricSnapshot → Python权威热度排序 → 正文/知识检索 → 模型分析/校验
→ 同一analysis_runs快照保存sql_tool_trace/榜单/分析 → 页面只读投影
```

用户确认要求：企业数仓不可提供时，使用合成热点新闻聚合数据库完成前后端全链路展示，
数据库格式 Markdown 冻结，场景提供可填写模板。v1 采用新闻维表、小时指标表、基线表
和唯一只读视图；不保留企业用户行为。文档交付只读权限并绑定代码 SHA-256；所有者仍能
重新授权，所以不是绝对不可修改，修改内容会阻断查询，合法演进必须另建版本。

2026-10-03 用户确认：Text2SQL 必须服务于热点 Agent，页面仅可视化同一条运行，不能
拆成独立查询业务。已将本地触发入口改为同一次热点 Workflow 的 SQL取数口；旧页面
重定向到热点页，独立 SQL preview/execute 路由不再挂载。本地模型仍是有限词表规则模拟，
SQL演示hot_score只挑选候选，最终热度沿用Python四分量公式。运行只选2026-10-03样本内
一个完整小时，禁止跨小时UV相加，缺失的基线UV/时长为null，不改变既有指标Schema。
已claim查询冻结权限与配置，Activity内SQL单次尝试，Temporal负责有界重试；结果checkpoint
复用，失败不回退旧内存数据。企业适配、真实跨小时去重与持续调度语义仍未验收。

完整文件调用链、配置与测试见
[`text2sql-local-demo.md`](../agent/docs/text2sql-local-demo.md)。

### Data / Configuration Loop

```text
运行结果、校验失败、低置信、检索异常和人工反馈
→ 脱敏 Feedback Case
→ Golden / Fresh bad case / High-risk Dataset
→ 当前、候选和上一实验 Bundle 回放
→ 确定性 Evaluation Gate
→ 人工批准或拒绝
→ 既有 Workflow 按批准结果继续激活，或显式回滚
```

当前不会自动训练大模型。可控迭代对象是 Prompt、模型路由、热度规则、检索重排参数、
Schema 和 Validator。

## 5. 已实现能力

- 新闻抓取、正文缓存、标题 NLP、图文入库、QA 入库和视频文本化降级链。
- OpenAI-compatible 推理/Embedding HTTP Adapter、YAML 配置和同 Port 本地替身。
- Python 文档分块、批量 Embedding、PostgreSQL 台账、版本更新和向量检索。
- 行为数据 Port、窗口聚合、基线、热度、排行、正文查询和证据重排。
- 热点结构化 Agent、指标/证据/因果校验、查询 API/SSE、Worker 与 Schedule。
- Text2SQL 候选生成与 SQL 安全护栏；权威结果只来自只读 SQL。
- 本地交互式 Text2SQL 页面、冻结 v1 数仓契约、场景 YAML 模板、真实 PostgreSQL 只读
  查询、预览/执行审计及有限重试。独立 E2E 工厂挂载，生产入口不暴露模拟 API。
- 写作任务状态机、人工节点、Checkpoint、Artifact、Reviewer 返工与 CMS Port。
- Feedback、标签二审、三层评测、候选 Bundle、人工晋升、激活与回滚。
- 短期/长期 Memory、作用域权限和确定性冲突解析。
- Vue 控制台、独立 Compose、本地热点全链路和完整回归测试。

“已实现”表示仓库代码和本地验证存在，不等于企业生产接口已经验收。

## 6. 状态、存储与版本

- PostgreSQL 是任务、运行、反馈、配置、知识文档和审计状态的事实源。
- MinIO/S3 保存不可变报告与评测 Artifact；Redis 只做缓存和事件推送。
- 向量索引是可重建投影，必须可由文档台账和 Embedding 版本重建。
- Production Bundle 使用 `model_profile_id`、`model_version`、Prompt、Schema、Validator、
  热度与重排规则版本形成不可变快照。
- Agent 调用审计使用 `model_request_id`；不得把请求 ID 当作业务幂等键。
- 数据库迁移 `20261002_0017` 会保留现有值、改名字段、改写候选 JSON，并重算内容哈希和
  幂等指纹。

## 7. Harness 与自主权边界

Harness 包括：明确输入契约、依赖注入、确定性计算、模型 Port、严格 Schema/Validator、
状态持久化、超时重试、可观测性、离线评测和人工 Gate。

Agent 可自主：选择已批准策略、有限工具和候选配置，生成解释与草稿，提出低风险候选。

Agent 不可自主：修改生产 Prompt/规则/模型，发布外部内容，扩大数据访问，跳过审批，保存
企业原始用户明细，或让外部文本指令改变系统行为。

## 8. 当前验证状态（2026-10-02）

- 写作服务完整 pytest：通过，6 个依赖型用例跳过。
- 爬虫 pytest：`121 passed, 1 deselected`。
- 前端 `npm run build`：TypeScript/Vue 类型检查与 Vite 生产构建通过。
- Alembic 已在已有本地数据库升级到 `20261002_0017`，旧运行列不存在。
- 一次性数据库已验证旧格式 Bundle/候选的升级、降级和再升级；字段值、JSON 资产名、内容
  哈希和幂等指纹均可正确往返。
- 原生 Compose 的 API、热点 Worker、写作 Worker 和 Outbox Worker 已用新镜像重启；
  `/ready` 返回 PostgreSQL、Redis、Temporal 全部 `ok`。
- 本地热点端到端触发完成：5 篇上榜、5 篇分析，Workflow 状态为 `completed`。
- 前端页面可在 `http://127.0.0.1:5174/hot-news` 使用本地热点模拟。

这些结论只覆盖本地隔离环境。汇报前若工作区继续变化，应重新执行测试，不机械沿用数字。

2026-10-03 本地 Text2SQL 里程碑验证（独立查询助手阶段，已由下方热点集成阶段取代）：

- 写作服务完整 pytest：`887 passed, 13 skipped`；显式启用本地 HTTP/数仓验收后另有
  `18 passed`，包含真实聚合、CTR核对、小时趋势、双租户与双用户隔离和结果复用。
- 前端 TypeScript/Vue 类型检查、Vite 构建和相关文件 ESLint 通过。
- 浏览器实测 `/sql-assistant`：视频点击率排行5行、财经小时趋势24行；修改查询范围后
  旧快照失效。默认样本窗口为北京时间2026-10-03全天，不依赖企业接口。
- 主演示租户与既有网关一致：`11111111-1111-4111-8111-111111111111`。
- 冻结文档 `agent/docs/sql-warehouse-schema-v1.md` 权限444，SHA-256
  `e4498800da447c72beb3be13ad375d9efa458605f8accf90af56b531c0aee3d0`。
- 企业模型/数仓验收仍未完成；失败尝试完整审计、审计保留策略、生产只读账号/配额、
  生产签名式审计和并发执行单飞仍是后续项，不属于本地展示已完成范围。

2026-10-03 热点 Agent SQL 工具集成里程碑：

- 当前后端完整回归 `914 passed, 13 skipped`；显式启用真实本地 PostgreSQL 和热点HTTP
  全链路验收 `27 passed`，包含同一run的SQL/候选/榜单/分析绑定、重复提交结果复用、
  租户隔离、关闭独立SQL入口与单小时取数映射。前端类型检查、构建、ESLint通过。
- 浏览器在 `/hot-news#query-tools` 一键实测：视频点击率前5候选，Python排行5篇、模型
  分析5篇，SQL query_id 与热点run_id均在同一详情可见；分析报告可展开。
- API `/ready` 健康，热点Worker已使用更新镜像运行；数据库历史运行与旧样本未清空。
- 冻结v1 Markdown未修改，内容SHA-256与444权限保持不变。企业端点、跨小时真实UV、
  调度语义、失败运行轨迹可视化与查询成本/审计保留策略仍是TODO。

## 9. 当前未完成项

- 企业推理/Embedding 真实端点的鉴权、JSON 模式、限流、超时、错误码、向量维度和数据出域
  合同测试。
- 企业行为、内容、基线和关联新闻 RPC Client 的真实 IDL/SDK 联调。
- 生产向量平台 Adapter、索引重建/删除/更正语义与规模压测。
- Gateway/IdP、企业密钥系统、网络治理、K8s 后端清单、长稳与故障恢复验证。
- CMS 真实契约与 exactly-once/at-least-once 发布语义确认。
- 前端写作与 Data Loop 页面更完整的本地全链路交互验收。

## 10. 当前优先级

1. 对企业模型与 Embedding 接口做合同测试，不更改业务 Schema。
2. 接入企业行为/内容 Adapter，并保持本地模拟可重复。
3. 完成生产向量索引 Adapter、恢复与容量验证。
4. 补齐写作/Data Loop 的前端到 Worker 本地验收。
5. 完成 Gateway/IdP、CMS、K8s、监控、告警与发布演练。

## 11. 协作偏好

- 先给当前真实链路和完成度，再给下一步，不把计划写成已完成。
- 指出文件、类、函数、上游、下游、配置、测试入口和剩余 TODO。
- 优先补断点和验证证据，不为视觉上的“完整”添加假能力。
- 尊重已有修改；尤其不要无故覆盖用户正在编辑的热点分析文件。

## 12. 持续对话 Agent（2026-10-04）

用户确认新增持续对话目标，分阶段实现；当前“持续”指同一会话多轮追问和持久历史，不是
无人值守后台自治。当前目录为 `agent/`（服务和前端）、`news/`（采集）；旧里程碑中的路径
是当时目录名，不能据此恢复旧框架连接。

第一阶段已实现并验收：

- `/chat` → `/api/v1/conversations` → tenant/user 绑定 → PostgreSQL 会话/幂等轮次 →
  `ConversationAgentService` → 原生结构化模型 Port → 严格只读工具 → Python 快照显示 →
  持久化终态/轨迹。会话不会自动提升到长期 Memory。
- 工具仅为能力说明、已完成热点列表/报告读取、新闻知识检索；热点 Text2SQL 仍归原有
  热点 Workflow，聊天不另建 SQL 链路、不创建新运行、不发布/审批/修改规则。
- 默认最近8个完成轮次、60秒总预算、3次工具调用；暂态推理/只读工具各至多重试一次。
  同请求幂等、同会话单飞；GET/下一轮请求恢复过期 processing，迟到提交不能覆盖终态。
- 本地 Compose 显式开启会话和租户共享新闻知识读取；生产配置默认都关闭。知识库不是
  逐用户企业文档 ACL，企业语料权限需先确认；会话本身按租户和用户双重隔离。
- Alembic 在已有本地数据库从0017升级到 `20261004_0018`，只新增会话与轮次表，未清空
  已有热点/写作历史。冻结SQL Schema v1的SHA-256和444权限未改变。
- 本次完整后端回归 `1119 passed, 24 skipped`；显式本地会话HTTP验收 `6 passed`，真实
  PostgreSQL并发/恢复验收 `5 passed`；前端 `108 passed`，ESLint、类型检查和构建通过。
- 浏览器实测消息 → 上一轮回顾 → 最近热点 → 第一条新闻追问；实际重启API后恢复同一
  会话历史并继续读取同一run。`/ready` 的PostgreSQL/Redis/Temporal均为ok。

只验证了隔离本地链路，不代表企业模型质量或生产长稳验收。当前工具轨迹在终态才提交；
硬崩溃/保存失败前未提交的轨迹可能丢失。后续需要企业合同与权限联调、逐工具检查点、
企业增量推理、生产锁/事务超时、数据保留治理及负载/故障演练。聊天内写作任务和长期记忆
需另行确认权限并复用现有Workflow/人工审批，不可默认开启。

当前可用的对话取数、模型配置与测试说明见
[聊天数仓与模型说明](../agent/docs/conversation-warehouse-model.md)；后续阶段见
[TODO](../TODO.md)。原 `conversation-agent.md` 已从工作区移除。

第二阶段安全 SSE 已实现并在隔离本地环境验收（2026-10-04）：

- `/chat` → `conversationsApi.sendStream` → UTF-8/SSE 解码 → 鉴权与请求占用 →
  `StreamingConversation.iter_events` → 同一个 `ConversationAgentService.run_turn` →
  原有模型/只读工具 → 结构与数字校验 → PostgreSQL 提交 → `answer_delta/done`。
  处理阶段和工具摘要实时发送；答复必须校验并保存后才分段，不透传模型原始 token。
- 默认空闲心跳10秒、每段128字符；前端流预算150秒，沿用模型 YAML 和幂等请求 UUID。
  断线取消并等待有界清理；同 UUID 终态重放不再次调用模型/工具，迟到 interrupted
  不能覆盖已完成结果。未提交进度不是持久检查点，硬退出仍靠既有轮次过期恢复。
- 当前部署镜像完整后端回归 `1135 passed, 31 skipped`，其中显式本地会话 HTTP/流式
  验收另跑 `13 passed`；前端 `153 passed`，ESLint、类型检查和构建通过。
- 浏览器实测最近热点 → 第一条新闻追问 → 刷新恢复，两轮引用同一热点运行；
  `/ready` 的 PostgreSQL/Redis/Temporal 均为 ok，既有数据和冻结 SQL Schema v1 未改。
- 当前不是企业模型边生成边输出 token，也没有 token/cursor 级断点续传；企业增量 Port、
  Gateway SSE/超时合同、并发与长稳验收仍未完成。详见 `agent/docs/conversation-streaming.md`。

第三阶段聊天驱动取数与兼容模型模板（2026-10-04）：

用户确认热点 Agent 应执行“输入问题 → 查询数仓 → 模型分析 → 返回答复”，不只读取旧报告；
模型通过兼容 Chat Completions 的 URL 服务调用，可使用批准的企业内部或外部模型。用户
暂不提供端点/模型/密钥，先完成代码和 YAML 模板。应用层继续由 Python 独立实现，不引入
FastGPT 或其他 Agent 框架。

- `/chat` → 网关身份/权限 → `ConversationAgentService.run_turn` → 结构化计划 →
  `ConversationTools.execute_hot_news_query` → `LocalConversationHotNewsQuery` → 与热点
  工作台共享的 `execute_local_hot_news_query` → Text2SQL 意图/SQL护栏/只读取数 → 原有
  Temporal热点Workflow → Python权威指标/排行 → 模型分析/校验 → 同run工具结果 →
  会话模型再次生成解释 → PostgreSQL保存 → SSE答复。没有第二套SQL执行器或内部HTTP自调用。
- 新查询仅限显式启用的本地E2E租户和 `hot-news:admin` 权限；READ用户仍只能读已有结果。
  服务端固定 `news-ranking` 场景和单小时合成窗口，不允许模型传SQL/身份/日期/场景。
  查询写审计/运行，不按普通只读工具自动重试；同UUID终态重放不重复调用。
- 断线只取消聊天等待，不取消已启动的持久热点Workflow；尽可能保存workflow/query引用。
  未确认完成不报成功、不回退旧报告。逐工具持久检查点和待完成Workflow恢复工具仍是TODO。
- 新增 `agent/deploy/model-runtime.compatible.example.yml` 和显式Compose Overlay；真实
  配置只声明密钥环境变量名，API和热点Worker必须使用同一模型路由、Prompt和Bundle。
  示例使用新Compose项目隔离数据卷，保留旧历史/Bundle，不自动改旧生产快照。
  当前模板是“真实推理接口 + 合成数仓 + 本地Embedding”，只含本次查询场景，不表示
  写作/Data Loop或企业Embedding已联调。缺密钥失败关闭，不静默回退规则模型。
- 页面通过受鉴权的 `/conversations/runtime` 显示实际provider、获准工具和固定样本窗口，
  不返回URL/密钥。SQL按指定指标选候选，最终榜单仍按Python权威热度排序，页面明确区别。
- 最终部署镜像回归 `1148 passed, 34 skipped`；显式本地会话/查询/SSE HTTP验收
  `16 passed`；前端 `155 passed`，ESLint、类型检查和构建通过。HTTP MockTransport
  验证兼容协议与工具结果复入；Compose公开占位校验确认环境合并和数据卷隔离。
- 浏览器实测新SQL查询 → 同run热点分析 → 追问第一条 → 刷新恢复两轮历史。API和热点Worker
  已更新运行，`/ready` 三项ok；既有数据未清空，冻结SQL Schema v1的SHA-256和444权限未变。

默认部署仍为本地规则模型Port，尚未调用真实模型端点；以上不代表企业模型质量或生产长稳
验收。后续需用户提供完整接口URL、模型名和密钥环境变量名（不在聊天中发送密钥），再做
真实服务合同/质量验收及企业数仓/权限联调。完整调用链、模板配置和测试入口见
`agent/docs/conversation-warehouse-model.md`。

## 13. 腾讯新闻内部榜单双来源查询目标（2026-10-04）

用户确认统一查询需要同时支持腾讯新闻已有内部榜单与NewsAgent依据聚合指标计算的榜单，
并明确区分来源。企业原榜必须保留原始排名、分数和快照，不能被项目热度重排覆盖；项目
计算榜必须说明指标/热度规则版本及候选覆盖范围。当前模拟v1仅有新闻维表、小时指标和
基线，无企业board_id/source_rank快照字段；真实内部表字典和接口尚待提供。

参考清单、已有模拟表格式、双来源建议合同及实施步骤已整理到
`agent/docs/tencent-news-ranking-warehouse-guide.md`。该文档是接入草案，不表示统一API、
企业原榜Adapter或生产取数已实现；冻结v1文件和现有运行链路未因该草案改变。

## 14. 运营审批驱动的 DataLoop（2026-10-04）

用户确认本工作窗口长期聚焦完善DataLoop：运营团队审批新闻推送建议，将决策与反馈沉淀
为运营策略集，重点编排架构为Temporal。先研究新闻推送策略并保存为Markdown，再持续
建设稳定链路；该方向不授予自动策略上线、模型训练或真实新闻推送权限。

研究目录、策略卡模板、审批契约、当前代码调用链及实施/验收清单已保存在
[`agent/docs/data-loop/README.md`](../agent/docs/data-loop/README.md)。其中新策略注册表、
推送审批Workflow和契约是待实施设计，不能当作已运行能力。

本次核对：现有DataLoop负责分析Bundle评测/人工晋升；热点运营决策仅驳回/修正生成
FeedbackCase，采纳/延后只存决定。推送Schema/生成器/Validator与单测已有，但未接API、
审批UI、专属Workflow和真实投递；个人Memory不等于团队运营策略库。

优先断点包括：原生DataLoop E2E driver缺失、Worker装配与UI契约、迟到审批回补、完整
审批版本/哈希绑定、四类案例回流、团队策略审核/激活、专属策略评测及长稳恢复。当前工作区
专项10模块78测试通过，只验证本地既有契约/领域/Workflow逻辑，不代表真实Temporal
端到端、企业接口或运营效果验收。完整配置、依赖、测试入口及TODO见上述文档集。

## 15. 会话上下文按长度压缩（2026-10-06）

用户确认会话记忆应达到足够上下文长度后压缩，不再固定仅取最近8轮。代码已改为按输入
JSON字符预算加载未压缩的完成历史，达到预算后通过独立原生 conversation_memory 场景
合并较早消息与已有摘要，保留按长度选择的近期完整消息。摘要与水位保存在同会话的
PostgreSQL JSONB字段，真实工具引用由Python独立提取，不从模型摘要扩大工具权限。
默认输入JSON预算32000字符、近期目标12000字符、摘要4000字符、引用另有同等预算；
这不是精确token预算。原始消息/工具台账不因压缩删除，也不自动晋升跨会话长期Memory。

新增迁移20261006_0019、版本化摘要Prompt和请求/水位并发保护。代码尚未更新到运行中的
演示或生产实例，需先迁移数据库、更新模型YAML和API镜像后重启。调用链、配置、测试和
部署步骤见 `agent/docs/conversation-memory.md`。本地确定性摘要替身只验证链路；企业
语义摘要质量、精确token计量与生产治理/发布仍需验收。

## 16. 个人运营策略积累与板块主线评测（2026-10-10）

用户确认运营策略由每个运营人员独立积累，审核后才可推广共享；各板块有不同职责与
判断依据。后续重点是形成可参考的分板块运营数据，并评测同一新闻主线随事实发展、
重复、过期和更正而改变判断的能力。该目标不授予自动共享、策略上线或真实投递权限。

旧版48例、24事件组、12类OPS策略素材仍保存在
`agent/evaluation/datasets/operations_synthetic_v1/`。新版
`agent/evaluation/datasets/operations_evolution_v2/` 覆盖国内、国际、财经、科技、社会、
本地、体育、娱乐、文化、教育、健康和天气12板块；12个模拟个人作用域、36条虚构主线、
216个新闻观察快照、432条对照运营候选。全部为合成、待人工复核且未执行的数据。

- 版本化 catalog → 严格 Schema → 截至观察时点的累计事实 → Python聚合指标/热度 →
  案例账本、分板块案例册、无答案输入与哈希清单。同主线各阶段保持同一split，
  development/validation/test各144条运营判断，分析投影去重后各72个快照。
- 新增原生Port运营审核runner、固定分母确定性评分、板块/人员/阶段/主线分组报告、
  当前与上一版本双基线离线门禁和逐案例检查点续跑。未知/未来证据、身份错配、
  不完整输出与分组退化不能被总体分数掩盖；没有更改生产Prompt或Bundle。
- 20模块本地回归 `324 passed, 2 skipped`，新增主线测试39项通过；两项真实Temporal
  分析回放未启用专用环境。旧包和新版49个产物重建/哈希校验通过，分析loader读取
  72个test快照成功。没有调用企业模型，不代表真实模型质量或投递收益验收。

六阶段主线是合成覆盖模板，不能代表真实新闻分布；改稿评分目前只检查修改字段。
数据尚未进入正式run/反馈/标签二审/冻结账本，作用域合同也不能代替线上个人权限。
待补运营人员校准、真实匿名聚合/非模板主线、正式导入与迟到标签回补、同板块多运营
隔离、个人→共享策略的审核/撤销/版本注册，以及批准策略参与下次建议。

完整调用链、依赖、配置、命令、测试与TODO见
[`agent/docs/data-loop/operations-evolution-dataset.md`](../agent/docs/data-loop/operations-evolution-dataset.md)。

## 17. 统一对话能力分阶段接入（2026-10-13 用户确认）

用户确认沿用 M1 的回执卡片形态继续 M2；写作轮询保持 3 秒，评测运行卡改为 15 秒。
对话作为统一入口，新增能力必须薄适配现有 Service/Workflow；长任务只等待启动受理，
对话断线不取消业务 Workflow。落地回执由 Python 绑定真实 ID、版本、Gate 与白名单链接，
确认参数属于模型建议，写操作经本轮用户确认。审批、标签二审、长期记忆晋升、配置
激活/回滚和发布仍由现有工作台人工执行，不授予对话 Agent 自批权限。

用户确认 M4 本轮只支持已注册模型／Prompt 预设候选，重排调参另列 TODO。当前 Bundle
仅绑定规则版本，分析输入回放不能评测完整检索或重排变化；不得伪造数字参数候选或
把其他资产变化当作重排效果。未来需版本化真实重排资产与完整证据重放后再接入。

三位正式人工合同评审仍待回填，合成标签演员和独立 Agent 技术审查不能代替人工审核。
统一对话能力的验收状态与未完成范围以 [TODO 最新账本](../TODO.md)为准；
当前实现见 [conversation 模块](../agent/app/modules/conversation/)。
原 `conversation-capability-hooks.md` 已从工作区移除。

## 18. 腾讯新闻的数据驱动写作定位（2026-10-15 用户确认）

用户明确业务背景是腾讯新闻，写作能力应产出有数据依据的新闻分析内容，而非以文学创作
为目标。后续研究、写作、审校和多智能体价值说明应围绕可追溯的数据、新闻事实与证据，
服务企业实际业务，并评估其成稿效率、事实与数字质量以及人工使用价值。

继续保持 Python/SQL 计算权威数字、模型解释与表达、必要聚合快照留存以及人工审批和
独立发布边界。本次确认是业务定位，不代表正文数字逐项核验、企业数据/模型联调或企业
生产效果验收已经完成，也不授予新增数据访问、自动发布或生产配置变更权限。

## 19. 第一阶段优先服务新闻运营（2026-10-15 用户确认）

用户确认第一阶段优先服务“新闻运营：看数、选题与跟进建议”。后续架构与企业提效分析
应围绕该岗位的实际任务、可用交付及人工使用成本展开，数据驱动写作作为相关能力继续保留。

事件跟进研判卡、试点流程及量化验收门槛仍是待验证的设计建议；本次岗位选择不表示用户
已确认这些具体方案，不授予自动推送、发布或生产配置变更权限。

2026-10-16 用户要求按最新主题兴趣与受众接口背景同步本窗口的架构、企业价值与评测说明。
该岗位场景增加“主题受众预览与人工推送决策准备”；原事件跟进与数据驱动写作目标继续
保留。组合研判卡、事件事实增量、持久审批与投递回执仍按设计建议或待实现分别说明，
不得把接口预览等同于已完成自主圈人或自动推送。

## 20. 腾讯新闻主题兴趣标签与受众接入

2026-10-16 用户确认：腾讯新闻根据每个用户访问的新闻内容及主题生成兴趣标签，并据标签
进行新闻推送；内部推荐算法架构不属于本项目建设范围。主题以科技、财经、AI、军事、教育
等为主，支持细分，例如科技下的自然。用户要求将这一企业业务背景落实为执行计划与接口，
并查阅公开推送技术作为设计参考。

本次接入范围是新闻运营工作台中的主题目录与受众预估。企业标签/画像系统继续生成逐用户
兴趣与通知资格，NewsAgent按已有租户热点报告、标签组合、版本和窗口请求去重聚合受众，
不获取或本地保存接收者名单、设备Token和企业原始浏览明细。两级目录以稳定ID与版本配置，
本地扩展标签为草案，不宣称是腾讯新闻完整内部分类。真实目录须由企业批准。

新增能力保持预览性质，必须人工决策，不执行真实推送、不自动批准或激活策略。实际企业
接口地址、标签ID、鉴权、画像和通知资格口径，以及正式审批/投递仍需合同联调。实现调用链、
配置、验证状态与TODO见 `agent/docs/enterprise-audience-implementation.md`。

## 21. 本地行为标签与多版本数仓合并（2026-10-16 用户确认）

用户要求根据模拟数仓构建详细、多样、贴近新闻消费场景的本地行为标签，并实际用于热点
Agent 检索；随后确认数仓多个版本可以合并。当前数仓没有可还原个人的访问明细，因而
采用“内容主题标签、新闻小时行为标签、独立合成事件与画像”三层，不能将合成个人行为
描述为真实企业用户数据或聚合数据的唯一还原。只有合成明细允许落入本地派生包，企业
原始个人明细的禁止落盘约束保持有效。

本地目录扩展为 12 个主题与 72 个细分草案。九个来源统一装载：v7/v8 冻结 JSONL 与
classic-v1、enterprise-v1/v2、public-headlines-v3、timeline-v4/v5、corpus-v6 的有界生成配置。
真实新闻 ID 去重并保留来源，合成新闻按场景隔离；不同模拟世界、指标定义与 UV 不相加。
最高权威 v8 的撤回/受限身份对旧真实源统一排除；发布时间晚于指标的观察剔除。

可选本地 runtime 按窗口冻结每日合成兴趣快照，只对原相关性门槛通过后的相关新闻加入
有界共享主题权重，不更改权威热点分数。运行摘要绑定包、画像时点、cohort 与策略版本，
排序证据进入报告；API 不返回个人明细。配置默认关闭、生产环境拒绝合成包，不授予真实
推送或自动发布权限。动态 Schedule 的画像冻结尚未接入，当前该组合明确拒绝。

具体构建入口、只读 API/CLI、调用链、功能验证与企业 TODO 见
`agent/docs/local-behavior-tags.md`。正式本地派生包位于
`var/behavior-tags/local-merged-v1-20261016`（2026-10-17 目录重构时由 `output/` 迁至 `var/` 运行数据根，
manifest/policy 仍入库），冻结输入保持不变，未重启已有实例。

## 22. 单新闻访问人群聚合与热点分析接入（2026-10-17）

用户要求实现“热点新闻 → 该新闻访问用户主要标签 → 推送主题分析”，并允许补充模拟数仓细节。
已新增 `agent/app/reader_profiles`：从已有独立合成事件构建去重新闻访客及访问前长期/短期兴趣，
再生成仅含聚合的不可变包。个人行为仍限合成数据，企业原始个人明细不落入本地。
正式包位于 `var/reader-profiles/local-readers-v1-20261017`（2026-10-17 由 `output/` 迁至 `var/`），包含 52,718 条新闻小时画像、
1,488 个租户窗口覆盖记录与 2,474 条内容绑定；源为 308,091 条事件和 1,200 个合成身份。

在线以 Python Port 接本地只读 SQLite 或企业固定 HTTPS 聚合服务；每批最多 20 篇、
Top100 最多五批，使用有界并发、缓存、全局期限与严格来源/窗口/目录/修订校验。
原热点 Enrichment、模型输入、推送策略输入、快照、榜单 API 和页面组件已接入；
新 Prompt 为独立 `native-e2e-prompt-v3-reader-profiles`，旧资产不改、既有 Bundle 不自动升级。
原热度计算保持不变，标签方向仅供人工审核，不执行真实推送。

真实本地聚合包到模型的两条公开正文 smoke 已通过，但每篇仅 1 位合成读者，不能作为推荐质量或企业规模验收。
企业真实聚合接口、模型质量、生产负载、动态 Schedule 的窗口画像冻结仍待完成。
Compose 新文件已配置与聚合包兼容的 corpus-v6 数据与场景；2026-09-22 北京时间10点至11点的Top100中83条非空，
Top1的5位合成读者已通过 Service/Enrichment/InputBuilder 定点验证。
整栈页面尚待启动验收，v8 尚未注册完整在线 SQL 场景。
当前功能、字段、测试与完整调用链见 `agent/docs/news-reader-profile-integration.md` 与
`agent/docs/news-reader-profile-warehouse.md`；未重启已有实例。

## 23. 目录架构重构（2026-10-17 用户确认执行）

按"业务模块、共享基础设施、启动装配"重组目录，四批完成并保持原有运行链路与业务契约：

- `agent/app/modules/`：`hot_news`、`writing`、`data_loop`、`memory`、`conversation`、`knowledge`、
  `audience`、`behavior_tags`、`reader_profiles`、`data_analysis` 各自持有 contracts/persistence/
  application/adapters/workflows；旧导入路径保留薄 shim/代理，内部代码统一使用新路径。
- `agent/app/bootstrap/`：API lifespan、写作/热点 Worker 运行时与热点依赖工厂；`app/main.py` 为薄入口。
- `agent/simulation/`：本地模拟装配与模拟数仓；`examples/` 仅保留演示脚本。
- 仓库根 `deploy/`：Compose、Docker、K8s（含独立分析服务清单）、监控与模拟 CMS；`agent/deploy`
  为兼容链接。e2e 镜像构建上下文改为仓库根。
- 运行数据根 `var/`：行为标签/读者画像运行包、数仓数据包、只读 BGE 知识快照与增量爬虫缓存；
  manifest/合同文件仍入库，批量数据忽略。数仓目录可用 `WAREHOUSE_DATASET_DIR` 覆盖。
- `app/evaluation` 的确定性评测核心迁入 `data_loop/evaluation`；外层 `evaluation/` 保留离线命令与样本。
- 旧 HTML 控制台（review/research-package/console）已由 Vue 工作台覆盖并移除。

验收：后端 pytest 4541 passed/107 skipped（含删除 2 个控制台测试），爬虫 226 passed；
未新增企业接口合同或规模验收，本重构不替代这些后续验证。

## 24. 运行与评测的逐问题可观测交付（2026-10-18 用户要求）

用户要求后续运行项目时，能够看到每一个问题的实际可观测执行链路。每次协助启动或执行
评测，应先核实本次实际 API、相关 Worker 和采集后端的版本、遥测启用、采样及查回状态，
再按问题/轮次提供真实链路证据，不能只给总体看板或最终回答。

- 按需关联 conversation_id、turn_id、request_id、OTel TraceID，以及实际产生的 query/run/
  workflow/job/model_request_id；业务字段 trace_id 与 OTel TraceID 必须区分。
- 展示本次实际经过的入口、会话/工具、SQL/检索/计算、模型及校验、持久化和响应阶段，附
  状态、耗时、重试/等待审批情况及可打开的 Trace/业务详情或脱敏证据。未经过的阶段不补画。
- 失败时定位最早失败节点；链路未采集、未采样、尚未结束、导出失败或已过期时明确标注，
  不将缺少遥测视为成功，不把已受理的异步任务视为完成。异步后续按真实 ID 继续关联。
- 本地逐问题复现使用全量采样，并核实 ParentBased 对上游采样决定的继承；SDK 采样配置
  不保证采集后端零丢失。临时存储停栈前保存必要的脱敏证据，不记录 Prompt、正文、回答
  原文、密钥或企业用户行为明细到通用遥测。

当前实现、配置、查询方法及测试入口见 `agent/docs/observability.md`。应用遥测默认关闭，
独立观测栈不会自动更新业务进程；聊天页已有工具轨迹与模型调用编号，逐问题自动汇总
完整分布式 Trace 的界面仍待实现。上述内容是后续运行交付要求，不表示本轮已启动栈或
完成真实逐问题端到端验收。

## 25. 内容运营工作重心与算法协作背景（2026-10-19 用户确认）

本窗口沿用“三个板块、六名运营、一名主管”的业务仿真背景，重点从运营实际遇到的
问题出发，提出解决思路。用户补充确认：运营团队日常工作主要是内容优化，包括视频
剪辑等工作；团队偶尔与算法团队对接，因此场景需适度纳入搜索、推荐与推送分发的
算法协作思考，不能将运营的主要职责描述为搜索推荐算法研发。

后续问题与方案应围绕内容制作和优化展开，并结合既有多人员 Memory、交接、反馈
归因和 Data Loop 讨论。内部推荐算法架构继续不属于本项目建设范围；具体岗位分工、
剪辑工具接入、算法协作接口、指标合同及实验方案尚待明确。该背景确认不代表自动
剪辑、搜索推荐接入、算法训练、真实投递或业务收益已经实现，也不新增这些操作权限。

## 26. 真实内容 v8 原地扩充与热点 Agent 接入（2026-10-19 用户确认）

用户确认在既有 v8 数仓上原地扩充真实内容，新增内容不得是模拟数据，并同时接入热点 Agent。
本次新增 7,459 篇 v2 公开缓存新闻（含标题、正文、URL、发布时间和内容哈希），使真实新闻
总数达到 8,120 篇；新增 `news_content_feature` 保存标题/正文字数、段落、句子、数字 token
和确定性 CJK 关键词。7,459 篇新文章没有企业行为数据，因此没有曝光、点击、UV、时长、基线、
榜单或生命周期行，只存在于 `dim_news` 和特征表；661 篇既有文章继续使用明确标记的模拟行为
指标。不得把内容-only 文章描述为热点候选，也不得用 0 或模拟值补齐。

v8 数据包已原地重建，摘要为
`e139126c4bdc49f885aa6f08681de48f7da4d445de85442207a77c3706d7c904`；扩展清单为
`catalog-extension-v2.json`（`6b7e65df3590ce84974adcf77d4505bc494e05ecbd5a0b1e7c5eec1732ad766f`），
正文仍留在被忽略的公开缓存和 `var/` 数据包中，不进入 Git。旧包备份在仓库外
`NewsAgent-legacy-backup/real-news-v8-pre-copyfix-20261019` 等目录。

新增 Agent profile `real-news-v8` 和冻结契约 `news-warehouse-v7`：Agent 侧导入 8,120 篇
真实内容，仅 661 篇进入 `dw.news_behavior_aggregate`；缺失基线保留为空，视图使用 LEFT JOIN。
场景 `deploy/text2sql-scenes.real-news-v8.yml` 支持 7 个演示栏目；独立 Compose 叠加文件
`deploy/docker-compose.real-news-v8-agent.yml` 使用新项目名和新卷，API `38000`、Postgres
`35432`、Temporal `37233`、MinIO `39000/39001`，不改变默认 native-e2e 演示。

已验证：真实内容包 61 项定向测试；完整后端回归（含 BGE 可选依赖测试）`4515 passed,
143 skipped`；独立 PostgreSQL 导入 16,240 条新闻维、13 张表、SQL 检查通过；Agent profile 初始化 26 秒，`dim_news=16,240`、`news_metric_hourly=140,724`、
内容-only 示例在榜单视图为 0 行；热点运行在 `2026-10-13 20:00` 完成 5 篇排名和分析，
正文使用真实公开内容而非合成占位；`/api/v1/conversations/runtime` 返回 661 指标文章和
7,459 内容-only 文章。企业真实行为指标、内容-only 的真实榜单资格、企业聚合接口和生产
负载/权限仍未验收；不能让本次本地结果替代这些后续验证。

## 27. 企业数仓技术背景（2026-10-23 用户确认）

用户确认企业数仓使用 **TDSQL-A、PostgreSQL 兼容**。后续企业聚合数据接入沿 PostgreSQL
方言设计，优先复用现有 SQL 编译、AST 校验与 `SqlWarehouseClient` Port；实际采用协议直连
还是企业 SQL 网关/SDK，以内部批准的连接方式为准。

该确认是企业技术背景，不代表本地已经实现或验收 TDSQL-A Adapter。内核版本、实际库表、
指标口径、身份权限、水位/修订及快照一致性仍需映射和验证；本地 PostgreSQL 模拟数据继续
与企业数据区分，不将当前 13 表结构或合成行为数值描述为企业真实 DDL 或指标。

接入与评测准备见 [企业 TDSQL 数仓合同](../agent/docs/enterprise-tdsql-warehouse-contract.md)，
逐字段参考见 [当前本地结构字典](../agent/docs/enterprise-warehouse-current-fields.md)。

## 28. 本地统一新闻内容存储与冻结素材（2026-10-23 用户确认并完成）

用户要求先统一企业新闻内容存储、导入此前获取的新闻，再制作评测集；确认本次落地为
**本地 PostgreSQL，按 TDSQL-A / PostgreSQL 兼容方向设计**。本次复用现有 native-e2e
PostgreSQL 16 实例的 `127.0.0.1:25432`，新建独立数据库
`newsagent_unified_news_20261023`，没有改动现有应用库或切换在线 Worker 内容源。

- 新增 Python 原生 `app/modules/news_storage`，通过四张内容版本、来源证据、冻结发布、
  发布选定项表及 `news_content_selected` 视图统一读取。发布时间为 TIMESTAMPTZ，扩展
  字段为 JSONB；未使用向量扩展或企业专属 DDL。本地验收不等于企业 TDSQL-A 验收。
- 租户 `public-news-local`，冻结版本 `news-0c74530f2864db20a73d7a96`：
  **21,435 个新闻 ID、21,828 个内容版本、22,369 条来源记录**。
  选定内容为 **8,441 篇缓存正文、11,794 个仅有历史片段的新闻、1,200 条仅标题记录**。
  来源覆盖 8,000 篇 v2、541 篇 v1、441 篇本地正文、12,187 个历史片段 ID 及 1,200 条标题；
  来源间重叠已按新闻身份和内容哈希处理，历史片段的 36,379 条原始切片仍保留。
- 不从片段拼接伪造全文，不推测缺失标题/URL/时间；保留 article/video 类型、短正文和
  元数据待核查标记。缓存正文均未经过人工完整性认证；不同新闻 ID 的相同正文不合并。
- 冻结包在 `var/news-storage/unified-public-news-20261023/`，清单包含文件校验和；
  现成素材在 `var/news-storage/exports/20261023/body-candidates.jsonl`，包含 8,144 篇
  正文（过滤不足 100 字的 290 篇及元数据待核查的 7 篇），保留版本、来源和质量标记。
  这只是待抽样、待标注的公开内容素材，不是完成的评测集，也不包含企业行为数据。
- 已通过 66 项定向测试、真实 PostgreSQL 单事务导入、全量 21,435 条选定记录及来源
  读回比对、重复导入幂等、固定租户/版本读取及 NewsContent Port 冒烟检查。
  验收证据在 `output/news-storage-unification-20261023/report.json`。

代码入口 `agent/tools/unify_news_storage.py`；连接必须显式设置
`UNIFIED_NEWS_DATABASE_URL`，不读取 `.env`。后续评测须固定 tenant/release/hash；
业务端接入使用 `ReleaseNewsContentRepository`，当前尚未替换 `tier_lifecycle_news` 的
在线有效内容及索引激活链。角色授权/RLS、企业实例兼容性、增量接入调度与线上投影另行实现。
没有调用模型、Embedding 或制作评测题/金标。使用方法与逐字段说明见
[统一新闻存储说明](../agent/docs/unified-news-storage.md)。

## 29. 统一新闻内容、主题标签与旧聚合查询串联（2026-10-23 用户确认并完成）

用户确认今天需将已有 tag 与统一新闻存储串联，而非只保留分离的数据包。
本次在同一个本地独立 PostgreSQL database 中新增标签发布、新闻标签、主题关系三表，
及 `news_content_tagged_selected` 联合视图；原正文 release 和全部原字段保持不变。

- 复用已有 `news-behavior-topic-rules-v1` 关键词规则和 `news-behavior-topics-demo-v1`
  目录（12 个根主题、72 个细分，draft），对统一原文重算，禁止模拟栏目兜底。
  标签属于规则推断，不是人工评测金标，也不代表腾讯官方主题分类。
- 标签 release 为 `tags-33c4054fd01bd98cd5ef2826`，绑定正文 release
  `news-0c74530f2864db20a73d7a96`。21,435 条选定新闻均有明确标签状态：
  7,566 条命中主题、2,068 条未分类、11,794 条因只有片段而输入不足、7 条元数据待复核。
  共 35,888 条新闻—主题关系；8,441 篇缓存正文中 7,212 篇命中主题、1,222 篇未分类、7 篇待复核。
- 旧内容标签与当前公开内容精确相交 781 个 ID（661 篇正文、120 条标题）。标题、正文
  哈希、规则版本及输出均一致的 670 条标记 `legacy_verified`；模拟栏目标签只作为审计
  来源，不复制为当前主题。新正文 7,780 篇按相同规则计算，缺失输入不拼凑、不推测标签。
- `unify_news_storage.py` 增加 `build-tags/verify-tags/import-tags/topics/list/behavior`，
  `get/summary/export` 可显式传入 `--tag-release-id`；返回中文主题名、规则命中位置、
  推断标记及版本。按主题过滤在分页前执行，无隐式 latest、无跨租户回退。
- 旧行为标签保留在原本地模拟包，按明确原租户、news_id、正文哈希及时间窗口只读查询
  新闻小时聚合，保留来源/场景/指标口径并标记 `synthetic`，不跨小时或跨来源累加 UV。
  无旧内容引用或无窗口数据时返回 unavailable，不补零；不读取个人事件或画像行。
- 已导出 `var/news-storage/exports/20261023/all-news-with-tags.jsonl`（21,435 条）和
  `body-candidates-with-tags.jsonl`（8,144 条）。原候选文件保留；新文件除增加 tags 外，
  每条原始字段与之前候选逐项一致。标签冻结包位于
  `var/news-storage/tags/public-news-topics-20261023/`。

验收：168 项定向测试通过；真实 PostgreSQL 全量 21,435 条联合读回、原内容不变、重复
标签导入幂等、主题目录计数与分页匹配、租户边界、旧模拟聚合可查询及缺数据明确返回均
通过。证据见 `output/news-storage-unification-20261023/tag-integration-report.json`。
完整字段与调用链仍见统一存储说明。没有切换在线 Worker、调用模型/Embedding 或发布
生产配置；企业权限/兼容验收、线上生命周期接入及人工主题质量评测仍待完成。

## 30. 新闻受众聚合正式落入 PostgreSQL（2026-10-23 用户确认并完成）

用户要求将已有受众聚合放入明确的表，能够直接编写 SQL 查询。本次在同一独立本地库
`127.0.0.1:25432/newsagent_unified_news_20261023` 创建 `news_audience` Schema，
没有修改正文/标签表或切换在线 Worker。早先 `news_audience_proposal_v1` 仍只是经过
事务回滚验证的设计稿，实际可查对象以 `news_audience` 为准。

- 来源为 `var/reader-profiles/local-readers-v1-20261017/` 已冻结、匿名聚合包；发布身份
  使用 manifest 原始字节 SHA：`03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5`。
  全部标记 `synthetic`，不代表企业真实访问人群，不导入个人事件或逐用户兴趣画像。
- 7 表包括 `package_release`、`content_binding`、`unified_news_binding`、
  `reader_snapshot`、`topic_dictionary`、`news_reader_population`、`news_reader_topic`。
  导入 84 个目录主题、2,474 条源新闻资格、1,488 个租户覆盖窗口；52,718 条新闻窗口
  受众记录拆为 105,436 条长短期人口行、1,282,053 条保留主题计数，不能把这些行数称为用户数。
- 视图 `v_news_reader_population_status` 与 `v_news_reader_topic_metrics` 暴露分母、
  中文主题名、原精度占比、同口径基准/lift、来源、质量与截断信息。源每期限最多20个
  主题，缺失主题不等于0；长期720h和短期24h分别计算，不能跨小时或标签直接相加UV。
- `knowledge_as_of` 明确为合成包冻结生成时点；`quality_basis` 是本地导入合同校验，
  不是企业历史事件到达时间或生产质量验收。覆盖范围为北京时间9月13日20点至10月14日20点。
  已知源新闻+完整覆盖窗口无访客行可返回empty/0；未绑定或窗口未覆盖不可补零。
- 显式 `unified_news_binding` 以 news_id+非空正文SHA关联旧业务UUID租户与
  `public-news-local` 内容域，共1,266条业务映射、661个不同公开新闻。没有把租户
  强制等同，也没有给新增内容编造行为/受众。正文仍使用原冻结release。

实现入口 `agent/tools/import_reader_audience.py`，命令 `import/bind-content/query`，
显式配置 `AUDIENCE_DATABASE_URL`，不读取 `.env`。`ReaderWarehouseSource` 校验来源SHA、
严格Reader合同、绑定、窗口和版本，流式COPY后全量按确定顺序读回摘要，再单事务提交。
重复导入检查已冻结投影，不追加或覆盖；SQL与CLI均显式绑定来源/租户/窗口。

验收：77项相关测试通过；全量投影读回、重复导入幂等、重复正文绑定、5段交付SQL、
来源Client快照/人数对照及10项真实PG集成检查通过。示例AI新闻`20260718A03B9V00`，
北京时间2026-09-24 22点至23点，有6位模拟详情读者，不能当作企业指标。
可执行SQL在 `agent/docs/sql/news-audience-local-query.sql`，说明在统一存储文档第11节，
结果与验收报告在 `output/news-storage-unification-20261023/audience-*.json`。
正文、内容标签与受众库的集中检索入口为
[本地统一数据库库表与查询手册](../agent/docs/news-storage-database-reference.md)，
包含实际连接、14张表/138个字段、4个视图、版本身份、数据路径及可执行查询示例。
企业真实聚合映射、线上SQL Reader Port装配、持续更新作业、权限/RLS和TDSQL-A验收仍待完成。

## 31. 新闻正文对象存储背景（2026-10-23 用户补充）

用户说明新闻内容库使用对象存储；后续企业接入按正文对象+版本/元数据台账设计，具体
产品、对象键与内容服务接口尚未提供。当前本地统一正文仍在PostgreSQL，本次没有迁到
对象存储。项目已有S3/MinIO Artifact能力不等于已接通新闻正文对象读取。
用户询问ES是否需要接入，尚未确认实施；当前没有线上ES/BM25客户端。已知新闻ID的正文
精确读取不依赖ES，相关内容的关键词检索可另评估ES与现有向量检索的组合。

## 32. 新闻日增规模与向量入库背景（2026-10-24 用户确认）

用户补充其所述腾讯新闻业务接入范围约 **8,000 条原始新闻/日**，其中存在无正文、
正文过短和纯视频条目。这是用户提供的业务规模口径，不是过滤后的有效入库量、切片向量量，
也不是对腾讯新闻全平台规模的独立核验；不得与本地一次性8,000篇公开缓存混为一谈。

用户希望先筛选适合文本检索的新闻再写向量库，并针对持续累积的历史新闻采用分层检索。
后续设计和简历应区分内容可用性筛选、索引生命周期、检索资源效率及召回质量四个问题。
短文本的有效性、无正文和视频的处理策略、有效入库率、平均切片数、保留周期、层阈值、
索引/精度及实际资源配置均需以落地策略和测量为准；不能由8,000条日增直接推导已验证的
高并发能力，也不能将分层本身表述为已提高召回率。

现有原生入库与分层生命周期能力见 `agent/docs/video-knowledge-ingest.md` 和
`agent/docs/tiered-knowledge-lifecycle.md`；企业连接、统一内容源接线及规模性能仍需分别核验。
本次仅持久保存业务背景，没有修改入库过滤、分层配置、索引或生产运行状态。

## 33. 新闻板块入库范围（2026-10-24 用户确认）

用户明确健康板块不进入向量知识库；其他板块暂不增加排除项。按明确来源分类过滤，
不能依据标题/正文含“健康”或推断的 health 主题误删跨板块新闻。

仓库新增 `news-board-admission-v1` 准入规则，普通文档、生命周期、视频文本化和QA
入口在模型/索引调用前跳过明确健康分类，返回可识别原因；爬虫保留filtered状态。
企业内容 DTO 与本地缓存对象补充可选 category；接入方仍需提供准确的企业栏目映射。
未提供分类的请求保持兼容，不能声称对未知分类已经完成健康筛除。

统一正文冻结契约当前未保存栏目，现有health标签属于推断主题，演示数仓category也不能
当作企业正式栏目。本次规则不修改原始正文、既有向量、冻结快照或已接受任务，旧版本
需要独立审计和撤出；实际企业部署和存量栏目补齐仍待完成。说明及调用链见
[`冷温热新闻索引配置与运行`](../agent/docs/tiered-knowledge-lifecycle.md)。
