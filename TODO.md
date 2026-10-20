# 统一对话 Agent · 执行计划（TODO）

状态：M0 工程合同与 M1–M5 工程实现已通过本地验收。正式人工评审与生产审批仍独立保留。
本文件是与助手对话迭代的活文档，
每次沟通收敛后更新对应条目。

最后更新：2026-10-13

前端补充：2026-10-13按用户要求精简对话与运营界面，技术参数默认折叠，保留业务状态、
确认内容和人工动作。前端332项测试、lint/typecheck/build通过；调用链与测试指引见
[简洁界面与对话测试](agent/docs/frontend-conversation-testing.md)。

## 0. 已确认的架构决策

- [x] 对话 Agent 是唯一入口，所有能力通过薄适配 hook 调用既有 Service/Workflow，
  不重建第二套业务逻辑（沿用 `query_hot_news` 已验证模式）。
- [x] 长任务一律异步：对话轮次内只同步等待「启动确认」，绝不等任务完成。
- [x] 审批动作只在页面（/jobs、/data-loop），对话内只读展示 Gate 状态并提供跳转。
- [x] 落地回执三态：已落库（实体 ID + 版本）/ 已保存待人工审批 / 未落地（纯解释）。
- [x] 不动数据库 Schema、不改变现有工具行为；每阶段独立可验收、可回滚。

## 1. 异步任务模式（核心新设计）

```text
对话轮次内（≤60s 预算）                轮次外（分钟~小时级）
────────────────────────              ─────────────────────────
用户："写一篇AI芯片稿"
→ start_writing_job hook               Temporal 写作 Workflow
  ├ 权限校验（writing:admin）           ├ Research → 人工Gate → …
  ├ 幂等（request_id 绑定 job 键）      └ 状态持续写 PostgreSQL
  ├ 启动 Workflow（只等 accepted）
  └ 立即返回回执卡片
卡片：job_id + 状态 + 跳转链接 ──轮询──→ JobService 状态
后续追问："写作进度？" → writing_job_status hook 读快照
```

- 断线语义沿用 `query_hot_news` 先例：取消对话等待，不取消已启动的 Workflow。
- 回执卡片自带轮询（复用现有 3 秒 poll 模式），对话不承载长等待。

## 2. 阶段计划

### M0 合同冻结（0.5-1 周）

前置依赖：无（纯新增，不动现有工具）

- [x] T0.1 定义 `CapabilityReceipt` schema
      改动：新增 `agent/app/conversation/receipts.py`
      内容：`entity_type`（枚举：hot_news_run / writing_job / feedback_case /
      memory / candidate / evaluation_run）、`entity_id`、`status`
      （landed / pending_approval / not_landed）、`version`、`deep_link`
      （仅白名单路径：/hot-news、/jobs、/data-loop）、`pending_gate`（可空）
      验收：非法 entity_type / 越界 deep_link 被校验器拒绝
      测试：新增 `agent/tests/test_conversation_receipts.py`
      （合法构造、枚举越界、deep_link 注入、缺失字段四类用例）
- [x] T0.2 扩展权限枚举
      改动：`agent/app/api/dependencies.py`（现有 HotNewsPermission 旁新增
      `writing:admin`、`data-loop:admin`，沿用同一挂载机制）
      验收：无新权限的用户在 runtime 返回的工具清单里看不到新 hook
      测试：扩展 `agent/tests/` 中网关权限相关用例
- [x] T0.3 设计 HookSpec 注册表结构
      改动：`agent/app/conversation/tools.py` 新增注册表常量
      （name / permission / argument_schema / execution_class / runner /
      receipt_builder 六字段），暂不注册任何新 hook
      验收：现有六个工具的行为完全不变（回归全绿）
      测试：现有 `test_conversation_agent.py` 全量通过
- [ ] T0.4 设计评审
      内容：三人过一遍 schema 与注册表（实体类型是否够用、
      确认卡片字段是否支撑 M2 交互）
      工程状态：三个独立 Agent 已审查合同、权限和卡片；意见与修复见第 9 节。
      未代替原定三位人工评审；正式人工意见回填后再勾选。

### M1 只读 hooks + 首张卡片（1 周）

前置依赖：M0 全部完成

