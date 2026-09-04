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
- 新闻正文及元数据写入 FastGPT。
- 基础文本切片、QA 生成与检索评测。
- 实体抽取、数值抽取和事件聚类的实验代码。

主要入口：

- `tencent-news-crawler/service/discovery_ingest_service.py`
- `tencent-news-crawler/service/news_ingest_service.py`
- `tencent-news-crawler/crawler/tencent_news.py`
- `tencent-news-crawler/service/fastgpt_client.py`
- `tencent-news-crawler/service/ingest_repository.py`

### `writing-agent-service`

新闻研究与辅助写作后端，当前包含：

- FastAPI 任务创建、查询、进度和人工决策接口。
- PostgreSQL 任务、步骤、Agent 调用、Artifact 和 Outbox 模型。
- 数据库幂等键与 Temporal Workflow ID 双重重复启动保护。
- Research → Outline → Section → Assemble → Review → Revise → Finalize 工作流。
- 研究、提纲和终稿人工确认节点。
- Reviewer 驱动的章节级定点返工，最多三轮审核。
- Pydantic 结构化数据契约。
- `job_id`、`section_id`、审核轮次和引用覆盖率等业务质量校验。
- S3/MinIO 版本化 Artifact 与 PostgreSQL Checkpoint。
- Activity 心跳、超时重试、失败恢复和 Outbox 事件。
- 基础健康检查、监控指标和 CMS 发布适配。

主要入口：

- `writing-agent-service/app/workflows/news_writing.py`
- `writing-agent-service/app/services/news_step_handler.py`
- `writing-agent-service/app/services/agents/`
- `writing-agent-service/app/services/artifact_pipeline.py`
- `writing-agent-service/app/services/checkpoint.py`
- `writing-agent-service/app/services/orchestrator.py`

### `FastGPT`

本地 FastGPT 源码及部署资源。它是当前知识库和模型应用的基础设施，不是 NewsAgent 自己实现的业务能力。简历和项目介绍中应将“基于 FastGPT 接入/建设”与“自主实现 FastGPT”区分开。

## 3. 当前真正完成的业务链路

目前相对完整的是两条后半链路。

### 新闻内容入库

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

### 辅助写作编排

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
```

因此当前仓库更准确的状态是：

> 已实现新闻内容知识库的基础接入，以及工程化程度较高的新闻研究/辅助写作后端；尚未实现由用户行为驱动的热点发现与关注度分析主链路。

## 4. 尚未实现或未完成集成的内容

### P0：形成项目核心闭环

以下能力是项目当前最关键的缺口，应优先完成。

#### 4.1 统一新闻身份和内容模型

当前内容链路主要以 URL 去重，尚未建立内部唯一 `news_id`。需要补充：

- `news_id` 
- URL 与 `news_id` 的映射。
- 图文与视频的统一内容模型。
- 正文、字幕和对象存储 URI。
- 内容哈希与内容版本。
- 新闻更正、撤稿和索引失效状态。
- `news_id + content_version` 唯一约束。

建议最小字段：

```text
news_id, content_type, title, summary, publish_time, channel,
content_uri, transcript_uri, content_hash, content_version, status
```

#### 4.2 企业行为数据读取与 Python 对象处理

企业已经提供 C 端行为数据库，因此本项目不需要实现埋点、日志采集、消息传输和行为数仓。当前缺少的是从企业数据库到分析代码之间的消费层：

- `BehaviorDataSource` 查询接口，屏蔽企业数据库的具体实现。
- 将查询结果映射为曝光、点击、阅读、播放和互动等 Python 领域对象。
- 只读取热点分析所需字段，遵守数据权限和最小化原则。
- 用户、新闻、事件类型和时间字段的防御性校验。
- 按查询窗口过滤数据，避免窗口边界重复计算。
- 图文与视频行为口径的标准化。
- 如果上游没有保证去重或异常值清洗，再补充 `event_id` 去重和时长异常处理；如果企业数据表已经保证，则不重复建设。
- 将数据库连接、SQL 或内部 API 与热点计算逻辑隔离，确保计算器可以直接接收 Python 对象进行测试。

建议最小字段：

```text
event_id, user_id, news_id, event_type, event_time,
content_type, duration, channel, device_type
```

推荐接口边界：

```text
企业数据库
  → BehaviorDataSource.fetch(start, end, filters)
  → list[BehaviorRecord]
  → NewsMetricCalculator.calculate(records)
  → list[NewsMetricSnapshot]
