# 运营审批与反馈契约

日期：2026-10-04。第1节描述当前代码，第2节之后为待实现的领域设计；不修改现有 API 枚举或业务 Schema。

## 1. 当前运营真正审批什么

```text
HotNewsView / HotNewsDecisionForm
→ hotNewsApi.recordDecision
→ POST /api/v1/hot-news/decisions
→ record_hot_news_decision
→ HotNewsDecisionService.record_decision
→ PostgresHotNewsRunStore.get_analysis_memory(tenant_id, run_id, news_id)
→ PostgreSQL hot_news_decisions
→ rejected/corrected：同事务 AnalysisFeedbackCollector.collect_from_operator_decision
→ feedback_cases → 人工标签提交/独立二审 → 可冻结的评测样本
```

DataLoop 页的 `/api/v1/data-loop/operator-decisions` 复用同一服务。现有动作是 `accepted/rejected/deferred/corrected`；没有 `plan_id` 或完整投递计划，采纳不会发送通知，延后没有持久的到期重评任务。`correction_payload` 不自动成为评测标签，仍需强类型标注和二审。

当前反馈默认可以分类为 `analysis_incorrect`。后续接运营策略时，必须先分清模型分析错误、编辑选择和策略不适配；不能把“今天频控已满”标为模型事实错误。

## 2. 建议的审批对象

新增版本化 `PushSuggestion`/`PushReviewDecision` 契约，保留现有分析运营决定。名称是设计建议，不代表已有类。一次审批须覆盖完整对象，而不是只审核标题。

| 部分 | 必备信息 |
|---|---|
| 身份和血缘 | 服务端tenant/team、suggestion/plan、revision、run_id、news_id、event_id、正文/证据版本与哈希 |
| 新闻背景 | 事件阶段、已确认事实、关键新增事实、相关报道、风险与limitations |
| 候选建议 | 推荐动作、标题/摘要/落地内容、人群规则及排除、渠道、发送窗/时区/失效时间 |
| 策略和规则 | 命中strategy_id/version、策略集版本/哈希、模型/Prompt、policy/schema/validator版本 |
| 确定性结果 | 权限/证据/事实/人群/链接/去重/频控/过期检查，阻断原因与可信快照引用 |
| 人工决策 | 动作、理由码、补充说明、修改差异、操作者/时间、绑定对象哈希、审批有效期 |
| 执行与结果 | 未执行、模拟、shadow、真实执行分开；delivery引用、聚合结果/观察窗、未知与缺失状态 |

网关验证身份后由业务层补tenant/user/team，模型输出和请求JSON不能提供可信审批人、租户或权限。team权限需另建明确合同，不能通过删除个人Memory的user过滤实现共享。

## 3. 动作与理由分开

新推送契约建议使用 `approve/reject/defer/revise`，迁移时显式映射现有枚举，不直接替换旧记录。

| 新动作 | 现有近似动作 | 回流意义 | 下游行为 |
|---|---|---|---|
| approve | accepted | 正向编辑案例；并不证明真实效果或通用规则有效 | 记录批准的具体修订版；首阶段只有模拟/shadow |
| reject | rejected | 记录不推原因、反例或模型错误，不能混成负面质量标签 | 结束本建议；需要纠错时进入标签工作台 |
| defer | deferred | 有价值但当前不适合；记录重评条件 | 用持久Timer重评，超过新闻有效期结束；不自动投递 |
| revise | corrected | 原建议和人工改动构成可学习差异 | 新建revision并重验/重审，旧审批不覆盖修改内容 |

建议理由码独立版本化：`insufficient_evidence`、`fact_or_attribution_error`、`low_public_value`、`audience_mismatch`、`no_material_update`、`duplicate_event`、`frequency_budget_exhausted`、`timing_inappropriate`、`content_expired`、`copy_misleading`、`channel_or_consent_mismatch`、`risk_requires_escalation`、`other_with_explanation`。允许多理由并设主要理由，保留运营原文，不让模型自动赋予权威标签。