- [x] T1.1 `list_writing_jobs` hook（sync_read）
      改动：`tools.py` 注册 + `service.py` 通用执行路径接 runner；
      runner 薄适配现有 JobService 列表查询
      依赖：T0.3
      验收：对话问「我有哪些写作任务」返回任务列表回执
      测试：`test_conversation_tools_writing.py` 新增（权限拒绝、
      参数校验、正常返回三用例）
- [x] T1.2 `writing_job_status` hook（sync_read）
      改动：同 T1.1 模式，按 job_id 查单个任务状态与当前 Gate
      验收：追问「那个任务进度怎样」返回阶段 + 等待审批状态
      测试：同上文件新增用例
- [x] T1.3 前端 `WritingJobCard` 组件
      改动：新增 `agent/frontend/src/components/conversation/WritingJobCard.vue`
      （继承第 5 节卡片基座规范：状态徽章、实体 ID、deep_link）
      验收：聊天历史中出现写作任务卡片，点击跳 /jobs 定位到该任务
      测试：`tests/writing-job-card.test.mjs`（三态渲染、未知类型降级）
- [x] T1.4 浏览器验收
      验收：5174 栈实测「发起一个写作任务（页面操作）→ 聊天里问进度 →
      卡片显示 + 跳转正确」全链路，截图或录屏留档
      测试入口：`cd agent/frontend && npm test && npm run lint && npm run build`
      + 后端 `docker compose ... test-runner python -m pytest -o addopts='' -q`

### M2 异步写作发起（1-2 周）

前置依赖：M1 完成且浏览器验收通过（交互形态确认）

- [x] T2.1 写作意图 hook（confirm_gated 第一段）
      改动：`tools.py` 注册 `start_writing_intent`；`service.py` 新增确认分支
      内容：模型从对话提取主题/要求（仅建议值，Python 构造回执字段），
      挂 turn inputs 槽位，未确认前不启动任何 Workflow
      验收：说「写一篇 AI 芯片周报」→ 弹确认卡片（主题/篇幅/语气，
      标注「模型建议」）→ 无 Workflow 产生
      测试：新增 `test_conversation_writing_confirm.py`
      （意图提取、未确认不启动、参数白名单三用例）
- [x] T2.2 确认执行（confirm_gated 第二段）
      改动：`service.py` turn inputs 消费逻辑 + `OrchestratorService.start_job`
      （job 幂等键绑定 request_id）
      验收：点「确认启动」→ Workflow accepted → 卡片转「已启动 job_id」；
      重复点击不重复启动
      测试：幂等 / 重复确认 / 确认令牌过期三用例
- [x] T2.3 断线语义
      改动：`service.py` 取消清理路径（学 query_hot_news：取消等待不取消
      Workflow；绑定信息回填轨迹）
      验收：确认后断网刷新，/jobs 中任务仍在执行
      测试：`test_conversation_stream.py` 扩展断线用例
- [x] T2.4 前端确认卡片与轮询
      改动：`WritingJobCard.vue` 扩展确认态（黄）；3 秒轮询
      （页面隐藏暂停、终态即停、连续失败降级，按第 5 节规范）
      验收：卡片 黄（待确认）→ 绿（已启动）→ 随任务推进自动刷新
      测试：`tests/writing-job-card.test.mjs` 扩展
      （防重复点击、过期作废、轮询启停）
- [x] T2.5 浏览器验收
      验收：全链路录屏：对话发起 → 确认 → /jobs 人工节点 → 卡片更新；
      全链路耗时统计显示正常

### M3 记忆 + 反馈沉淀（1 周）

前置依赖：M0；与 M2 可并行

- [x] T3.1 `save_short_term_memory`（sync 快写）
      改动：`tools.py` 注册，runner 薄适配 `MemoryWriteApplicationService`
      验收：「记住我关注科技频道」→ 绿色记忆卡（memory_id + 有效期）
      测试：新增 `test_conversation_memory_tools.py`
- [x] T3.2 `propose_long_term_memory`（仅候选）
      验收：回执为黄色「待人工晋升」卡，含候选 ID；对话追问记忆时
      附带候选状态（决策 4），不主动推送
      测试：同上文件新增候选/追问两用例
