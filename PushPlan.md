# NewsAgent 运营推送闭环实施计划

最后更新：2026-09-18  
状态：**待实现；当前阶段只做可审计的运营决策与模拟推送，不接真实百万级触达平台。**

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
- 租户隔离、幂等账本、运营 Memory 和不可变 Artifact 模式。

### 尚未实现

- `AudienceRule`、`PushPlan`、审核记录、投递任务和效果回执领域模型。
- `PushStrategyGenerator` 与 `PushPolicyValidator`。
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
- S/A 级、全站或 App 通知必须人工审核。
- 人群规则不超出已确认地域、主题和租户范围。
- 同事件、同策略、同人群版本、同渠道不重复发送。
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
tenant_id + event_id + strategy_version + audience_version + channel
```

### DeliveryResult

回流目标人数、送达、打开、有效阅读、负反馈和关闭通知等聚合结果，不保存本地用户行为
明细。MVP 可使用定时查询，生产可由回调或企业 MQ 传递。

## 6. 实施顺序

1. 定义 Pydantic Schema、状态枚举和策略版本契约。
2. 实现确定性 Validator，并先覆盖重复、过期、越权、证据不足和高风险样例。
3. 实现候选策略生成器；结构化输出必须经过 Validator。
4. 新增 PostgreSQL 表、Repository、审核 API 和租户/角色权限。
5. 定义 RPC Protocol，先实现 Mock Client 与故障分类，再接企业 SDK。
6. 使用 Temporal 编排审核等待、人群创建、任务提交和结果查询；副作用调用有限重试。
7. 扩展黄金集、前端评审页、效果回流和运营 Memory；先 shadow，人工验收后再讨论真实触达。

## 7. Definition of Done

- 候选 `PushPlan` 为严格结构化输出，并绑定热点运行、事件、证据和策略版本。
- 不可信、重复、过期、越权或证据不足计划被确定性阻断。
- 高风险计划必须由具备权限且职责独立的运营人员批准。
- Mock RPC 覆盖成功、重复、超时、限流、部分失败与不可重试错误。
- Workflow 可从审核等待和可重试 RPC 故障处恢复，不产生第二次投递。
- 回执可追溯至 `plan_id/news_id/event_id`，并只保存聚合效果。
- 离线回放和 shadow 验收通过；未获单独生产授权时不得真实推送。

