# NewsAgent 分层架构：编排、确定性计算、Agent 与人工 Gate

最后更新：2026-09-18

本文把 NewsAgent 各环节按"谁来控制、谁来计算、谁来决策"分层，作为团队评审和后续
实现边界的统一参考。判定规则只有一句话：

> 控制流 / 时序 / 权限 → 编排；数字 / 规则 / 校验 → 确定性代码；
> 开放文本理解与生成 → Agent；高风险变更与对外动作 → 人工 Gate。

本文是工程说明，不替代 `PROJECT_CONTEXT.md` 的长期上下文，也不替代生产 Memory。

## 1. 整体分层

```text
┌─────────────────────────────────────────────────────────────────────┐
│  L0 调度层  Temporal Schedule / 业务事件（有边界，一次只跑一个窗口）    │
│      idempotency: tenant_id + window + production_bundle_version      │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────┐
│  L1 编排层  Temporal Workflow（确定性代码，唯一控制流）               │
│   HotNewsMonitorWorkflow / NewsWritingWorkflow / DataLoopWorkflow    │
│   · 串行与分支 · 重试/退避 · 超时(45/90min) · 48h Timer · Signal/Gate │
└───────┬───────────────────────┬─────────────────────┬───────────────┘
        │                       │                     │
┌───────▼─────────┐   ┌─────────▼─────────┐   ┌───────▼───────────────┐
│ L2 确定性计算层  │   │ L3 Agent 推理层   │   │ L4 人工 Gate 层        │
│ Python / SQL     │   │ LLM，有界+结构化  │   │ 必须人审，超时=不动作   │
│ · 指标/基线/热度 │   │ · 热点分析解释    │   │ · 研究/大纲/终审门禁    │
│ · 重排规则       │   │ · 研究包生成      │   │ · 标签二审            │
│ · 数字/引用校验  │   │ · 章节写作        │   │ · 候选晋升            │
│ · 幂等/账本      │   │ · 评审            │   │ · 发布/回滚           │
│ · 数据集冻结     │   │ · 错误归因/候选   │   │                       │
└───────┬─────────┘   └─────────┬─────────┘   └───────┬───────────────┘
        │                       │                     │
┌───────▼───────────────────────▼─────────────────────▼───────────────┐
│  L5 事实/产物层  PostgreSQL(状态/快照/版本) S3(不可变Artifact)        │
│                  Redis(缓存/SSE) FastGPT(检索/模型应用，非事实源)     │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. 三条 Loop 的环节落位

### 2.1 Online Insight Loop（在线洞察）

| 环节 | 归属层 | 说明 |
|---|---|---|
| 窗口触发 / 幂等键 | L0 编排 | 有边界，不允许无界 `while` |
| 数据 watermark / 质量检查 | L2 确定性 | 失败属硬阻塞 |
| 行为数据查询 | L2 确定性 | 由企业 Adapter 提供，不落原始明细 |
| 指标聚合、历史基线、热度、排行 | L2 确定性 | 权威数字由 Python/SQL 计算 |
| 按 news_id 精确读内容 | L2 确定性 | 内容库为准 |
| FastGPT 关联新闻召回 | L2 确定性 | 检索是可重建投影 |
| 关联新闻重排 | L2 规则 | 规则可版本化、可 A/B |
| 构造 EvidencePacket | L2 确定性 | 证据白名单 |
| 趋势解释、假设、运营建议 | L3 Agent | 只解释与表达，不产出权威数字 |
| 数字反查 / 引用白名单 / 假设标记 | L2 确定性 | 校验是硬门槛 |
| 保存报告 Artifact | L2 → L5 | 不可变 |
| 运营采纳/驳回/延后/修正 | L4 人工 | 驳回/修正进入 Data Loop |

### 2.2 Data Loop（数据闭环）

| 环节 | 归属层 | 说明 |
|---|---|---|
| 读取 Active Production Bundle 快照 | L2 确定性 | 每次运行只读一次 |
| 校验失败/低置信/检索异常/运营拒绝 → Feedback Case | L2 确定性 | 统一转案 |
| 标签提交 / 独立二审 | L4 人工 | 禁止自审，代码强校验 |
| 按 cutoff 冻结三层数据集 | L2 确定性 | Golden / Fresh / High-risk，不可变 |
| 候选与三层离线回放 | L2 编排 + L3 推理 | 编排确定性，推理由 Agent 完成 |
| Gate 判定 | L2 确定性 | 版本化、确定性规则 |
| waiting_approval | L4 人工 + Timer | 48h 超时 → `system/reject`，绝不自动激活 |
| 审批账本 / 激活 / 回滚 | L2 确定性 | 稳定幂等键、可审计 |

### 2.3 Model Loop（模型闭环，当前仅迭代可控资产）

| 环节 | 归属层 | 说明 |
|---|---|---|
| 错误归因、候选 Prompt/规则/重排参数、Diff | L3 Agent | 只生成候选，不直接生效 |
| 三层评测 + 双基准对比 | L2 确定性 | 黄金集 / 新鲜 bad case / 高风险回归 |
| 候选批准、模型与生产发布 | L4 人工 | 高风险变更必须审批 |
| 超时 | L2 编排 | 默认不晋升、不发布 |

## 3. 自主权分级

| 级别 | 动作 | 例子 |
|---|---|---|
| 自动执行 | 数据同步、指标计算、检索、报告生成、离线评测 | 热度计算、EvidencePacket、Gate 回放 |
| Agent 自动 | 归因、解释、生成报告、生成候选与 Diff | 热点分析、错误归因、候选 Prompt |
| 人工必审 | Prompt / 热度规则 / 重排参数 / 模型 / 生产配置 / 发布 | 候选晋升、终审、CMS 发布 |
| 绝对禁止（自动） | 改线上配置、部署模型、自动发布新闻 | — |

## 4. 两条硬边界

1. **控制流永远不交给 LLM**：Agent 只能被 Workflow 的 Activity 调用，返回结构化结果；
   它不能决定"下一步做什么"，也不能自行重试或降级。
2. **数字永不由 LLM 生成**：LLM 引用指标必须能反查 Metric Snapshot；无证据必须标
   limitation；假设不能写成确定因果。

## 5. 代码映射

| 层 | 主要位置 |
|---|---|
| L1 编排 | `app/workflows/hot_news.py`、`app/workflows/news_writing.py`、`app/workflows/data_loop.py` |
| L2 确定性 | `app/analytics/*`、`app/evaluation/*`、`app/services/data_loop/*`、各 Validator |
| L3 Agent | `app/agents/hot_news_analysis.py`、`app/services/agents/*`（Research/Writer/Reviewer/ErrorAttribution） |
| L4 人工 Gate | `app/api/hot_news.py`（decisions）、`app/api/jobs.py`（decisions/resume）、`app/api/data_loop.py`（labels/decision/approval/rollback） |
| L5 事实/产物 | `app/models/*`、`app/storage/s3.py`、Redis SSE、FastGPT 检索适配器 |

## 6. 与 `TAOTIAN.md` 的映射

| TAOTIAN 原则 | NewsAgent 对应 |
|---|---|
| 用有边界的 Loop 组合系统，不做无限运行的大 Agent | L0/L1 的窗口化 Workflow + 三条 Loop |
| Data Loop 把失败/低置信/纠错转为版本化评测数据 | Feedback Case → 三层 Dataset |
| Model Loop 归因、生成候选、离线评测、人工决策 | 候选 + 三层回放 + Gate + 人工批准 |
| Harness 覆盖幂等、失败分级、重试上限、人工 Gate、超时不发布 | RetryPolicy、48h Timer、审批账本、显式回滚 |
| Agent 自主权分级 | 第 3 节自主权分级表 |
| Memory 分层、跨天可追溯 | Observation/Decision/Outcome + Approved Experience |
| 评测三集 + 双基准 | 黄金集 / 新鲜 bad case / 高风险回归 + 线上与上一实验基线 |

## 7. 总结

- Temporal 是骨架：决定何时跑、等谁、失败怎么办、超时默认不做什么。
- 确定性代码是神经系统：指标、规则、校验、账本。
- Agent 是可替换的肌肉：理解与表达，产出候选。
- 人工 Gate 是刹车：越靠近"改变系统行为"的动作，越靠人工。

运营推送是下一阶段的 L1/L2/L4 组合能力：Agent 只能生成候选策略，确定性代码校验范围、
证据、频控、过期和幂等，运营人员负责批准，Temporal 才能调用企业人群与投递 RPC。当前
尚未实现，边界与实施顺序见根目录 `PushPlan.md`。