- [x] T3.3 `save_feedback_case`（confirm_gated）
      改动：runner 薄适配 `AnalysisFeedbackCollector.collect_from_analysis_memory`
      内容：确认卡片展示 run_id / news_id / 问题类型 / 期望标签草稿
      （v1.5 确认的交互）；关联 run_id 与 production_bundle_version
      验收：「把刚才的错误分析沉淀为案例」→ 确认 → case_id 卡片 →
      /data-loop 反馈案例页可见同一案例
      测试：新增 `test_conversation_feedback_hook.py`
      （确认流程、来源快照完整性、run 绑定）
- [x] T3.4 前端记忆卡 + 反馈沉淀卡
      改动：新增 `MemoryCard.vue`、`FeedbackCaseCard.vue`（基座规范）
      测试：`tests/receipt-cards.test.mjs`（三态渲染、未知类型降级）
- [x] T3.5 边界固化
      验收：审批类动作（二审/approve/activate/rollback）不在工具词表中，
      模型试图调用返回 denied
      测试：越权词表用例

### M4 DataLoop hooks（1-2 周，含部署调整）

前置依赖：M3 完成；部署切 bge-demo 栈（决策 3）

- [x] T4.1 环境切换与冒烟
      内容：`start_local_demo.py` 起栈，确认 data-loop-worker poller 在线、
      三层数据集已有冻结样本
      验收：`/ready` 三项 ok + Worker poller 探测通过
- [x] T4.2 `start_data_loop`（confirm_gated + async_start）
      改动：runner 薄适配 DataLoop Orchestrator（复用 `start_data_loop`
      API 同一服务层，非 HTTP 自调用）
      内容：确认卡片展示候选 ID / 黄金集 / 高风险集 / 策略版本
      验收：确认后 Workflow 启动，回执 run 卡片；断线不取消
      测试：新增 `test_conversation_dataloop_hook.py`
- [x] T4.3 `evaluation_status`（sync_read）
      验收：追问「评测跑完了吗」返回阶段 / 门禁 / 等待审批三态
      测试：同上文件新增
- [x] T4.4 `propose_bundle_candidate`（confirm_gated）
      改动：`tools.py` 注册；runner 薄适配候选提交服务层
      （与 `propose_configuration_candidate` API 同一服务，非 HTTP 自调用）
      内容：确认卡片展示基准 Bundle（当前 active）、candidate_version、
      structured_diff 摘要与理由；spec 与 diff 由 Python 按预置模板构造，
      模型仅产出建议值与理由草稿
      验收：用户于 2026-10-13 确认本轮支持已注册模型／Prompt 预设候选，
      重排调参另列后续 TODO；预设版本建议 → 确认真实 spec/diff → 候选创建为
      pending_evaluation → /data-loop 配置候选页可见；评测未通过前
      无法批准（数据库约束兜底）
      测试：`test_conversation_dataloop_hook.py` 新增
      （确认流程、spec 白名单、基准 Bundle 绑定）
- [x] T4.5 前端评测运行卡
      改动：新增 `EvaluationRunCard.vue`；轮询间隔 15 秒（评测为分钟级
      任务，对决策 2「3 秒」的例外，**用户已确认**；理由：降低读放大）
      测试：卡片渲染 + 轮询启停
- [ ] T4.6 浏览器验收与人工批准
      验收：对话发起评测 → run 卡片 → 三层回放完成 → 人工批准在
      /data-loop 页面完成（对话内无批准按钮）
      工程状态：浏览器链路、三层门禁与同一 Workflow 跳转已通过；
      当前 waiting_approval，正式人工决定仍待用户在评审台完成。

### M5 权限矩阵 + E2E + 演示（1 周）

前置依赖：M2-M4 全部完成

- [x] T5.1 按角色裁剪工具清单
      改动：runtime 接口返回获准 hooks；prompt 输入与本地规划器
      共用同一清单（防上下文膨胀）
      验收：READ 用户词表中无写操作 hook；admin 按权限可见对应组
      测试：权限矩阵用例（角色 × 工具清单一览表）
- [x] T5.2 E2E 补齐
      内容：幂等（同 UUID 重放）、越权拒绝、审批不可绕过、断线恢复
      四类场景的 HTTP 级验收
      测试：扩展 `test_conversation_hot_news_e2e.py` 同级 E2E 文件
