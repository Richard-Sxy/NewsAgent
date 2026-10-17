# 完整本地模拟：一键启动与展示

本入口固定使用 **local 模式**，查询理解、热点分析和写作模型均使用本地测试 Port；不需要企业 API Key，也不需要 `model-runtime.compatible.yml`。新闻指标来自确定性合成数仓，相关新闻使用 `var/knowledge-snapshots/rag-recall-demo/` 中真实的本地 BGE 模型与 HNSW 索引。

PostgreSQL、Redis、Temporal、MinIO、API、Worker 和前端在启动时都作为真实容器运行。规则模型用于验证项目调用链，不能作为企业模型质量验收。第一次构建仍需联网下载基础镜像、Python/npm 依赖并编译 hnswlib；新闻检索模型和索引使用已有本地包。

## 复制这段开始

先打开 Docker Desktop，确认引擎可用。准备宿主 Python 3.9+、Docker Compose 2.24.4+、完整仓库和 `var/knowledge-snapshots/rag-recall-demo/`；镜像内应用使用 Python 3.11。默认预检要求宿主磁盘空余至少 **10 GiB**、Docker 内存至少 **6 GiB**。

```bash
cd /Users/shi/Project/myProject/NewsAgent
python3 agent/tools/start_local_demo.py --check && python3 agent/tools/start_local_demo.py
```

前一个命令仅预检；通过后才执行后一个部署命令。没有模型密钥提示，也不读取 `.env`。脚本构建前端容器，所以不需要安装宿主 Node.js 或另外运行 `npm run dev`。

2026-10-12 已完成真实构建和启动验收。API/前端健康，五个基础服务健康，四个 Worker 运行，三个 Temporal 队列的 Workflow/Activity poller 均通过检查。数据库已迁移到 `20261010_0022`，数据卷与已有实例隔离。构建开始时磁盘空余约 **29 GiB**，部署后约 **22 GiB**。

