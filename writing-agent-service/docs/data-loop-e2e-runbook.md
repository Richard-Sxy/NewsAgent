# DataLoopAgent 全链路与人工介入验收手册

本文用于验证真实控制链，而不是规则 Demo。专用环境会启动 PostgreSQL、Temporal、
Redis、MinIO、API、热点 Worker、Data Loop Worker 和一个确定性 FastGPT HTTP 替身。
替身只让测试可重复；生产发布前还必须把相同流程对真实 FastGPT App 和企业数据
Adapter 再跑一遍。

## 1. 验收范围

完整链路如下：

```text
热点场景行为数据
  → HotNews Temporal Workflow
  → FastGPT 结构化分析
  → analysis_runs 持久化
  → 运营人员 rejected 决策
  → Feedback Case
  → 标注员提交标签
  → 独立二审人批准标签
  → Golden / High-risk 数据集人工冻结
  → Fresh bad-case 数据集自动冻结
  → Candidate 与三层离线回放
  → 确定性 Gate
  → waiting_approval（必须停住）
  → 发布审核人 APPROVE / REJECT
  → 审批账本
  → Bundle 激活或保持原版本
  → 下一次线上运行读取新 Active Bundle
  → 独立回滚人员显式回滚
```

测试驱动为每个职责生成不同 UUID，Gateway Token 只从环境变量读取，不写入状态
文件。代码同时拒绝以下自审行为：

- 标签提交人批准自己的标签；
- Candidate 提交人批准自己的 Candidate。

默认快速路径的 `prepare` 会用两个不同的合成 UUID 自动执行标签提交和标签二审，
以验证职责分离及 API 合约。需要真人逐岗操作时，使用第 2.1 节的 opt-in 命令；它会在
标签提交和独立二审处分别读取 stdin。无论采用哪条路径，最终发布闸门都必须读取 stdin
（除非 CI 显式传入 `--action`）。L3 发布验收还必须用真实 IdP 用户和企业 Gateway
重跑，合成身份不能替代真实人员身份验收。

## 2. 最快启动独立测试环境

在 `writing-agent-service` 目录执行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml up -d --build
```

确认所有核心服务：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml ps
curl --fail http://localhost:18000/ready
curl --fail http://localhost:13000/health
```

预期：API 返回 `status=ready`，FastGPT Stub 返回 `status=ok`。Temporal UI 位于
`http://localhost:18233`，MinIO Console 位于 `http://localhost:19001`。

确认迁移版本：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml exec -T api alembic current
```

预期 head 为 `20260910_0013`。这个 Compose 使用一次性本地凭据，不能用于生产。
所有宿主端口只绑定 `127.0.0.1`。

状态文件和数据库卷会跨普通 `down` 保留。每轮验收应使用新的状态文件名（例如把下文
的 `manual-approve.json` 改为带构建号的名称）；若误用已越过人工闸门的状态文件，驱动
会立即失败并提示换新路径，而不会等待旧 Workflow。需要从零重置时使用第 11 节的
`down -v`。

### 2.1 可选：真人完成标签提交与独立二审

这一模式替代第 3.1 节的单个 `prepare` 命令，但不会改变默认快速路径。第一位测试人员
先把真实链路运行到 Feedback Case：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare-feedback --state /state/manual-label.json
```

确认输出中的 `analysis_run_id`、`feedback_case_id`、原始分析证据和 `labeler` UUID 后，
由标签提交人员运行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner label-submit --state /state/manual-label.json
```

命令只有在操作人员输入字面值 `SUBMIT` 后才调用提交 API；标签必须处于 `pending`，
且 `labeled_by` 必须等于输出中的 `labeler` UUID。此 E2E 命令提交的是驱动内预设的
宽松 smoke-test 标签内容，人工动作只表示“确认提交”，不提供交互式标签编辑；真实标注
内容和标注 UI 的验收仍属于 L3。然后由另一位测试人员核对标签内容和
`label_reviewer` UUID，运行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner label-review --state /state/manual-label.json \
  --reason "独立二审已核对原始分析、证据和标签约束"
```

输入 `APPROVE` 时，驱动会验证 `approved_by != labeled_by`，并把标签批准时间保存为后续
Dataset 冻结截止时间。随后用同一状态继续到最终发布闸门：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-label.json
```

输入 `REJECT` 时，驱动调用标签 `/review` 接口执行 `request_changes`：服务端把当前标签
写为 `rejected`，同时保存独立审核人、审核时间、退回理由与审核幂等键。该版本不能冻结进
Dataset；标注人可把它作为 `expected_previous_version` 提交下一版本。权限为 `0600` 的
本地状态仍保存同一决定，用于中断恢复和服务端交叉核验。

CI 如需覆盖命令编排，可分别显式传入 `label-submit --action submit` 和
`label-review --action approve|reject`；不传 `--action` 才是本节要求的真人 stdin 验收。
状态文件仍不保存或输出 Gateway Token。

## 3. 必测：真实人员在门禁处批准

### 3.1 准备到人工门禁

测试执行人运行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-approve.json
```

