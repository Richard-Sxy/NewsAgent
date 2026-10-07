# 聊天数据分析：可操作的本地演示

需要更丰富的企业运营情形时，使用 [企业情形合成数据演示](enterprise-synthetic-data.md)：
独立数据实例、10组运营场景和20项数据契约演练，保留本页的标准两窗口及已有快照。

这份演示在 `/chat` 中展示概况、分布、最高／最低比较、质量检查、保存基线对照和两个
可比窗口的趋势。新闻、数仓和模型均为隔离合成样本；分析通过独立服务运行固定 Python
计算，不代表企业模型质量、企业数仓或生产环境已经验收。

演示准备两个固定北京时间窗口：`2026-10-03 22:00–23:00` 和
`2026-10-03 23:00–2026-10-04 00:00`。两次取数均使用 `news-ranking` 场景、同一批准
Bundle 和点击量前 12 的候选策略。12 是查询上限；SQL 候选数、上榜数、模型分析数及
共同新闻数均以实际返回为准，不能把少于 12 条的运行说成完整 12 条。

`28000` 上的既有企业模型演示继续保留。本次使用独立业务项目
`news-agent-analysis-demo`、API `28010` 和前端 `5175`；不重建、停止或清空旧项目。

## 1. 启动独立分析服务

从项目根目录进入 `agent/`，在当前终端注入一个**公开的本地专用测试 token**。以下值
只用于此合成演示，不是企业凭证。两套 Compose 均显式使用空环境文件，不读取项目
`.env`；真实生产 token 必须由秘密管理系统独立注入。

```bash
cd agent
export ANALYSIS_SERVICE_TOKEN=newsagent-analysis-demo-token-not-for-production
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml up -d --build
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml ps
curl -fsS http://127.0.0.1:28100/health \
  -H "Authorization: Bearer ${ANALYSIS_SERVICE_TOKEN}"
```

独立项目名为 `newsagent-analysis-service`。`analysis-service` 留在 internal 私有网络；
只有固定 `analysis-gateway` 加入 ingress 网络并发布本机 `28100`。普通业务 API 不挂载
Docker socket，也不能指定任意分析端点或执行代码。

## 2. 启动独立的合成业务栈

