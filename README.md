# NewsAgent

## 1. 项目定位

NewsAgent 的目标不是单纯做一个新闻写作机器人，而是构建一套消费企业已有数据、由数据驱动的新闻分析系统。C 端曝光、点击、阅读、播放和互动数据已经由企业数据库提供，本项目不负责埋点采集、日志传输或建设行为数仓：

1. 从企业数据库按权限读取用户曝光、点击、阅读、播放和互动数据，并映射为 Python 领域对象。
2. 对领域对象执行必要的字段校验、时间窗口过滤、聚合和热点计算。
3. 通过新闻唯一 `news_id` 获取对应的图文正文、视频元数据或字幕文本。
4. 从知识库检索同一事件的相关报道、历史背景和可追溯证据。
5. 由分析智能体组合行为指标与新闻事实，输出热点趋势、关注群体和候选原因。
6. 对复杂研究和创作任务，使用 Research、Writer、Reviewer 工作流生成资料包和辅助稿件。

核心关系如下：

```text
企业行为数据库 ─→ 查询适配器 ─→ Python对象 ─→ 指标聚合 ─→ 热点发现 ─┐
                                                                  ├─→ 热点分析智能体
新闻内容 ───────→ 清洗/切分 ─→ 内容库和向量知识库 ────────────────┘
                                                                         ↓
                                                                研究、解释、辅助写作
```

两条数据链路必须分开建设：

- 企业行为数据库提供原始或已聚合的 C 端行为数据，本项目只做授权查询和分析消费。
- Python 分析层回答“什么新闻正在变热、哪些用户群体在关注”，是否回写结果由后续接口需求决定。
- 内容库和向量知识库回答“新闻讲了什么、有哪些相关报道和证据”。
- 智能体负责调用受控工具组合结果，不负责采集或保存企业原始行为明细，也不直接处理视频二进制。

`news_id` 是行为数据、新闻内容、知识索引和分析结果之间的统一关联键。

## 2. 当前仓库组成

### `tencent-news-crawler`

新闻内容接入与知识库实验项目，当前包含：

- 腾讯新闻分类页发现和 URL 去重。
- 新闻页面抓取，标题、作者、发布时间和正文解析。
- 不可读区块、编辑署名和图片来源等基础噪声过滤。
- 空标题、空正文和正文过短校验。
- 以 URL 哈希为键的本地 JSON 缓存。
- SQLite 入库状态、失败次数和重试记录。
- 新闻正文及元数据写入 FastGPT，并在元数据中写入 `news_id`。
- 基础文本切片、QA 生成与检索评测。
- 实体抽取、数值抽取和事件聚类的实验代码。
- 事件关系人工标注候选集（`datasets/annotations/`）。

主要入口：

- `tencent-news-crawler/service/discovery_ingest_service.py`
- `tencent-news-crawler/service/news_ingest_service.py`
- `tencent-news-crawler/crawler/tencent_news.py`
- `tencent-news-crawler/service/fastgpt_client.py`
- `tencent-news-crawler/service/ingest_repository.py`

### `writing-agent-service`

新闻研究、辅助写作、热点分析与数据闭环后端。当前包含：

- FastAPI 任务创建、查询、进度、SSE 和人工决策接口。
- PostgreSQL 任务、步骤、Agent 调用、Artifact、Outbox 与热点相关模型。
- 数据库幂等键与 Temporal Workflow ID 双重重复启动保护。
- Research → Outline → Section → Assemble → Review → Revise → Finalize 写作工作流。
- 热点监控 Workflow、窗口派发 Workflow、Temporal Schedule 运维 CLI 与 Worker 入口。
- 确定性指标聚合、历史基线、热度评分、排行、内容富化与关联新闻重排。
- 热点分析 Agent 的结构化输入构造、FastGPT 调用、业务校验和统一 Service 入口。
- 热点结果持久化（`analysis_runs`）、热点事件去重/合并生命周期（`hot_events`）。
- 热点运营 API：榜单/详情、运营决策、转交研究/写作。
- Data Loop：Feedback Case、人工标签与二审、不可变评测数据集、三层离线回放与门禁。
- Model Loop：Production Bundle、候选配置、确定性评测、人工晋升、激活与回滚。
- 运营 Memory：短期记忆、长期候选、长期记忆、晋升审批与运行时上下文解析。
- 本地内容桥接与确定性语料检索（读取爬虫 JSON 缓存与 SQLite 台账）。

