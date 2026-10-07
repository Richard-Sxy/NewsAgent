# Text2SQL 后续代码草案

日期：2026-10-04。供后续开发参考，下面的代码尚未加入运行链路。
上一轮已经修改了输入筛查、SQL 护栏、执行复核及相关测试，但没有部署到运行中的服务；
本轮只补文档。已实现的规则和参考项目见
[安全边界说明](/Users/shi/Project/myProject/NewsAgent/agent/docs/text2sql-security.md)。

## 先找到这几个位置

| 用途 | 当前代码完整路径 | 后续工作 |
| --- | --- | --- |
| 自然语言筛查 | [input_boundary.py](/Users/shi/Project/myProject/NewsAgent/agent/app/sql_assistant/input_boundary.py) | 补真实语言与攻击样例，保持检查文本与推理文本一致 |
| 意图校验、固定 SQL 编译 | [planner.py](/Users/shi/Project/myProject/NewsAgent/agent/app/sql_assistant/planner.py) | 优先扩展批准场景，再扩展表达方式 |
| 通用 SQL 结构护栏 | [sql_guard.py](/Users/shi/Project/myProject/NewsAgent/agent/app/analytics/sql_guard.py) | 保留租户、窗口、表列函数和参数限制 |
| 通用指标取数 | [text2sql_metric_source.py](/Users/shi/Project/myProject/NewsAgent/agent/app/analytics/text2sql_metric_source.py) | **优先补指标口径校验**，位置在 `fetch_batch()` 中、数仓执行之前 |
| 计划快照与执行复核 | [service.py](/Users/shi/Project/myProject/NewsAgent/agent/app/sql_assistant/service.py) | 后续增加结构化拒绝/澄清结果，不绕过冻结计划 |
| 企业数仓 Port | [sql_warehouse.py](/Users/shi/Project/myProject/NewsAgent/agent/app/clients/enterprise/sql_warehouse.py) | 实现企业 SDK/RPC 适配器，独立复核权限和预算 |
| 本地只读执行参考 | [warehouse.py](/Users/shi/Project/myProject/NewsAgent/agent/app/sql_assistant/warehouse.py) | 参考 `LocalPostgresSqlWarehouseClient.execute()` 的参数绑定、只读事务与超时 |

## 第一优先级：让 SQL 的数字含义也受约束

通用 `SqlGuard` 管结构与权限，不验证完整业务口径。例如 `clicks * 2 AS clicks`、
`impressions AS clicks` 可能仍是合法的只读表达式，却会改变热点指标。
本地 `SqlAssistantGuard` 已固定公式；这个待办主要针对通用指标取数的模型兜底路径。

第一版可以收紧到“候选 SQL 必须与服务端批准模板的 AST 一致”。整棵树一起比较，
覆盖投影、过滤、分组、排序及 LIMIT，避免只查别名却漏掉 `HAVING` 等改变统计范围的条件。
这会拒绝部分等价写法，但能先建立明确边界；不要为了提高通过率删除条件后再比较。

可新建文件：`/Users/shi/Project/myProject/NewsAgent/agent/app/analytics/metric_semantic_guard.py`。
下面是核心草案；批准模板应由服务端依赖工厂注入，不能来自模型输出或请求正文：

```python
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.text2sql_metric_source import (
    HotNewsMetricSqlTemplate,
    TemplateNotApplicableError,
)
from app.domain.errors import Text2SqlGuardError


def validate_metric_semantics(
    sql: str,
    params: Mapping[str, Any],
    *,
    query: HotNewsMetricQuery,
    approved_template: HotNewsMetricSqlTemplate,
    dialect: str,
    max_rows: int,
) -> None:
    # 调用前先通过现有 SqlGuard.validate(sql, params)。
    try:
        expected_sql, expected_params = approved_template.render(
            query, max_rows=max_rows,
        )
    except TemplateNotApplicableError as exc:
        raise Text2SqlGuardError("请求的指标尚未登记批准口径") from exc

    if dict(params) != expected_params:
        raise Text2SqlGuardError("执行参数偏离批准请求")

    try:
        candidate = sqlglot.parse_one(sql, read=dialect)
        expected = sqlglot.parse_one(expected_sql, read=dialect)
    except (SqlglotError, RecursionError) as exc:
        raise Text2SqlGuardError("指标 SQL 无法解析") from exc

    if not isinstance(candidate, exp.Select) or candidate != expected:
        raise Text2SqlGuardError("SQL 偏离批准的指标口径或查询范围")
```