默认路径会自动完成：Base Bundle 初始化、一次在线热点运行、运营拒绝、Feedback Case、
两个独立合成身份完成标签提交与二审、两层基准集冻结、Candidate 创建、Fresh 集自动
冻结和三层回放。若已按第 2.1 节完成真人标签二审，则此命令从已批准标签继续。它只有在
下面三个条件同时满足时才成功退出：

```text
phase == waiting_approval
gate_passed == true
waiting_for_approval == true
```

输出中必须保存并核对：`tenant_id`、`workflow_id`、`analysis_run_id`、
`feedback_case_id`、三个 `dataset_id`、`evaluation_run_id`、`candidate_id` 和各岗位
UUID。此时 Active Bundle 仍是 Base，不能提前切换。

### 3.2 审核人做真实决定

由另一位测试人员打开 Temporal UI 和上一步输出，确认三层 Dataset 及 Gate 后运行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner decide --state /state/manual-approve.json \
  --reason "人工审核已核对三层数据集、评估报告和版本差异"
```

命令不会默认批准。审核人必须亲自输入：

```text
APPROVE
```

输入其他内容不会提交任何发布决定。批准后驱动会验证：

- Candidate 状态为 `activated`；
- Active Bundle 切换到 Candidate；
- 再启动一次真实 HotNews Workflow；
- 新 `analysis_runs` 使用 Candidate Bundle 版本；
- FastGPT Stub 的新调用全部使用 Candidate App ID。

### 3.3 再核验并回滚

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify --state /state/manual-approve.json

docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner rollback --state /state/manual-approve.json

docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify --state /state/manual-approve.json
```

最终应看到 `rollback_ledger_created=true`、`rollback_restored_base=true`，且三个 Dataset
ID 互不相同。

## 4. 必测：人工拒绝路径

拒绝测试必须使用另一份状态文件；驱动会自动创建另一租户，避免污染批准路径：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-reject.json

docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner decide --state /state/manual-reject.json \
  --reason "人工审核拒绝该候选版本"
```

第二个命令中输入：

```text
REJECT
```

然后核验：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner verify --state /state/manual-reject.json
```

预期 Candidate 为 `rejected`，Active Bundle 仍是 Base，不得产生激活结果。

## 5. CI 自动模拟人工决定

自动模式显式传入动作，不读取 stdin。批准、验证下一次线上运行并回滚：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner auto --state /state/ci-approve.json \
  --action approve --rollback
```

拒绝分支：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner auto --state /state/ci-reject.json --action reject \
  --reason "Automated E2E reviewer rejected the candidate"
```

也可以对已启动环境运行 opt-in Pytest：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  --entrypoint python -e RUN_DATA_LOOP_E2E=1 e2e-runner \
  -m pytest tests/e2e/test_data_loop_full_chain.py -q
```

默认全量单测不会误连外部系统；只有 `RUN_DATA_LOOP_E2E=1` 才执行黑盒 E2E。
该文件的 5 个测试会自动覆盖批准与下一次运行、显式回滚、拒绝保持基线、人工标签退回的
服务端审计与冻结阻断、独立二审后恢复到发布闸门，以及“已审批但激活失败”的受限恢复；
因此它是完整 L2 自动
验收入口。自动套件用显式 `--action` 重放人工协议，真实 stdin 行为仍按第 2.1、3、4 节
单独验收。

## 6. 数据库账本人工复核

把 `<TENANT_UUID>` 替换成驱动输出的租户：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml exec -T postgres \
  psql -U newsagent_e2e -d newsagent_e2e
```

在 psql 中执行：

```sql
SELECT id, production_bundle_version, status, completed_at
FROM analysis_runs
WHERE tenant_id = '<TENANT_UUID>'
ORDER BY completed_at;

SELECT labeled_by, approved_by, reviewed_by, review_reason,
       approval_status, label_version
FROM feedback_labels
WHERE tenant_id = '<TENANT_UUID>';

SELECT dataset_layer, dataset_version, case_count, artifact_uri,
       content_sha256
FROM evaluation_datasets
WHERE tenant_id = '<TENANT_UUID>'
ORDER BY dataset_layer;

SELECT candidate_version, status, proposed_by, approved_evaluation_run_id
FROM configuration_candidates
WHERE tenant_id = '<TENANT_UUID>';

SELECT gate_decision, artifact_uri, artifact_sha256
FROM candidate_evaluation_runs
WHERE tenant_id = '<TENANT_UUID>';

SELECT action, actor_id, candidate_id, evaluation_run_id,
       from_bundle_id, to_bundle_id, created_at
FROM promotion_decisions
WHERE tenant_id = '<TENANT_UUID>'
ORDER BY created_at;

SELECT bundle_version, status, source_candidate_id, activated_by,
       activated_at, deactivated_at
FROM production_bundles
WHERE tenant_id = '<TENANT_UUID>'
ORDER BY created_at;
```

批准链预期有且仅有一条 `approve` 和一条 `activate`；回滚后再有一条 `rollback`。
`approve.actor_id` 必须不同于 Candidate 的 `proposed_by`。任意时刻每个租户只能有一个
`active` Bundle。

## 7. MinIO Artifact 复核