使用 `docker-compose.native-e2e.yml` 加小型 Overlay
[`docker-compose.analysis-demo.yml`](../deploy/docker-compose.analysis-demo.yml)。不要把独立
分析服务的完整 Compose 合并进业务 Compose。Overlay 自带独立项目名、镜像及端口；
无需 `-p`，也不要删除任何项目的数据卷。其 `ports: !override` 需要 Docker Compose
`2.24.4+`，见 [Compose 官方覆盖规则](https://docs.docker.com/reference/compose-file/merge/#replace-value)；
较旧版本应升级工具后再用此模板，不手工删旧服务来腾端口。

```bash
# migrate 定义构建入口；API 和热点 Worker 复用该业务镜像。
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml build migrate
```

若正常构建因 Python 包下载的 DNS／网络错误失败，仅在本机**已经存在且已确认可信**的
`news-agent/writing-agent-service:native-e2e` 镜像时使用离线备用入口。先检查镜像 ID，
再把下面变量替换为核对过的完整 `sha256:...` ID；不要使用企业镜像或未知远程镜像补缺：

```bash
docker image inspect news-agent/writing-agent-service:native-e2e --format '{{.Id}}'
E2E_CACHE_IMAGE_ID=sha256:REPLACE_WITH_VERIFIED_IMAGE_ID
docker tag "$E2E_CACHE_IMAGE_ID" news-agent/e2e-dependency-cache:local
docker build --network none --pull=false \
  --build-arg E2E_DEPENDENCY_IMAGE=news-agent/e2e-dependency-cache:local \
  -f Dockerfile.e2e.cached -t news-agent/writing-agent-service:analysis-demo .
```

[`Dockerfile.e2e.cached`](../Dockerfile.e2e.cached) 只从缓存提取 `/usr/local` 依赖，使用固定
官方 Python 基底和当前工作区代码，不继承缓存 `/app`。构建会检查当前 `pyproject.toml`
依赖约束并运行 `pip check`；缓存缺失、版本不兼容或检查失败时停止，不绕过检查。备用
镜像构建成功后仍执行以下原启动命令，**不加 `--build`**，避免重新触发联网构建。

```bash
# 第一次启动：同时启动所需数据库、迁移、缓存、Temporal 等依赖。
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml up -d api hot-news-worker
curl -fsS http://127.0.0.1:28010/ready
```

若**这个独立演示项目**的依赖已经运行、数据库迁移也已完成，仅用以下命令更新其 API
和热点 Worker。它不指向 `28000` 的旧项目：

```bash
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml up -d --no-deps api hot-news-worker
```

新镜像为 `news-agent/writing-agent-service:analysis-demo`，新项目的数据卷由
`news-agent-analysis-demo` 命名空间隔离，重复启动继续使用本项目的卷。业务 API 在
`28010`，PostgreSQL 为 `25442`，Temporal 为 `27243`，MinIO 为 `29100/29101`，不会
占用旧演示端口。Overlay 让 API 加入外部网络
`newsagent-analysis-service_analysis-ingress`，通过固定地址
`http://analysis-gateway:8080/v1/analyze` 调用独立服务；因此独立服务必须先启动。
这是 `e2e` 的明确 HTTP 例外，仍要求远程结果声明 Linux OS 资源限制，不是生产 TLS
策略。生产仍要求 HTTPS、独立秘密管理和实际网络治理。

演示相关配置链是：`ENVIRONMENT=e2e`、`LOCAL_SIMULATION_ENABLED`、
`CONVERSATION_ENABLED`、`CONVERSATION_HOT_NEWS_QUERY_ENABLED`、
`CONVERSATION_DATA_ANALYSIS_ENABLED` → `DATA_ANALYSIS_BACKEND=service` →
固定 `DATA_ANALYSIS_SERVICE_URL` → 与独立服务一致的 `DATA_ANALYSIS_SERVICE_TOKEN`。
Overlay 将客户端总预算设为 `DATA_ANALYSIS_TIMEOUT_SECONDS=6`，显式允许本地 HTTP，
并保留 `DATA_ANALYSIS_SERVICE_REQUIRE_OS_LIMITS=true`。输入／输出、并发、内存和行数仍
沿用现有批准预算；失败不会回退宿主进程或复用旧分析。

## 3. 在聊天页面演示

另开终端，从项目根目录运行：

```bash
cd agent/frontend
npm ci
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28010 \
VITE_DEV_PORT=5175 VITE_LOCAL_SIMULATION=1 npm run dev
```

首次安装或锁文件变更才需要 `npm ci`。开发服务器读取
[`dev-server/local.yml`](../frontend/dev-server/local.yml) 中的公开本地代理身份；本次只通过
显式 shell 参数覆盖新端口和 API 地址，**不修改 YAML**，不影响已有 `5174` 页面。
无需粘贴旧 `.env` 的本地配置，企业凭证也不能填进这份 YAML。身份只由 Node/Vite 代理
注入，不由浏览器业务代码构造。

打开 [本地聊天页面](http://127.0.0.1:5175/chat)，按以下顺序操作：

1. 在“数据分析”区域点击“准备两窗口演示”。这一按钮依次创建或复用两个热点运行，
   等待原有 Workflow 完成，自动创建独立演示会话，再真实读取参考窗口和当前窗口。
   完成后页面切换到新会话；准备中可点击“停止准备”，但已开始的 Workflow 或已保存
   记录仍可能保留，不能把取消理解成服务端回滚。
2. 核对新会话中的两条读取答复与已完成的 `read_hot_news` 工具轨迹。它们将来源纳入
   本会话的授权范围，不能只把 UUID 当作已经读取的证据。窗口和实际完整排行数量以
   读取结果／分析来源卡为准；需要判断是否复用时，对照历史运行 ID／Workflow ID，
   接口没有独立的 `reused` 标记。
3. 点击六种操作按钮之一。按钮只将问题填入输入草稿；确认草稿后点击“发送”。
   每次发送进入原有结构化计划和分析工具链，不是前端直接计算权威数字。
4. 查看结果卡，再展开“计算证据与资源审计”和工具调用记录。核对操作、指标、范围、
   Bundle／算法版本、源快照 SHA-256、执行输入 SHA-256、执行限额和耗时。
5. 依次完成六种操作，最后刷新页面，确认已提交的会话和工具结果可从 PostgreSQL 恢复。

默认演示应使用本地模型、获准的新查询和已启用的数据分析工具。缺少这些条件时不要用
普通聊天、其他租户或企业接口替代这次演示；页面与 CLI 会明确提示未满足准备条件。
点击准备是有副作用的管理员操作，会创建或复用 SQL／运行记录与会话；六个分析操作只
读取已经保存的聚合快照。

六个按钮填入的草稿如下。页面不自动发送；如需复现默认口径，保留草稿原文逐条发送：

| 按钮 | 填入的问题 |
| --- | --- |
| 指标概况 | 统计这批热点的指标概况 |
| 指标分布 | 分析这批热点点击率的分布、中位数和P90 |
| 高低对比 | 比较这批热点曝光最高与最低的新闻 |
| 数据质量 | 对这批热点做数据质量检查 |
| 已存基线 | 比较这批热点点击量与已保存基线 |
| 两窗口趋势 | 比较已读取的两个窗口的点击率趋势 |

## 4. 六种结果应怎样读

| 操作 | 预期观察 | 不可推断的内容 |
| --- | --- | --- |
| 概况 `overview` | 当前完整榜单的曝光、点击等加法指标总计，以及总点击／总曝光的加权 CTR | 全站总量、跨新闻去重 UV |
| 分布 `distribution` | 指标的 count、min、max、mean、median、P90；P90 使用最近秩法 | 未上榜候选或全量新闻的分布 |
| 比较 `compare` | 同一榜单最高与最低新闻及差值，零分母的相对变化为未知 | 跨时间趋势或因果 |
| 质量 `quality` | 零曝光、CTR 不一致、点击高于曝光的口径核对提示 | 仅靠这些提示认定业务采集错误 |
| 基线 `baseline` | 逐新闻当前值、已保存参考值、差值、样本量与参考版本 | 把合成参考样本量当作真实历史窗口数 |
| 趋势 `trend` | 两窗口共同新闻的变化、加权 CTR／加法指标／热度均值以及新增／退出数量 | 把两份榜单总计变化当成全量增长或连续走势 |

两个完整榜单共同计入行数预算。趋势要求等长且非重叠窗口、同 Bundle／Workflow、相同
非空选择指纹及共同新闻类型一致；只在新闻交集上聚合。新增或退出榜单的新闻不参与变化。
零曝光 CTR、零参考分母和缺失参考保留未知，不能补零。UV 永不跨新闻或窗口求和。

合成 SQL 基线的 UV 和时长未提供，保持 `null`；热度分数也没有保存的基线值。
如果选择这些缺失参考的指标，应观察“无法计算／缺失指标”，而不是伪造数值。执行记录中
`backend=process`、`transport=service` 表示专用服务内部启动固定进程，不是业务 API
回退本地执行。哈希用于一致性追溯，不等同于生产签名式审计。

## 5. CLI 与失败定位

不启动浏览器也可运行同一演示：

```bash
cd agent
python -m examples.data_analysis_demo
# 准备窗口、新建会话并读取两个来源；不执行六种分析。
python -m examples.data_analysis_demo --prepare-only
```

默认 CLI 使用本机 `http://127.0.0.1:28010` 和公开本地身份，先检查 runtime，再走真实
HTTP 热点准备／运行详情／新会话／读取／工具链。它不访问企业端点，不读取 `.env` 或
模型秘密。`--prepare-only` 仍创建会话和两条真实读取记录，只跳过后续六次分析发送；
返回 `status=prepared`、两份 run ID／窗口／上榜数、会话 ID、来源 turn ID 和选择指纹。
完整模式返回 `status=verified` 与六个工具结果的来源和执行记录，并重算固定确定性结果
核验服务端输出完整性；这个复核不会代替 Agent 的 HTTP 执行或保存结果。

可用返回的 `conversation_id` 打开
`http://127.0.0.1:5175/chat?conversation=<conversation_id>`，检查实际持久化的读取／分析
记录。CLI 仅接受带显式端口的 HTTP 回环 IP 地址，不接受企业地址、代理环境或重定向。

重复准备相同窗口和冻结查询范围应复用既有运行；新演示会话仍按实际创建结果展示。如果
已有 22／23 点运行绑定了不同范围，可能返回 **409**。不要覆盖旧运行、清空数据库或
自动改用不同场景／指标／Bundle 来“跑通”；先进行只读定位：

- 在 `/hot-news` 的历史详情按返回或已知 `run_id` 检查窗口、Bundle、SQL query_id、
  场景、排序、row_limit、筛选参数和工具轨迹。
- 在 `/chat` 展开失败的准备／读取／分析工具记录，核对固定错误码与实际来源；未经成功
  读取的 run 不会获得本会话的分析授权。
- 只查看 `docker compose ... ps`、`/ready` 和带鉴权的分析 `/health`；不要打印完整
  环境或企业凭证。保留历史证据后再由管理员决定后续运行策略。

缺失或未知 SQL 范围、不同 Bundle／Workflow／筛选范围、重叠或不等长窗口、没有共同
新闻、身份／哈希漂移、榜单不完整、超量、独立服务不可达或鉴权失败都应明确失败。
结果不会通过截断榜单、补造数据、忽略范围或切回其他后端替代。发送断线时先刷新确认
同一 UUID 的终态，再使用页面的“重试原请求”；不要不断创建变体请求。

## 6. 完整调用链、验证与 TODO

```text
前端 /chat 演示准备 或 examples.data_analysis_demo
→ Vite 本地代理 / CLI 公开本地管理员身份
→ 本地准备入口 → execute_local_hot_news_query
→ SqlAssistant 受限意图、compile_query、SQL Guard、冻结格式与 run 原子绑定
→ Temporal HotNews Workflow / Worker
→ PostgreSQL 只读候选 + 同小时 UV/时长/保存基线补充
→ NewsMetricSnapshot → HotNewsRanker 权威热度 → 内容与知识检索
→ 原生模型 Port → Schema/Validator → analysis_runs 不可变快照
→ 新会话真实 read_hot_news 参考／当前 → 成功 ToolTrace 授权来源
→ 六按钮填草稿 → 用户发送 → ConversationAgentService / 原生结构化计划
→ analyze_hot_news_data → project_dataset 数值白名单和范围核对
→ RemoteAnalysisRunner → 固定 analysis-gateway → 独立分析服务
→ 固定 worker → engine.analyze → 全版本完整结果再次确定性复核
→ Python 答复渲染 → PostgreSQL 终态／ToolTrace → SSE → 结果卡与执行审计
```

业务依赖包括 Python 3.11、FastAPI、Temporal、PostgreSQL、Redis、MinIO 和现有模型／
知识 Port；前端依赖 Node、Vue、TypeScript、Vite 和 `js-yaml`。独立服务依赖 Docker、
固定 Nginx 网关和锁定分析镜像；worker 计算仍只用标准库。`news/` 负责采集，但此次
固定合成数据演示不需要临时抓取外部新闻。

验证入口如下；这是可执行检查清单，实际通过数由本次运行记录提供，不能把命令写成已
执行结果：

```bash
cd agent
python -m pytest tests/test_data_analysis_engine.py tests/test_data_analysis_comparison.py \
  tests/test_data_analysis_output_integrity.py tests/test_conversation_analysis_comparison.py \
  tests/test_data_analysis_remote.py tests/test_data_analysis_service.py \
  tests/test_data_analysis_demo.py -q
python -m pytest -q
cd frontend
npm test
npm run lint
npm run build
```

`tests/test_data_analysis_demo.py` 默认用隔离 HTTP fixture 验证准备、真实来源读取和六种
工具输出；真实本地 HTTP 测试默认跳过。新栈已启动时，可显式运行它：

```bash
cd agent
NEWSAGENT_DATA_ANALYSIS_DEMO_BASE_URL=http://127.0.0.1:28010 \
python -m pytest tests/test_data_analysis_demo.py \
  -k actual_local_http_prepares_and_verifies_all_six_operations -o addopts='' -q
```

页面专项测试位于 `frontend/tests/analysis-demo.test.mjs`，通过 `npm test` 进入；浏览器端
按上述操作核对草稿、发送、来源和历史恢复。
专门的 Docker／独立服务验收须显式启动服务并注入测试 token，见
[分析服务说明](analysis-service.md)。[聚合数据合同检查](analysis-data-acceptance.md) 的
合成通过只表示本地合同通过，真实租户隔离仍为 `not_verified`。

剩余 TODO：获准企业数据 Adapter／IDL／watermark／UV／基线口径、独立二租户授权
fixture、真实模型工具规划质量、生产 TLS／Secret／CNI、容量与故障验收。生产发布和
模型／Prompt／规则晋升仍须既有人工审批；此演示不自动改变企业配置。

停用演示时可停止选定服务，保持数据：

```bash
cd agent
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.analysis-demo.yml stop api hot-news-worker
docker compose --env-file /dev/null \
  -f deploy/docker-compose.analysis-service.yml stop
```

不要使用 `down -v` 清空历史；若要删除独立分析网络，先处理仍连接该网络的 API，避免
把网络删除失败误认为业务数据需要重置。