主要入口：

- `writing-agent-service/app/workflows/news_writing.py`、`hot_news.py`、`data_loop.py`
- `writing-agent-service/app/activities/`、`app/services/news_step_handler.py`
- `writing-agent-service/app/services/agents/`、`app/services/hot_news_analysis.py`
- `writing-agent-service/app/services/hot_news_orchestration.py`、`hot_news_run_store.py`
- `writing-agent-service/app/services/data_loop/`、`app/services/production_bundle.py`
- `writing-agent-service/app/services/memory_*.py`
- `writing-agent-service/app/api/`（`jobs`、`events`、`hot_news`、`data_loop`、`memory`）
- `writing-agent-service/app/analytics/`、`app/retrieval/`
- `writing-agent-service/app/hot_news_worker.py`、`app/data_loop_worker.py`、`app/outbox_worker.py`

### `FastGPT`

本地 FastGPT 源码及部署资源。它是当前知识库和模型应用的基础设施，不是 NewsAgent 自己实现的业务能力。简历和项目介绍中应将“基于 FastGPT 接入/建设”与“自主实现 FastGPT”区分开。

## 3. 当前真正完成的业务链路

### 3.1 新闻内容入库

```text
发现新闻 URL
  → 抓取页面
  → 解析正文和元数据
  → 基础规则清洗
  → 内容质量校验
  → URL 幂等去重及缓存
  → 同步调用 FastGPT
  → 记录 collection_id、成功或失败状态
```

### 3.2 辅助写作编排

```text
创建任务
  → Research 生成研究包
  → 人工确认
  → Writer 生成提纲
  → 人工确认
  → 分章节生成并组装
  → Reviewer 审核
  → 指定章节返工或转人工
  → 人工确认终稿
  → 保存版本化产物
  → CMS 发布（需配置）
```

### 3.3 热点发现与分析闭环

```text
窗口触发（Temporal Schedule / 手动）
  → 查询企业行为数据并聚合指标
  → 历史基线、热度评分、排行
  → 按 news_id 精确读取正文
  → FastGPT 召回关联新闻 + 确定性重排
  → 构造 EvidencePacket（证据白名单）
  → 热点分析 Agent（结构化输入/输出）
  → 数字、指标引用、证据引用校验
  → 保存 analysis_runs 不可变快照
  → 热点事件去重/合并与生命周期推进
  → 运营决策（采纳/拒绝/纠正）
  → 可转交 Research/Writing 流程
```

关键约束：权威指标、热度分量和排行全部由 Python/SQL 确定性计算；模型只做趋势解释与
表达；新闻正文、检索结果和模型输出按不可信输入处理。

### 3.4 热点 Data Loop 与 Model Loop

```text
校验失败 / 低置信 / 检索异常 / 运营拒绝 / 发布效果
  → Feedback Case
  → 强类型人工标签（提交 → 独立二审）
  → 不可变评测 Dataset（golden / fresh_bad_case / high_risk_regression）
  → 三层离线回放（候选 / 线上基线 / 上一实验）
  → 确定性 Evaluation Gate
  → Temporal 等待人工批准（48 小时超时默认不晋升）
  → 审批账本 → 激活账本 → 新 Active Bundle / 显式回滚
```

人工标签执行四眼分离（提交人 ≠ 二审人），候选提交人 ≠ 发布批准人；激活与回滚均为显式、
可审计、可幂等重放的命令。模型只生成候选与 Diff，不自动训练、不自动发布。

### 3.5 运营 Memory