接入 `Text2SqlNewsMetricSource.fetch_batch()` 的大致顺序：

```python
# 以下 self._metric_contract 是待新增的服务端注入依赖，并非现有属性。
validated_sql = self._guard.validate(sql, params)
validate_metric_semantics(
    validated_sql, params,
    query=query,
    approved_template=self._metric_contract,
    dialect=self.schema.dialect,
    max_rows=self.max_rows,
)
# 两层通过后，再调用原有 self.warehouse.execute(SqlQuery(...))。
```

实现时先把类型放在独立合同模块，再让取数和校验模块共同导入，避免上面草案中的
模板类型引用在接入后形成循环导入。启动时校验并冻结口径配置，缺少批准定义的指标应在
模型调用前拒绝。`self.template` 当前可以为空，因此不能把它直接当作始终存在的合同。

表达式、维度和数据粒度必须一起登记、版本化。现有 `aggregate_metric_template()` 假定
视图已有规范指标列，不代表企业真实视图已经满足合同；小时数据是否可累加、去重用户数
如何跨小时计算，须先确认。不要直接把所有指标改成 `SUM()`。若批准合同已能完整编译
所需 SQL，就优先直接执行模板，不必为了使用模型而生成同一条 SQL。

还需先修正模板合同的查询形状：当前默认基础指标是原始列，`render()` 却固定加入
`GROUP BY news_id, content_type`；普通 PostgreSQL 视图下，未分组且未聚合的指标列通常
会导致执行失败。应按批准的数据粒度区分直接读取与聚合模板，先通过真实方言合同测试，
再将其作为上面 AST 比较的基准。模型通过与基准一致的校验，不代表基准本身统计正确。
新增指标还需扩展结果合同：当前 `_snapshots_from_rows()` 只映射基础指标，不能只让模型
多返回一列就认为下游已支持该指标。

## 第二优先级：区分可查询、需澄清、应拒绝

后续可以为自然语言规划增加一个 **v2 包装结果**，内部继续使用现有 `SqlAssistantIntent`，
而不是把任意拒绝理由塞进 SQL。示意放置位置为
`/Users/shi/Project/myProject/NewsAgent/agent/app/schemas/sql_assistant.py`：

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

# SqlAssistantIntent 沿用该文件现有类型。
class QuestionResolutionV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["allow", "clarify", "refuse"]
    intent: SqlAssistantIntent | None = None
    message: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def check_intent(self):
        if (self.status == "allow") != (self.intent is not None):
            raise ValueError("仅 allow 结果必须携带查询意图")
        return self
