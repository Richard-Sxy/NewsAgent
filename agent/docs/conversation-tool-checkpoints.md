# 会话工具终态检查点与审计保留

每个工具终态现在先提交 PostgreSQL，再发送 `tool_finished` 或调用下一次规划模型。检查点
保存已执行工具的原始白名单结果与已取得的模型请求 ID；轮次最终答复仍须独立保存后才发送
`answer_delta/done`。不改变工具审批、租户权限、指标算法或热点 Workflow。

## 完整调用链

```text
/chat → conversationsApi.send/sendStream
→ /api/v1/conversations/{id}/messages[/stream]
→ Gateway tenant/user + hot-news:read
→ ConversationAgentService.prepare_turn
→ PostgresConversationRepository.begin_turn：会话行锁、请求 UUID 与 processing 单飞
→ ConversationAgentService.run_turn
→ 原生结构化模型 Port → ConversationPlan
→ 有界批准工具（读取、分析或已批准的热点查询）
→ ToolTrace：completed/failed/denied、attempts、参数、白名单结果、错误码
→ PostgresConversationRepository.checkpoint_turn
→ 同一 tenant/user/conversation/turn/request + processing 校验
→ 已提交 tools/model_request_ids 前缀核对
→ conversation_turns 两个 JSONB 字段事务提交
→ tool_finished SSE 摘要 → 下一次规划或 Python 渲染
→ finish_turn：前缀核对、答复与终态事务提交
→ 普通 JSON 响应或 answer_delta/done
```

`checkpoint_turn` 与 `finish_turn` 都通过所属会话的 `FOR UPDATE` 锁串行执行，不能删除、
缩短或改写已保存的工具和模型请求 ID 前缀。检查点按完整列表提交，相同列表重复提交可
幂等接受。请求 UUID 不匹配、跨租户/用户/会话、轮次不存在会拒绝；已经完成或中断的轮次
不再允许检查点或迟到提交。没有公开的检查点写入 API，身份和 UUID 由原有服务端 claim 传入。

复用现有 `conversation_turns.tools/model_request_ids`，无需数据库迁移。上游输入继续经过
工具参数 Schema 和业务白名单；分析数值由隔离执行器计算和校验。核心依赖仍是 Python
会话服务、SQLAlchemy 和 PostgreSQL，不新增模型接口或外部 Agent 框架。模型/Prompt/Bundle
版本绑定的 `runtime_metadata` 不因检查点改变。

## 失败恢复与预算

- 检查点成功后硬退出，已提交工具仍可由 GET 历史读取。GET 或下一条消息按原有过期规则
  将旧 processing 标为 failed/interrupted；该恢复只更新轮次状态和完成时间，保留工具及
  模型请求 ID。相同消息 UUID 重放不会重执行工具，也不会自动续跑中间计划。
- 普通断线/取消仍进行至多 5 秒的中断保存，并等待任务清理。已提交工具不能被空列表覆盖。
- 检查点存储失败会停止本轮规划，不发送该次 `tool_finished`；随后尝试保存失败轮次。
  如果最终保存也不可用，流仅返回安全错误，历史依原有过期恢复处理，不伪造保存成功。
- `CONVERSATION_MAX_TOOL_CALLS` 默认 3、上限 6。完成、失败和拒绝均占用工具预算，每个
  终态至多新增一次检查点事务；最终提交仍为一笔独立事务。普通只读工具的有界重试仍只
  保存最终 ToolTrace 和 attempts，不新增重试，也不单独提交每次失败尝试。
- `CONVERSATION_TURN_TIMEOUT_SECONDS` 默认 60 秒、范围 5–120 秒；检查点占用同一轮次
  预算。过期恢复仍为单轮预算加 30 秒，流式外层仍给处理/保存额外 10 秒。历史默认最近
  8 个完成轮次。没有新增配置项或后台清理任务。

该版本只保证工具终态提交后的可追溯性。工具执行完成到检查点成功之间仍存在崩溃间隙；
查询绑定、Temporal 启动及工具内部执行步骤没有新增中途检查点。`query_hot_news` 的
`on_bound` 在尝试启动 Workflow 前发生，绑定引用不能证明 Workflow 已启动或完成。
已启动 Workflow 不随聊天断线取消。本能力不能宣称端到端 exactly-once、自动恢复规划或
模型 token 断点续传。检查点本身没有新增逐工具时间戳、独立审计表或生产签名。

## 审计保留策略

本地最小策略版本为 **`retain-until-approved-v1`**：保留会话工具检查点和原有业务记录，
本功能不自动删除、裁剪、覆盖或搬迁 PostgreSQL 会话、热点运行、SQL 审计和 Artifact。
它是本说明明确的保留规则，不是假称已经完成企业数据治理，也未作为企业批准的期限写入
生产配置。既有 `ON DELETE CASCADE` 数据库关系未改变，本功能没有增加删除入口。

访问仍受租户、用户和会话归属限制；运行结果只包含工具已批准的字段，不能新增企业原始
行为明细、秘密或任意执行输入。规模预算沿用各工具与分析执行器的行数/字节限制、会话
工具次数与请求预算；历史查询的 limit 只限制返回条数，不删除更早的审计。

实际保留天数、法律保全、归档与销毁权限、审批人、签名式审计、加密/脱敏及容量告警需由
企业明确后另行版本化。未经批准不设置自动删除天数；未来变更必须同时审查会话幂等、
引用与业务证据生命周期，不能为节省存储自动删除原业务记录。

## 测试入口与未完成边界

```bash
cd agent
python -m pytest tests/test_conversation_tool_checkpoints.py \
  tests/test_conversation_store.py tests/test_conversation_agent.py \
  tests/test_conversation_stream.py tests/test_conversation_hot_news_query.py \
  tests/test_conversation_data_analysis.py tests/test_conversation_analysis_comparison.py -q
python -m pytest tests/test_conversation_postgres.py -q
```

专项验证包含工具提交后模拟进程退出与过期恢复、同请求重放、跨 tenant/user/request 拒绝、
旧/短列表与内容改写拒绝、正常 SSE 提交顺序、断线后工具保留和检查点存储失败后停止规划。
PostgreSQL 专项需显式设置 `CONVERSATION_TEST_DATABASE_URL`，仅接受原有隔离本地目标，
使用随机身份并保留测试记录；未启用时跳过，不表示真实数据库已验收。测试不连接企业模型。

2026-10-04 验证：上述会话专项 `157 passed`；显式启用隔离本地 PostgreSQL 后，其
6 项验收全部通过，包括新增的检查点提交、跨身份/请求拒绝、旧 finish 拒绝、重连与过期
恢复。随机测试记录保留，没有删除已有会话或热点业务数据。以上不代表企业生产长稳验收。

未完成：真实生产数据库负载与锁/事务超时验收、工具内部及查询绑定检查点、逐尝试审计、
受控恢复流程、企业保留期限及销毁审批、生产签名与不可变存储、长期容量治理。