- 短期记忆：有任务边界和有效期。
- 长期候选 → 人工审批 → 长期记忆，支持版本、有效期、替代关系和过期。
- 运行时按 `tenant_id + team_id + section_id + role_id + user_id` 作用域过滤，确定性解析冲突。
- 模型可见上下文经过大小裁剪，未注入的 Memory 单独留作服务端审计。

### 3.6 内容侧桥接

- `TencentIngestContentRepository`：合并爬虫 JSON 正文缓存与 SQLite 入库台账，提供带
  `collection_id` 的精确 `NewsContent`。
- `CorpusKnowledgeSearchClient`：在本地语料上做确定性词面召回，作为 FastGPT 检索的离线回退。

### 3.7 验证状态

截至 2026-09-12：

- 全服务默认回归：**671 passed, 6 skipped**（6 项为 opt-in 集成/E2E）。
- Temporal time-skipping L1：1 项通过。
- 隔离 Compose 栈（PostgreSQL / Temporal / Redis / MinIO / FastGPT 替身）黑盒 Data Loop
  E2E：**5/5 通过**。
- 迁移头：`20260912_0015`；`hot_events` 已在真实 PostgreSQL 建表。
- Memory API 与热点转交写作 API 已在真实栈上写入并回读验证。

```bash
cd writing-agent-service
.venv-local/bin/python -m pytest -o addopts="" -q
.venv-local/bin/python -m evaluation.run_hot_news_eval
docker compose -f deploy/docker-compose.data-loop-e2e.yml up -d --build
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  --entrypoint python -e RUN_DATA_LOOP_E2E=1 e2e-runner \
  -m pytest tests/e2e/test_data_loop_full_chain.py -q
```

## 4. 尚未完成或未在生产启用的内容

### 4.1 生产依赖接入（P0）

以下能力代码已就绪，但尚未接入真实企业环境，是上线前的硬性缺口：

- **企业行为 / 基线 / 正文 / 检索 RPC Adapter**：仓库只提供 Port 契约与本地 Adapter，
  真实 SDK、鉴权与服务发现由部署侧实现（入口 `app/hot_news_dependencies.py` 的依赖工厂）。
- **网关 / IdP**：Data Loop、热点、Memory 已使用共享 Bearer + 角色头的最小权限模型，
  但 `/api/v1/jobs`、`/api/v1/events` 尚未收口到同一网关鉴权，且尚未对接企业 SSO。
- **真实 FastGPT App 质量验收**：热点分析 App 的 System Prompt、输出约束与质量尚未验收。
- **Temporal Schedule 生产注册与热点 Worker 真实装配**：Schedule CLI 与 Worker 入口已具备，
  需在目标环境配置租户组与真实 Adapter 后启用。

### 4.2 数据与评测补齐（P1）

- 热点 Agent 种子评测集当前 30 条，建议扩到 100 条。
- 事件关系标注候选集 300 对中仅 100 对已标注，其余需**人工**补标。
- 需要在目标环境冻结真实的 golden / high_risk_regression 数据集并校准门禁阈值。

### 4.3 生产化与可观测性（P2）

- 目标生产库的迁移 upgrade/downgrade 演练与并发冲突测试。
- 对象存储 SSE/KMS、Bucket Versioning、Object Lock 与生命周期治理。
- API/Worker 的 K8s 清单、镜像不可变 tag、灰度与回滚。
- 热点指标与 SSE、审计告警、越权与高风险操作告警。
- CMS 发布网关配置（未配置时发布接口保持关闭）。

### 4.4 扩展场景（P3）

- 视频字幕与按需 ASR、必要时的抽帧/OCR/多模态理解。
- 事件级热度聚合与运营界面、事件时间线展示。
- 受控 Text2SQL 的生产化（表/字段白名单、SQL AST 只读校验、行数与超时限制、评测集）。
- 用户群体关注度与标签体系。

## 5. GMV异常归因能力的定位

GMV 异常归因不是新闻热点主链路的必要组成部分，但可以作为同一套“指标异常分析框架”的扩展场景：