- [x] T5.3 多租户并发项（联动第 7 节）
      内容：租户级并发预算（超限回「排队中」）、卡片轮询合并、
      多租户压测脚本与资源水位记录
      测试：并发压测脚本 + 水位报告留档
- [x] T5.4 demo 脚本与文档
      内容：一问发起 → 确认 → 卡片落地 → 跳转详情的演示脚本；
      更新 conversation-agent 文档
      验收：后端完整 pytest 回归 + 前端 test/lint/build + 浏览器全链路演示
      一次通过

## 3. 横切关注点（每阶段适用）

- 审计：每个 hook 挂 `model_request_id` + 工具轨迹 checkpoint（沿用现有机制）。
- 安全：模型不可传身份 / SQL / 日期 / 场景；工具参数白名单校验；
  新闻正文与模型输出均为不可信输入。
- 回归：每阶段完成时跑后端完整 pytest、前端 `npm test / lint / build`。
- 边界：对话 Agent 不可自批研究包 / 提纲 / 终稿，不可自动激活或回滚 Bundle，
  不可发布 CMS，不可修改 Prompt / 热度规则，不可扩大数据访问。

## 4. 已决策（2026-10-12 对话确认）

1. [x] 写作参数来源：模型从对话提取主题/要求 → 先展示确认卡片 →
   用户点确认后才启动 Workflow（防误发起，模型不直接构造最终参数）。
2. [x] 回执卡片轮询：3 秒固定、任务终态即停，不新增配置项。
3. [x] `data-loop-worker` 部署：M4 切换到 `start_local_demo.py`（bge-demo）栈验证，
   该栈已含 data-loop-worker；不改 native-e2e compose。
4. [x] 长期记忆候选：提交后回执卡片标注「待人工审批」；后续仅在用户追问
   记忆相关问题时附带候选状态，不主动推送。
5. [x] 推进节奏：M1（只读 hooks + 卡片）完成并浏览器验收后，评估交互形态
   再启动 M2（异步写作发起）。

## 5. 前端细节设计（2026-10-12 补充）

### 卡片体系（落地回执呈现）

- 统一基座组件 `CapabilityReceiptCard`：能力名 + 状态徽章
  （绿=已落库 / 黄=待人工审批 / 灰=未落地）、实体 ID + 版本、
  deep_link 跳转、时间戳；写作 / 反馈 / 记忆 / 评测卡片继承基座。
- 铁律：卡片数据只来自工具快照（turn.tools[].result），
  模型生成的数字与 ID 一律不渲染，保证确定性。
- 未知 entity_type 降级为只读 JSON 视图，不允许异常阻塞聊天主流程。

### 确认卡片（M2 确认步）

- 复用现有 turn inputs 机制（inputs 表 + append input API），
  不新造确认通道。
- 确认请求带幂等 ID，按钮防重复提交；确认卡片仅当轮有效，
  过期显示作废说明与重新发起入口。
- v1 只读确认 + 「重新描述」重试；可编辑表单留 v2。

### 轮询生命周期

- 3 秒固定、终态即停；页面不可见时暂停、恢复可见时续跑。
- 会话切换与组件卸载必须清理定时器；页面刷新后仅对
  未终态的历史卡片恢复轮询。
- 连续失败降级：显示「状态未知」，提供手动刷新与跳转详情，
  不无限重试。

### 跨轮次一致性

- 卡片轮询实体状态（job / run API），不回写历史消息文本；
  旧卡片自刷新，聊天记录保持不可变。
- 历史卡片显示「当时快照 + 当前最新状态」两层，不混淆。

### 权限可见性与灰度

- runtime 接口返回获准 hook 清单，前端按清单渲染功能入口：
  无权限即不可见，同时作为灰度开关
  （M1 只读卡片先上，确认卡片、异步发起依次放开）。

### 前端稳定性测试

- 每类卡片组件渲染测试（node:test + compileScript 现有模式）。
- 轮询启停 / 页面隐藏暂停 / 失败降级 / 刷新恢复测试。
- 确认卡片防重复提交与过期作废测试。

## 6. 工具调用层与 Hook 设计（2026-10-12 补充）

### 现状基线

