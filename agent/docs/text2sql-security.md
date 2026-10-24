# Text2SQL 安全边界 v3

更新日期：2026-10-22。本文记录当前代码的 SQL 安全策略及验证入口，不表示生产配置
已发布或企业模型已经验收。本次业务问题、改动前后数据和逐例结果见
[业务复核报告](../../output/reports/text2sql-business-review-2026-10-22/report.html)。

后续待写代码及接入点见
[Text2SQL 后续代码草案](/Users/shi/Project/myProject/NewsAgent/agent/docs/text2sql-next-steps.md)。

## 参考源码与取舍

- [WrenAI policy.py](https://github.com/Canner/WrenAI/blob/2cc843fd87d6cfe9831554721559018dda2938a0/core/wren/src/wren/policy.py#L252)：
  借鉴全树 AST 检查及函数治理；只检查根节点 SELECT 会漏掉写 CTE、INTO、锁等。
  NewsAgent 采用更窄的单视图查询能力，并坚持解析失败即拒绝。
- [WrenAI engine.py](https://github.com/Canner/WrenAI/blob/2cc843fd87d6cfe9831554721559018dda2938a0/core/wren/src/wren/engine.py#L247)：
  借鉴编译后再次检查最终 SQL；执行器仍独立验证，不能依赖生成阶段的通过标记。
- [Vanna registry.py](https://github.com/vanna-ai/vanna/blob/365d0617c1a4567ffee1b19b40c27feb4206bfcf/src/vanna/core/registry.py#L104)：
  借鉴基于服务端用户上下文的权限与参数复核。其默认参数转换是 NoOp，不能当作已提供
  租户隔离；NewsAgent 自己绑定身份并核对冻结计划。
- [OWASP SQL Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)：
  值用参数绑定，表、列和排序方向用批准名单，数据库账号按最小权限配置。
- [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)：
  处理混淆输入并保持工具权限独立于文本理解；关键词筛查无法证明任意提示都安全。

仅借鉴工程原则，代码由本项目实现，没有新增 Agent 框架或安全模型服务。

## 上游入口、转换和下游输出

当前对话查询路径：

```text
对话工具 query_hot_news → simulation/conversation_hot_news.py:_understand
→ normalize_question → QueryUnderstanding 模型提案
→ resolve_structured_query（真实模型）或 resolve_query_understanding（本地规则）
→ 受控规范问句 + expected_intent → SqlAssistantService.preview
→ local_query_intent 独立复核规范问句 → 场景栏目范围检查
→ compile_query 固定公式 → SqlAssistantGuard → 计划快照与运行绑定
→ Temporal → execute_for_hot_news → _execute 再查身份/范围/计划/缓存
→ 只读数仓 → 同小时权威指标交叉核对 → Python 热度/排行 → 分析和页面
```

原始问题与模型提案是否语义一致仍需独立评测。规范问句一致性检查只能保证后续 SQL
遵守提案，不能证明模型正确理解了原始问题。以下是直接调用 preview 的工具路径：

```text
/hot-news#query-tools → POST /api/v1/local-simulation/hot-news/run
→ examples/local_simulation.run_local_hot_news（网关身份、单小时、排行场景）
→ SqlAssistantService.preview
→ input_boundary.screen_question（NFKC 规范化、隐藏字符拒绝、风险信号筛查）
→ NativeStructuredAgentClient → StructuredInferenceService → InferencePort
→ SqlAssistantIntent → validate_intent → compile_query
→ SqlAssistantGuard.validate(SQL, parameters)
→ QuerySnapshotStore → HotNewsSqlBindingStore 原子绑定
→ Temporal Workflow/Activity → SqlAssistantService.execute_for_hot_news
→ _execute（SQL、参数、身份、冻结场景及运行绑定复核；缓存结果也要先复核）
→ LocalPostgresSqlWarehouseClient 再验完整护栏
→ PostgreSQL 参数绑定、READ ONLY、statement_timeout
→ SqlAssistantResult → 同小时补充指标 → NewsMetricSnapshot
→ Python HotNewsRanker → 正文/关联检索 → 模型分析及校验
→ analysis_runs/sql_tool_trace → 页面只读展示
```

通用指标取数路径：

```text
热点编排 → HotNewsMetricQuery
→ Text2SqlNewsMetricSource.fetch_batch
→ 请求指标与 UTC 完整单桶校验 → 服务端 metric_contract.render
→ SqlGuard 校验批准 SQL → 模板优先；仅配置生成路径时调用 Text2SqlAgentRunner
→ SqlGuard 校验候选 SQL → 执行参数与批准参数完全相同
→ validate_metric_sql_contract 比对完整查询
→ SqlWarehouseClient → 截断/重复新闻行/类型/数值校验
→ SqlMetricBatch（口径版本及哈希）→ NewsMetricSnapshot → Python CTR/热度/排行
```

通用源通过 `app/bootstrap/hot_news.py:create_text2sql_metric_source` 显式装配；默认 Worker
当前走行为数据源，页面走上面的专用链。因此通用源的缺口不能直接等同于页面已出错。
该通用入口接受结构化指标请求，不直接接受页面自由文本。安全演练 API 调用
`evaluate_prompt_injection_test`，共用文本筛查和通用 SQL Guard，但一直为 dry-run；
正文中的风险信号只产生警告，不能增加工具权限。

## 拒绝条件

文本首先 NFKC 规范化，使全角 SQL/数字与普通字符使用相同判断。拒绝不可见、双向控制
及其他控制字符；检测指令覆盖、跨租户、敏感数据、手写 SQL 和写操作信号。
规范化后的同一文本传给规划 Port，不能一套文本检查、另一套文本推理。
这是辅助筛查，不是完整注入检测器；不自动解码任意 Base64，也不承诺识别所有同义改写。

本地规则 Port 仍只支持有限词表：明确拒绝不同的行数、相反的排序方向、小数及无法
归属的数值。“全部新闻”要求改成明确前 N 条，避免悄悄按默认 10 条解释。
入口默认使用的“按点击量取前5条新闻”现在也能解析。

通用 SQL Guard 要求单个 SELECT 直接读取一张批准视图，拒绝 CTE、JOIN、子查询、
集合查询、OR/NOT、星号、窗口函数、INTO、锁、注释和多语句；AST 节点及函数使用
允许名单，未知函数在投影、条件和排序的任何位置均被拒绝。转换为危险类型也不被批准。
Tokenizer 还在解析前拒绝带引号的函数调用，避免 AST 将 `"SUM"(...)` 归一化为
内置 SUM，丢失 PostgreSQL 中大小写和函数身份的区别；字符串中的相同文字不算调用。

必须在外层 AND 条件中精确出现：

```sql
tenant_id = :tenant_id
AND event_time >= :window_start
AND event_time < :window_end
```

命名参数来自 AST，不使用扫描 SQL 字符串的方式判断，因此字符串中的 `:window_end`
不能冒充真实参数。执行参数集合必须与占位符集合一致，租户必须是有效标识，时间带时区、
递增且在窗口预算内。LIMIT 只接受 `:row_limit` 或正的有界整数，不能使用租户等其他参数。
单内容类型请求必须有精确 `content_type = :content_type`，其值仅允许 article/video，
不能通过投影里的占位符或返回标签伪造过滤。

通用指标源在通用 Guard 后增加完整查询比对：公式、投影、过滤、分组、HAVING、排序和
LIMIT 均绑定到批准口径。仅容许格式变化、投影顺序变化、同名列的冗余别名；不承诺识别
所有等价 SQL。`metric_contract` 来自服务端工厂，不能来自模型或用户文本；未配口径时
在调用生成器前拒绝。模板路径同样接受检查。批准表达式被复制、冻结，并记录版本/哈希。

当前快照仅支持六个基础指标及 Python 计算的 CTR；请求 `growth` 等未接入指标会明确拒绝。
默认按 UTC 对齐的一个完整小时取数，不允许把小时 UV 相加当作跨小时去重 UV。
`aggregate_metric_template` 直接读取单新闻/类型/桶唯一行，不加无效 GROUP BY；重复身份
返回会拒绝。需要汇总的明细模板必须显式提供批准表达式和分组配置。

本地 SqlAssistantGuard 进一步固定排行/趋势的维度、聚合公式、过滤条件和排序。
Service 在执行或复用结果前还核对 row_limit、类型、栏目与冻结意图完全相同，租户与
当前认证身份相同。快照哈希是完整性检查，不是可防御数据库管理员的数字签名。
已保存结果还必须匹配当前 query_id/SQL 哈希，行数与 rows 一致、未截断且不超过批准上限。
栏目解析统一使用场景 `allowed_categories`；省略栏目时，批准集合必须等于冻结数仓版本
的完整栏目集合（v1–v6 四栏目，v7 七栏目）。七栏目数仓中仅批准四栏目不再被误当全量；
预览和执行都检查，旧缓存也需先通过。单栏目场景继续自动绑定。

## 配置、依赖与兼容边界

- 现有场景配置：`deploy/text2sql-scenes.local.yml`；排行最多 100 行，模型最多 2 次，
  SQL 超时默认 10000ms。当前热点入口仍固定样本内单小时排行。
- 现有模型/Prompt 绑定：`deploy/model-runtime.local.yml`。本次未改变 Prompt/模型版本。
- `SqlGuardPolicy.allowed_functions`：代码中的已批准内置聚合/算术函数；拒绝未识别函数
  和 UDF。`max_window` 默认 7 天，`max_rows` 不得超过 Schema 已批准上限。
- 通用指标契约额外限制为一个完整桶，默认 `bucket_seconds=3600`，默认版本
  `hot-news-metric-template-v2`。更长桶仅能由有对应权威聚合数据的服务端配置提供，不能
  通过扩大桶长把小时 UV 相加。原先仅配置 generator 的调用者须新增 `metric_contract`；
  这是有意收紧，不能保持不受业务口径约束的兼容兜底。
- 工厂沿用 `text2sql_max_rows`、`text2sql_timeout_ms`；模型输入新增可选
  `metric_contract_version`、`approved_sql`，不包含数仓行数据。运行时无新增依赖。
- 现有依赖：sqlglot、Pydantic、PyYAML、SQLAlchemy、模型 Port、PostgreSQL、Temporal；
  SQL 规划不需要 Embedding，Embedding 用在下游关联检索。
- 本次未部署、重启现有服务或修改冻结 v1 Markdown。旧模型输出若缺少精确边界、使用
  inline 内容类型或复杂查询结构，会被新护栏拒绝；不会自动放宽权限或重写危险 SQL。

## 测试入口和 TODO

在 `agent` 目录运行（需要已安装项目 dev 依赖）：

```bash
python -m pytest tests/test_sql_assistant_planner.py tests/test_sql_assistant_guard.py \
  tests/test_sql_assistant_service.py tests/test_text2sql_guard.py \
  tests/test_text2sql_metric_source.py tests/test_prompt_injection_harness.py \
  tests/test_sql_assistant_api.py tests/test_local_simulation.py \
  tests/test_hot_news_sql_binding.py tests/test_native_hot_news_sql_support.py \
  tests/test_sql_assistant_warehouse.py -q
```

回归覆盖越权/混淆文字、scope 绕过、参数伪造、非批准函数、计划和绑定不一致，以及拒绝
之后模型/数仓没有被调用。依赖型数仓测试仍需显式启用；本地规则测试不是企业模型验收。

2026-10-03 的历史验证使用已有 `news-agent/writing-agent-service:native-e2e` 镜像的新临时容器，
关闭网络、只读挂载不含 `.env` 的代码副本；没有重启已有服务。相关回归为
`360 passed, 2 skipped`，后端完整回归为 `1048 passed, 13 skipped`。

TODO：企业推理与数仓合同测试，企业方言/真实字段和指标语义覆盖，生产专用只读账号与
必要的行级隔离，查询配额/成本/审计留存，以及自然语言完整语义与真实攻击集评测。
通用指标口径绑定已于 2026-10-22 完成；仍需验证批准口径本身与企业真实字段/粒度一致。
参数绑定不保证统计口径正确，关键词拦截不保证所有 Prompt Injection 都被识别。

## 2026-10-22 本次验证与复现

业务开发集 `evaluation/datasets/metric_contract_business_v1.json` 冻结 14 例：3 例正常，
11 例异常。保留旧源文件的基线放行异常 10/11；候选最终版放行 0/11，11/11 均在访问
数仓前拒绝。正常查询两版均为 3/3，逐行基础指标与显式 gold 相同。
这是开发者编写的针对性契约集，真实模型调用为 0；不能解释为线上风险从 90.9% 降至 0%。

原始证据分别位于仓库根目录：

- `output/evaluation/2026-10-22-metric-contract-baseline-01/`：完整旧源模块、运行规格、逐例与汇总。
- `output/evaluation/2026-10-22-metric-contract-candidate-02/`：包含 UTC 夏令时修正的最终源模块及相同开发集结果。
- `output/evaluation/2026-10-22-metric-contract-candidate-01/`：修正时区前的中间实验，保留不覆盖。
- `output/evaluation/2026-10-22-metric-contract-regression-01/`：相邻链路 226 passed，1 skipped。
- `output/evaluation/2026-10-22-metric-contract-regression-02/`：最终指标相关 47 passed，含夏令时回拨反例。

前一批核心回归 215 passed，包含其中 46 项指标测试，以及栏目预览/执行/旧缓存回归。
最终 47 项指标回归替换前一批 46 项，合计覆盖 442 个通过的独立测试用例；另有 1 项真实
PostgreSQL 测试因未设置 `SQL_WAREHOUSE_INTEGRATION_URL` 跳过，不计入通过数。

在 `agent` 目录复现业务开发集，输出目录必须为新的目录：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.run_metric_contract_business \
  --output-dir ../output/evaluation/metric-contract-new-run --require-mode candidate
```

Runner 的链路为：冻结 JSON → 预设 SQL 的生成器替身 → 真实 `fetch_batch` / `SqlGuard`
→ 内存 SQLite 实际执行 → 真实快照转换 → 逐例 gold 比较 → `cases.jsonl/results.json`。
运行前保存数据、源码和依赖身份，并以 audit hook 禁止 `.env` 读取与外网连接。
SQLite 的裸列聚合语义比 PostgreSQL 宽松，漏/错 GROUP BY 示例不能外推为 PostgreSQL
会返回同样数字。本轮没有运行 Temporal、真实模型或企业数据库。

本轮待办：原始自然语言意图一致性独立评测；真实 PostgreSQL/企业接口与小数时长合同；
跨小时 UV 的权威去重来源；候选数量达到 LIMIT 时的完整性检测（不能只依赖 truncated）；
独立业务审核及代表性线上样本。当前契约限制不等同于上述待办已经完成。
