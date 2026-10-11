# NewsAgent 本地全链路演示

本次演示使用隔离合成数仓、本地确定性模型 Port 和独立固定操作分析服务，验证取数、
指标／热度、Agent 工具、校验、持久化和页面展示；不代表企业模型、企业数据或生产验收。
`28000` 的既有企业模型演示继续运行，本次使用独立 `news-agent-analysis-demo` 项目、
API `28010` 与前端 `5175`，不改旧项目、镜像或数据卷。

完整命令、配置、口径、拒绝场景与 CLI 见
[聊天数据分析演示](../agent/docs/data-analysis-demo.md)。以下命令从项目根目录开始。

扩充的企业情形使用另一个独立实例：API28020、前端5176，保留本页的经典演示。
数量扩容使用API28030、前端5177：默认两租户共2,400篇新闻、57,600条小时指标和同量基线，单次Top100；另可配置每租户120／12,000篇的新隔离实例。
10组运营场景、20项数据契约演练、选择方式及完整调用链见
[企业情形合成数据演示](../agent/docs/enterprise-synthetic-data.md)。

## 1. 启动两个独立项目

需要 Docker Compose `2.24.4+`，用于 Overlay 的 `ports: !override`，见
[Compose 官方覆盖规则](https://docs.docker.com/reference/compose-file/merge/#replace-value)。
先启动专用分析服务，再启动合成业务栈；不要合并两套完整 Compose，也无需手工设置 `-p`。

```bash
cd agent
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml up -d --build
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml build migrate
```

正常构建因 Python 包下载 DNS／网络错误失败时，只有可信本地 `native-e2e` 镜像已存在
才能用离线备用构建。先核对镜像 ID，替换变量中的占位值，再执行：

```bash
docker image inspect news-agent/writing-agent-service:native-e2e --format '{{.Id}}'
E2E_CACHE_IMAGE_ID=sha256:REPLACE_WITH_VERIFIED_IMAGE_ID
docker tag "$E2E_CACHE_IMAGE_ID" news-agent/e2e-dependency-cache:local
docker build --network none --pull=false \
  --build-arg E2E_DEPENDENCY_IMAGE=news-agent/e2e-dependency-cache:local \
  -f Dockerfile.e2e.cached -t news-agent/writing-agent-service:analysis-demo .
```

备用入口只提取依赖，使用当前源码并校验 `pyproject.toml`／`pip check`，不继承缓存
`/app`。缓存缺失或不兼容就停止，不从企业或未知镜像替代。正常或备用构建成功后都用
以下启动命令，不加 `--build`：

```bash
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml up -d api hot-news-worker
curl -fsS http://127.0.0.1:28010/ready
curl -fsS http://127.0.0.1:28100/health \
  -H "Authorization: Bearer ${ANALYSIS_SERVICE_TOKEN}"
```

这个 token 是公开本地测试值，不是企业凭证；两套 Compose 都不读取项目 `.env`。
`/ready` 应显示 PostgreSQL、Redis 和 Temporal 为 `ok`。已有这个新演示项目时，只需
重建共享业务镜像，再 `up -d --no-deps api hot-news-worker`，保留它的基础依赖和历史。
不要把这些更新命令用于旧项目。

## 2. 启动新前端

另开终端，从项目根目录运行：

```bash
cd agent/frontend
npm ci
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28010 \
VITE_DEV_PORT=5175 VITE_LOCAL_SIMULATION=1 npm run dev
```

首次安装或锁文件变化才需 `npm ci`。Vite 使用 `dev-server/local.yml` 中的公开本地身份，
上述 shell 参数只覆盖新 API／端口，不修改 YAML，也不影响已有 `5174` 页面。企业凭证
不进入前端 YAML 或浏览器代码。

打开 `http://127.0.0.1:5175/chat`。

## 3. 逐项演示六种分析

1. 点击“准备两窗口演示”，准备固定北京时间 `2026-10-03 22:00–23:00` 与
   `23:00–2026-10-04 00:00`；两次均为 `news-ranking`／点击前 12 候选策略。
   按钮等待真实热点 Workflow 完成，并自动创建新演示会话、真实读取参考和当前运行。
2. 核对两条读取答复与成功 `read_hot_news` 轨迹，确认窗口和完整排行数量；12 只是
   上限，不能只展示 UUID 就视为授权完成。复用情况按历史 run／Workflow ID 核对，
   返回没有独立 `reused` 标记。
3. 依次点击“指标概况”“指标分布”“高低对比”“数据质量”“已存基线”“两窗口趋势”。
   按钮只填草稿，用户点击“发送”
   才通过原有 Agent／分析工具执行。
4. 查看结果卡、“计算证据与资源审计”和工具轨迹，核对操作、指标、窗口、范围、
   算法／Bundle、快照和输入哈希、实际执行后端／transport、限额与耗时。
5. 刷新页面确认服务端恢复历史；重复准备相同范围应复用运行。旧 22／23 点范围不兼容
   导致 409 时，只读核对 run／SQL／Bundle，不覆盖旧记录或自动改指标、场景来跑通。

权威数字由 Python／SQL 计算。趋势仅比较同规则、同版本、等长非重叠窗口的共同新闻，
新增／退出新闻不参与聚合；UV 不跨新闻或窗口相加，零分母和缺失基线保持未知。两个
观测窗口不代表全站增长、实时新闻、连续走势或因果。

同链路 CLI：

```bash
cd agent
python -m examples.data_analysis_demo
python -m examples.data_analysis_demo --prepare-only
```

CLI 默认指向本机 `28010`，使用公开本地身份；只在本地模型、新查询和分析均获准时继续。
`--prepare-only` 也创建会话和两条读取记录，只跳过六种分析；完整模式继续发送六次
问题并验证实际工具结果。不访问企业端点。可用返回的 `conversation_id` 打开
`http://127.0.0.1:5175/chat?conversation=<conversation_id>` 检查保存的结果。

## 4. 讲解完整调用链

```text
/chat 准备 / CLI
  → 本地管理员身份与 runtime 守卫
  → 同一 news-ranking 场景的两个固定小时
  → Text2SQL 受限意图／SQL Guard／原子 run 绑定
  → Temporal 热点 Workflow
  → PostgreSQL 只读候选与同小时指标／保存基线
  → NewsMetricSnapshot → Python 权威热度／排行
  → 内容／知识证据 → 原生模型 Port → Schema／Validator
  → analysis_runs 保存结果
  → 新会话 read_hot_news 参考／当前 → 成功来源轨迹
  → 用户发送分析草稿 → ConversationAgentService
  → analyze_hot_news_data → project_dataset 可信数值投影／范围检查
  → RemoteAnalysisRunner → 固定 analysis-gateway → 独立服务
  → 固定 worker / engine.analyze → 完整结果再次确定性复核
  → Python 渲染 → PostgreSQL ToolTrace／终态 → SSE → 结果卡／审计
```

讲解重点：模型不能改权威数字、范围、来源、权限、规则或发布边界。无效输出或不兼容
数据明确失败，不换口径、不补造数据、不退回宿主进程。独立服务中的 `backend=process`
加 `transport=service` 表示服务内固定进程，不表示 API 本地执行。

## 5. 可选知识入库与验证

仅对这个新的合成业务栈演示知识入库，公开本地 token 与企业凭证无关：

```bash
curl -fsS -X POST http://127.0.0.1:28010/api/v1/knowledge/documents \
  -H 'Authorization: Bearer newsagent-native-knowledge-token-not-for-production' \
  -H 'X-Tenant-ID: 11111111-1111-4111-8111-111111111111' \
  -H 'Content-Type: application/json' \
  --data '{"documents":[{"document_id":"demo-001","news_id":"demo-001","content_version":1,"title":"示例新闻","text":"这是用于本地知识检索的示例正文。","publish_time":"2026-10-02T10:00:00+08:00","source_url":"https://example.invalid/demo-001","media_type":"article","text_source":"demo","metadata":{"topic":"demo"}}]}'
```

响应 `created/updated/skipped/failed` 是实际索引状态；正文经确定性切片、Embedding
替身和向量索引后可进入关联证据链。这些合成内容不能用于新闻事实判断。

从项目根目录执行验证；以下是测试入口，实际结果以当次执行记录为准：

```bash
cd agent
python -m pytest -q
cd ../news
python -m pytest -q
cd ../agent/frontend
npm test
npm run lint
npm run build
```

演示专项及独立容器验收入口见[演示说明](../agent/docs/data-analysis-demo.md)。依赖包括
Python、FastAPI、PostgreSQL、Redis、Temporal、MinIO、Docker／固定网关，以及前端
Node／Vue／Vite；`news/` 负责采集，本次固定样本无需抓取企业或外部新闻。

## 6. 停止本次演示

从项目根目录进入 `agent/`，仅停止新项目的选定服务：

```bash
cd agent
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml stop api hot-news-worker
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml stop
```

保留两个新项目的数据和网络，旧企业模型演示不受影响。不要运行 `down -v`。
剩余 TODO 是真实企业 Adapter／IDL／watermark／UV／基线口径、独立双租户授权 fixture、
模型工具规划质量、生产 TLS／Secret／CNI、容量与故障验收；发布和配置晋升继续走人工
审批，不能把此演示通过写成企业上线完成。