现有链路：模型输出 `ConversationPlan`（直接回答 or 工具名+参数）→
`ConversationTools.execute()` 统一分发（权限检查、参数校验、有限重试一次）→
`ToolTrace` 轨迹 → `checkpoint_turn` 落库 → SSE `tool_finished` → 前端渲染。
`query_hot_news` 因「写操作 + 异步」走专用分支（参数逐字校验、预置 failed
轨迹占位、`remember_binding` 回填实体 ID）。Hook 设计是将该模式制度化，
不新造调用层。

### Hook 注册表（HookSpec）

```text
HookSpec {
  name              # 工具名词表项（进 ConversationPlan 词表）
  permission        # writing:admin / data-loop:admin / memory:write
  argument_schema   # Pydantic 严格白名单；模型不可传身份/SQL/日期/场景
  execution_class   # sync_read | async_start | confirm_gated
  runner            # 薄适配器：只调既有 Service，零业务逻辑复制
  receipt_builder   # 从服务端结果构造 CapabilityReceipt（Python 构造）
}
```

### 三类执行语义

| 类别 | 例子 | 语义 |
| --- | --- | --- |
| sync_read | writing_job_status、list_writing_jobs、evaluation_status | 走现有通用 `_execute`：有限重试一次，终态即返回 |
| async_start | start_data_loop、save_feedback_case | 学 `query_hot_news` 专用分支：预置 failed 轨迹占位 → 启动 Workflow 只等 accepted → `remember_binding` 回填实体 ID → 轮次立即结束，绝不等待完成 |
| confirm_gated | start_writing_job、save_feedback_case、propose_bundle_candidate、start_data_loop | 两段式：首轮只产出「待确认回执」（挂 turn inputs 槽位）→ 用户确认（现成 append-input API，带幂等 ID）→ 验证确认令牌后才启动，后续同 async_start |

### 统一守护（每个 hook 强制继承）

1. 权限在 `execute()` 入口统一拦截：无权限 → `denied` 轨迹 + error_code，
   模型只解释不重试。
2. 回执由 Python 从服务端结果构造；模型文本不可指定实体 ID / 版本 /
   deep_link（deep_link 仅白名单路径，防跳转注入）。
3. 重复调用检测（扩展现有 `seen_calls`）：同轮重复 → `denied: repeated_tool_call`。
4. 每个 hook 一条轨迹 + `model_request_id` 审计 + checkpoint 落库后
   前端才可渲染。
5. 工具词表按权限在服务端裁剪（runtime 返回获准清单，本地规划器与
   prompt 输入共用同一清单），防上下文膨胀。

### 现有工具注册链（新增 hook 的挂载点，已核实到行号）

| # | 位置 | 作用 |
| --- | --- | --- |
| 1 | `agent/app/conversation/plan.py:7` | 词表：`ToolName = Literal[...]`，工具名唯一枚举 |
| 2 | `agent/app/conversation/tools.py:154` | 分发器：`ConversationTools.execute()` 按 name 分发；参数校验与权限检查入口（专用 handler 示例 `execute_hot_news_query` 在 :130） |
| 3 | `agent/app/conversation/service.py:374` | 通用执行路径 `_execute()`：只读类挂这里（专用分支 `_execute_query()` 在 :401，写操作/异步类参照） |
| 4 | `agent/app/conversation/context.py:162` | 上下文注入：获准工具清单写入模型输入 `available_tools`（权限裁剪生效点） |
| 5 | `agent/app/conversation/local.py` | 本地规划器规则表：本地确定性模式下问题→工具的选择规则 |

配套（非必须）：`tools.py:286` `render_tool_results()` 结果渲染、
前端 `ChatView.vue` 工具进度 chips。

### 代码落点（设计层面）

- `app/conversation/plan.py`：ToolName 词表扩展。
- `app/conversation/tools.py`：HookSpec 注册表 + 分发改造
  （词表与分发从注册表生成，避免 15 个工具散落五处手改）。
- `app/conversation/service.py`：async_start / confirm_gated 专用分支扩展。
- `app/conversation/receipts.py`（新增）：CapabilityReceipt 与各实体校验器。

## 7. 多租户与并发考量（2026-10-12 补充）

### 已有防线（复用，不重建）

