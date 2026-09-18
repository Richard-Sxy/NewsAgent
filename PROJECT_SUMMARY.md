# NewsAgent 当前项目摘要

最后更新：2026-09-18

## 1. 项目定位

NewsAgent 是面向新闻运营的分析与辅助写作系统。它消费企业已有行为与内容数据，通过
确定性指标发现热点，用知识库补充证据，再由 Agent 解释趋势、生成运营建议，并把人工
反馈转为可审计的评测与配置迭代闭环。

统一关联键为 `news_id`。Python/SQL 负责权威指标、基线、排行和业务校验；LLM 只处理
开放文本理解与表达。FastGPT 是外部知识库和模型应用基础设施，不是本项目自研能力。

## 2. 当前已实现能力

### 内容与知识接入

- 腾讯新闻发现、正文与元数据解析、清洗、质量校验、URL 幂等、缓存和失败重试。
- 图文内容写入 FastGPT，并保留 `news_id`、来源和 `collection_id` 映射。
- 视频内容支持 ASR、画面概括、统一文本化、内容充分性闸门和可降级入库；本地离线链路
  已验证，企业 ASR/多模态网关仍待接入。
- 新闻事件聚类、标题实体特征、离线召回实验和人工事件关系标注候选。

### 热点发现与可信分析

- 企业数据 Port、防腐层和本地/SQL 模拟数据源；不在本地保存企业原始行为明细。
- 按 `[window_start, window_end)` 聚合曝光、点击、有效消费、互动、CTR 和历史基线。
- 可解释热度评分、排行、`news_id` 精确正文查询、FastGPT 召回和规则重排。
- 结构化热点分析 Agent：InputBuilder → FastGPT Runner → Pydantic Report → Validator → Service。
- 指标引用、证据白名单、新闻 ID、推断/事实边界和无证据 limitation 校验。
- Text2SQL 候选查询支持 SQL AST/白名单/只读/行数与超时护栏；确定性模板仍是安全回退。

### 运行、运营与写作

- `analysis_runs` 不可变快照、热点事件去重/合并和生命周期管理。
- Temporal 热点 Workflow、窗口 Dispatcher、Worker、Schedule 管理、超时和有限重试。
- 热点榜单/详情、运营决策、SSE 进度流和向研究写作流程的幂等转交。
- Research → Outline → 分章节写作 → Reviewer → 定点返工 → 人工终审 → CMS 的可恢复流程。
- PostgreSQL Checkpoint、S3/MinIO Artifact、Outbox、Redis SSE 和 Mock CMS。

### Data Loop、配置迭代与 Memory

- 校验失败、低置信、检索异常、人工拒绝和聚合发布效果统一进入 Feedback Case。
- 强类型标签、提交人与独立审核人分离、不可变 Golden/Fresh/High-risk 数据集。
- 候选、线上基线和上一实验三层回放，确定性 Gate、48 小时超时拒绝、人工激活与回滚。
- Production Bundle、审批/激活账本、租户隔离、不可变 Artifact 和授权失败恢复。
- 短期用户记忆、长期候选、长期记忆、人工晋升和作用域冲突解析。

## 3. 已验证范围

- 2026-09-12 曾完成 `671 passed, 6 skipped` 的默认回归基线。
- Temporal time-skipping 验证了 48 小时无人审批默认拒绝。
- 隔离 Compose 栈验证 PostgreSQL、Temporal、Redis、MinIO、API、Worker 和确定性 FastGPT
  替身下的批准、拒绝、激活失败恢复、下一次运行与回滚路径。
- 视频统一文本链路使用本地 faster-whisper 做过真实离线演练。
- 当前迁移头仍为 `20260912_0015`。

当前工作区不是新的绿色回归基线：仓库 `.venv` 解释器不可用，系统 Python 执行测试时又
发现用户正在编辑的 `app/agents/hot_news_analysis.py` 存在缩进错误。因此新增改动在修复
该文件并使用完整依赖环境复跑前，不能宣称全量测试通过。

## 4. 尚未完成或未生产启用

- 企业行为、基线、正文、检索 RPC 的真实 SDK 映射、数据水位和 SLA 联调。
- 真实 FastGPT 热点 App 的质量验收、企业网关/IdP、生产凭据和网络治理。
- 生产 Schedule 注册、目标数据库迁移演练、对象存储治理、告警、审计和完整 K8s 发布。
- 热点评测集扩充与人工冻结；2026-09-18 新增的候选草稿仍待人工审核。
- 运营推送闭环尚未实现：缺少 PushPlan、策略生成/校验、审核表、企业 RPC 和效果回执。
- 真实厂商推送、百万用户扇出、完整画像平台和自动发布不在当前实现范围。

## 5. 当前优先级

1. 修复当前热点分析文件的语法问题，并恢复可重复的完整测试环境。
2. 人工审核并冻结首批热点 Golden/High-risk 评测样本。
3. 按 `PushPlan.md` 先实现结构化候选计划和确定性安全校验，再建设审核与 Mock RPC。
4. 继续企业 RPC、真实 FastGPT、网关/IdP 和生产基础设施联调。

结论：热点分析、写作、Data Loop/配置迭代和 Memory 已形成较完整的本地工程闭环；项目
距离生产使用的主要差距是企业依赖、真实数据与模型验收、生产治理，以及尚未实现的运营
推送闭环。