```

本地开发时使用内存中的 `list[BehaviorRecord]` 或 JSON fixture 模拟企业查询结果，不复制企业数据库，也不在项目中保存真实用户明细。

#### 4.3 新闻指标与热点计算

需要按 `news_id + 时间窗口` 聚合：

- 曝光人数和次数。
- 点击人数、点击次数和 CTR。
- 有效阅读、平均停留时长和阅读完成度。
- 视频有效播放、平均播放进度和完播率。
- 评论、收藏和分享等互动率。
- 当前值、历史基线、增长率和时间衰减。
- 图文与视频归一化后的热点分数。
- 新闻级热点榜和查询接口。

第一版本地实现直接对 `list[BehaviorRecord]` 进行小时级聚合即可，不需要先建设本地行为库，也不需要引入 Kafka、Flink 或 Spark。后续如果需要保存热点快照，可以只将聚合结果写入 PostgreSQL，不落企业原始用户行为。

#### 4.4 热点与知识库联合检索

需要实现以下顺序，而不是把所有问题都交给向量检索：

1. 从指标库获得热点 `news_id`。
2. 根据 `news_id` 从内容库精确读取当前新闻。
3. 使用标题、摘要、实体和主题进行向量检索。
4. 按发布时间、频道和内容类型过滤、去重。
5. 合并行为指标、新闻事实和来源证据。

#### 4.5 热点分析智能体

当前 Research Agent 面向新闻研究和写作，尚没有专门的热点分析 Agent。需要新增：

- 热点指标查询工具。
- 新闻内容精确查询工具。
- 关联新闻语义检索工具。
- 用户群体关注度查询工具。
- 结构化热点分析报告。
- 指标结论引用具体数值、新闻事实引用具体来源的校验。
- 数据不足时拒绝得出确定结论的机制。

智能体的职责是规划查询和解释证据，不应自由访问整个数据库或自行计算权威指标。

### P1：知识库更新可靠性

当前新闻抓取后同步调用 FastGPT，新闻保存和知识索引耦合。需要拆分为：

```text
新闻清洗并保存
  → 同事务写入知识索引 Outbox
  → 异步 KnowledgeIndexWorker
  → 切片、向量化并写入知识库
  → 更新索引状态