- 租户数据隔离：业务表全带 tenant_id，唯一约束与索引租户化，
  跨租户数据在服务层显式拒绝（`dataset_freezer.py`）。
- 同会话单飞：`uq_conversation_turns_processing` 部分唯一索引，
  数据库级保证同一会话同时仅一个处理中轮次（`models/conversation.py`）。
- 幂等：`(conversation_id, request_id)` 唯一；热点运行以
  tenant+窗口+bundle 版本为幂等维度。
- 状态机 CHECK：processing 无完成时间、终态必有，迟到提交不覆盖终态。
- 配置单活：每租户至多一个 active Bundle（部分唯一索引）。
- 分析子进程并发：全局 `Semaphore(max_concurrency)` + 行数/字节上限。
- SSE 有界队列（maxsize 64）与断线清理。

### 新增 hook 必须补的并发项

- [x] 租户级并发预算/限流：异步发起类 hook（写作、评测）增加
      per-tenant 并发上限与排队说明，防单租户占满共享 worker 池；
      超限返回明确的「排队中」回执而非静默失败。
- [x] 卡片轮询合并：同页同一实体多张卡片在前端合并一份请求；
      同租户多用户/多窗口的 GET 每次独立鉴权，再由后端有界
      singleflight/3秒或15秒缓存合并下游读取，避免跨身份浏览器广播。
      后端 job/run 单实体查询核对无数据库 N+1。
- [x] 负载验收：多租户并发发起对话 + 异步任务 + 卡片轮询的
      压测脚本与资源水位记录（并入 M5 验收）。

## 8. 变更记录

- 2026-10-12 v1：初版。确认统一对话 Agent + Capability Hook + 长任务异步模式，
  阶段 M0-M5 与五个开放问题。
- 2026-10-12 v1.1：五个开放问题全部决策完毕（见上节），阶段计划无结构性调整；
  M2 增加确认卡片步骤，M4 明确使用 bge-demo 栈。下一步：M0 合同冻结。
- 2026-10-12 v1.2：补充前端细节设计（卡片体系、确认卡片复用 turn inputs、
  轮询生命周期、跨轮次一致性、权限可见性灰度、稳定性测试）；
  交付目标明确为稳定系统：卡片数据只来自快照、异常降级不阻塞聊天。
- 2026-10-12 v1.3：补充工具调用层与 Hook 设计：HookSpec 注册表、
  sync_read / async_start / confirm_gated 三类执行语义、五条统一守护
  （权限拦截、回执 Python 构造、重复调用检测、轨迹审计、按权限裁剪词表）
  及代码落点。
- 2026-10-12 v1.4：补充多租户与并发考量：盘点既有数据库级防线
  （同会话单飞、幂等、状态机 CHECK、配置单活、有界队列），
  新增三项待办：租户级并发预算/限流、卡片轮询合并防读放大、
  多租户负载压测（并入 M5）。
- 2026-10-12 v1.5：明确 DataLoop 进入对话层的确认框交互：沉淀案例 /
  提候选 / 启动评测弹黄色确认卡片（Python 构造字段 + turn inputs 幂等确认），
  审批类动作（二审 / approve / activate / rollback）仍仅限评审台页面；
  confirm_gated 语义的适用清单同步扩充。
- 2026-10-12 v1.6：M0/M1 细化为任务卡粒度（改动点/依赖/验收/测试六要素，
  含具体文件路径与测试文件名）；M2-M5 待确认粒度后同样细化。
- 2026-10-12 v1.7：M2-M5 全部细化为任务卡（用户确认粒度后铺开）；
  全计划共 27 个任务卡（M0×4 / M1×4 / M2×5 / M3×5 / M4×5 / M5×4），
  每个带改动文件、前置依赖、验收标准、测试入口；
  新增一处待确认项：评测卡轮询间隔 15 秒（对决策 2 的例外）。
- 2026-10-12 v1.8：补 T4.4 `propose_bundle_candidate` 任务卡
  （消除 Hook 设计表与任务卡的不一致）；原 T4.4/T4.5 顺延为 T4.5/T4.6；
  任务卡总数 28，新增 hook 总数 9（现状 6 工具 + 9 = 未来 15）。
