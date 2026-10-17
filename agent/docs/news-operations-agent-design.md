# NewsAgent 新闻运营架构与评测说明

更新日期：2026-10-16。

NewsAgent 面向腾讯新闻运营，帮助运营将热点分析与主题兴趣受众对齐，准备“是否跟进、
是否进入人工推送评估”的依据。企业画像系统生成逐用户兴趣与通知资格；本项目读取必要
聚合结果，提供新闻分析、主题选择和受众预览。研究写作在选题确定后按需启动。

本版统一更新架构、简版流程、企业价值、Data Agent 定位、评测分层和优化顺序。
2026-10-15 本窗口的相关 PDF 保留为历史版本；它们没有覆盖本次新增的主题受众分支。
本版图解是 `output/pdf/NewsAgent_多智能体运营架构更新_2026-10-16.pdf`。

## 企业任务与交付

第一阶段服务新闻运营的看数、选题和跟进建议，并加入人工推送决策准备。一次任务应能把
“什么新闻值得关注”与“所选兴趣人群是否存在通知资格受众”放在同一工作上下文中。
当前热点报告和受众预览已经可分别展示；以下完整的事件与受众研判卡仍为产品设计。

| 信息 | 内容 | 当前状态 |
| --- | --- | --- |
| 新闻分析 | 已完成的 run/news、行为指标、证据、关注理由、运营建议 | 已有热点合同和报告 |
| 事件变化 | 相比上次新增、重复、反转、更正及已做动作 | 有事件生命周期；事实增量账本待补 |
| 兴趣主题 | 一级主题和细分标签、包含/排除、any/all、目录版本 | 人工选择；已有两级目录合同 |
| 聚合受众 | 去重匹配人数、通知资格人数、来源模式 | 已有受众预览；企业真实口径待联调 |
| 有效性 | 快照、画像和来源版本、窗口、水位、生成与失效时间 | 已有严格校验 |
| 人工后续 | 观察、补证、选题写作，或进入推送评估 | 已有一般运营决定；正式推送审批/执行回执待接 |

当前 `consider` 只表示 `eligible_users > 0`，可以进入人工评估；`observe` 表示零资格受众。
这条确定性规则没有评价新闻价值、最佳主题、发送时机或预期收益。存在资格人群不能单独
推出“应该发送”。预估人数与新闻浏览 UV、实时可送达人数、实际送达和点击预测均不同。

## 当前架构与多智能体分工

```text
企业行为聚合 / 正文证据
  → SQL与Python指标、基线、热度 → 热点分析角色 → Schema/Validator
  → PostgreSQL / MinIO热点报告 → 页面 / API / SSE
                                       │
                     ┌─────────────────┼──────────────────┐
                     ↓                 ↓                  ↓
               运营观察与跟进      人工选择兴趣主题       按需研究写作
                                  ↓                  Research
                          聚合受众Port                ↓ 人工确认
                                  ↓                  提纲人工确认
                                  ↓                  Writer
                     版本/身份/水位/时效校验           ↓ Reviewer
                                  ↓                  人工终稿确认
                             受众预览                 独立CMS发布
                                  ↓
                        是否进入人工推送评估
                       正式审批和真实投递待接
```

| 组件 | 架构职责 | 不应混淆的能力 |
| --- | --- | --- |
| ConversationAgentService | 单个对话 Agent 的受控工具循环，复用既有服务 | 不等于动态多 Agent 总控；受众尚未接聊天工具 |
| NativeHotNewsAnalysisRunner | 根据可信快照解释热点，生成结构化报告 | 数字和热度仍由 SQL/Python 计算 |
| Research / Writer / Reviewer | 固定研究写作流程中的角色分工及有界返工 | 角色不等于三套独立模型；Reviewer不等于独立事实 oracle |
| AudiencePreviewService | 绑定新闻，查询并校验聚合受众，返回人工预览 | 没有新增 Agent，不调用 LLM，不自动挑选主题 |
| Temporal与业务Validator | 持久状态、重试、幂等、审批边界及确定性检查 | 受众预览本身不是新增投递 Workflow |

简版关系是：**看数与查证据 → 热点报告 → 人工选主题 → 去重受众预览 → 人工决定下一步**。
需要补资料时选择 `research_package`；需要成稿时进入 `assisted_writing`。写作的研究、
提纲和终稿人工关口不应被画成每次预估受众的必经步骤。当前写作转交没有自动冻结受众
快照；后续连接时必须明确版本和有效期。

## 企业价值与 Data Agent 定位