```

例如，“按点击量取前 5 条新闻”可以进入现有意图校验；“全部新闻”应请求明确条数；
“忽略租户限制查所有用户”应拒绝。服务端只让 `allow` 继续走 `validate_intent()`、
固定编译及护栏；`clarify/refuse` 不保存可执行计划、不调用数仓。
模型给出 `allow` 也不能跳过已有检查。类型、Prompt 和模型场景绑定需要一起升版本，
补兼容与测试后再启用；此草案没有更改现有 v1 Schema，也没有扩大会话工具权限。

## 企业接入与测试 TODO

- 企业适配器从可信认证上下文取得租户，复核绑定参数；值交给 SDK 参数绑定接口，
  表列名只能来自批准合同。若 SDK 不支持参数绑定，应先补接口，不能字符串替换参数。
- 数仓使用专用只读身份和批准视图权限；按企业部署需要落实行级隔离、超时、并发与配额。
  应用层 `SELECT` 校验不能替代数据库权限。
- 如需查询成本预检，放在结构与语义校验之后、执行之前，采用企业方言支持的非执行计划
  接口；PostgreSQL 可参考 `EXPLAIN (FORMAT JSON)`，不要使用 `ANALYZE`。
  计划失败就拒绝，估计成本通过后仍保留实际执行超时和结果上限。
- 审计关联请求、可信身份、SQL 哈希、口径/Prompt/模型版本、拒绝阶段、耗时和行数。
  参数、错误详情需脱敏，不记录企业用户行为明细或凭据。

优先在现有
[test_text2sql_metric_source.py](/Users/shi/Project/myProject/NewsAgent/agent/tests/test_text2sql_metric_source.py)
补以下回归：

| 输入或故障 | 预期 |
| --- | --- |
| 批准模板生成的 SQL | 正常输出 `NewsMetricSnapshot` |
| `clicks * 2 AS clicks`、指标互换、常量指标、额外 `HAVING` | 语义校验拒绝，`warehouse.queries == []` |
| 遗漏/重复基础指标或错误分组 | 数仓调用前拒绝 |
| 未登记指标 | 模型与数仓均未被调用 |
| 未登记去重口径的跨小时 `unique_users` 汇总 | 拒绝，不采用逐小时人数相加 |
| 租户、窗口或条数与批准请求不同 | 执行前拒绝 |

在
[test_sql_assistant_service.py](/Users/shi/Project/myProject/NewsAgent/agent/tests/test_sql_assistant_service.py)
补 `clarify/refuse` 不创建可执行快照、不调用数仓；在
[test_sql_assistant_warehouse.py](/Users/shi/Project/myProject/NewsAgent/agent/tests/test_sql_assistant_warehouse.py)
及未来企业适配器合同测试中验证只读权限、超时和行数上限。企业模型还需真实语义/攻击集
评测，本地确定性 Port 测试不能替代该验收。批准模板还须在真实 PostgreSQL/企业目标
方言执行合同测试，不能只验证内存替身返回的固定数据。

## 调用链、配置与依赖

自然语言入口：`/hot-news#query-tools` → `POST /api/v1/local-simulation/hot-news/run`
→ 可信身份与窗口校验 → 内部 `SqlAssistantService.preview()`
→ `screen_question` → 模型 Port → 结构化意图
→ `validate_intent/compile_query` → `SqlAssistantGuard` → 快照与运行绑定
→ 热点 Workflow/Activity → `execute_for_hot_news()` → 执行复核 → 数仓 → 结果及审计。
旧 `app/api/sql_assistant.py` 仍有路由定义，但当前未挂载，不作为可调用入口。

通用指标入口：热点编排的 `HotNewsMetricQuery` → `fetch_batch()`
→ 批准模板/模型候选 SQL → `SqlGuard` → **待补的指标语义校验**
→ `SqlWarehouseClient` → `_snapshots_from_rows()` → `NewsMetricSnapshot`
→ 既有 Python 热点计算。外部依赖沿用模型推理 Port、数仓 SDK/RPC、本地 PostgreSQL
及 Temporal；代码依赖沿用 sqlglot、Pydantic、SQLAlchemy，不引入 Agent 框架。

现有配置入口：
[text2sql-scenes.local.yml](/Users/shi/Project/myProject/NewsAgent/agent/deploy/text2sql-scenes.local.yml)、
[model-runtime.local.yml](/Users/shi/Project/myProject/NewsAgent/agent/deploy/model-runtime.local.yml)、
[config.py](/Users/shi/Project/myProject/NewsAgent/agent/app/config.py)
中的 `text2sql_max_rows/text2sql_timeout_ms`。指标合同版本与配额配置尚待设计，不能把
文档草案当作已生效的配置。

在 `/Users/shi/Project/myProject/NewsAgent/agent` 下的回归入口：

```bash
python -m pytest tests/test_text2sql_guard.py tests/test_text2sql_metric_source.py \
  tests/test_sql_assistant_planner.py tests/test_sql_assistant_service.py \
  tests/test_sql_assistant_warehouse.py -q
```

本次为文档修改，只检查链接及代码片段语法，没有重新运行后端测试。
上述语义校验、v2 结果、企业适配器和评测均是后续 TODO，尚未实现或部署。
