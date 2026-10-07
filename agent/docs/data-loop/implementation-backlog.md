# DataLoop 实施顺序与验收

核对日期：2026-10-04。清单均为待实现/待验证项，本次只交付文档并核验既有能力。

## 1. 先补底座和当前断点

| 优先项 | 当前证据 | 最小修复与完成标准 |
|---|---|---|
| 原生DataLoop端到端入口 | `tests/e2e/test_data_loop_full_chain.py`调用 `examples.data_loop_e2e`，当前模块已不存在 | 重建原生driver/runbook；使用隔离租户/数据；贯通标签二审、冻结、评测、批准、激活、下一run与回滚 |
| Worker实际消费 | native-e2e Compose无DataLoop Worker；standalone为data-loop profile | 明确profile/队列/依赖装配，API与Worker使用相同Manifest/Prompt/模型快照；观测到实际poller与任务完成 |
| 迟到审批 | `_freeze_dataset`把cutoff绑定window_end，按发生窗挑选 | 引入独立摄取水位、跨窗待处理/重叠回补；当天发生次日批准能进入新批次，旧冻结集哈希不变 |
| UI请求契约 | `frontend/src/api/dataLoop.ts`发status，API接statuses；候选Diff示例为path/from/to | 对齐请求与后端asset/before_version/after_version/reason；通过真实API与浏览器验证筛选和候选提交 |
| 离线tenant透传 | Replay调用factory.create(spec)，默认offline-evaluation | 扩展Port及调用，模型请求/配额/审计使用真实tenant；双租户合同测试，不泄露案例 |

上述问题应先于“页面增加一个策略管理按钮”。恢复端到端入口时不得恢复历史框架服务或旧协议替身。

## 2. 建议里程碑

### M1：可审核建议与完整案例回流

上游：热点run/news、正文/证据快照及运营已批准的参数。

实现：新增建议/修订/审批事件/案例账本与迁移；复用 `PushStrategyInputBuilder`、`PushStrategyService` 的契约，在原生模型YAML中建立推送场景、Prompt及路由绑定；完善 `PushPolicyValidator`；提供运营inbox/详情/四类动作与理由。

当前推送Runner仍依赖通用 `StructuredAgentClient` 的执行identity，未装配原生push场景；只增加API不能算完成模型运行链。

当前Validator的审批匹配仅为 `approved_plan_id + approved_strategy_version`，尚无审批revision、内容hash、操作者和有效期的完整绑定。同plan/strategy版本的内容改变不能继续使用旧批准。

重点防止当前 `PushPolicyResult.can_dispatch` 被误用：B/C定向站内计划可能直接PASSED，即便仍为draft且requires_review=True。`test_low_risk_targeted_plan_passes`明确覆盖该现状。本窗口所有建议都应经过运营审批，投递资格必须另验合法状态、审批revision/hash和有效期。

下游：四类DecisionCase、待处理命令/收据、可追溯修改diff。采纳/延后进入案例池；确认为错误且二审的案例才进入坏例评测。没有真实触达。

完成标准：两个审核者并发只有一项决定；同键重放不重复回流；改稿使旧审批失效；跨租户、过期、无证据均阻断；四类动作刷新后仍可追踪。

### M2：Temporal 持久审批与跨天消费

实现：专属有界建议Workflow、审核deadline/延后Timer；审批命令台账/relay/对账；Case摄取水位和批次成员账本；稳定业务键、已提交步骤幂等、故障分级与观测。

完成标准：API断线、Worker重启、命令送达后响应丢失、DB提交后Activity回执丢失均能收敛；新闻过期后的批准被拒绝；次日批准和标签修订不漏不重；超量案例被分片而非丢弃。

### M3：团队运营策略集

实现：独立tenant/team策略注册表、[策略卡契约](strategy-card-template.md)、支持案例/反例、策略候选二审、不可变策略集、审核/显式激活/撤销/回滚。个人Memory保留既有用户隔离。

核心数据转换：Observation + Decision + 可用Outcome → 受限模型归纳 → 结构化策略候选 → 确定性规则校验与人审。模型只能提出有限候选；业务参数与证据等级不能由模型自授。

完成标准：一次建议采纳不会自动创建Active策略；提出者不能自审；仅批准且有效的本作用域策略进入下一次建议；冲突可解释；撤销阻断未执行建议并保留历史；Markdown导出与注册表hash一致。

### M4：策略专属评测与模拟闭环

实现：新闻背景/人群/时机/频控/文案/应推或不推的强类型标签；Golden/Fresh/High-risk的策略场景与反例；线上/上一实验双基准；冻结推送运行快照和独立评测器，复用已有Artifact/账本/Gate模式。

当前 `ProductionBundleSpec` 没有push-strategy/push-policy/push-prompt/audience/frequency版本；旧分析回放器也不验收这些维度。必须扩展真实可执行资产与输入契约，再谈策略门禁通过。

接模拟Audience/Delivery/Outcome Port，只保存结构化规则、人群版本和聚合结果。完整展示“新建议 → 审批 → 模拟执行/未执行 → 回流 → 策略候选 → 审核 → 下一建议复用”。

完成标准：成功、重复、超时未知、迟到/逆序回执、部分失败、更正撤回均有可验证状态；同事件合法新增进展不被永久去重；模拟结果明确标记，不声称企业模型质量或运营效果提升。