- 2026-10-12 v1.9：第 6 节补充「现有工具注册链」：五处挂载点
  （词表 / 分发器 / 执行语义 / 上下文注入 / 本地规划器）核实到行号，
  新增 hook 按注册表生成词表与分发，避免散落手改。


## 9. 实施记录与验收证据（2026-10-13）

- M0 工程合同：CapabilityReceipt 六类实体与三态、严格 ID/版本/时间和同实体白名单链接；
  Writing READ/ADMIN 独立网关头，复用已有 DataLoop ADMIN；HookSpec 六字段冻结，
  新词表从注册表生成。工程审查分别覆盖合同/权限、前端状态/轮询、服务复用/确认语义。
  人工三人评审尚未记录，T0.4 保持未勾选。
- M1：list_writing_jobs / writing_job_status 复用 JobService；卡片按工具落库快照渲染，
  同实体共享 3 秒轮询、隐藏暂停、终态停止、连续三次失败降级。5174 已验证页面创建
  任务 → 对话列表 → 跨轮追问 → 同 job_id 定位。用户已确认沿用卡片形态并继续 M2。
- M2：start_writing_intent 只建议 topic/target_words/tone；先 checkpoint 才发送确认快照。
  确认复用 inputs API，令牌绑定当轮/固定 input_request_id，先统一空白再检查权限；
  确认过期/取消/补充要求使旧确认作废。Token 与控制文本不会进入模型上下文。
  确认执行追加第二条不可变轨迹，job 业务幂等键绑定 conversation_id + 原始 request_id；
  先 commit 和绑定任务引用，再仅等待 Temporal accepted，断线不发送 Workflow cancel。
- M2 完整后端回归：2968 passed / 106 skipped；前端 281 passed，lint/typecheck/build 通过。
  2026-10-13 真实本地 HTTP/SSE 回归：20 passed，包含确认重复、原请求重放、越权拒绝、
  取消与确认前断线；规则替身只验证链路，不代表企业模型质量。
- 浏览器 M2：会话 378765c5-c8a8-4ec3-85ad-b27846b7a829，任务
  c80dc816-b572-4320-b176-e7857e095001；确认等待计入全链路 17.5 秒。刷新恢复原卡片，
  原快照 created / 0%，当前状态 research_review / 15%，仍等待页面上的研究包人工审批。
  截图位于 output/conversation-hooks/。演示覆盖不包含替代人工审核或 CMS 发布。
- 用户确认评测卡 15 秒轮询；写作卡保持 3 秒。
- M3：显式短期保存、长期候选与按用户追问读取状态通过；反馈复用既有驳回决策与收集服务，确认卡明确披露副作用，草稿不提交或二审。HTTP 记忆/反馈/角色/tenant边界 3 项与写作 4 项通过。
  浏览器会话 2f20989c-cd56-41a3-b74e-c4c3834f0169，短期实体 657267a7-be75-43a7-ac41-800d5264b558，长期候选 393c2b1d-67d7-45af-97e5-3e72414c9d5a，反馈案例 827a1630-cabc-44b8-beae-e400bd1a779a，同一实体详情跳转已核对。
- M4：使用 bge-demo overlay；/ready 的 PostgreSQL/Redis/Temporal 三项 ok，三个 Worker 队列的 Workflow/Activity poller 各 1。local-only注册第二个Prompt夹具，未改active Bundle。真实 Freezer + MinIO 保存3层合成样本（每层1例），测试演员标签明确非人类审查。缺源运行统计不被当作企业质量证明。
  已注册 Prompt 候选 f68202cf-ac6a-4e60-a34c-a616d2c445f3 → 确认发起 → Workflow hot-news-data-loop-1f8d79f83ee600dacb376fb9e072e47cdd6b0caab438107d4e7974908c0c19c7 → 三层门禁通过 → waiting_approval；真实评测记录 64bbc94f-a879-41f7-8da7-97ae987cb071。卡片从排队快照更新为真实运行，15秒轮询与同Workflow详情跳转通过。助手未提交批准，T4.6正式人工操作仍待用户在页面执行。