先列出对象：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  --entrypoint /bin/sh minio-init -c \
  'mc alias set local http://minio:9000 newsagent-e2e-minio newsagent-e2e-minio-secret >/dev/null && mc ls --recursive local/newsagent-e2e-artifacts'
```

针对 SQL 中每个 `artifact_uri`，用 `mc stat` 核对 Metadata，再用 `mc cat ... |
sha256sum` 计算内容哈希。必须满足：

- 实际 SHA-256 等于数据库的 `content_sha256` / `artifact_sha256`；
- Dataset 对象 Metadata 为 `artifact-kind=evaluation-dataset`；
- Evaluation 对象 Metadata 为 `artifact-kind=data-loop`；
- `content-sha256` Metadata 与对象内容一致；
- Golden、Fresh、High-risk 三层为不同 Dataset，且都能反查同一个已审批 Case 血缘。

## 8. 激活故障与恢复演练

只在此一次性 E2E 数据库执行。先创建专用场景，并从输出记下
`<TENANT_UUID>` 和 `<CANDIDATE_UUID>`：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/recovery.json
```

确认 Workflow 到达 `waiting_approval` 后，在 psql 注入仅匹配该租户和 Candidate 的
故障（先替换两个占位符）：

```sql
CREATE FUNCTION e2e_block_activate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.action = 'activate'
     AND NEW.tenant_id = '<TENANT_UUID>'
     AND NEW.candidate_id = '<CANDIDATE_UUID>'::uuid THEN
    RAISE EXCEPTION 'E2E forced activation failure';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER e2e_block_activation
BEFORE INSERT ON promotion_decisions
FOR EACH ROW EXECUTE FUNCTION e2e_block_activate();
```

提交批准，并用较短等待观察预期失败：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner decide --state /state/recovery.json \
  --action approve --skip-next-online-run --timeout-seconds 20
```

此命令超时是故障演练的预期现象；状态文件已经记住人工批准。确认 Candidate 为
`approved`、Workflow phase 为 `activate_bundle`，且只有 `approve` 账本存在后移除
故障：

```sql
DROP TRIGGER IF EXISTS e2e_block_activation ON promotion_decisions;
DROP FUNCTION IF EXISTS e2e_block_activate();
```

运行受限恢复：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner recover --state /state/recovery.json
```

预期：恢复流程复用原审核人、理由和审批幂等键；最终只有一条 `approve`、一条
`activate`、一个 Candidate Bundle，且下一次线上运行使用新 Bundle。恢复端点不能给
没有既有批准的 Workflow 越权激活。

恢复只处理“已批准但首次激活未完成”。正常激活后再由人工回滚属于新的治理决定；此时
调用 `recover` 必须被拒绝，不能用旧审批覆盖回滚。如需重新启用候选，必须创建新的
显式 roll-forward 审批和账本。

## 9. 48 小时无人审批

生产 Workflow 的默认策略是等待 48 小时，然后记录系统拒绝并返回
`approval_expired`，绝不自动激活。不要在普通 E2E 中真实等待 48 小时；该分支应在
Temporal 官方 time-skipping 测试环境中验证：

```bash
RUN_TEMPORAL_TIME_SKIPPING=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest tests/test_data_loop_time_skipping.py -q
```

该测试运行真实 `HotNewsDataLoopWorkflow` 和真实 Activity 边界，把 Temporal 逻辑时间
推进至少 48 小时，并断言最终只依次执行 `freeze_dataset`、`attribute_errors`、
`evaluate_candidate`、`record_promotion_decision`；最后一条决定必须是 `system/reject`，
`activate_bundle` 不得执行。

首次运行时 Temporal Python SDK 会下载与当前 SDK 兼容的官方 test-server 二进制；CI
可用 `TEMPORAL_TEST_SERVER_DOWNLOAD_DIR` 指定缓存目录，或用
`TEMPORAL_TEST_SERVER_PATH` 指向预置二进制以禁用下载。测试需要启动本地子进程和回环
端口，因此受限沙箱中要为这两项能力授权。它默认 opt-in，不会让普通单测误下载或启动
额外服务。

## 10. 测试分层与放行条件

| 层级 | 触发时机 | 必须通过的内容 |
| --- | --- | --- |
| L0 | 每个 PR | 单元、Schema、RBAC、幂等、Workflow/Activity 组件测试 |
| L1 | 每个 PR 或合并前 | 真实 PostgreSQL 迁移、MinIO 不可变写、Temporal time-skipping |
| L2 | 合并 / Nightly | 本文独立 Compose，approve、reject、next-run、rollback、recovery |
| L3 | 发布前 | 真实 FastGPT 两个 App、企业行为/内容 Adapter、真实 Gateway 身份与人工签署 |

只有 L2 通过，才能说“本地依赖全链路闭环”；只有 L3 通过，才能说“生产接入验收
完成”。FastGPT Stub 通过不能代替真实模型质量验收。

## 11. 清理

完成后执行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml down
```

若确认不再需要任何 E2E 数据，再执行下面的命令删除**仅属于该专用 Compose 项目**的
PostgreSQL、Temporal、MinIO 和状态卷：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml down -v
```