| 设计维度 | 本版定位 |
| --- | --- |
| Data Agent 的数据分析职责 | 统一指标口径、关联新闻证据、保留数据与受众快照身份，支持核验 |
| 新闻运营的业务职责 | 把热点、事实和兴趣人群组织成可供人工判断的材料 |
| 企业已有算法职责 | 从访问行为生成用户兴趣、判断通知资格、推荐排序及最终投递 |
| 后续可借鉴的 Agent 设计 | 根据证据缺口有限补查，显式停止条件，以及按版本回放反馈 |

本项目适合描述为“面向新闻运营的数据分析与决策辅助 Agent”。Data Agent 在这里是设计
方向，不能作为已有独立组件的名称，也不能用来证明推荐收益。实现继续使用 Python 原生
Service/Port，不引入外部 Agent 框架。

可验证的首期价值是减少跨看板查数、整理新闻背景、选择标签后跨系统核验受众、以及重复
解释上下文的人工成本。试点要比较同一种真实交付的完成耗时、审改负担、过期快照误用、
主题组合错误和实际采用价值。匹配人数增加不等于提效，更不能直接推断 CTR 提升。

建议先选一个板块，观察 3–5 次当前任务，再开展约 10 个工作日、至少 30 个可比任务的
小规模试点。比较现有 BI/画像工具加人工、相同工具加通用模型、NewsAgent 三种方式；
统一数据权限、截止点和难度，计入材料准备、核验、改写、失败补救与维护分摊。受众预览
默认关闭，真实试点前需要企业合同及获准环境。此前讨论的 25% 节时门槛仍未获业务批准。

## 评测资产与新增受众层

| 层级 | 可复用资产 | 新增或尚缺的验证 | 允许的结论 |
| --- | --- | --- | --- |
| 数据写作 | 八月缓存集27题、9事件组、18数值标签 | 真实角色执行、全文事实/数字评分 | 结构、标签运算与隔离；不是模型成绩 |
| 事件演化 | operations_evolution_v2的432判断、36合成主线 | 运营人员校准与真实非模板主线 | 合成边界覆盖；不是企业分布 |
| 主题与聚合合同 | taxonomy/client/preview/runtime四模块及前端测试 | 批准目录、企业去重和资格对照 | 执行到的本地契约或组件行为 |
| 新闻到主题的判断 | 旧运营题型中的理由、证据及禁用结论 | 人工可接受主题集合、误选主题、合理动作、独立runner | 当前人选主题；没有自动圈人质量成绩 |
| 企业使用效果 | 可复用既有三组试点设计 | 真实净人工时间、全候选漏报检查、实际采用与成本 | 获准试点范围内的效率和质量 |

旧运营集的 `AudienceRule` 使用 `interest_tags/region_codes` 等字段，没有绑定新
`TopicCatalog/AudienceSelection/AudienceSnapshot`。旧板块 `technology` 也不能直接当作
新主题 `tech`。新增受众层须另立版本和映射，不能改名后声称旧题已覆盖新接口。
原八月集与运营主线集本轮均未重建或改标签。

新增样例先按下列六类准备，正式质量标签由运营复核，人数真值由获准聚合对照产生。
合成集合人数只用于合同测试，不能伪装成企业真实人群。

| 样例类 | 重点检查 |
| --- | --- |
| 单一主题新闻 | 人工选择的主题合法，新闻和聚合快照身份一致 |
| AI与财经交叉新闻 | 允许多种合理主题；any/all含义明确，不由模型编造人数 |
| 父子主题重叠 | 企业按组合统一去重，不能相加父子人数 |
| 排除后零资格 | 组合有效但人数为零时观察；不强行制造投递建议 |
| 错版本或失效快照 | 旧目录、串新闻、过期及不完整水位阻断，页面改选后废弃旧结果 |
| 旧事件出现新进展 | 同一主线按时点可见事实判断；新进展不自动触发发送 |

每组冻结事件、split、as_of、tenant/run/news、主题版本、组合、快照/画像/来源版本及水位。
私有标签与推理输入分离，转载、阶段与更正链不跨 split；真实热点选样仍需可审计依据。

## 优化顺序

1. 先联调真实标签目录、组合去重与通知资格合同，验证现有预览的数据含义。
2. 将新闻分析与受众快照形成统一可核验的材料，并补事件事实增量；组合卡仍需实现。
3. 单独建设持久推送计划、修订/哈希审批绑定、频控复核与实际执行回执；不得从预览直接发送。
4. 真实错误经分类与人工标签审核后进入冻结评测，再比较候选版本。事实错误与编辑策略
   分歧分别处理；个人策略经审核才可共享。配置发布审批后按既有Workflow执行激活，不能
   将其误画成必然需要第二次人工点击，也不能跳过批准。

正式投递、自动画像或推荐模型训练不属于本次文档同步。真实 CTR、触达收益和长期效果
需要另行授权、对照与归因条件。

