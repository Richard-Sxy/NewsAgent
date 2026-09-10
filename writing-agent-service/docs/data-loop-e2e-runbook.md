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

这里需要区分两类“人工”：`prepare` 会用两个不同的合成 UUID 自动执行标签提交和
标签二审，以验证职责分离及 API 合约；真正读取 stdin、要求测试人员作出决定的是最终
发布闸门。L3 发布验收还必须用真实 IdP 用户和企业 Gateway 重跑，合成身份不能替代
真实人员身份验收。

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

## 3. 必测：真实人员在门禁处批准

### 3.1 准备到人工门禁

测试执行人运行：

```bash
docker compose -f deploy/docker-compose.data-loop-e2e.yml run --rm \
  e2e-runner prepare --state /state/manual-approve.json
```

命令会自动完成：Base Bundle 初始化、一次在线热点运行、运营拒绝、Feedback Case、
两个独立合成身份完成标签提交与二审、两层基准集冻结、Candidate 创建、Fresh 集自动
冻结和三层回放。它只有在下面三个条件同时满足时才成功退出：

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
该文件会自动覆盖批准与下一次运行、显式回滚、拒绝保持基线，以及“已审批但激活失败”
的受限恢复；因此它是完整 L2 自动验收入口。人工 stdin 行为仍按第 3、4 节单独验收。

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

SELECT labeled_by, approved_by, approval_status, label_version
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
Temporal time-skipping 集成环境中验证。当前仓库已有 Workflow 语义级回归，但专用
Compose 尚未内置 time-skipping server，这是仍需补进 CI L1 的测试基础设施 TODO。

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
