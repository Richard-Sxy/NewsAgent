# 热点分析 Data Loop

状态：代码级业务闭环已实现。生产启用前仍需在目标环境执行 Alembic、配置 PostgreSQL、
Temporal、S3/MinIO 与 FastGPT，并用真实租户数据做验收。

## 闭环调用链

```text
HotNewsMonitorWorkflow
  -> HotNewsActivities.run_hot_news_window
  -> ActiveProductionBundleHotNewsService (每次运行读取一个 active Bundle 快照)
  -> HotNewsOrchestrationService
  -> FastGPT HotNewsAnalysisAgentRunner + deterministic validator
  -> analysis_runs (validated input/output snapshot)
  -> AutomaticHotNewsFeedbackSink
       -> validation_failed / low_confidence / retrieval_error

Operator decision API ---------------------> Feedback Case
Aggregate publication outcome API --------> Feedback Case (policy triggered)
                                                |
                                                v
                                      human label + approval
                                                |
                                                v
                                   immutable Evaluation Dataset
                                   (PostgreSQL index + S3 JSON/hash)
                                                |
                                                v
                           optional ErrorAttributionAgent (read-only advice)
                                                |
                                                v
             candidate/base/previous replay on three frozen dataset layers
                                                |
                                                v
                   deterministic EvaluationGate + evaluation artifact
                                                |
                                                v
                  Temporal waits for human approve/reject (48 hours)
                                                |
                                                v
                       approve ledger -> explicit activation ledger
                                                |
                                                v
                        new active ProductionBundle / explicit rollback
```

黄金集、当日新鲜 Bad Case 集和高风险回归集缺一不可。`ErrorAttributionAgent` 只能输出
归因和候选修改建议，不参与权威评分，也不能直接修改生产版本。指标、门禁和基准比较均由
Python/SQL 确定性执行。

## 首次接入顺序

1. 执行 `alembic upgrade head`，当前迁移头为 `20260910_0013`。
2. 用 `POST /api/v1/data-loop/production-bundles/bootstrap` 注册该租户唯一的初始生产
   Bundle。此接口只允许首次初始化；后续只能走候选、评测、审批、激活或回滚。
3. 将企业行为、基线、正文和检索 Adapter 注入并运行热点 Worker。成功结果、模型校验失败、
   低置信结果和高热度无关联证据会自动进入 Feedback Case；运营拒绝/纠正可调用
   `POST /operator-decisions`；发布平台只提交聚合 Outcome，不提交用户行为明细。
4. 在 `/feedback-cases/{id}/labels` 提交强类型标签，再调用对应 `/approve` 接口人工
   批准。提交标签和批准标签是两个版本化动作，且批准人不能是原标签提交人。
5. 用 `/datasets/freeze` 分别冻结 `golden` 与 `high_risk_regression` 基线集。日常运行会
   自动把指定窗口内、截至窗口截止时刻已批准的 Case 冻结为 `fresh_bad_case`。
6. 用 `/configuration-candidates` 提交一个相对当前 Bundle 的强类型 Diff。所有改变的资产
   必须与完整 `ProductionBundleSpec` 一致。
7. 调用 `POST /runs` 启动 Data Loop Workflow，并给出候选、黄金集、高风险集以及门禁
   版本。若使用双基准门禁，还必须给出上一实验 Candidate。
8. Workflow 完成回放且门禁通过后处于 `waiting_approval`。调用 `/runs/{id}/decision`
   批准或拒绝；批准会先写审批账本，再执行独立激活动作。48 小时无响应默认拒绝。

## HTTP 控制面

- `POST /api/v1/data-loop/operator-decisions`
- `GET /api/v1/data-loop/feedback-cases`
- `GET|POST /api/v1/data-loop/feedback-cases/{case_id}/labels`
- `POST /api/v1/data-loop/feedback-cases/{case_id}/labels/{label_id}/approve`
- `POST /api/v1/data-loop/publication-outcomes`
- `POST /api/v1/data-loop/datasets/freeze`
- `GET /api/v1/data-loop/datasets/{dataset_id}`
- `POST /api/v1/data-loop/production-bundles/bootstrap`
- `GET /api/v1/data-loop/production-bundles/active`
- `POST /api/v1/data-loop/configuration-candidates`
- `GET /api/v1/data-loop/configuration-candidates/{candidate_id}`
- `POST /api/v1/data-loop/runs`
- `GET /api/v1/data-loop/runs/{workflow_id}`
- `POST /api/v1/data-loop/runs/{workflow_id}/decision`
- `POST /api/v1/data-loop/runs/{workflow_id}/activation/recover`
- `POST /api/v1/data-loop/production-bundles/rollback`