```

需要增加：

- `KnowledgeIndexService`：知识写入、更新、删除和状态查询。
- `KnowledgeIndexWorker`：异步消费索引任务。
- `IndexTaskRepository`：记录版本、状态、错误和重试次数。
- `ReconciliationService`：定时检查遗漏、失败、卡住和版本不一致任务。
- `news_id + content_version + index_type` 索引幂等键。
- 新版本索引成功后再使旧版本失效。
- 撤稿新闻的知识条目删除或禁用。

主链路采用事件驱动，定时任务只做补偿和对账，不能靠定时扫描知识库发现新增新闻。

### P1：视频新闻覆盖

不能完全忽略视频新闻，但也不应全量下载视频并直接交给智能体。需要实现分级处理：

1. 全量接入视频标题、简介、频道、时长、已有标签和播放指标。
2. 优先读取内部已有字幕或 ASR 转写文本。
3. 清洗字幕时间戳、重复句和无意义片段后进入统一内容链路。
4. 只有热点且缺少文本的视频按需执行 ASR。
5. 只有需要验证画面事实时才执行抽帧、OCR 或多模态分析。

知识库保存视频元数据和转写文本，不保存视频二进制；原始视频继续位于对象存储。

### P1：用户关注度和标签体系

需要补充：

- 新闻主题、实体、事件和内容类型标签。
- 用户短期兴趣与长期兴趣分值。
- 行为强度和时间衰减。
- 标签计算依据与证据 `news_id`。
- 用户群体与主题的关注度聚合。
- 标签版本、更新时间和失效机制。

基本计算关系：

```text
用户兴趣分值 = Σ(内容标签 × 行为强度 × 时间衰减)
```

项目第一版建议先实现群体关注度，再考虑个人级画像，以降低范围和隐私风险。

### P2：事件理解能力集成

仓库已有实体抽取、数值抽取和事件聚类实验，但尚未完整接入主链路。需要补充：

- 清洗后的内容自动进入实体和主题抽取。
- 通过实体、时间、标题和向量相似度生成 `event_id`。
- 多篇报道归并为事件。
- 新闻级热度汇总为事件级热度。
- 官方更正和事实版本更新。
- 固定标注集和聚类效果评测。

完成集成前，应将这部分描述为实验或基础实现，而不是生产级实时事件平台。

### P2：受控 Text2SQL

当前尚未实现 Text2SQL。后续如果为运营提供自由查询，需要增加：

- 指标词典和语义层。
- 表、字段及租户白名单。
- 自然语言意图识别和歧义澄清。
- SQL AST 只读校验。
- 时间范围、扫描量、行数和超时限制。
- 查询结果校验及解释。
- 固定问题评测集。

核心热点链路应先使用固定查询工具，不能为了展示 Text2SQL 而让 Agent 自由查询生产数据库。

### P2：运营工作台与生产交付

当前缺少完整前端闭环和真实生产验证，需要补充：

- 热点列表和趋势图。
- 图文/视频指标对比。
- 用户群体关注度。
- 相关新闻、事件时间线和证据展示。
- 智能体任务进度、人工确认和局部重试。
- 企业 SSO/RBAC、审计与数据权限。
- CI/CD、灰度发布和版本回滚。
- 日志、Tracing、告警、压测和故障演练。

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

### 第一阶段：纯 Python 数据计算

目标是练习代码并形成可测试的确定性核心，不连接 Agent。

1. 按企业查询结果定义订单或用户行为 Python 对象。
2. 实现查询结果到领域对象的映射、时间窗口过滤和合法性校验。
3. 计算 GMV 或新闻行为指标快照。
4. 计算历史均值、标准差和变化率。
5. 使用变化率与绝对差额规则判断异常。
6. 为正常、空数据、边界时间、重复记录和异常值编写测试。

只使用 Python 标准库：`dataclasses`、`datetime`、`decimal`、`enum`、`statistics` 和 `collections`。

### 第二阶段：新闻热点最小闭环

1. 建立统一 `news_id` 内容表。
2. 使用脱敏样例或自建 fixture 模拟企业数据库查询结果。
3. 定义 `BehaviorDataSource` 接口，并先实现返回 Python 对象的本地假数据版本。
4. 实现领域对象校验和小时级指标聚合。
5. 实现简单、可解释的热点分数。
6. 根据热点 `news_id` 精确读取内容。
7. 从 FastGPT 召回关联报道。
8. 输出包含指标和引用的结构化热点报告。

完成这一阶段后，项目才真正从“写作助手”变为“用户行为驱动的新闻热点分析系统”。

### 第三阶段：可靠异步链路

1. 将 FastGPT 同步入库拆为索引任务。
2. 增加 Outbox 和异步索引 Worker。
3. 增加幂等、超时、重试、死信和定时对账。
4. 让热点任务能够提升相关新闻的索引优先级。
5. 在知识索引未完成时，从内容库精确读取并降级分析。

### 第四阶段：智能分析与工程完善

1. 增加热点分析 Agent 及受控工具。
2. 集成事件聚类与事件级热度。
3. 增加用户群体关注度和标签解释。
4. 增加视频字幕和按需 ASR。
5. 增加运营工作台、监控、权限和部署闭环。

## 7. 下一项编码任务

当前不要继续扩展 Writer/Reviewer，也不要先引入大数据组件。下一项最小任务建议为：

> 使用纯 Python 定义企业行为查询结果对应的领域对象，并实现“按 `news_id`、小时窗口聚合新闻指标”，用单元测试覆盖对象映射、窗口边界、空数据和图文/视频的不同行为口径。

建议首先创建：

```text
writing-agent-service/app/analytics/entities.py
writing-agent-service/app/analytics/news_metric_calculator.py
writing-agent-service/tests/test_news_metric_calculator.py
```

上述骨架现已创建，具体说明、基础包和 TODO 顺序见
`writing-agent-service/app/analytics/README.md`。测试模板默认标记为 skip；每完成一个
TODO，删除对应测试的 skip 并运行该测试。

第一版输入：

```text
event_id, user_id, news_id, event_type, event_time,
content_type, duration
```

第一版输出：

```text
news_id, window_start, window_end,
impressions, clicks, unique_users, ctr,
total_duration, effective_consumptions, interactions
```

验收标准：

- 如果企业数据源不保证唯一，则相同 `event_id` 不重复计算；如果上游已经保证，测试中记录这一数据契约。
- 使用左闭右开时间窗口 `[start, end)`。
- 缺少 `news_id`、非法时间和负时长被拒绝或进入错误集合。
- 空分母时 CTR 返回 0，不发生除零。
- 图文阅读和视频播放使用明确的事件类型，不混用口径。
- 同一输入重复执行得到相同输出。
- 所有规则有对应的 pytest 测试。

完成这个小模块之后，再实现热点分数；热点分数正确后，再接企业数据库适配器、API、Temporal 和 Agent。PostgreSQL 只在需要保存热点快照、异常事件和分析任务时使用，不作为企业行为明细的替代数据源。

## 8. 简历表述边界

当前可以深入讲解：

- 新闻抓取、规则清洗、质量校验、URL 幂等和 FastGPT 入库。
- Temporal 多智能体编排、人工节点、章节级返工。
- 结构化契约和跨任务业务校验。
- S3 Artifact、PostgreSQL Checkpoint、Outbox 和异常恢复。

完成核心闭环前不应表述为已经实现：

- 企业 C 端行为采集或数仓建设（不属于本项目职责）。
- 企业行为数据查询适配、Python 对象映射和热点聚合（当前尚未实现）。
- 完整用户标签库。
- 实时热点发现平台。
- 全量视频多模态理解。
- 热点与知识库联合分析 Agent。
- 生产级 Text2SQL。
- 自动 GMV 根因定位和策略执行。

后续每完成一个阶段，再将对应能力从“规划”调整为“已实现”，并补充可验证的测试、接口、样例数据和运行说明。
