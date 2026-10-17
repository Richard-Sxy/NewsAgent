# 模型理解原问，Python 校验结构化条件

## 入口与职责

企业兼容模型的查询理解采用v5：用户无需匹配日期或新闻查询正则，模型输出已有
`QueryUnderstanding` Pydantic结构；Python只检查基础文本、Schema、状态、指标/栏目/
类型/数量、榜单来源、时间口径、整点小时、水位和数据覆盖。标准问题由Python构造，
交给已有的SQL意图复核和编译链路。

原始问题、模型调用引用和实际窗口保留在工具记录中。模型漏条件或改日期已不由原句
正则证明，须通过模型样例评测和审计核对语义完整性；这些字段门禁不能替代语言质量评测。
模型、原问和检索内容不能改变服务端权限、SQL、配置或发布审批。

## 完整调用链

```text
/chat → 网关身份 → 消息/SSE API → ConversationAgentService.run_turn
→ query_hot_news 专用分支 → 原问保留/授权校验 → ConversationTools.execute_hot_news_query
→ LocalConversationHotNewsQuery._understand
  → normalize_question（长度、空内容、隐藏控制字符）
  → NativeStructuredAgentClient.run_structured(query_understanding v5)
  → QueryUnderstanding（固定枚举、ISO日期、整点小时、SqlAssistantIntent）
  → openai_compatible: resolve_structured_query
      → 结构/状态/白名单/来源/时间口径 → Python服务端时钟和窗口 → 水位/覆盖
      → QueryResolution + _canonical_question
  → local测试Port: 原有限规则替身与resolve_query_understanding
→ execute_local_hot_news_query → preview(expected_intent)
→ 标准问题独立槽位复核 → compile_query → SQL AST Guard/服务端租户参数
→ SQL预览与运行绑定 → Temporal热点Workflow/Activity → PostgreSQL只读聚合
→ Python指标/热度 → 既有热点分析模型与Validator → PostgreSQL/MinIO报告
→ 查询引用交叉核对 → 工具检查点 → 会话终态 → SSE done/历史GET → 页面
```

核心文件是`app/conversation/structured_query_resolution.py`的`resolve_structured_query`、
`examples/conversation_hot_news.py`的`_understand`和原有SQL编译/Guard。
`resolution_mode=structured_fields`表示兼容模型使用结构化字段校验；`local_rules`表示
本地确定性Port仍采用有限解析。前置检查不再对原问做关键词拒绝；本地替身并不是大模型。
时间解析由模型负责；例如`9.19日八点到九点`可输出2026-09-19、8和9，Python核验实际
窗口，不要求先增加一条日期正则。两小时请求不能被缩成一小时获准。

## Prompt、配置与本地部署

`deploy/model-runtime.local.yml`注册并绑定`native-query-understanding-v5`；兼容公开模板
注册并绑定`compatible-query-understanding-v5`。v1/v2/v4继续保留供版本追溯。
独立客户端沿用`CONVERSATION_QUERY_UNDERSTANDING_CONFIG_PATH`和已有兼容连接配置，
从应用配置选择批准Prompt，从连接配置选择模型路由与凭证环境变量。

新API镜像为`news-agent/writing-agent-service:bge-query-understanding-v5`，基于已有
BGE镜像仅叠加本次查询代码、Prompt和测试；不复制整个工作区、私有连接文件或.env。
`deploy/docker-compose.structured-query-demo.yml`只覆盖API镜像。

已构建该镜像后，在本地终端运行：

```bash
python3 agent/tools/activate_structured_query_demo.py
```

`activate_structured_query_demo.main`复用`BgeDemoDeployment`：先从进程环境或getpass隐藏
输入获取现有模型密钥，再用五层Compose显式`--env-file /dev/null`校验配置，最后仅
`--no-deps --no-build --pull never --force-recreate api`并检查HTTP、实际理解provider和
Worker poller。不清空数据、不重建数据库、不改变Worker的分析模型。不打印或落盘密钥。

页面仍为`http://127.0.0.1:5178/chat`，API28060。runtime应显示：

```text
query_scope.understanding_model.provider = openai_compatible
query_scope.understanding_model.prompt_version = native-query-understanding-v5
query_scope.resolution_mode = structured_fields
```

这里是隔离本地演示的查询模型更新，不等于生产模型/Prompt发布审批。
外部依赖沿用批准的模型HTTP接口、PostgreSQL、Temporal、Redis、MinIO及本地BGE包；
无新增数据库迁移或Agent框架。

## 测试与验收

新增`tests/test_structured_query_resolution.py`覆盖原问无需正则、配置模型实际选择新门禁、
非法Schema/日期、未知栏目/数量/来源、缺小时/跨小时、相对日期、最新/默认窗口、
基础检查与模型返回状态。`tests/test_activate_structured_query_demo.py`验证缺凭证零部署
和只重建API。既有理解集成测试更新为“基础检查前置，语义进入理解Port”；本地替身的
旧规则校验和SQL编译/Guard仍有回归。

验收入口：上述两模块和`test_query_understanding*`、
`test_sql_assistant_confirmed_intent.py`、`test_hot_news_query_windows.py`、
`test_conversation_query_resolution.py`。新镜像断网只读回归243项通过，只代表本地代码
和HTTP替身合同，不代表真实模型理解质量。

真实交互应使用独立验收会话发送用户原句，检查保存的工具窗口为2026-09-19 08:00至09:00，
理解模型请求引用存在，SQL provider为python-confirmed-intent，工具与报告的sql_query_id
一致。日期覆盖外和非单小时请求应有说明，SQL/Workflow不应执行。

TODO：安全注入现有凭证、启用并重启兼容模型API后完成本次真实聊天验收；补充模型
漏条件、未知表达、时间歧义与提示词干扰的质量样例。生产发布和企业真实数据仍按既有
审批与合同流程执行。