### 可信网关认证与最小权限

整个 `/api/v1/data-loop/**` 控制面默认关闭。API 容器必须通过 Secret 注入
`DATA_LOOP_GATEWAY_TOKEN`；未配置或为空时所有 Data Loop 管理请求返回 `503`。可信网关
调用时必须同时发送：

```http
Authorization: Bearer <DATA_LOOP_GATEWAY_TOKEN>
X-Tenant-ID: <authenticated-tenant-uuid>
X-User-ID: <authenticated-user-uuid>
X-Data-Loop-Roles: data-loop:read,data-loop:label-submit
```

网关必须先删除外部客户端自带的 `X-Tenant-ID`、`X-User-ID` 和
`X-Data-Loop-Roles`，再依据已认证会话重新注入；API 不接受请求体声明审核者、批准者或
激活者身份。共享 Token 只应存在于网关和 API 容器，不下发给浏览器，也不注入 Worker。

| 最小权限 | 允许操作 |
| --- | --- |
| `data-loop:read` | 查询 Workflow、Feedback、标签、Dataset、当前 Bundle 和候选 |
| `data-loop:feedback-write` | 写运营决策和聚合发布 Outcome |
| `data-loop:label-submit` | 提交强类型人工标签 |
| `data-loop:label-approve` | 独立二审标签 |
| `data-loop:dataset-manage` | 冻结评测 Dataset |
| `data-loop:candidate-manage` | 初始化已注册 Bundle、提交候选 |
| `data-loop:run` | 发起有边界的离线评测 Workflow |
| `data-loop:release-approve` | 批准/拒绝发布以及恢复已批准的激活 |
| `data-loop:rollback` | 显式回滚生产 Bundle |
| `data-loop:admin` | 紧急管理用途，可执行以上全部操作 |

身份 Header 必须是 UUID，权限 Header 使用逗号分隔。Bearer 无效返回 `401`，缺少权限返回
`403`。建议网关默认不授予 `data-loop:admin`，并分别绑定标注、一审/二审、实验执行、发布
和回滚岗位。

## Worker 与配置

```bash
writing-agent-data-loop-worker
```

必需基础配置沿用主服务的 `DATABASE_URL`、`TEMPORAL_ADDRESS`、
`TEMPORAL_NAMESPACE`、`FASTGPT_BASE_URL`、`FASTGPT_API_KEY`、`ARTIFACT_*`；新增：

```text
TEMPORAL_DATA_LOOP_TASK_QUEUE=hot-news-data-loop
FASTGPT_ERROR_ATTRIBUTION_APP_ID=<optional>
HOT_NEWS_RUNTIME_MANIFEST_JSON=<complete ProductionBundleSpec array>
DATA_LOOP_REPLAY_MAX_CONCURRENCY=8
DATA_LOOP_MAX_CASES_PER_COHORT=20
DATA_LOOP_GATEWAY_TOKEN=<secret; API container only>
```

不配置归因 App 时，归因阶段记录为 `degraded`，确定性评测仍可继续。当前内置门禁：

- `hot-news-gate-v1`：候选与线上基准比较；
- `hot-news-gate-double-baseline-v1`：同时要求上一实验 Candidate 基准。

门禁版本只从服务端版本化 Registry 读取，不接受请求直接传阈值。

## 幂等、恢复与安全边界

- Workflow ID、Dataset、Evaluation、Decision 和 Activation 都使用稳定幂等键；有副作用
  Activity 最多尝试两次（首次加一次重试）。
- 已提交到 PostgreSQL 的 Evaluation 会先查账本后重放，不会再次调用模型。FastGPT 本身
  没有业务幂等键，因此进程若在模型已响应、Evaluation 账本尚未提交之间崩溃，Activity 的
  一次受限重试仍可能重复调用模型；当前执行语义是有界的 at-least-once，而不是虚假的
  exactly-once。
- Dataset 与评测产物按内容 SHA-256 定址；读取时同时校验 S3 内容、Manifest、数据库
  Reference 与逐 Case 索引。写入使用 `If-None-Match: *`，遇到并发对象时会读取并核验
  内容和元数据，不覆盖已有对象。
