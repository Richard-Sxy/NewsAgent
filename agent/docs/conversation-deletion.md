# 会话删除与撤销

会话列表每一行提供“删除”按钮。确认后从列表隐藏该会话；原消息、工具检查点和摘要保留。
本页提供“撤销删除”，恢复后可继续读取历史。删除会话不会删除关联热点运行、分析报告、
运营反馈或写作任务。当前不提供永久清除或跨用户管理权限。

## 操作与接口

- `/chat` → 会话行“删除” → 确认弹窗 → `DELETE /api/v1/conversations/{id}`。
- 服务端成功返回204后，前端移除会话；删除当前会话时切换到剩余会话或空状态，清理对应
  草稿、待发送状态和轮询。删除其他会话不切换当前对话。
- “撤销删除”调用 `POST /api/v1/conversations/{id}/restore`，返回200和`ConversationView`。
  撤销入口只保留本页最近一次成功删除；刷新后可用同一身份通过restore接口恢复。
- 存储、网关鉴权沿用会话接口。两接口均从可信身份取得tenant/user，不接受请求体中的
  身份或角色。跨租户、跨用户或不存在的会话统一返回404。
- 重复删除返回204，重复恢复返回200。删除状态下的历史、普通消息、SSE和摘要访问被拒绝。
- 未过期processing轮次返回409，页面提示等待答复。已超过“单轮预算+30秒”的轮次先
  记录interrupted，再执行删除；不会取消已经启动的热点Temporal Workflow。

## 完整调用链

```text
ChatView 每行删除按钮 / 确认弹窗
→ conversationsApi.remove(id)
→ DELETE /api/v1/conversations/{id}
→ 网关 Bearer / HotNewsPermission.READ / tenant_id / user_id
→ PostgresConversationRepository.delete
→ 按 tenant + user + id SELECT conversations FOR UPDATE
→ 检查 processing；过期轮次转换为 failed/interrupted
→ PostgreSQL conversations.deleted_at = 当前UTC时间
→ 204；列表过滤 deleted_at，前端清理该会话并切换

撤销按钮 → conversationsApi.restore(id)
→ POST /{id}/restore → 同一归属与行锁校验
→ deleted_at = NULL → ConversationView → 恢复列表与历史读取
```

`_get_conversation`默认过滤软删除，`_require_conversation`与begin/checkpoint/finish/summary
写入共用会话锁，避免删除和新消息同时成功。get、context_page、load_memory也验证父会话。
删除不会调用推理或Embedding Port；外部依赖仅沿用PostgreSQL与现有身份网关，热点等
下游业务运行保持独立。客户端对迟到列表／详情／流事件加状态保护，删除成功后不能被
旧响应重新插回。接口失败时保留原页面状态，便于确认服务端结果。

## 配置、迁移与测试入口

没有新增权限或环境变量。沿用`CONVERSATION_ENABLED`、`CONVERSATION_TURN_TIMEOUT_SECONDS`
和已有数据库／网关配置。迁移`20261007_0020`在0019后给conversations增加可空的
`deleted_at TIMESTAMPTZ`；原会话默认NULL、历史消息和摘要不改写。

更新本地模拟需要先构建新镜像，再执行迁移，最后重建API。已有环境仍在0018时会同时
经过0019的context_memory新增步骤；禁止让新API查询旧Schema。本地三层Compose恢复命令见
[项目模拟运行](local-project-simulation.md)。

后端测试入口：`tests/test_conversation_deletion.py`以及会话store/security/stream回归。
真实PostgreSQL测试入口是`tests/test_conversation_postgres.py`，显式设置
`CONVERSATION_TEST_DATABASE_URL`，只接受指定本地模拟库，使用随机owner并保留测试记录。
前端测试入口：`frontend/tests/conversations.test.mjs`，同时执行`npm run lint`、
`npm run typecheck`与`npm run build`。验收需覆盖取消确认、删除当前／其他／最后一个会话、
失败保留、撤销、权限隔离、活跃请求冲突和迟到响应。

2026-10-07本地验收：相关后端六模块106项通过，隔离PostgreSQL九项通过；前端全量208项通过，
lint、typecheck和build通过。实际HTTP验证删除204、重复删除204、列表隐藏／读取与发送404、
跨租户与用户404、恢复200及消息历史一致；浏览器用新建验收会话完成取消确认、删除当前会话、
切换剩余会话、撤销和恢复历史。原演示会话未删除。当前扩量API已部署0020。

后续TODO：跨刷新可见的回收站列表、生产数据保留期限与审核后的永久清除；当前删除表示
隐藏并可恢复，不宣称已执行个人数据永久擦除。
