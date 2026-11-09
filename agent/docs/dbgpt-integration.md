# Text2SQL 核心链路与 DB-GPT 模板裁剪

状态：2026-11-07，本地可选实现，未启用在线/生产候选模式。本文区分代码合同验证、真实数据库锁验证与尚未执行的模型/完整业务验收。

## 实际复用范围

没有安装 `dbgpt`、`dbgpt-app`，没有运行 DB-GPT 服务。复用范围为固定版本 SQL Prompt 资产改编：

- DB-GPT v0.8.2，提交 `83609c471266607490b24e72188311efa23a0d16`。
- 来源 `packages/dbgpt-app/src/dbgpt_app/scene/chat_db/auto_execute/prompt.py`；原文件摘要、修改说明见 `app/sql_assistant/assets/dbgpt_source.json`。
- MIT 与版权声明见同目录 `DB-GPT-LICENSE.txt`。去掉全局注册、自由回答、图表、连接和执行路径。
- 本项目拥有候选 Port、严格 JSON 解析、预算、业务合同与执行控制。不能把此实现描述成完整 DB-GPT 引擎集成或模型微调成果。

## 完整调用链

```text
/chat 或热点工作台
→ ConversationTools.execute_hot_news_query（已有身份与权限）
→ LocalConversationHotNewsQuery._understand
→ build_query_understanding_payload
  → 可选 query_catalog：按当前 Policy 投影指标、栏目、类型、数量和时间能力
→ 现有 StructuredAgentClient / QueryUnderstanding / provider 对应 resolver
→ canonical_question + expected_intent + 已确认完整小时
→ execute_local_hot_news_query
  → build_hot_news_sql_scope：包含可选候选配置与源码/资产身份
  → 候选模式 HotNewsSqlBindingStore.plan_once：先查绑定，锁竞争立即503
→ SqlAssistantService.preview：独立复核确认意图，服务端构造参数
→ compile_query 生成基准
→ 可选 DbgptTemplateCandidateGenerator.generate
  → 固定模板、批准 Schema、确认意图、完整投影和占位符合同
  → NativeStructuredAgentClient / 共享 InferencePort（已有治理层）
  → 严格 JSON；一次模型尝试；无数据库对象、无执行回调
→ SqlAssistantGuard + validate_metric_sql_contract
→ SqlAssistantPreview.generation / 原审计 JSONB 快照 / 原子绑定
→ Temporal HotNewsMonitorWorkflow / Activity
→ SqlAssistantHotNewsMetricSource（simulation/native_hot_news_sql_support.py）
→ execute_for_hot_news / validate_selected_plan / 服务端参数复核
→ LocalPostgresSqlWarehouseClient：READ ONLY、绑定参数、statement_timeout
→ 同小时 UV/时长/基线补充 → NewsMetricSnapshot → Python 权威热度
→ 正文与知识证据 → 现有热点模型分析和校验
→ 同 query_id/run_id 的报告、sql_tool_trace、API/SSE/页面
```

没有增加公开 SQL 路由；当前装配仍限 `environment=e2e`。原 `modules/hot_news/scoring/text2sql_metric_source.py` 是另一条指标源，保留原行为。小时热点查询仍只允许 ranking 和完整一小时；底层 trend 编译能力不等于聊天新增趋势/跨小时UV权限。

## 查询理解上下文

`app/modules/conversation/query_catalog.py` 和 `query_catalog_v1.json` 提供固定摘要的指标字典、能力说明和三类开发示例。示例标记 `development_draft_not_human_reviewed`，不是审核金标。当前场景没有批准的栏目/类型/指标不会从目录获得额外权限。

线上与 `evaluation/query_semantics_runner.py` 共用 `build_query_understanding_payload`。旧 Prompt 版本、模型 YAML 和默认载荷保留；开启上下文属于显式候选配置。离线 `run_query_semantics.py run --query-context` 使用相同资产并记录摘要；旧 base/LoRA 比较器仅补上下文身份一致性门禁，不能用于 Native/DB-GPT 效果比较。旧冻结包保留，代码变化后的实验应重新 prepare 新包。

