### 1.NewsAgent 运营推送链路完善计划

## 1. 项目目标与边界

当前项目的核心仍然是 Hot News Analysis Agent：根据行为指标发现热点，结合正文与相关新闻证据，解释热点原因并输出可信分析。

下一步建议把项目完善到“运营决策闭环”，而不是自己开发完整的百万级设备推送基础设施：

热点排行
→ 相关新闻检索与重排
→ HotNewsAnalysisAgent
→ 运营策略生成
→ 确定性规则校验
→ 人工审核
→ RPC创建目标人群
→ RPC提交推送任务
→ MQ/推送平台执行
→ 效果数据回流

项目需要真实实现：策略生成、规则校验、审核状态、RPC Adapter、任务状态和效果回流。

项目可以模拟：用户画像服务、地域圈选服务、厂商推送平台和百万用户扇出。

## 2. 模块划分

# 2.1 HotNewsAnalysisAgent

职责：回答“发生了什么、为什么变热、证据是什么、可能影响谁”。

建议补充输出字段：

class HotNewsAnalysisReport:
    news_id: str
    event_id: str
    trend_summary: str
    attention_reasons: tuple[AttentionReason, ...]
    affected_region_codes: tuple[str, ...]
    affected_groups: tuple[str, ...]
    evidence_news_ids: tuple[str, ...]
    limitations: tuple[str, ...]

这里的地域和人群只是新闻影响范围，不是具体用户集合。

# 2.2 PushStrategyGenerator

职责：把热点分析结果转换为运营建议，包括：

运营等级：S/A/B/C；

推荐渠道：全站置顶、频道置顶、热榜、信息流、App 通知、消息盒子；

目标人群条件：地域、兴趣、订阅、活跃状态；

发送时间和失效时间；

是否需要人工审核；

是否需要建立专题或持续跟踪。

该模块可以使用 LLM，但只能生成“候选计划”，不能直接执行推送。

# 2.3 PushPolicyValidator

职责：用确定性代码检查候选计划：

news_id/event_id 是否与可信输入一致；

证据是否来自允许集合；

S/A 级计划是否要求人工审核；

推送人群是否超出新闻影响范围；

同一 event_id 是否已经推送过；

是否超过用户、频道或事件频控；

新闻是否已经过期；

是否存在主体错位、风险标签或未确认信息；

标题是否包含无证据的绝对化表达。

校验失败直接阻断，不允许静默降级后继续发送。

# 2.4 EditorialApproval

运营人员可以通过、拒绝或修改计划。建议状态：

DRAFT
→ PENDING_REVIEW
→ APPROVED / REJECTED
→ AUDIENCE_RESOLVING
→ READY_TO_DISPATCH
→ DISPATCHING
→ SENT / FAILED / EXPIRED

需要记录 reviewer_id、审核时间、修改前后内容和拒绝原因，便于审计和后续评测。

## 3. 核心领域对象

避免使用无约束的 target_audience: dict，建议结构化：

class AudienceRule:
    region_codes: tuple[str, ...] = ()
    interest_tags: tuple[str, ...] = ()
    subscription_topics: tuple[str, ...] = ()
    user_segments: tuple[str, ...] = ()
    active_within_days: int | None = None
    notification_enabled: bool = True


class PushPlan:
    plan_id: str
    tenant_id: str
    news_id: str
    event_id: str
    priority: str
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

strategy_version 用于说明本次计划采用哪一版运营规则；event_id 用于同事件去重；plan_id 用于审核、执行和效果回流关联。

## 4. 最少需要对接的企业 RPC

# 4.1 人群圈选 RPC

用途：根据运营条件创建人群包，返回 audience_id，不要向 Agent 返回大量用户 ID。

CreateAudienceRequest

参数

含义

tenant_id

租户隔离

request_id

请求追踪标识

idempotency_key

防止重复创建人群包

news_id / event_id

关联新闻和事件

region_codes

行政区编码，优先使用城市级编码

interest_tags

科技、财经、体育等兴趣标签

subscription_topics

用户明确订阅的主题

user_segments

新用户、活跃用户、付费用户等分群

active_within_days

最近多少天活跃

notification_enabled

是否已开启通知

exclude_recently_reached

是否排除近期已触达用户

expire_at

人群快照失效时间

CreateAudienceResponse

{
  "audience_id": "audience-8821",
  "audience_version": "v1",
  "estimated_user_count": 320000,
  "expire_at": "2026-09-18T20:00:00+08:00"
}

第一版可以实现 MockAudienceRpcClient，根据条件返回固定或计算出的估算人数。

# 4.2 消息推送 RPC

用途：把已经审核通过的计划提交给企业消息平台。

SubmitPushTaskRequest

参数

含义

tenant_id

租户隔离

idempotency_key

防止重复创建推送任务

plan_id

关联运营计划

news_id / event_id

内容身份与事件去重

audience_id / audience_version

目标人群快照

channels

App Push、消息盒子、信息流等

title / summary

推送文案

deep_link

新闻详情页或专题页

priority