### M5：长稳与企业合同

实现：历史Workflow Replay/版本部署、逐case检查点与租户/全局配额、命令与Artifact清理/保留政策、告警、恢复演练、持续Temporal Schedule和回补治理；联调真实模型、人群、频控、投递、聚合效果和网关权限。

生产投递需要独立授权与平台合同。初期不在NewsAgent建设用户画像、原始行为存储或通知扇出系统。

## 3. 本次验证证据

主代理检查的 `/usr/bin/python3` 及 `agent/.venv/bin/python` 未具备pytest。本次将**当前工作区**的app/tests/pyproject复制到已存在的 `news-agent-native-e2e-api-1` 容器临时目录 `/tmp/newsagent-dataloop-docs-audit-20261004`，使用已有依赖运行10个模块，合计 **78个测试通过**。未重启服务、未清空数据库、未启动审批/投递任务。

该结果验证既有Schema、Mock/纯领域服务、反馈/数据集/Workflow逻辑和门禁；不代表前端、真实Temporal长稳、企业接口或实际发送验收。本次未启用time-skipping与真实E2E。tar复制产生的macOS扩展属性提示不影响pytest成功。

在已安装项目和dev依赖的隔离Python环境，可复验：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
python -m pytest \
  tests/test_push_policy.py \
  tests/test_push_strategy.py \
  tests/test_hot_news_decision_service.py \
  tests/test_feedback_collector.py \
  tests/test_dataset_freezer.py \
  tests/test_data_loop_workflow.py \
  tests/test_data_loop_activity_retries.py \
  tests/test_evaluation_gate.py \
  tests/test_production_bundle.py \
  tests/test_publication_outcome_feedback.py \
  -o addopts= -q
```

相关测试入口还包括 `test_data_loop_api/orchestrator/worker/step_handler/artifact_store/management_api.py`、`test_offline_replay.py`、`test_memory_promotion*.py`。测试名这里只表示对应文件族；新增变更时运行与变更相关的真实文件，不把所有旧结果视作本次实测。

真实审批Timer入口为 `tests/test_data_loop_time_skipping.py`，需显式 `RUN_TEMPORAL_TIME_SKIPPING=1` 并使用可用的测试Server。黑盒入口 `tests/e2e/test_data_loop_full_chain.py` 需 `RUN_DATA_LOOP_E2E=1`，但先修复缺失driver；不能用跳过的测试充当成功。

## 4. 新链路必须覆盖的验收矩阵

| 场景 | 期望结果 |
|---|---|
| accepted/rejected/deferred/corrected | 全部保留案例；错误标签独立复核；不混淆运营选择与模型质量 |
| 同键不同内容、两人并发审核 | 冲突可见，保留唯一权威决定与原收据 |
| 低风险草稿、修改已批计划 | 不可投递；新revision重验重审 |
| 提交/批准次日到达、提交乱序、水位推进前崩溃 | 不漏样本、不重复消费；冻结版本不被修改 |
| 审核时新闻有效、执行前过期/撤回 | 执行前阻断并保留原因 |
| 候选无反例、样本不足、跨事件泄漏 | 不晋升或明确证据不足，不报虚假效果 |
| 拟用未批/撤销/跨团队策略 | 拒绝；不能通过模型建议绕过策略注册表 |
| 模型调用限流/timeout、批次中途崩溃 | 有界恢复、已提交case不重做、租户预算与审计可追踪 |
| RPC受理后响应丢失、重复/逆序回执 | 用原业务键查询收敛；unknown不虚报成功；状态不倒退 |
| Worker升级与含待审Timer历史 | Replay兼容，旧运行可继续，参数与版本不可静默漂移 |
| 回滚后恢复旧激活、恢复已发送消息 | 不复活已回滚策略/Bundle；不伪造撤回事实 |

## 5. 与 TAOTIAN 原则的映射

保留其自动数据回流、错误归因、双基准、跨天经验与人审原则；调度落实为本项目Temporal有界任务和Schedule，状态落实为PostgreSQL账本与不可变Artifact。提交类有界重试，未知结果查证，数据门禁硬阻断，解释失败可降级。

不照搬电商业务、ODPS/训练平台、自动训练权限或“低风险Reward自动上线”。NewsAgent迭代的是获准的策略与可执行配置，任何扩大权限、改变生产规则/Prompt/模型和实际发布仍遵守相应人工Gate。

## 6. 未完成 TODO 汇总

- 原生DataLoop E2E driver/runbook、Worker与前端真实验收；修复现有UI契约错配。
- 完整建议快照/审批版本绑定/四类案例与结构化理由；原生推送模型场景。
- 迟到回补、水位乱序治理、审批命令收据/对账器、逐case检查点。
- 团队策略注册表、二审/激活/撤销、强类型推送评测和运行版本。
- 频控单位/时间窗/累计预算、业务去重、人群及模拟投递/回执Port。
- 模型真实tenant透传、全局配额、历史Replay和长稳观测。
- 企业接口与权限合同、真实触达授权、数据与Artifact保留/恢复政策。

实施时每个里程碑都需补完整入口、数据转换、类/函数、依赖、输出、配置、测试和剩余TODO；在证据齐全前不更新“生产已完成”状态。