- M5：默认关闭的对话异步预算已接入写作/评测；Temporal持久FIFO，不因Gate、断线、TTL或读失败释放槽位；页面/API端口不纳入该预算。真实隔离Temporal测试验证budget2/队列顺序/Worker重启/终态释放/历史Replayer（1项通过）。两个随机租户各3个实际HTTP确认请求已通过：每租户峰值2、超限排队、引用去重及队列位置观测全部通过；15秒观察，轮次最大0.544秒。CPU/内存前后聚合水位与 m5-load-report.json 已留档。此有限观察不等于生产长稳容量。
- 最终源码后端全量：3208 passed / 116 skipped / 3既有弃用警告；前端全量322 passed，lint/typecheck/build通过。HTTP/SSE与真实Temporal为独立opt-in入口，跳过项不能算企业或生产验收。
- 最终部署只读复验：API/web均healthy且镜像匹配最终tag；/ready三项ok，既有评测回执GET=200、private,no-store，真实评测记录completed且Workflow仍waiting_approval。证据见output/conversation-hooks/final-deployment-check.json。
- 完整入口、数据转换、核心函数、依赖、输出、配置和测试见 agent/docs/conversation-capability-hooks.md；重排资产/完整证据重放、三位正式人工合同评审、企业模型效果与生产容量仍保留。

## 10. 已确认另列的后续范围（2026-10-13）

- [ ] 版本化实际重排参数资产与完整检索证据重放，再提供重排调参候选；当前明确未落地，不用模型／Prompt变化冒充重排变更。
- [ ] 三人正式合同人工评审（T0.4），合成标签演员与 Agent 技术审查不替代人工意见。
- [ ] 在现有评审台独立人工批准／驳回已评测候选；对话永不代替批准、激活或回滚。

- M4独立HTTP/SSE主链路4项通过，既有数据集跨tenant409拒绝已按旧合同修正测试并对现有flow只读复验1项通过；候选与评测断线/UUID重放、实际评测ID/新Fresh、role403与其他实体tenant404均通过。没有为复验重复创建占用人工Gate的任务。

- 真实多租户业务 Worker 再重启后只读复验：原六个实体仍保留；每租户仍为2个运行、1个排队，预算2未被重启放大，m5-worker-restart-check.json 已留档。

- 第7节回执读取：前端同页Map + 后端每API进程 tenant/hook/entity singleflight；每caller先重新校验权限/启用/Schema，写作3秒/评测15秒短缓存，256项LRU/128个在途、10秒有界只读任务，失败不缓存，断线不取消共享读取，lifespan释放。排除用户Memory；不是跨API副本全局缓存，浏览器HTTP仍各自鉴权。专项86通过。

## 7. 真实内容 v8 扩充与热点 Agent 接入（2026-10-19）

- [x] 原地扩充 v8：8,120 篇真实公开新闻（661 冻结 + 7,459 v2 增量），正文/URL/发布时间/
  内容哈希来自公开缓存，不生成模拟标题或正文。
- [x] 新增 `news_content_feature`：标题/正文字数、段落、句子、数字 token、确定性 CJK 关键词。
- [x] 7,459 篇内容-only 文章不生成曝光/点击/UV/时长/基线/榜单/生命周期行；661 篇既有指标
  继续明确标记为模拟。
- [x] 数据包原地重建并校验：摘要 `e139126c4bdc49f885aa6f08681de48f7da4d445de85442207a77c3706d7c904`；
  独立 PostgreSQL 13 张表、16,240 条新闻维、SQL 检查通过。
- [x] 新增 Agent profile `real-news-v8`、契约 `news-warehouse-v7`、场景 YAML 和独立 Compose
  叠加文件；热点运行已验证 5 篇排名/分析，正文来自真实公开内容。
- [x] 完整后端回归（使用带 numpy/threadpoolctl 的本地测试镜像，包含 BGE 可选依赖测试）：
  `4515 passed, 143 skipped`；v8 专项 `61 passed`。
- [ ] 内容-only 文章进入热度排行需要企业真实行为指标或另行批准的非行为评分规则；不得补 0
  或生成模拟值。
- [ ] 企业真实聚合接口、权限、生产负载、长稳与内容-only 内容检索接入仍待验收。
- [ ] `catalog-extension-v2.json` 为本地生成清单（未纳入 Git）；克隆后需先运行
  `agent/tools/freeze_real_news_warehouse_extension.py`。