推送等级

send_time

计划发送时间

expire_at

超时后禁止继续发送

frequency_limit

频控参数

strategy_version

运营策略版本

approved_by

审核人

trace_id

全链路日志追踪

SubmitPushTaskResponse

{
  "delivery_task_id": "push-task-10086",
  "status": "ACCEPTED",
  "accepted_at": "2026-09-18T10:30:00+08:00"
}

第一版只需要模拟任务受理、失败、超时和重复请求，不需要真的对接 APNs、华为或小米推送。

# 4.3 推送结果查询或回调 RPC

用于把执行结果回流到运营平台，至少包含：

{
  "delivery_task_id": "push-task-10086",
  "status": "COMPLETED",
  "targeted_users": 320000,
  "delivered_users": 301200,
  "opened_users": 48200,
  "effective_read_users": 31500,
  "negative_feedback_users": 320,
  "notification_disabled_users": 85
}

如果只做 MVP，也可以先用定时查询代替回调。

## 5. Temporal、MQ 与 RPC 的分工

Temporal：管理一次热点运营任务的长流程、等待人工审核、超时、重试和状态恢复。

RPC：同步创建人群包、提交推送任务、查询任务状态。

MQ：推送平台内部的大规模用户分片、异步发送和重试。

Outbox：保证“数据库状态已提交”和“推送任务消息已发布”的最终一致性。

Temporal Workflow 的粒度是一份 PushPlan，不能为每个用户创建 Workflow。

建议幂等键：

tenant_id + event_id + strategy_version + audience_version + channel

同一幂等键重复调用时，应返回原有任务，不得重复发送。

## 6. 如何复用现有黄金评测集

现有热点分析黄金集继续评测：

热点结论是否正确；

指标引用是否正确；

reason_type 是否合规；

证据新闻是否属于白名单；

无证据时是否给出 limitation。

在此基础上新增“运营策略黄金集”，每个样例增加：

{
  "allowed_priorities": ["A", "B"],
  "required_channels": ["TECH_CHANNEL"],
  "forbidden_channels": ["GLOBAL_PUSH"],
  "required_audience_rules": {
    "interest_tags": ["AI", "科技"]
  },
  "requires_review": true,
  "must_reference_evidence": true,
  "expected_policy_result": "PASS"
}

建议分四层评测：

结构评测：字段、枚举、时间范围和 ID 是否合法。

策略评测：优先级、渠道、人群和审核要求是否落在黄金允许范围。

安全评测：重复事件、证据不足、过期新闻、越权人群是否被 Validator 阻断。

链路评测：RPC 超时、重复请求、审核拒绝和推送失败后，状态是否正确恢复。

不要要求生成文本与黄金答案逐字一致，重点比较约束字段、必选项、禁止项和证据引用。

## 7. 下一步实施顺序

阶段一：固定契约

增加 event_id、影响地域和影响人群字段；

定义 AudienceRule、PushPlan 和状态枚举；

确定 S/A/B/C 对应的渠道和审核规则。

阶段二：实现策略生成与校验

实现 PushStrategyGenerator；

实现确定性的 PushPolicyValidator；

增加重复事件、过期时间、证据、频控和高风险阻断测试。

阶段三：实现人工审核

建立推送计划表和审核记录表；

提供通过、拒绝、修改接口；

保存修改前后内容和操作人。

阶段四：实现 RPC Adapter

定义 AudienceService 和 DeliveryService Protocol；

先实现 Mock Client；

覆盖成功、超时、限流、重复请求和服务不可用场景。

阶段五：接入 Temporal 与 Outbox

编排生成、校验、等待审核、人群创建、任务提交和回执；

为 Activity 设置合理的超时与重试；

对确定性业务错误设置为不可重试；

使用 Outbox 记录待发送事件。

阶段六：扩展评测与效果回流

在现有黄金集上补充运营策略标签；

增加 Validator 对抗样例和 RPC 故障样例；

记录策略采纳率、人工修改率、有效阅读率和负反馈率；

第一阶段只做离线回放或 shadow 模式，不真实触达用户。

## 8. MVP 完成标准

满足以下条件即可认为推送部分形成完整项目闭环：

热点分析结果可以生成结构化 PushPlan；

不可信、重复、过期或越权计划会被阻断；

S/A 级计划必须经过人工审核；

可以通过 Mock RPC 创建 audience_id 并提交推送任务；

重复调用不会产生第二次任务；

Temporal 能从等待审核或 RPC 失败位置恢复；

推送结果能够关联回 plan_id/news_id/event_id；

黄金集能够评测热点分析和运营策略两个层次。

## 9. 暂时不需要实现

精确定位和用户位置采集；

真实百万级用户 ID 扇出；

APNs、华为、小米等厂商通道；

完整推荐算法或实时用户画像平台；

自动绕过人工审核的全自动推送。

项目最终定位：

以热点分析 Agent 为核心，补充运营策略、规则校验、人工审核、企业 RPC 接口和效果回流，形成可演示、可评测、可恢复的新闻运营决策闭环。