BGE 镜像固定安装 `torch==2.8.0+cpu`，使用 [PyTorch 官方 CPU 仓库](https://download.pytorch.org/whl/cpu/torch/)，避免下载 CUDA。前端使用 Docker 官方 Node 的 ECR Public 分发镜像和 [Nginx 官方 GHCR 分发镜像](https://github.com/nginx/docker-nginx-unprivileged#supported-image-registries-and-platforms)，均固定多架构摘要。本机 Docker Hub 镜像代理返回 403 时，不需要修改全局代理即可构建此前端。

## 启动后访问哪里

| 用途 | 地址 |
| --- | --- |
| 热点对话与相关新闻卡片 | [http://127.0.0.1:5178/chat](http://127.0.0.1:5178/chat) |
| 同一运行的热点工作台 | [http://127.0.0.1:5178/hot-news](http://127.0.0.1:5178/hot-news) |
| 写作任务与人工审核 | [http://127.0.0.1:5178/jobs](http://127.0.0.1:5178/jobs) |
| API 就绪检查 | [http://127.0.0.1:28060/ready](http://127.0.0.1:28060/ready) |

这是独立项目 `news-agent-bge-demo`，业务和前端镜像分别为 `news-agent/writing-agent-service:bge-demo`、`news-agent/frontend:bge-demo`。只发布本机回环地址的 `28060`、`5178`；数据库、Temporal 和 MinIO 不发布宿主端口。项目网络与数据卷独立，旧 timeline/扩量项目保留；同一 BGE 项目再次启动会复用已有数据与聊天历史。

端口被其他项目占用时，可明确改用另一对端口：

```bash
python3 agent/tools/start_local_demo.py --api-port 28061 --web-port 5179
```

随后使用 `5179/chat`；状态命令也带相同端口参数。这个入口固定 local，不用添加 `--model`。

## 脚本具体启动了什么

合并配置共有 **14 个服务定义**，其中 `test-runner` 属于独立 `test` profile，不随正常部署启动：

- 基础服务：`postgres`、`temporal-postgres`、`redis`、`temporal`、`minio`。
- 一次性任务：`minio-init` 初始化报告桶，`migrate` 升级本项目数据库。
- 应用：`api`、`hot-news-worker`、`writing-worker`、`outbox-worker`、`data-loop-worker`、`web`。
- 独立测试服务：`test-runner`。

| 终端阶段 | 成功条件 |
| --- | --- |
| 1/6 预检 | 文件、本地 Docker 连接、版本、磁盘、内存、端口和配置有效；不构建或启动容器 |
| 2/6 构建 | 业务镜像包含 BGE 依赖，前端完成类型检查和静态构建；失败即停止后续启动 |
| 3/6 基础服务 | 基础容器健康，`minio-init` 已退出且状态码为 0 |
| 4/6 迁移 | 每次新执行 `run --rm --no-deps migrate`，不复用旧的退出容器 |
| 5/6 应用 | `--no-deps --no-build --pull never` 启动六个应用服务，避免重复触发依赖迁移 |
| 6/6 验证 | API `/ready`、前端 `/healthz`、本地模型 runtime、全部应用运行状态，以及三个队列最近 90 秒的 Workflow/Activity poller |

API 和热点 Worker 使用同一 profile、数量与数据库。初始化由 PostgreSQL 事务锁串行保护，并核对完整数据指纹；首次生成数仓、重复核验和加载 BGE 可能较慢。应用启动命令允许最多 600 秒，API 健康探针有 300 秒初始化宽限，随后另有最多 600 秒的全部依赖就绪检查。

`/ready` 只检查 PostgreSQL、Redis、Temporal 连接；脚本还检查 Worker 的实际 poller。Data Loop Worker 在线不代表已经执行完整评测或批准策略，写作任务仍须人工审核资料、提纲和终稿，不会自动发布到外部 CMS。

## 在聊天里展示三条问题

看到终端“部署就绪”后，新建对话，依次发送：

1. **`查询点击率最高的前5条视频新闻并分析原因`**：执行原有热点查询，返回同一运行的榜单和分析，并自动为前 3 条源新闻召回相关文章。
2. **`解释第一条新闻`**：读取同一运行第一条新闻，展示该条对应的相关文章。
3. **`检索第一条的相关新闻`**：按已授权标题检索，排除源新闻，返回相关片段。

答复下的“相关新闻”面板最多显示 3 组、每组 3 篇。默认显示前 300 字，“展开片段”展示最多 600 字；缺失的发布时间和链接显示“未知”“来源链接未提供”。卡片在工具检查点与答复保存后，经 SSE `done` 展示；刷新页面恢复已保存卡片，旧历史无此字段时需发送新问题。

样本 profile 为 `timeline-v4`，每租户 1,200 篇合成新闻，覆盖北京时间 **2026-09-10 00:00 至 2026-10-10 00:00，结束不含**。请用样本问题展示，不能把结果说成今日真实热点。BGE 是离线语义召回，不保证候选发布时间匹配热点窗口，合成标题与真实离线语料的匹配质量也需核对。

## 状态、恢复、更新和停止

以下命令从仓库根目录执行：

```bash
# 只查看本项目公开状态
python3 agent/tools/start_local_demo.py status

# 已构建的本项目镜像：停止或重启机器后恢复
python3 agent/tools/start_local_demo.py --skip-build

# 源码或依赖改变后：重新构建并部署
python3 agent/tools/start_local_demo.py

# 停止本项目，保留镜像、数据卷和历史
python3 agent/tools/start_local_demo.py stop
```

`--skip-build` 会先验证已有 BGE 依赖，再启动服务；它不会把新源码装入旧镜像。`--check --skip-build` 只检查镜像存在，不运行依赖探测容器。恢复和更新仍重新执行迁移。

阶段失败或按 Ctrl+C 后，已经启动的本项目服务和数据保留。先用 `status` 确认状态，按失败阶段修复后重跑；脚本不会重启 Docker、清理镜像或删除数据卷。

在同一本机 Docker context 下查看对应日志：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
docker compose --env-file /dev/null -p news-agent-bge-demo -f deploy/docker-compose.native-e2e.yml -f deploy/docker-compose.bge-snapshot.yml -f deploy/docker-compose.bge-demo.yml logs --tail 100 api hot-news-worker
```

可将服务名换成 `web`、`postgres`、`temporal`、`minio-init` 或其他 Worker。业务日志在本机查看，不输出容器环境或完整展开配置。

| 问题 | 定位 |
| --- | --- |
| Docker 连接超时，构建停在 0/0 | 先在 Desktop 恢复引擎，再执行预检，避免重复启动构建 |
| 磁盘或内存不足 | 满足预检预算后重跑，不靠降低阈值代替容量确认 |
| 首次构建下载/编译失败 | 检查失败步骤的网络或包错误；BGE 依赖首次安装需要下载与编译 |
| 基础服务、初始化或迁移失败 | 查看该服务日志；后续应用尚未启动，不以旧迁移容器代替执行 |
| 页面可访问但 Worker 未就绪 | 检查相应 Worker 的注册/连接日志；单独 `/ready` 不能代替 poller 验证 |
| 热点有结果，相关文章为空或超时 | 查看卡片提示、源标题、快照配置与 5 秒召回预算；热点结果保持原值 |

## 调用链、配置与测试

部署链：[tools/start_local_demo.py](../tools/start_local_demo.py) 固定 local 并转发参数 → [tools/deploy_bge_demo.py](../tools/deploy_bge_demo.py) 的 `main`/`BgeDemoDeployment` → `preflight`/`compose_command`/`_run` → Docker 构建 → 基础服务与初始化 → 独立迁移 → API、Worker、Nginx → `_wait_until_ready`/`POLLER_PROBE` → 浏览器入口。

业务链：`/chat` → Nginx 注入公开模拟身份 → 会话 API/`ConversationAgentService` → local 查询理解、Python 条件与 SQL 校验 → 只读 PostgreSQL/Temporal 热点 Workflow → Python 权威指标、本地分析 Port → `ConversationTools`/`ConversationRelatedArticles` → 配套 BGE 编码与 HNSW 检索 → 排源、去重、裁剪 → PostgreSQL 工具检查点 → SSE `done.turn.tools`/历史 GET → `projectRelatedArticles`/`RelatedArticlesPanel`。检索片段作为不可信证据展示，不改变指标或系统权限。

核心配置为三层 Compose、`model-runtime.local.yml`、`text2sql-scenes.timeline-v4.yml`、`knowledge-search.bge-snapshot.yml`、`bge-demo.nginx.conf` 和 `bge-demo.frontend.json`。项目/profile/数量和镜像标签固定；端口、磁盘预算和是否跳过构建由 CLI 参数控制。本地模型不要求企业配置、密钥或接口；外部依赖仅为 Docker、首次构建所需镜像/包仓库及上述本地中间件与 BGE 包。

启动封装测试为 `agent/tests/test_start_local_demo.py`，部署合同测试为 `agent/tests/test_bge_demo_deploy.py`，前端卡片测试为 `agent/frontend/tests/related-articles.test.mjs`。在已安装项目 dev 依赖的 Python 环境中执行：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
python3 -m pytest -o addopts='' -q tests/test_start_local_demo.py tests/test_bge_demo_deploy.py
```

本地入口与部署合同共 **58 项测试通过**，包括固定 local、拒绝企业模型覆盖、无需企业配置/输入密钥、移除继承的模型密钥、完整编排顺序与失败停止。实际构建的 CPU 镜像中，`test_bge_snapshot_index.py`、`test_bge_snapshot_wiring.py`、`test_conversation_article_delivery.py` 另有 **50 项测试通过**。调用链和检索限制另见 [BGE 快照说明](bge-snapshot-recall.md)。

2026-10-12 真实 HTTP/SSE 与浏览器验收：

- 第一个问题耗时约 6.4 秒，返回 5 条视频新闻、同一 SQL/热点运行与分析，为前 3 条源新闻各召回 3 篇相关文章。
- 第二个问题约 0.3 秒，读取同一运行第一条新闻，带 3 篇相关文章；第三个问题约 0.2 秒，按同一源标题检索 3 篇并排除源 ID。
- 检索版本为 `bge-large-zh-v1.5:e68c268dadcf434d`；检查源运行/源 ID、去重与 600 字边界通过。历史 GET 与终态相同；相同 request_id 重放返回原轮次，不重复执行查询。
- 浏览器实际显示三轮答复和三个相关新闻面板；展开/收起可用，刷新后仍从服务端恢复全部内容。
- 验收会话为 `04b707f1-6c56-4fd3-85ac-8230eb018287`，热点运行为 `acd6ff81-956f-4bda-ba4d-f912678e0539`。打开 [本次验收会话](http://127.0.0.1:5178/chat?conversation=04b707f1-6c56-4fd3-85ac-8230eb018287) 可继续展示；换端口或使用全新数据卷后需重新运行示例。

本轮修复了失效 Docker Hub 代理导致的前端构建 403、PyTorch 默认选择 CUDA 包以及首次 BGE/数仓初始化超过原 120 秒等待的问题。修复后已使用固定 CPU 依赖、官方前端分发镜像与新的启动预算完成部署。

TODO：进一步评测真实业务中的 BGE 召回质量；补齐发布时间/来源、增量索引、版本审计与高并发取消治理。当前快照中有缺失标题和短片段，页面按实际数据展示；不从正文猜测结构化元数据。写作和 Data Loop 的完整人工审批/评测链及企业模型/数仓效果仍须单独验收。