```text
交易指标持续计算
  → 确定性异常检测
  → 异常事件触发 Temporal
  → 指标树拆解
  → 渠道/地区/版本/商品等维度下钻
  → 关联活动、发布、库存和系统故障
  → Agent 解释证据并生成候选策略
  → Reviewer 或人工确认
```

实现原则：

- 检测算法发现异常，不让 LLM 定时扫描数据库判断波动。
- SQL 和程序计算指标、基线和贡献率，LLM 不计算权威数字。
- Agent 负责决定下钻路径、关联证据和组织解释。
- 先检查数据新鲜度，避免把数据延迟误判为业务下跌。
- 相关性只能作为候选原因，必须通过时间和对照维度验证。
- 高风险策略只输出建议，不允许 Agent 自动修改价格、发券或全量回滚。

它可以复用当前项目的 Temporal、Outbox、Checkpoint、Artifact、人工确认和 Reviewer 能力，但应建立独立的 `gmv_anomaly_analysis` 工作流，不应塞进新闻写作步骤。

## 6. 推荐实施顺序

### 第一阶段：纯 Python 数据计算（已完成）

领域对象、时间窗口过滤、指标聚合、历史基线、热度分数与排行均已实现，并有对应单元测试。

### 第二阶段：新闻热点最小闭环（已完成）

统一 `news_id`、`BehaviorDataSource`、指标聚合、热点分数、精确内容读取、FastGPT 关联召回、
结构化热点报告与业务校验均已打通，并已通过真实依赖的端到端验证。

### 第三阶段：可靠异步链路（已完成）

Temporal Workflow/Activity、幂等持久化、Outbox、Checkpoint、失败恢复、人工 Gate 与
Data Loop/Model Loop 闭环均已实现。

### 第四阶段：生产接入与工程完善（进行中）

1. 企业 RPC Adapter、网关/IdP、真实 FastGPT App 验收。
2. `/api/v1/jobs`、`/api/v1/events` 收口到统一网关鉴权。
3. 评测数据补齐与门禁阈值校准。
4. 生产化部署、可观测性与告警。

## 7. 下一项任务

按当前缺口，建议优先级为：

1. **真实依赖端到端验证**：已完成（迁移、Memory API、转交写作、Data Loop E2E 全部通过）。
2. **`/api/v1/jobs`、`/api/v1/events` 网关鉴权**：复用现有 `get_data_loop_principal` 模式，
   补共享 Bearer 与独立角色头，堵住跨租户越权。
3. **评测数据补齐**：种子集 30 → 100；组织人工补标事件关系候选对。
4. **企业接入**：真实 FastGPT App、企业 RPC Adapter、IdP/网关。

## 8. 简历表述边界

当前可以深入讲解：

- 新闻抓取、规则清洗、质量校验、URL 幂等和 FastGPT 入库。
- Temporal 多智能体编排、人工节点、章节级返工。
- 结构化契约和跨任务业务校验。
- S3 Artifact、PostgreSQL Checkpoint、Outbox 和异常恢复。
- 热点发现、指标聚合、热度评分、排行与关联新闻确定性重排。
- 热点分析 Agent 的可信输入、结构化输出与业务校验。
- Data Loop / Model Loop：人工标签与二审、不可变评测集、三层回放、确定性门禁、人工晋升与回滚。
- 运营 Memory 的作用域、冲突解析与晋升审批。

尚未在生产启用、不应表述为已经上线的部分：

- 企业 C 端行为采集或数仓建设（不属于本项目职责）。
- 企业行为数据 RPC、网关/IdP 与真实 FastGPT App 的生产接入。
- 实时热点发现平台与持续调度（本地闭环已验证，生产 Schedule 未注册）。
- 全量视频多模态理解、生产级 Text2SQL。
- 自动 GMV 根因定位和策略执行。

后续每完成一个阶段，再将对应能力从“规划”调整为“已实现”，并补充可验证的测试、接口、样例数据和运行说明。
