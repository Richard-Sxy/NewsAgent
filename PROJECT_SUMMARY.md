# NewsAgent 已完成内容简述

本文简要说明 NewsAgent 当前**已经完成**的业务与技术内容，用于快速了解项目现状。
详细的长期上下文见 `PROJECT_CONTEXT.md`，阅读路线见 `READING_PLAN/README.md`。

## 一、项目形态

一套消费企业已有行为数据、持续发现新闻热点、组合可信证据、辅助研究写作，并通过
Data Loop / Model Loop 持续改进的新闻运营 Agent 系统。仓库由三部分组成：

- `tencent-news-crawler`：腾讯新闻抓取、清洗、去重、FastGPT 知识入库与实验评测。
- `writing-agent-service`：研究与辅助写作、热点分析、Data Loop / Model Loop、运营 Memory 后端。
- `FastGPT`：知识库与模型应用基础设施（上游工程，非本项目自研业务）。

统一关联键为 `news_id`。权威指标、热度与排行由 Python/SQL 确定性计算，LLM 只做解释与表达。

## 二、已完成的核心闭环

### 1. 新闻内容接入

- 腾讯新闻发现、抓取、正文/元数据解析、噪声过滤、质量校验。
- URL 哈希本地缓存 + SQLite 入库状态与重试记录。
- 正文与元数据写入 FastGPT，元数据携带 `news_id`，记录 `collection_id`。

### 2. 研究与辅助写作工作流

- Research → Outline → 分章节写作 → Reviewer → 定点返工（最多三轮）→ 终稿 → CMS 发布。
- 研究包、提纲、终稿人工确认节点。
- 任务幂等、状态机、Checkpoint、不可变 Artifact、失败恢复、Outbox、Redis SSE。

### 3. 热点发现与分析闭环

- 按窗口读取企业行为数据，聚合指标、历史基线、热度评分与排行。
- 按 `news_id` 精确读取正文，FastGPT 召回关联新闻并做确定性重排。
- 热点分析 Agent：可信输入构造 → FastGPT 结构化调用 → 业务校验 → 统一 Service 入口。
- 校验：输出 `news_id` 一致、指标引用白名单、证据 `news_id` 白名单、假设必须标记为推断。
- 结果以 `analysis_runs` 不可变快照幂等持久化。
- 热点事件去重/合并与生命周期（`emerging / active / cooling / closed`，`hot_events`）。
- 热点运营 API：榜单/详情、运营决策、转交研究/写作（幂等）。
- Temporal 热点 Workflow、Activity 重试边界、Worker 入口、Schedule 运维 CLI。

### 4. Data Loop / Model Loop

- 校验失败、低置信、检索异常、运营拒绝、发布效果统一转为 Feedback Case。
- 强类型人工标签 + 独立二审（四眼分离）。
- 按 cutoff 冻结不可变评测 Dataset：`golden / fresh_bad_case / high_risk_regression`。
- 三层离线回放（候选 / 线上基线 / 上一实验）+ 确定性 Evaluation Gate。
- Temporal 等待人工批准（48 小时超时默认不晋升），审批账本与激活账本分离。
- 生产 Bundle 候选、评测、激活、回滚与授权故障恢复，全部幂等可重放。
- Model Loop 只迭代 Prompt、热度配置、重排参数、输出 Schema，不自动训练大模型。

### 5. 运营 Memory

- 短期记忆（任务边界 + 有效期）、长期候选、长期记忆、晋升审批。
- 运行时按租户/团队/板块/角色/用户作用域过滤，确定性冲突解析。
- 模型可见上下文大小受控，未注入记忆单独留作审计。

### 6. 内容侧桥接

- 合并爬虫 JSON 正文缓存与 SQLite 台账，提供带 `collection_id` 的精确内容。
- 本地语料确定性词面检索，作为 FastGPT 检索的离线回退。

## 三、离线数据与评测

- 热点 Agent 种子评测集：30 条，覆盖明显热点、低样本、有效证据、主体冲突、
  证据不足、安全边界六类。
- 企业 RPC 故障演练场景：9 类。
- 事件关系人工标注候选：300 对（100 对已标注，200 对待人工补标）。
- 本地演示场景：5 条新闻的行为与历史数据。
- 评测脚本：`evaluation/run_hot_news_eval.py`、`evaluation/run_fault_drills.py`。

## 四、验证状态（2026-09-12）

- 全服务默认回归：**671 passed, 6 skipped**（跳过项为 opt-in 集成/E2E）。
- Temporal time-skipping L1：1 项通过。
- 隔离 Compose 栈（PostgreSQL / Temporal / Redis / MinIO / FastGPT 替身）黑盒 Data Loop E2E：**5/5 通过**。
- 迁移头 `20260912_0015`，`hot_events` 已在真实 PostgreSQL 建表。
- Memory API 与热点转交写作 API 已在真实栈写入并回读验证。

## 五、尚未生产启用的部分

- 企业行为/基线/正文/检索 RPC Adapter、网关/IdP、真实 FastGPT App 质量验收。
- `/api/v1/jobs`、`/api/v1/events` 尚未收口到统一网关鉴权。
- Temporal Schedule 生产注册与热点 Worker 真实 Adapter 装配。
- 目标生产库迁移演练、对象存储治理、K8s 清单、审计与告警。
- 评测数据补齐（种子集 100 条、事件关系人工补标）与门禁阈值校准。
- 视频 ASR/多模态、事件级运营界面、生产级 Text2SQL。

> 结论：核心业务闭环（写作 + 热点 + Data Loop/Model Loop + Memory）已完成并通过真实依赖
> 的本地端到端验证；距离生产上线主要差企业依赖接入、数据补齐与生产化治理。