## 接口调用链与配置

```text
HotNewsView → HotNewsRankTable → AudiencePreviewPanel → api/audience.ts
  → GET /api/v1/audience/topics
  → POST /api/v1/audience/runs/{run_id}/news/{news_id}/preview
  → 网关租户/用户与热点权限 → AudiencePreviewService.preview
  → TopicCatalog.validate_ids → get_analysis_memory → AudienceQuery/指纹
  → AudienceProfilePort.estimate
  → HttpAudienceProfileClient / 隔离LocalAudienceProfileClient
  → AudienceSnapshot → _validate_snapshot
  → Python consider/observe → 页面展示和人工评估
```

API路由是 `app/api/audience.py`，核心实现为 `app/audience/service.py`，Port与快照在
`app/audience/contracts.py`，目录在 `app/audience/taxonomy.py`；`main._business_lifespan`
装配并关闭Client。外部依赖为企业画像聚合接口、既有热点PostgreSQL仓储、FastAPI、httpx、
Pydantic与PyYAML。预览不新增数据库表，不持久保存审批或投递状态，指纹不承诺持久重放。

配置为 `AUDIENCE_ENABLED`（默认false）、`AUDIENCE_BACKEND`、`AUDIENCE_TAXONOMY_PATH`、
`AUDIENCE_ESTIMATE_URL`、由密钥系统注入的 `AUDIENCE_API_TOKEN`、
`AUDIENCE_TIMEOUT_SECONDS`（默认5）、`AUDIENCE_MAX_RESPONSE_BYTES`（默认65536）和
`AUDIENCE_MAX_SNAPSHOT_AGE_SECONDS`（默认900）。企业模式要求批准目录与HTTPS端点，
依赖失败不回退合成数据或旧预估。具体使用方法沿用[受众接口实现说明](enterprise-audience-implementation.md)。

## 本轮核对与测试入口

本轮是内容同步与离线检查，没有变更业务实现、部署或启用功能。检查原始命令、直接相关
源码/数据字节摘要、依赖身份、日志及结果在仓库根目录
`output/operations-design-sync-20261016/`；摘要标记为partial，不能概括为全部评测通过。

| 检查 | 本轮结果 | 范围 |
| --- | --- | --- |
| test_data_writing两模块 | passed，26项 | 标准库离线断言 |
| 八月集 --check | passed | 原产物逐字节一致，27题未变 |
| 受众前端组件 | passed，15项 | 编译组件和API契约，替身请求 |
| 主线集 validate --verify-artifacts | blocked | 当前解释器缺少opentelemetry，未到产物校验 |
| 受众后端四模块 | not_run | 所选解释器缺pytest/httpx/fastapi |
| 模型、企业接口及真实运营效果 | not_run | 无真实模型/端点/试点执行 |

本轮未安装依赖。以下离线命令从 `agent/` 执行，不读取dotenv：

```bash
python3 -B -m unittest discover -s tests -p 'test_data_writing*.py'
python3 -B -m evaluation.data_writing_dataset \
  --seed evaluation/datasets/data_writing_august_2026_v1/seed.json \
  --output-dir evaluation/datasets/data_writing_august_2026_v1 --check
```

前端在 `agent/frontend/` 执行 `node --test tests/audience-preview.test.mjs`。本轮实际Python
和Node路径见运行规格；复跑须使用具有对应依赖的解释器。

后端四模块入口为 `tests/test_audience_taxonomy.py`、`tests/test_audience_client.py`、
`tests/test_audience_preview.py`、`tests/test_audience_runtime.py`。在已有完整依赖环境运行时，
先同进程将 `Settings.model_config['env_file']` 与
`VideoIngestSettings.model_config['env_file']` 设为None，再调用pytest；完整方法见
`newsagent-evaluation` Skill的commands参考。不要直接导入默认设置读取本地dotenv。

待办仍包括：企业批准的目录和标签ID、去重/资格/窗口口径、鉴权和SLA合同；组合研判卡及
受众到写作任务的版本绑定；事实增量；持久审批、正式投递与回执；新增主题语义人工标签、
运营校准与真实提效试点。

## 文档与图解生成链

上游为本地源码、既有接口说明和本次运行日志；`output/operations-design-sync-20261016/verify.py`
冻结直接相关文件并依次调用原有只读CLI/测试，输出run-spec、日志和results。
`output/diagrams/render_operations_design_sync_20261016.py`读取这些结果，使用ReportLab及
Arial Unicode中文字体生成一页PDF；pypdf/pdfplumber检查页数、文本和边界，Poppler渲染后
做视觉检查。它们是交付工具，不进入应用运行时；本轮不新增模型配置或业务环境变量。