- PostgreSQL 账本使用租户复合外键、唯一约束、状态 CAS 和不可变触发器。
- Bootstrap 与 Rollback 会在写活跃版本前检查完整 Bundle 已登记在
  `HOT_NEWS_RUNTIME_MANIFEST_JSON`；候选允许先提案，评测/激活仍会再次 fail-closed 校验。
- 原始用户行为不进入 Data Loop；失败模型原文也不保存。仅保留聚合指标、受控输入快照、
  已校验输出、标签、证据引用、版本和哈希。
- 新闻正文、检索文本、模型输出与归因建议一律按不可信数据处理。
- 生产发布永远需要人工审批命令（Temporal Update）；超时不会晋升。回滚同样是显式、
  可审计命令。
- 标签提交人与标签二审人、Candidate 提交人与发布批准人均执行四眼分离；服务层拒绝
  自审，数据库迁移同时约束标签自审和批准账本中的 Candidate 自审。
- 审批使用原子的 Temporal Update；已批准但激活步骤发生基础设施故障时，只允许通过
  `/activation/recover` 先幂等重放原审批账本、再重放原审批授权的同一激活幂等键，不能借
  恢复接口替换候选、评测、审核人或审批理由。
- 已完成的激活或人工回滚不能通过恢复接口再次启用；激活账本重放若指向已 inactive 的
  Bundle 会 fail closed，重新启用必须走新的显式 roll-forward 审批。

## 测试入口

```bash
pytest -q
alembic heads
alembic upgrade head --sql
docker compose -f deploy/docker-compose.data-loop-e2e.yml config --quiet
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  --entrypoint python -e RUN_DATA_LOOP_E2E=1 e2e-runner \
  -m pytest tests/e2e/test_data_loop_full_chain.py -q
```

完整的独立依赖环境、人工 `APPROVE`/`REJECT` 停点、激活恢复、下一次线上运行与回滚步骤见
[`docs/data-loop-e2e-runbook.md`](../../../docs/data-loop-e2e-runbook.md)。其中
`examples/data_loop_e2e.py` 是可恢复的验收驱动，状态文件不保存 Gateway Token；
`examples/fastgpt_e2e_stub.py` 只用于稳定验证管道，不能替代真实 FastGPT 质量验收。

2026-09-10 全服务默认回归为 554 项通过、3 项 opt-in E2E 跳过；独立 Compose 的完整
L2 黑盒套件为 3 项通过，并已另外实操 stdin 人工 `REJECT` 停点。两条默认回归告警来自
Starlette/httpx 的弃用提示。

重点测试覆盖 Feedback Schema/Repository/Collector、自动回流边界、人工决策原子性、
Dataset Freezer 完整性、归因引用校验、三层 Offline Replay、双基准 Gate、Bundle 状态机、
Temporal Workflow/Activity、Step Handler、Bearer 网关认证、租户/用户身份注入、端点级 RBAC
与故障演练。

## 生产验收仍需完成

- 在目标生产 PostgreSQL 实例执行迁移的 upgrade/downgrade 演练与并发冲突测试；本地
  Compose PostgreSQL 已通过 `20260910_0013` 迁移与闭环验收。
- 用真实 S3/MinIO 验证 SSE/KMS、Bucket Versioning、Object Lock、retention policy 和
  生命周期；清理数据库失败产生的孤儿内容寻址对象。
- 配置三个数据集的真实样本量和业务阈值，经过运营、算法与安全评审后发布新的门禁版本。
- 对 FastGPT 各 Bundle 的 App ID/Prompt/模型版本建立外部不可变发布约束；当前仓库只记录
  和选择版本，不声称自主实现 FastGPT 能力。
- 在企业网关/IdP 中把上述权限映射到真实用户组，轮换共享 Token，并把认证失败、越权和
  高风险操作接入审计告警。
- 接入企业行为/基线/正文/检索 RPC 的真实 SDK、鉴权与服务发现，并由部署侧完成热点
  Worker 的真实 Adapter 装配和 Schedule；仓库中的 Worker/Adapter 契约与 Data Loop 已就绪，
  但不能替代企业环境联调。
- 若生产要求模型调用严格 exactly-once，需要 FastGPT 提供幂等请求键，或增加逐 Case
  外部调用 checkpoint；当前最多一次自动重试只限制放大范围，不能消除提交前崩溃窗口。
