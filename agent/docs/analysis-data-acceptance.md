# 聚合数据 Port 合同验收准备

这条链路验证已批准 `NewsMetricSource` 返回的聚合快照是否符合项目合同，不访问或保存
原始用户行为。本地合成通过状态为 `local_contract_passed`；明确选择 Adapter 后通过状态为
`contract_passed`。这两个状态都不表示企业联调、生产发布或企业租户隔离已经验收。

## 完整调用链

```text
管理员显式 CLI / 可信管理侧代码
→ tools/check_analysis_data_contract.py main / run_cli
→ 显式公共 YAML → exact 字段白名单 → AcceptanceRequest.validate / to_query
→ 默认 SyntheticAggregateSource 或显式 module:callable factory
→ run_aggregate_acceptance(source, HotNewsMetricQuery)
→ NewsMetricSource.fetch_snapshots 一次调用 / 有界超时，无自动重试
→ exact list[NewsMetricSnapshot] → 身份/窗口/类型/计数/CTR/重复/行数检查
→ UTC + Decimal 固定精度规范化聚合快照 → SHA-256
→ JSON 报告：检查状态、行数、内容类型行数、匿名 scope/hash、限制说明
```

`app/data_analysis/acceptance.py` 独立于分析引擎，不造热度分数，不改热点排名、业务 Schema、
生产 Bundle 或现有 Adapter。它可以依赖注入现有 `NewsMetricSource`；管理员 factory 必须
返回有 `fetch_snapshots(query)` 的就绪对象。CLI 不挂载到 FastAPI、会话工具或模型调用链。
factory 是管理员明确授权执行的 Python 插件，须事先审核；它不是隔离执行自由代码的接口。

配置只来自显式 `--config`，不自动发现文件、加载 `.env`、发现企业端点或导入 Adapter。
只读取指定的 `.yml/.yaml` 文件，最大 64 KiB，重复键、未知字段和原始密钥字段直接拒绝。
默认参数也可完全不读文件；`--factory` 明确覆盖 YAML 的 `source_factory`。

外部依赖仅 Python 标准库、项目现有领域对象和 PyYAML。默认 fixture 没有数据库、网络或
凭证依赖。企业 factory 的鉴权、密钥管理、RPC/SQL 客户端生命周期和传输超时属于既有
批准 Adapter；本检查器不复制密钥。公共 YAML 不包含密钥字段，插件如需使用企业凭证应
沿用既有 secret manager／环境变量机制，配置文件最多记录环境变量名，不能记录密钥值。

## 配置与执行

示例：[analysis-data-acceptance.example.yml](../deploy/analysis-data-acceptance.example.yml)。
严格字段为 `schema_version/tenant_id/window_start/window_end/content_types/max_rows/
timeout_seconds/source_factory`；所有字段必须存在。时间带时区且开始早于结束，
内容类型为 `article/video` 非重复列表，行数 1–1000，超时 0.1–30 秒。
`tenant_id` 是可信管理员传入的 UUID，不从模型或新闻文本获取；报告中仅保存带命名空间的
租户 SHA-256，不重复输出租户 ID。

```bash
cd agent
# 本地合成，完全不读配置、不联系企业
python tools/check_analysis_data_contract.py
python tools/check_analysis_data_contract.py \
  --config deploy/analysis-data-acceptance.example.yml

# 仅在管理员明确授权并审核实际插件后执行。该例不是已提供或已验收的企业实现。
python tools/check_analysis_data_contract.py \
  --config deploy/analysis-data-acceptance.example.yml \
  --factory approved_enterprise_adapter:build_metric_source
```

报告写到标准输出；通过退出码 0，失败退出码 1。报告不含新闻 ID、指标值、原始明细、
SQL、端点、异常文本或密钥；只有规范化聚合内容的 hash 在报告中作为证据引用。检查器
本身不写本地文件；如管理员需要保留报告，须使用批准的审计存储和保留策略。

## 口径与验收边界

- 返回必须严格是 `list[NewsMetricSnapshot]`，拒绝字典、原始事件、附带用户字段的子类。
- 身份键是 `(news_id, content_type)`；窗口必须与请求的 UTC 时刻一致，类型不能越界，
  重复身份及超过行数限制都失败，不静默截断。
- 基础计数必须是非负 signed-64-bit 整数，拒绝 bool、浮点数、Decimal 计数及负值。
  CTR 必须是有界有限非负 Decimal，并在 `0.00005` 容差内等于点击／曝光。
  零曝光沿用现有快照合同中的 CTR=0；点击超过曝光不擅自判成错误，须确认事件口径。
- UV 不跨新闻或窗口相加。基线不属于该 Port 返回对象，本工具不补零或假造历史参考。
- 空结果可通过结构合同，但报告明确说明实际行指标和身份行为尚未验证。
- 外部错误只返回固定安全 code；超时只尝试一次。asyncio 取消是合作式取消，实际
  Adapter 必须落实其传输层 deadline；此工具无法强行中止不服从取消的企业客户端。

**租户隔离缺口不可由此工具消除。** 请求确实将可信 `tenant_id` 交给 Port，但
`NewsMetricSnapshot` 没有响应租户字段，单个客户端或合成对象不能证明企业返回的数据
属于该租户。报告始终将 `tenant_isolation` 标为 `not_verified`。上线前仍须通过真实
Gateway/IDL、独立二租户 fixture、明确授权与拒绝场景，核对返回证据和数据出域边界。

## 测试入口与 TODO

```bash
cd agent
/opt/homebrew/bin/python3.11 -m pytest tests/test_analysis_data_acceptance.py -q
```

测试覆盖真实默认 CLI、显式 factory 选择、公共配置白名单／重复键、禁止 `.env` 读取、
原始类型／额外字段拒绝、租户请求传递、精确数字与 CTR、窗口与类型作用域、重复与行数、
规范化证据 hash、超时取消及单次失败、异常／凭证文本不泄漏。只运行本地合成和测试替身，
不访问企业端点。

剩余 TODO：企业真实 Adapter/IDL 与鉴权联调；独立二租户数据和授权 fixture 验收；企业
transport deadline、配额、真实数据规模与口径核对；历史基线策略／数据版本保留；审计
存储签名、访问控制及保留治理；生产负载与故障演练。需要真实企业端点、批准凭证和测试
数据才能完成这些外部验收，不把本地替身通过写成企业完成。