## 模式、预算与配置

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `CONVERSATION_QUERY_CONTEXT_ENABLED` | false | 将开发目录追加到现有查询理解输入 |
| `SQL_CANDIDATE_CONFIG_PATH` | 未设置 | 指向版本化候选 YAML；未设置保持 native |
| `MODEL_RUNTIME_CONFIG_PATH` | 沿用已有值 | 主模型路由、端点和批准场景 |
| `MODEL_GOVERNANCE_CONFIG_PATH` | 沿用已有值 | 显式启用既有 Redis 全局并发/RPM 治理 |
| `SQL_ASSISTANT_SCENARIOS_PATH` / `SQL_ASSISTANT_DATASET_PROFILE` | 沿用已有值 | 场景、覆盖范围、冻结 Schema |

候选 YAML 模板是根目录 `deploy/sql-candidate.example.yml`。只改自己的候选配置副本，不覆盖现有私有模型配置或 `.env`。

- `native`：不构造候选适配器，不读候选资产、不调用候选模型；保持原 SQL 字符串复核与旧运行 scope。
- `dbgpt_shadow`：同步、有总预算地生成并比较，实际只执行原生 SQL。失败保存脱敏分类和摘要，原生查询继续；不会另起无法追踪的后台协程。
- `dbgpt_candidate`：只有通过 Guard 和本次意图合同的候选可执行；失败保存不可执行快照并返回明确错误，没有静默回退。

首版每次生成最多 **1 次模型请求、0 次自动修复**，默认10秒总生成期限、2048输出token、40000输入字符、16000输出字符。限制可在模板允许范围内配置；不是无限重试。`InferenceRequest.max_output_tokens` 经 HTTP Adapter 实际发送为端点已有的 `max_completion_tokens` 或 `max_tokens`，原请求无该字段时保持原行为。指标/时间/租户参数实际值不进入候选 Prompt；租户仍传模型调用上下文用于鉴权/计费。

候选沿用 `model_scene` 对应路由。实际调用的新场景为 `sql_candidate` / `sql_candidate_shadow`，在默认治理策略中均属于后台容量；本次未改变既有治理配置，也未实现租户公平配额或Token总配额。规则本地模型不代表候选模型质量；真实试验需批准且支持结构化输出的兼容端点。

## 快照、幂等与回退

- `SqlCandidateTrace` 保存模式、资产/模型/比较器身份、身份摘要、结果分类、候选SQL摘要和模型用量。元数据由适配器填充，模型输出不能自报身份或结果。
- 原快照指纹已覆盖 preview，新增 generation 自然进入指纹；预览没有 generation 的旧记录仍可执行。
- 新模式/资产改变运行 scope。已 claim 的任务继续读取原SQL，执行时按冻结模式检查；把配置切回 native 不会中途换 SQL。
- 候选只允许现有 Guard 接受的格式变体，再由窄 AST 比较器确认完整业务合同。安全但错误的排序/方向也拒绝；没有放宽 JOIN/子查询/公式权限。
- `plan_once` 使用运行键导出的 PostgreSQL 事务 advisory try-lock。并发竞争立即失败，后续重试先复用既有绑定，不重新生成；客户端应复用原请求身份。
- 锁在模型生成期间占用一个连接，保存与 claim 使用已有独立事务；入口另有“生成期限+10秒”总占用上限。进程在保存后、claim前崩溃可能留下不可用于热点运行的孤儿预览，重试可重新生成；不宣称外部模型 exactly-once。
- 已冻结候选恢复不依赖当前生成器，但需保留匹配的验证器资产/Worker版本。未知验证器摘要失败关闭，不通过破坏性数据库降级回退。

新增字段位于原 JSONB 快照中，无须新建业务表或修改冻结数仓Markdown。该指纹用于应用完整性检查，不是防数据库管理员篡改的密码学签名。

