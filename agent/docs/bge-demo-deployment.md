# 独立 BGE 演示：从启动到聊天展示

本教程把当前代码部署到新的本地 Docker 项目。默认沿用已经配置的企业模型做查询理解；热点指标、SQL 校验和热度由 Python/SQL 计算，其余分析模型使用本地测试 Port。BGE 使用 `var/knowledge-snapshots/rag-recall-demo/` 中的真实离线模型和新闻索引。合成运营数据与本地模型不能作为企业模型质量验收。

全部脚本命令从仓库根目录 `/Users/shi/Project/myProject/NewsAgent` 执行。前端会构建为独立 Nginx 容器，不需要另外运行 `npm run dev`。

## 第 1 步：认识这次部署的对象

- **镜像**是代码与依赖的打包结果；修改 Python 或前端源码后，要重新构建镜像。
- **容器**是镜像启动后的进程；停止容器不会删除镜像或数据卷。
- **数据卷**保存数据库和报告；再次部署会复用本项目的卷，保留聊天和运行历史。

| 对象 | 本次独立部署 |
| --- | --- |
| Compose 项目 | `news-agent-bge-demo` |
| 业务镜像 | `news-agent/writing-agent-service:bge-demo` |
| 前端镜像 | `news-agent/frontend:bge-demo` |
| 聊天入口 | [http://127.0.0.1:5178/chat](http://127.0.0.1:5178/chat) |
| API 就绪检查 | [http://127.0.0.1:28060/ready](http://127.0.0.1:28060/ready) |
| 数据与网络 | 项目名前缀隔离的 PostgreSQL、Temporal 数据库、MinIO 数据卷和网络 |
| 新闻快照 | 原目录只读挂载；不复制到镜像，不写入或删除 |

仅 API 的 `28060` 和前端的 `5178` 绑定本机回环地址；数据库、Temporal 和 MinIO 不发布宿主端口。旧 `timeline-demo`、`enterprise-scale-demo` 的镜像标签、容器和卷不会被这套脚本覆盖。

合并后的配置包含 **14 个服务定义**：

| 分组 | 服务 |
| --- | --- |
| 持续运行的基础服务 | `postgres`、`temporal-postgres`、`redis`、`temporal`、`minio` |
| 一次性任务 | `minio-init` 初始化报告桶；`migrate` 执行数据库迁移 |
| 应用 | `api`、`hot-news-worker`、`writing-worker`、`outbox-worker`、`data-loop-worker`、`web` |
| 独立测试 profile | `test-runner`，默认部署不启动 |

## 第 2 步：准备环境，先预检

准备宿主 Python 3.9+、Docker Desktop 和 Docker Compose 2.24.4+。脚本只用标准库；镜像内应用使用 Python 3.11，前端也在镜像内构建，无需另外安装 Node/npm。脚本默认要求宿主磁盘至少空余 **10 GiB**、Docker 至少分配 **6 GiB** 内存；这些是启动预算，不是容量保证。第一次构建需要访问镜像仓库、Debian 和 Python/npm 包仓库，PyTorch 等依赖及 hnswlib 编译会花费时间。

仓库中需保留完整 `var/knowledge-snapshots/rag-recall-demo/`、本教程对应的部署文件，以及已经配置好的 `deploy/model-runtime.compatible.yml`。脚本只检查企业配置文件是否存在，不读取、输出或生成其内容；API 启动时由已有专用配置加载器读取连接和查询模型绑定。

```bash
cd /Users/shi/Project/myProject/NewsAgent
python3 agent/tools/deploy_bge_demo.py --check
```

预检核对文件、磁盘、有效 Docker 本地连接、服务端版本、Compose 版本、内存、端口和配置。它不构建镜像、不启动容器；`--check --skip-build` 也只检查镜像是否存在。脚本固定本地默认 builder，去掉远端 builder 的环境覆盖；Docker 默认 builder 与 Docker context 的关系见 [Docker 官方说明](https://docs.docker.com/build/builders/)。

默认模式需要 `NEWSAGENT_MODEL_API_KEY`。如果当前终端已注入该变量，脚本复用它；否则在交互终端中使用隐藏输入询问，不要求写入 `.env`。**预检中输入的密钥只存在于该次脚本进程；随后启动部署若没有环境变量，会再次询问。** 非交互终端缺少密钥会明确停止。

密钥不进入构建环境，也不写入仓库文件。兼容模型 overlay 只把它交给 API 的查询理解客户端；热点、写作和 Data Loop Worker 使用本地模型 Port。Compose 显式使用 `/dev/null`，不自动读取 `.env`。

2026-10-12 Docker 和磁盘恢复后，真实预检已通过，local 模式完整实例已启动并通过三条聊天问题验收；详情见 [本地模拟记录](local-bge-simulation.md)。当前默认企业模式仍需要用户在终端注入已有模型密钥，不能把 local 验收当作企业查询理解验收。脚本不会自动重启 Docker、清理镜像或删除数据卷。

## 第 3 步：一条命令部署

预检通过后运行：

```bash
python3 agent/tools/deploy_bge_demo.py
```

默认是 `compatible` 模式，保留已有企业查询理解配置。脚本按下面的阶段执行；某阶段失败会停止后续阶段，保留已经启动的本项目服务与数据。

| 阶段 | 实际动作与成功条件 |
| --- | --- |
| 1/6 预检 | 重复上述检查，确认模型密钥和配置可用 |
| 2/6 构建 | 构建业务与前端镜像；构建环境没有企业模型密钥，失败不启动基础服务 |
| 3/6 基础服务 | 启动基础容器，等待健康；`minio-init` 必须退出且状态码为 0 |
| 4/6 迁移 | 每次执行 `run --rm --no-deps migrate`，不复用旧的已退出迁移容器 |
| 5/6 应用 | 使用 `--no-deps --no-build --pull never` 启动 API、四个 Worker 与前端，避免再次触发依赖迁移 |
| 6/6 验证 | 检查 API `/ready`、前端 `/healthz`、前端代理的会话 runtime、全部应用运行状态，以及三个队列最近 90 秒的 Workflow/Activity poller |

API 和热点 Worker 都初始化并核对同一份数仓；PostgreSQL 事务锁避免并发重复写入。首次生成样本、全量指纹核验和加载 BGE 可能较慢，阶段输出会提示正在等待。

如果端口已被其他项目占用，可为本项目选择另一对端口：

```bash
python3 agent/tools/deploy_bge_demo.py --api-port 28061 --web-port 5179
```

后续状态命令也带相同端口参数，浏览器入口相应变为 `http://127.0.0.1:5179/chat`。本项目已经占用的原端口可复用，脚本不会将它当成其他实例冲突。

仅做不连接企业模型的规则演示时，明确选择：

```bash
python3 agent/tools/deploy_bge_demo.py --model local
```

此模式不需要企业配置或模型密钥；查询理解与分析均为本地测试 Port，不会在企业模型不可用时自动切换到它。

## 第 4 步：展示热点对话和相关新闻

看到“部署就绪”后，打开 [聊天页面](http://127.0.0.1:5178/chat)，新建对话，依次发送：

1. **`查询点击率最高的前5条视频新闻并分析原因`**：查询合成数仓、完成同一热点运行后显示榜单，并自动为返回结果的前 3 条新闻召回相关文章。
2. **`解释第一条新闻`**：读取同一运行的第一条新闻，并展示该新闻对应的相关文章。
3. **`检索第一条的相关新闻`**：按已授权源新闻标题显式检索，排除源新闻，显示检索结果卡片。

“相关新闻”面板位于已完成答复下方，最多 3 组，每组 3 篇；默认显示前 300 字，点击“展开片段”可看到最多 600 字。发布时间或来源链接缺失时显示“未知”“来源链接未提供”，不会补造链接或日期。检索失败会给出提示并保留热点结果；用户取消和整轮超时仍遵循原会话规则。

刷新页面应恢复已保存的卡片。旧历史没有相关文章字段时不会凭空生成卡片；需发送一个新问题。卡片在 SSE `done` 到达后展示，不在尚未保存的进度事件中展示。

这份 `timeline-v4` 数仓覆盖北京时间 **2026-09-10 00:00 至 2026-10-10 00:00，结束不含**，默认每租户 1,200 篇合成新闻。它不是今日新闻。BGE 快照只负责语义相关召回，不保证候选发布时间落在热点查询窗口内；合成标题与离线语料之间的语义匹配需实际核对。

“部署就绪”验证服务、已加载配置和 Worker 在线状态，**不会请求企业模型**。这三条实际聊天问题才会验证企业查询理解、SQL、Workflow、会话保存和卡片的整条链路；模型鉴权、网络或语义输出失败仍可能在此时出现。

## 第 5 步：查看状态、更新、恢复和定位

以下命令仍从仓库根目录执行：

```bash
# 查看本项目服务的公开状态，不打印容器环境变量
python3 agent/tools/deploy_bge_demo.py status

# 源码或依赖变更后重新构建并部署，复用本项目的数据卷
python3 agent/tools/deploy_bge_demo.py

# 仅在本项目镜像已构建且仍对应所需代码时复用镜像
python3 agent/tools/deploy_bge_demo.py --skip-build

# 停止本项目容器，保留镜像、卷和历史
python3 agent/tools/deploy_bge_demo.py stop
```

`--skip-build` 不会把修改过的源码装入旧镜像。部署时它先在隔离临时容器中核对 BGE 依赖版本，再启动服务；预检本身不会运行这个临时容器。停止或机器重启后，可用 `--skip-build` 恢复已构建的本项目；迁移仍重新执行。使用 `--model local` 部署的环境，恢复时也带上该参数，避免意外切换查询模型。

失败时先看脚本标出的阶段，再执行 `status`。需要查看业务日志时，在部署所用的同一本机 Docker context 下执行固定的三层配置；在本机查看日志，不粘贴模型密钥、完整展开配置或容器 `Config.Env`：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
docker compose --env-file /dev/null -p news-agent-bge-demo -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.bge-snapshot.yml -f deploy/docker-compose.bge-demo.yml logs --tail 100 api hot-news-worker
```

按问题把末尾服务名替换为 `postgres`、`temporal`、`minio-init`、`writing-worker`、`outbox-worker`、`data-loop-worker` 或 `web`。这里只读取同项目日志，不需要企业密钥 overlay，也不会重建服务。

| 现象 | 下一步 |
| --- | --- |
| 预检报空间不足 | 先释放空间再预检；不要用降低阈值代替容量确认 |
| Docker version/context 超时，或构建一直没有步骤 | 在 Desktop 确认引擎正常；修复后再运行，避免重复启动构建 |
| 构建报下载、依赖或编译错误 | 查看失败步骤；确认包仓库网络，首次 BGE 构建需要编译 hnswlib |
| 基础服务未健康或 `minio-init` 非零退出 | 查看对应服务日志；此时脚本尚未执行后续迁移和应用启动 |
| 迁移失败 | 检查迁移输出；修复后重跑脚本，不以旧迁移容器代替执行 |
| API/前端可访问，但 Worker 未就绪 | 检查相应 Worker 日志；`/ready` 本身不保证队列有 poller |
| 聊天时报模型鉴权、连接或理解失败 | 核对现有企业端点配置与当前密钥；脚本不会静默降级成本地模型 |
| 热点答复有结果，相关文章为空或超时 | 看卡片提示及 API 日志；核对快照、配套编码器、源标题和 5 秒召回预算 |

按 Ctrl+C 只取消当前脚本命令；已经启动的本项目容器和数据保留。随后用 `status` 确认，再选择修复后部署或 `stop`。脚本不提供清空数据卷操作。

## 代码调用链、配置和验证范围

部署入口是 [tools/deploy_bge_demo.py](../tools/deploy_bge_demo.py)：`main` 解析参数 → `BgeDemoDeployment.preflight` 核对文件、资源、本地连接、端口和密钥 → `compose_command` 组合基础、快照、演示 overlay，兼容模式在运行阶段另加 `timeline-query-model` → `_run` 执行有界、可取消、日志脱敏的子进程 → 构建/启动/迁移 → `_wait_for_infrastructure`、`_wait_until_ready` 和 `POLLER_PROBE` 验证 → 浏览器入口。

配置文件为 `docker-compose.bge-demo.yml`、`docker-compose.bge-snapshot.yml`、`knowledge-search.bge-snapshot.yml`、`bge-demo.nginx.conf`、`bge-demo.frontend.json` 和现有查询模型 overlay。前端使用 `frontend/Dockerfile.bge-demo` 与专用构建上下文白名单；不发送本地身份 YAML 或 `.env`。CLI 提供模型模式、API/前端端口、磁盘预算和是否跳过构建；数仓 profile/数量、项目和镜像标签由演示配置固定。

业务链：浏览器 `/chat` → Nginx 注入公开本地测试身份 → 会话 API/`ConversationAgentService` → 企业查询理解与 Python 条件校验 → 原有 Text2SQL/只读 PostgreSQL → Temporal 热点 Workflow → Python 权威指标与本地分析 Port → 已完成报告 → `ConversationTools`/`ConversationRelatedArticles` → 配套 BGE 编码与 HNSW 检索 → 去重、排源、片段裁剪 → PostgreSQL 工具检查点 → SSE `done.turn.tools`/历史 GET → `projectRelatedArticles`/`RelatedArticlesPanel`。自动片段不进入规划模型上下文，不改变热点指标或评分。

外部依赖为本地 Docker、镜像/包仓库、PostgreSQL、Redis、Temporal、MinIO、企业查询理解 HTTP 接口及本地 BGE 包；没有新增 Agent 框架。检索合同和剩余语料工作见 [BGE 快照说明](bge-snapshot-recall.md)。

部署离线测试入口（使用已安装项目 dev 依赖的 Python 环境）：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
python3 -m pytest -o addopts='' -q tests/test_bge_demo_deploy.py
```

前端卡片测试入口为 `agent/frontend/tests/related-articles.test.mjs`，类型检查与生产构建入口为前端目录下的 `npm run build`。本轮 **40 个部署离线测试通过**，覆盖编排、失败停止、预检无副作用、连接/端口隔离、密钥脱敏和真实 SDK probe 合同；含这些用例的相关回归 **70 项通过**，不累加成 110 项。专用前端 Docker 输入的隔离生产构建、宿主临时配置的 Nginx 语法检查也通过。这些测试不启动 Docker 服务、不调用企业模型。

已完成 local 模式真实镜像构建与全栈部署、三条聊天问题、相关文章页面展开/刷新、历史与幂等重放验收。尚未完成：三问的企业查询理解效果验收、生产容量与 BGE 超时后台线程治理、权威发布时间/来源补齐、增量索引和版本审计。当前运行实例为 local，企业模式需另行部署和验收。
