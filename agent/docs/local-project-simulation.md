# 本地项目模拟运行

这份说明用于在本地实际操作 NewsAgent。当前扩量项目使用本地推理／Embedding Port 和确定性合成聚合数据；FastAPI、PostgreSQL、Temporal、Redis、MinIO 和独立分析服务均真实运行。

## 当前入口

| 页面 | 用途 | 本次验收状态 |
| --- | --- | --- |
| [会话 Agent](http://127.0.0.1:5177/chat) | 自然问法查询热点、时间范围说明、追问、六种数据分析、删除与撤销会话 | runtime为local模型；自然问法、缺数据说明与同run追问经真实HTTP验收，删除／撤销经HTTP和页面验收 |
| [热点工作台](http://127.0.0.1:5177/hot-news) | 同一运行的 SQL、指标、榜单和解释 | 已保存 Top100 合成运行可读取 |
| [写作任务](http://127.0.0.1:5177/jobs) | 创建选题、查看研究与人工节点 | 真实任务完成 research，等待资料审核 |
| [反馈案例](http://127.0.0.1:5177/data-loop/feedback) | 运营反馈、标注与复核 | 真实合成反馈入库，状态 needs_label |
| [安全演练](http://127.0.0.1:5177/security-tests) | SQL／租户／注入校验器 dry-run | 沿用现有演练入口，不执行 SQL |

业务 API 为 `http://127.0.0.1:28030`。项目名 `news-agent-enterprise-scale-demo`，独立数据卷保留历史；数量固定为每租户1200篇、两租户共2400篇。日期窗口仍是2026-10-03的固定合成样本。

## 恢复已有环境

需要 Docker Desktop、Node.js 20.19+ 和已经构建的本地镜像。首次构建及独立分析服务准备按
[分析服务说明](data-analysis-demo.md) 和 [扩量说明](enterprise-synthetic-data.md)执行；以下命令复用已有镜像，不下载镜像，也不删除数据。

在仓库根目录进入 `agent`：

```bash
cd agent
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml up -d --pull never \
  analysis-service analysis-gateway
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml up -d --pull never \
  api hot-news-worker writing-worker outbox-worker data-loop-worker
curl -fsS http://127.0.0.1:28030/ready
```

令牌是仓库公开的本地测试值，不能用于企业部署。命令显式使用 `/dev/null` 作为 Compose 环境文件。不要把运行中的数据库改成另一数量档；换档需另建隔离项目和空数据卷。

在另一个终端启动前端并保持终端运行：

```bash
cd agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28030 \
VITE_DEV_PORT=5177 VITE_LOCAL_SIMULATION=1 npm run dev
```

依赖尚未安装时先运行 `npm ci`。若5177已在运行，直接使用现有页面，避免启动另一个前端。源码修改后需要重新构建后端镜像；仅执行 `up` 不会把新 Python 源码装入旧镜像。

2026-10-07已将当前API镜像更新并迁移到`20261007_0020`，包含0019摘要列和0020软删除标记；
迁移前后原有11条会话、87条轮次记录数量不变。删除与撤销已实际验收；这次迁移本身不代表
长度触发的摘要压缩已完成真实端到端验收。

以后更新会话代码时，在`agent`下先按扩量说明重新构建镜像，再显式运行一次迁移，最后仅重建API：

```bash
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml run --rm --no-deps migrate
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml up -d --no-deps --pull never --force-recreate api
curl -fsS http://127.0.0.1:28030/ready
```

已有退出的migrate容器不能代替这次迁移。新API必须在新增列就绪后启动；软删除列允许为空，
摘要列使用默认空对象，兼容现有Worker；本次保留Worker运行状态和数据卷。

## 实际操作

聊天中新建对话，展开“其他示例问题”，点击“今日热点”或“看看热点新闻”只会填入草稿，确认后再发送。
2026-10-07已部署`native-conversation-v5`；未指定日期时使用
`2026-10-03T00:00:00+08:00`至`2026-10-03T01:00:00+08:00`的固定样本。
可依次发送“你好，你能告诉我今日热点新闻有哪些吗？”、“你好，请帮我看看热门新闻”、
“解释第一条新闻”：第一问提示当天数据不在覆盖内，第二问返回5条样本新闻，第三问沿用第二问的同一运行。
再发送“官方热榜前五条”或“查询点击量超过1000的前五条新闻”，查看明确的来源或条件限制。
三个拒绝请求均未启动Workflow，实际SQL审计条数不变；没有用旧榜单代替今日结果。
浏览器也已实测“今日热点”只填草稿，用户发送后经SSE完成并显示缺数据说明；
独立验收会话目前保存6轮，用户原有会话未被修改。完整调用链、配置、测试、
实际HTTP证据、浏览器截图与保留会话见[自然问法说明](query-understanding.md)。

原示例`查询点击率最高的前5条视频新闻并分析原因`仍可使用。企业情形通过页面的数据分析区域选择并准备，
再执行所选分析；可直接查看已有[榜单进出会话](http://127.0.0.1:5177/chat?conversation=7496a096-630b-42e1-a26e-d210e34a8281)。

每条会话右侧可点击“删除”，确认后从列表隐藏；本页提供最近一次删除的“撤销删除”。
生成答复时暂时禁删，删除当前会话会切换到剩余会话，最后一条则进入空状态。消息历史仍保留，
详见[会话删除与撤销](conversation-deletion.md)。

写作页面已有选题 `合成演示：企业季度数据解读与证据核查`，任务 ID 为
`cd678c4e-2f90-4106-b54e-c52c35c5a259`。选择该行，查看进度、研究指标、资料包和事件流。
实际状态为 `research_review`，进度15%，Temporal等待 `research` 人工节点；研究包保存3条模拟事实、2个不同示例URL、引用覆盖100%、平均置信度0.1。示例URL不代表已经联网取证或独立媒体来源。

也可以填写另一个明确标记“合成演示”的选题并点击创建任务。系统应执行 research 后停在同一审核节点；后续资料／提纲／终稿决策需要人在页面操作，不能把已到人工节点描述为已完成全文。

反馈页面已有案例 `5a141db0-4e78-404b-be8a-fa7cc5bb94df`，来自已完成的运行
`541580c9-07b5-4f13-a932-37ca39215e6f` 和新闻 `scale-news-000007`，问题类型
`missing_evidence`、状态 `needs_label`。选中案例可查看真实保存的输入与报告，提交标签后仍需人工复核。

Data Loop Worker 已连接 `hot-news-data-loop` 的 workflow 和 activity 队列。本次仅验证 Worker 就绪与反馈回流；完整评测要求审核标签、冻结数据集、支持的候选与基准 Bundle，之后还需人工批准／激活。没有自动批准标签、修改生产配置或发布内容。

## 运行验收与定位

`/ready` 只证明数据库、Redis、Temporal连通，不能替代 Worker 验收。在 `agent` 下执行：

```bash
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml \
  -f deploy/docker-compose.enterprise-scale-demo.yml ps
docker exec news-agent-enterprise-scale-demo-temporal-1 temporal task-queue describe \
  --address temporal:7233 --task-queue hot-news-data-loop
```

第二条命令应显示 workflow 和 activity poller。写作任务的实际HTTP验收入口为
`GET /api/v1/jobs/{id}/progress`、`/research-package`、`/research-metrics`，使用与前端代理相同的公开本地租户／用户头；反馈通过 `GET /api/v1/data-loop/feedback-cases` 验收，并要求本地 Bearer 和只读角色。前端代理验证入口为 `GET http://127.0.0.1:5177/api/v1/conversations/runtime`。

本次配置验证比较了 Data Loop 与 writing Worker 的完整环境、内部网络和隔离参数，并实际确认 Temporal poller。新增 Worker 显式继承原始 `docker-compose.native-e2e.yml` 的 writing-worker；不能只继承当前 overlay 中仅有 image 的同名片段，否则会丢失环境和依赖。

相关现有测试入口为 `tests/test_native_writing_chain.py`、`tests/test_data_loop_worker.py`、
`tests/test_feedback_collector.py` 与 `tests/test_scaled_demo_http.py`。写作、反馈和Worker就绪的验收范围仍如上文。
本次自然问法后端回归`2097 passed / 5 skipped / 62 deselected`，最后范围渲染调整后3个新模块`118 passed`；
前端全量`220 passed`，lint、typecheck与build通过。真实HTTP没有调用企业模型；兼容模型场景及MockTransport
证明协议和条件门禁，语言质量与真实当日数据仍需单独验收。

## 调用链、配置与后续项

- 写作：页面／jobs API → `JobService.create_or_get`保存任务与outbox → `OrchestratorService.start_job` → Temporal `NewsWritingWorkflow` → `NewsStepHandler`／本地 Research Port → Schema与确定性质量校验 → MinIO Artifact、PostgreSQL Checkpoint → `research`人工节点 → API／页面；`outbox-worker`通过Redis投影事件。
- 反馈：已完成热点运行 → `record_operator_decision` → `HotNewsDecisionService` → `AnalysisFeedbackCollector`保存不可变案例 → PostgreSQL → 反馈页面／标注／人工复核；审核案例才可进入数据集与评测。
- Data Loop：runs API → `DataLoopOrchestrator` → `HotNewsDataLoopWorkflow` → `DataLoopActivities`／`HotNewsDataLoopStepHandler` → 本地归因与离线回放、MinIO资产、确定性评测门禁 → 人工晋升／激活节点 → 页面。此完整评测链没有在本次触发。
- 会话查询：示例填稿／用户发送 → 网关身份与新查询授权 → `ConversationAgentService`持久轮次 → 原问题一致性校验 → `query_understanding`模型Port → Python日期、来源、条件和水位门禁 → 规范问句与`expected_intent` → 原Text2SQL逐项一致校验 → SQL绑定与Temporal热点链路 → 同run指标、排行、分析 → 工具检查点与Python范围说明 → 保存答复／SSE页面；不满足范围时在SQL和Workflow前停止。
- 配置：`MODEL_RUNTIME_CONFIG_PATH`绑定本地模型YAML；`TEMPORAL_TASK_QUEUE=news-writing`、`TEMPORAL_DATA_LOOP_TASK_QUEUE=hot-news-data-loop`；三层Compose绑定上述数据库／Artifact／网关测试身份，前端通过`VITE_DEV_*`指向28030／5177。没有新增业务Schema或扩大批准范围。
- TODO：真实当日数据、水位与企业模型自然语言质量验收，见自然问法说明；补齐原生 Data Loop E2E driver 与前端评测全链路验收；会话压缩需单独进行长度触发的真实验收；会话回收站和永久清除策略见删除说明；企业模型／数据合同与生产隔离仍待联调。旧 `examples.data_loop_e2e` 驱动缺失，旧运行manifest不能直接套用当前本地模型注册表。