## 独立离线比较

`evaluation/run_sql_candidates.py` 独立于训练评测，验证“已确认意图→候选SQL合同”；从不执行数据库SQL。内置少量固定合成开发探针，不能代表真实语言理解准确率、数据库结果等价或企业效果。

在仓库 `agent/` 目录：

```bash
python -B -m evaluation.run_sql_candidates run --max-calls 6 --max-seconds 60 --out ../output/sql-candidates/native-example
# 候选配置显式选择 dbgpt_shadow 或 dbgpt_candidate；密钥只从现有环境注入。
python -B -m evaluation.run_sql_candidates run   --candidate-config /absolute/path/sql-candidate-trial.yml   --model-config /absolute/path/approved-model.yml   --tenant-id APPROVED_TENANT --max-calls 6 --max-seconds 60   --out ../output/sql-candidates/candidate-example
python -B -m evaluation.run_sql_candidates compare   --baseline ../output/sql-candidates/native-example   --candidate ../output/sql-candidates/candidate-example
```

输出目录必须不存在。报告包含逐例合同结果、失败/未运行分母、用量缺失状态、耗时、源码和资产身份；比较要求相同案例、Schema、范围、预算与runner。没有使用封存题改Prompt，也没有调用训练接口。

## 验证入口与当前证据

修改前定向基线416 passed、1 deselected。最终受影响39模块回归893 passed、8 skipped、5 deselected（2026-11-07）。没有把未运行用例计为通过。原生离线合同冒烟6/6，0次模型调用、0次数据库执行，报告位于 `output/dbgpt-contract-native-20261107/report.json`（相对仓库根）。

核心新增入口：`test_sql_candidate.py`、`test_sql_candidate_service.py`、`test_sql_candidate_token_budget.py`、`test_sql_candidate_binding.py`、`test_sql_candidate_local_flow.py`、`test_query_catalog.py`、`test_sql_candidate_evaluation.py`。继续覆盖旧SQL服务/Guard/已确认意图、热点映射、数仓身份、模型HTTP和语义评测测试。

`test_sql_candidate_binding_postgres.py` 通过显式 `SQL_CANDIDATE_BINDING_POSTGRES_URL` 连接一次性测试库；2项真实PostgreSQL测试已通过，验证并发竞争、绑定恢复与取消后锁释放。临时postgres:16容器已清理，未操作常驻业务数据库。共享命令接线5项通过，但其中Temporal/仓库是测试替身，不能称为真实完整Workflow验收。

临时构建使用setuptools 80.9.0与wheel 0.45.1，已验证六个模板/目录/许可证资产进入wheel；临时构建目录已清理，既有环境未改。

测试应使用Python3.11+；本机复用了 `Hunyuan7B/runtime/writing-eval/app-venv/bin/python`，未安装新框架。执行前将 `Settings.model_config["env_file"]`、`VideoIngestSettings.model_config["env_file"]` 设为None，设置 `PYTHONDONTWRITEBYTECODE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，pytest显式加载 `pytest_asyncio.plugin` 并禁用cacheprovider。集成/外部服务测试单独显式启用。

## 未完成 TODO

1. 目录开发示例的人工审核与真实查询语义集；候选SQL审核示例当前为空。
2. 获准真实模型对照、真实影子流量、质量/时延/成本收益；本次未调用企业模型。
3. 合成数仓到实际Temporal Worker、模型分析、页面的完整新模式验收；未切换常驻服务或生产配置。
4. 真实Worker进程崩溃/重启、持续负载、连接池和配额容量；当前真实恢复测试是重建Store对象与请求取消。
5. 保存前取消/硬崩溃的持久失败台账、审计保留策略、生产签名与租户公平配额。
6. 如后续需要多次修复、异步shadow抽样或扩大SQL能力，应版本化合同并另做评测，不能仅放宽Guard。