`defer` 必须说明 `recheck_at` 或受控重评事件，以及最长有效期。即使到时事实已补齐，也只生成新建议/待审任务。`revise`必须保存字段diff和前后哈希；扩大人群、改渠道、改事实、改时间均重审。

## 4. 审批如何成为 DataLoop 数据

```text
不可变 SuggestionSnapshot + ReviewDecision
→ 同事务写 DecisionCase + 待处理命令
→ 经验证的场景/理由分类
→ 独立标注复核（可评测标签，不可评测原因）
→ 按审批完成/摄取水位消费，保留原事件时间
→ 正向/反例/延期/改稿的案例池
→ 冻结策略评测输入及期望动作
→ 模型归纳候选 + 人工检查支持与反例
→ 确定性回放/门禁 + 策略审核
→ 显式激活 ApprovedStrategySet
→ 下一次建议读取指定作用域内的批准版本
```

案例池与现有Fresh bad case数据集不是同一概念。四类决定都保留为案例；只有确认为模型/规则错误且经过独立标注的样本进入对应坏例评测，不能把全部采纳或延后塞进Fresh bad case。

每条候选策略至少关联支持案例、反例、适用范围、原始审批事实和效果证据等级。只出现一次的判断可以作为待验证经验；要推广为通用规则须审核、评测、版本化。样本量、容忍退化和业务参数由批准政策给出，不由模型自行决定。

## 5. 四个 Gate

| Gate | 审批对象 | 允许改变什么 | 不授予什么 |
|---|---|---|---|
| G1 单条建议 | 建议revision/hash和有效期 | 本次建议的运营决定 | 团队通用策略或配置上线 |
| G2 标签二审 | 案例标签版本/期望动作 | 样本可评测性和权威标签 | 实际通知发送权限 |
| G3 策略审核/激活 | 策略版本、证据、适用范围、评测、策略集hash | 下一次建议可选的策略范围 | 修改生产模型/Prompt、绕过用户权限 |
| G4 配置发布 | 候选Bundle/运行时快照、门禁报告 | 经授权的生产版本切换 | 未审批新闻的对外触达 |

职责分离遵循现有标签和Bundle治理：提交人不自审；范围、版本、哈希或评测变动使批准失效。低风险策略可简化材料，但不能通过一次新闻采纳暗中升级成团队规则。

## 6. 幂等、冲突与撤销

- 建议稳定业务键包含tenant、事件、事件增量/正文修订、策略集、受众规则版本、渠道；规范化后再哈希。实际投递使用独立delivery_intent_id，并绑定该新闻更新，避免相同事件的合法后续更新被永久去重。
- 审批命令绑定suggestion_id、expected_revision、内容哈希和idempotency_key。同键同内容返回原收据，同键不同内容冲突；两个审核者竞争只接受一个权威决定。
- 人工决定/案例/命令账本同事务提交。向Temporal送达失败可以重放同一命令；传输成功不是下游投递成功。
- 正文更正/撤回、用户授权失效、建议过期或策略撤销须阻断未发计划。已发送内容保留事实，只能按平台已确认能力做补偿，不能在本地伪造“撤回成功”。
- 覆盖决定使用 `supersedes_decision_id` 或专属撤销事件；不原地改写历史。冻结数据集固定旧版本，后续版本纳入新标签/更正并保留血缘。

## 7. 审批工作台应显示的业务材料

待审列表按租户/团队、优先级、剩余有效时间和阻断状态显示。详情提供新闻背景、可引用事实、重要增量、建议人群/渠道/时机、命中策略、可理解的规则检查、标题与落地内容、修改diff和历史决策。

运营可以在同一界面选择动作与理由；标签和策略候选从案例追踪进入独立审核。Workflow ID、Bundle JSON、Artifact SHA用于审计详情，不作为普通运营必须手填的业务输入。
