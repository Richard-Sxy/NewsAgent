# Text2SQL 当前状态与后续工作

核对日期：2026-10-23。状态依据当前工作区源码；不表示这些改动已部署，亦不表示企业模型或数仓已验收。

## 1. 指标查询契约：已有实现，后续验证企业口径

原先“新增指标公式契约”的待办已实现，不应重复建设。
核心入口是 `Text2SqlNewsMetricSource.fetch_batch()`，位于
[通用指标取数](agent/app/modules/hot_news/scoring/text2sql_metric_source.py)。

```text
HotNewsMetricQuery
→ 校验请求指标及服务端批准合同
→ HotNewsMetricSqlTemplate.render 生成批准 SQL 与参数
→ 模板优先；必要时通过模型 Port 生成候选 SQL
→ SqlGuard.validate + 参数一致性检查
→ validate_metric_sql_contract 比较完整查询结构
→ SqlWarehouseClient.execute
→ 截断、结果行与范围检查 → NewsMetricSnapshot
→ Python 热度与排行
```

[validate_metric_sql_contract](agent/app/modules/hot_news/scoring/metric_sql_contract.py)
只允许格式、投影顺序和同名冗余别名等有限差异；公式、列、过滤范围、分组、排序和 LIMIT
偏离批准合同会被拒绝。合同缺失时在生成和执行前拒绝，输出携带合同版本与 SHA-256。
`clicks * 1000 AS clicks`、常量替代点击量等不能再仅凭通用只读护栏通过。

默认模板、基础指标映射、单桶 UV 和重复行已有相应约束。批准合同本身仍须对照企业真实
口径验证；该机制不是通用 SQL 等价证明，也不能证明模型正确理解了用户原意。
例如曝光 200 万、点击 1000 时，点击率为 0.05%，必须由批准口径和确定性计算得到。

## 2. 自然语言到热点运行：已有编译与执行复核

```text
/hot-news#query-tools / 对话 query_hot_news
→ 可信身份与窗口 → 问题规范化、结构化理解或意图规划
→ SqlAssistantService.preview → validate_intent / compile_query
→ SqlAssistantGuard → 冻结计划与热点 run 绑定
→ Temporal Workflow / Activity → execute_for_hot_news
→ 身份、范围、配置和计划复核 → PostgreSQL 只读取数
→ 同小时指标补充与校验 → Python 排行 → 正文/检索/模型分析
→ 同一运行快照、审计与页面
```

对话结构化理解与澄清已有实现；原始问题到模型提案是否忠实仍待独立评测。
旧 `/sql-assistant` 前端路由重定向热点页，独立后端查询路由未挂载。
`app/api/sql_assistant.py` 的共享辅助函数仍由模拟装配使用，不能因路由停用而整文件删除。

## 3. 记忆内容来源绑定：仍待完善

当前 `_memory_runner()` 检查用户原文是否明确要求记住或保存偏好，但传给
`ConversationMemoryHooks` 的仍是模型提供的 `arguments.content`；写入时标记
`origin="explicit_user"`、`confidence=1`，并附用户消息来源引用。

待补：将保存内容与用户原文的明确片段绑定；模型改写或补充部分保留衍生来源，不将
“用户授权保存”当作“模型改写内容全部由用户亲述”。摘要及转交也应保留可信来源引用。
本次没有修改记忆权限或写入行为。

## 4. 细粒度执行权限：仍待完善

当前服务已检查租户、用户、查询快照与运行绑定，但尚未见此目录中实现统一的
`ExecutionScope` 权限对象。后续可将允许表列、指标、角色和策略版本绑定到服务端可信
执行上下文，并在预览与执行时复核。该对象是设计建议，不是现有配置项。

企业数据库账号仍需验证实际授权、只读视图及行级隔离；只读事务不能单独保证读取范围。

## 5. 自然语言筛查误拦：仍待改善

[input_boundary.py](agent/app/sql_assistant/input_boundary.py) 的风险正则仍会将独立的
`FROM`、`WITH` 等词识别为手写 SQL 信号，普通英文问题可能误拦。
应先用独立样例区分自然语言与完整 SQL 结构，再评估是否需要额外分类器。
任何分类模型都不能替代 Python、AST 护栏和数据库权限，也不能自行扩大查询能力。

## 配置、依赖与验证入口

- 配置：`deploy/text2sql-scenes.*.yml`、`deploy/model-runtime.*.yml`；
  `agent/app/config.py` 中的 `text2sql_max_rows`、`text2sql_timeout_ms` 及
  `conversation_query_understanding_config_path`。指标合同由服务端装配，不能由模型提交。
- 依赖：原生推理 Port、数仓 Port、本地 PostgreSQL、Temporal，以及 sqlglot、Pydantic、SQLAlchemy。
  企业数仓按 TDSQL-A / PostgreSQL 兼容方向准备，实际企业 Adapter 尚需验收。
- 在 `agent/` 下的定向回归入口：

```bash
python -m pytest tests/test_metric_sql_contract.py tests/test_text2sql_metric_source.py \
  tests/test_sql_assistant_planner.py tests/test_sql_assistant_confirmed_intent.py \
  tests/test_sql_assistant_service.py tests/test_text2sql_guard.py \
  tests/test_conversation_memory_tools.py -q
```

本次仅核对源码并更新文档，没有重新运行上述测试或调用企业接口。
剩余重点是企业指标与方言合同、原始问题语义忠实度、记忆来源绑定、英文误拦及执行权限治理。
当前安全链路详见[Text2SQL 安全说明](agent/docs/text2sql-security.md)；
[后续代码草案](agent/docs/text2sql-next-steps.md)保留历史方案，不能将其中旧 TODO 当作当前状态。
