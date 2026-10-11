# NewsAgent 运营推送闭环实施计划

最后核对：2026-10-04（原计划制定于2026-09-18）

状态：**推送Schema、候选生成服务、确定性Validator与单测已存在；持久审批、策略集与投递运行链路待实现。第一阶段目标为模拟/shadow，不接真实百万级触达平台。**

运营策略研究、策略卡、审批回流契约、当前Temporal调用链与长期实施顺序见
[运营审批驱动的DataLoop文档集](agent/docs/data-loop/README.md)。其中清楚区分一次建议审批、标签二审、通用策略审核与配置发布；本计划中的RPC与专属Workflow仍为待建方案。

## 1. 目标与边界

NewsAgent 已能从行为指标发现热点、结合正文与关联报道生成可信分析。下一阶段是在现有
热点运营链路后增加“候选推送计划 → 确定性校验 → 人工审核 → 企业 RPC → 效果回流”：

```text
热点运行与分析报告
→ PushStrategyGenerator 生成候选计划
→ PushPolicyValidator 确定性校验
→ 运营人员批准、修改或拒绝
→ AudienceService 创建人群快照
→ DeliveryService 提交推送任务
→ 推送平台/MQ 执行
→ 聚合效果回流 Feedback Case、Evaluation Dataset 和运营 Memory
```

本项目负责策略契约、校验、审核、幂等 RPC Adapter、状态恢复和效果回流；不自行建设
用户画像平台、厂商通道或百万用户扇出系统。第一阶段只使用 Mock RPC 或 shadow 模式，
不得真实触达用户。

## 2. 当前状态

### 已有可复用能力

- 热点排行、新闻内容查询、关联报道检索与重排。
- 结构化热点分析 Agent、指标/证据/因果表述校验。
- `analysis_runs`、热点事件生命周期、热点查询 API、SSE 和运营决策入口。
- Temporal 有界 Workflow、人工 Signal、超时、有限重试、Checkpoint 与 Outbox。
- Feedback Case、人工标注与独立二审、三层评测集、候选评测、人工晋升与回滚。
- 租户隔离、幂等账本、热点运行/决策历史和不可变 Artifact 模式；个人Memory不能直接替代团队运营策略库。
- `agent/app/schemas/push.py` 中 `AudienceRule/PushPlan/PushStrategyCandidate`。
- `PushStrategyInputBuilder/PushStrategyService`、`PushStrategyAgentRunner` 与 `PushPolicyValidator`，当前仅为未接API/Workflow的服务及单测。

### 尚未实现

- PushPlan持久化、修订与审核记录、投递任务和效果回执领域模型。
- 候选生成器/Validator的原生模型装配和运行链路；完整审批revision/hash绑定、实际频控与原子去重。
- 团队运营策略注册表、四类决定回流、策略独立审核/激活与下一建议复用。
- 人群圈选、推送提交和结果查询/回调 RPC Port、Mock Client 与企业 Adapter。
- 推送专用 Temporal Workflow、数据库迁移、API、前端页面和评测样本。

因此当前不能在简历或项目介绍中表述为“已实现自动推送”。

## 3. 最小领域契约

```python
class AudienceRule:
    region_codes: tuple[str, ...]
    interest_tags: tuple[str, ...]
    subscription_topics: tuple[str, ...]
    user_segments: tuple[str, ...]
    active_within_days: int | None
    notification_enabled: bool


class PushPlan:
    plan_id: str
    tenant_id: str
    news_id: str
    event_id: str
    priority: str                 # S / A / B / C
    audience_rule: AudienceRule
    channels: tuple[str, ...]
    title: str
    summary: str
    deep_link: str
    send_time: datetime
    expire_at: datetime
    frequency_limit: int
    requires_review: bool
    reason: str
    evidence_news_ids: tuple[str, ...]
    strategy_version: str
    status: str
```

状态机：

```text
DRAFT → PENDING_REVIEW → APPROVED / REJECTED
APPROVED → AUDIENCE_RESOLVING → READY_TO_DISPATCH
READY_TO_DISPATCH → DISPATCHING → SENT / FAILED / EXPIRED
```

审核必须记录操作者、时间、理由以及修改前后的版本。LLM 只能生成候选计划，不能批准、
扩大人群或直接提交推送。

## 4. 确定性安全规则

`PushPolicyValidator` 至少检查：

- `tenant_id/news_id/event_id` 与可信热点输入一致。
- 证据 ID 是热点分析白名单的子集。
- 当前Validator至少要求S/A、全站、App/SMS审核；本窗口目标为所有建议均由运营批准具体revision/hash及有效期，低风险PASSED不能替代批准。
- 人群规则不超出已确认地域、主题和租户范围。
- 同事件增量、同策略、同人群版本、同渠道不重复发送；合法的新进展以新修订/投递意图区分。
- 发送时间、失效时间和频控合法，过期计划直接阻断。
- 风险标签、未确认事实、绝对化标题或证据不足时不得进入投递。

校验失败必须进入明确失败状态，不允许静默降级后继续发送。

## 5. 企业 RPC 边界

### AudienceService

输入结构化人群条件，返回 `audience_id`、版本、估算人数和失效时间。Agent 不接收用户 ID
明细。请求必须携带租户、追踪 ID 和幂等键。

### DeliveryService

只接受已经审核并校验通过的计划，返回 `delivery_task_id` 和受理状态。推荐幂等维度：

```text
tenant_id + event_id + event_update_version + strategy_version + audience_version + channel
```

投递提交使用独立delivery_intent_id作为稳定幂等键并绑定上述业务维度；不能以整条事件的永久去重阻断合法后续进展。

### DeliveryResult

回流目标人数、送达、打开、有效阅读、负反馈和关闭通知等聚合结果，不保存本地用户行为
明细。MVP 可使用定时查询，生产可由回调或企业 MQ 传递。

## 6. 实施顺序

1. 已有Pydantic候选Schema与状态枚举；补齐持久审核、修订和策略集版本契约。
2. 已有Validator与单测；补真实频控、原子去重和所有建议的完整审批绑定，不能把低风险PASSED当作已批准。
3. 已有候选生成服务；补原生模型/Prompt装配和API/Workflow接入，结构化输出继续经过Validator。
4. 新增 PostgreSQL 表、Repository、审核 API 和租户/角色权限。
5. 定义 RPC Protocol，先实现 Mock Client 与故障分类，再接企业 SDK。
6. 使用 Temporal 编排审核等待、人群创建、任务提交和结果查询；副作用调用有限重试。
7. 扩展黄金集、前端评审页、效果回流和运营 Memory；先 shadow，人工验收后再讨论真实触达。

## 7. Definition of Done

- 候选 `PushPlan` 为严格结构化输出，并绑定热点运行、事件、证据和策略版本。
- 不可信、重复、过期、越权或证据不足计划被确定性阻断。
- 所有建议必须由具备权限的运营人员批准具体revision/hash及有效期；高风险审批另要求相应职责分离。
- Mock RPC 覆盖成功、重复、超时、限流、部分失败与不可重试错误。
- Workflow 可从审核等待和可重试 RPC 故障处恢复，不产生第二次投递。
- 回执可追溯至 `plan_id/news_id/event_id`，并只保存聚合效果。
- 离线回放和 shadow 验收通过；未获单独生产授权时不得真实推送。
