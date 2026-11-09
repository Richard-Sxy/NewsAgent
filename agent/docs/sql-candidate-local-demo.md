# SQL Candidate 本地全链路演示

本入口用于验证 NewsAgent 的聊天/热点查询、SQL 候选生成、只读查询、Temporal 热点分析与页面展示是否连接到同一次运行。数仓行为、基线和模型均为隔离合成/确定性模拟；DB-GPT 只裁剪复用了固定版本的 Prompt 模板，没有安装或启动 DB-GPT 框架。不能据此报告企业模型准确率、生产容量或真实运营效果。

## 1. 环境与隔离范围

- 在 Linux/WSL 中从当前仓库运行，主目录为 `/home/shi/project/NewsAgent`。不要从 Windows UNC 路径调用 Docker build；该工作区包含 Linux 符号链接，应由 WSL 发送构建上下文。
- 需要可用的本地 Docker Engine、Docker Compose >=2.24.4、Python 3，以及 Linux Node >=20.19。
- 本机可用 Node 为 `/home/shi/.nvm/versions/node/v22.22.0/bin/node`；其他电脑可通过 PATH 或 `--node` 指定。
- 固定 Compose 项目为 `news-agent-sql-candidate-demo`，不会使用已有其他演示项目的数据卷。
- API 仅绑定 `127.0.0.1:12800`，前端仅绑定 `127.0.0.1:12880`。PostgreSQL、Redis、Temporal 和 MinIO 仅在该项目网络内使用，不发布依赖端口。
- 所有 Compose 命令显式使用 `--env-file /dev/null`。前端使用 `build:isolated`，不读取 dotenv 文件；脚本也不加载 `.env` 或企业密钥。
- 前端 Nginx 注入公开合成租户/用户/令牌。这是本地演示网关，不是生产鉴权方案。

启动脚本仅使用 Python 标准库，不要求安装应用 Python 依赖。首次缺少前端依赖时会执行 `npm ci`，构建镜像/拉取依赖需要网络；已有依赖与镜像可使用 `--skip-build`。

## 2. 启动、检查和停止

在仓库根目录执行：

```bash
python3 agent/tools/start_sql_candidate_demo.py start
```

指定本机 Node：

```bash
python3 agent/tools/start_sql_candidate_demo.py start \
  --node /home/shi/.nvm/versions/node/v22.22.0/bin/node
```

复用已经构建、核对过的镜像和前端：

```bash
python3 agent/tools/start_sql_candidate_demo.py start --skip-build
```

`--skip-build` 只验证镜像及 `dist/index.html` 存在，不证明它们对应最新源码。代码或模板变化后应重新构建。默认就绪等待为300秒，可通过 `--timeout 600` 延长；构建时间不计入该等待值。

```bash
python3 agent/tools/start_sql_candidate_demo.py status
python3 agent/tools/start_sql_candidate_demo.py logs
python3 agent/tools/start_sql_candidate_demo.py stop
```

`stop` 使用 Compose stop，保留容器和卷。脚本没有删除数据卷的命令。启动超时也保留现场，不自动清理或重试部署。

服务就绪后打开：

- 业务入口（持续对话）：`http://127.0.0.1:12880/chat`
- 热点结果查看：`http://127.0.0.1:12880/hot-news`
- API 就绪：`http://127.0.0.1:12800/ready`

脚本成功表示 API、Web 与 `hot-news`、`news-writing`、`hot-news-data-loop` 三条队列的 Workflow/Activity poller 已就绪。它不自动提交业务请求；业务全链路需要发送下节示例并核对终态。

## 3. 手动验证一条完整运行

请从 `/chat` 输入当前端到端测试 `test_sql_candidate_e2e.py` 使用的问题：

> 查询点击率最高的前5条视频新闻并分析原因

该问题不需要人工猜测样本日期。服务端依据已配置的冻结样本范围确认窗口；实际选择的起止时间以响应 `query_resolution` 与 SQL 绑定参数为准。上面的测试问题是本轮验收输入，实际通过状态见第6节记录，不能把用例存在当作已通过。

此处 Nginx 提供的是 production build 的静态前端，`/hot-news` 用于查看结果，不显示仅开发模式开放的本地启动工具区。需要直接指定日期/小时的 HTTP 验收，由上述 E2E 用例先读取 `/api/v1/local-simulation/hot-news/sql-config` 的数据覆盖，再提交完整单小时；不要要求用户在静态热点页寻找不存在的启动表单。

本地模型是有限规则模拟。界面若提示表达或范围不支持，应保留错误证据，不将失败结果改写成真实模型质量结论。不要使用“今天”替换冻结样本日期。

检查同一次运行中：

1. 确认的指标为 `ctr`、类型为视频、数量为5；确认后的完整单小时窗口贯穿同一次运行。
2. 预览的 `generation.mode` 为 `dbgpt_candidate`，`generation.status` 为 `validated`；元数据中的模型供应商为 `local`，请求编号带 `native-local-sql-candidate-v1-`。
3. SQL 只引用批准视图与服务端占位符；SQL 文本与原生编译器仅格式不同。
4. SQL `query_id` 与热点 `run_id/workflow_id` 绑定，最终运行完成后能查看候选、Python 排名及分析。
5. 重复相同请求应复用已绑定运行和冻结 SQL，不重新生成另一个候选。重试不代表新增业务运行。

最终热度由已有 Python 公式计算；SQL `hot_score` 仍只用于候选召回。小时 UV 不跨小时相加。无数据、查询执行失败和候选生成失败必须保持不同状态。

## 4. 配置与完整调用链

部署入口：

```text
agent/tools/start_sql_candidate_demo.py
→ 验证本地 Docker / Compose 文件 / Node
→ npm run build:isolated → Vue 类型检查 / Vite → agent/frontend/dist
→ docker compose build migrate → 独立 sql-candidate-demo Python 镜像
→ native-e2e + sql-candidate-demo Compose overlay
→ migrate / API / Worker / Nginx 与隔离依赖
→ /ready、/healthz 与 Temporal poller 检查
```

业务入口：

```text
/chat（静态站业务入口；/hot-news 查看已完成结果）
→ Nginx 公共合成身份 → 既有热点/会话 API
→ QueryUnderstanding 与 Python 时间/范围/意图复核
→ SqlAssistantService.preview
→ compile_query 生成基准 + DbgptTemplateCandidateGenerator
→ NativeStructuredAgentClient → InferencePort
→ LocalHotNewsInference 的 sql_candidate 场景
→ 固定目录与槽位校验 → 确定性候选 SQL / unsupported
→ 严格 JSON → SqlAssistantGuard → 窄 AST 语义一致性校验
→ 预览/版本/摘要保存 → plan_once 原子运行绑定
→ Temporal Activity → execute_for_hot_news 冻结计划复核
→ PostgreSQL 只读查询与必要指标补充
→ NewsMetricSnapshot → Python 权威热度 → 本地热点分析
→ 同一运行的报告、API/SSE和页面
```

关键配置：

| 配置文件/项目 | 用途 |
| --- | --- |
| `deploy/docker-compose.sql-candidate-demo.yml` | 独立项目、镜像、端口和配置挂载 |
| `deploy/sql-candidate.local.yml` | 演示固定 `dbgpt_candidate`；总预算10秒、输出2048 tokens、一次尝试、零修复、禁止静默原生降级 |
| `SQL_CANDIDATE_CONFIG_PATH` | API 与热点 Worker 读取同一候选配置 |
| `deploy/model-runtime.local.yml` | 原有本地模型/Embedding Port；不接企业端点 |
| `deploy/text2sql-scenes.local.yml` | 冻结数仓版本、允许指标/栏目、行数、执行时限 |
| `deploy/sql-candidate.frontend.json` | 前端运行时设置；API 使用同源 Nginx 代理 |
| `agent/app/sql_assistant/assets/` | DB-GPT 模板来源/许可、候选 Prompt、批准目录与显式空示例资产 |

`dbgpt_shadow` 模式只生成并比较候选，实际执行原生 SQL，不执行第二条候选查询。`native` 模式不调用候选生成器。正式环境的默认路由保持 native；上述候选默认仅属于这套隔离演示配置。

配置切回 native 只影响新运行。已经 claim 的候选运行继续读取原快照；需要保留匹配的校验器/编译器资产。不能通过切开关在途更换 SQL，也不需要破坏性数据库降级。修改本地配置后重新启动 API/热点 Worker，不能仅刷新浏览器。

## 5. 不使用启动脚本时的等价命令

以下命令均在仓库根目录运行。前端须先准备兼容 Node/npm；首次使用可在 `agent/frontend` 目录执行 `npm ci`。

```bash
npm --prefix agent/frontend run build:isolated

docker compose --env-file /dev/null --project-name news-agent-sql-candidate-demo \
  --file deploy/docker-compose.native-e2e.yml \
  --file deploy/docker-compose.sql-candidate-demo.yml build migrate

docker compose --env-file /dev/null --project-name news-agent-sql-candidate-demo \
  --file deploy/docker-compose.native-e2e.yml \
  --file deploy/docker-compose.sql-candidate-demo.yml \
  up -d --no-build api hot-news-worker writing-worker outbox-worker data-loop-worker web
```

查询状态或日志时沿用同一组 `--env-file`、项目名和两个 Compose 文件，再追加 `ps --all` 或 `logs --tail 80 api hot-news-worker web`。不要只使用原生基础 Compose 文件，否则会选择其他项目或端口。

## 6. 测试入口与尚未完成项

候选专项测试包括：

```text
agent/tests/test_sql_candidate.py
agent/tests/test_local_sql_candidate_inference.py
agent/tests/test_sql_candidate_service.py
agent/tests/test_sql_candidate_binding.py
agent/tests/test_sql_candidate_binding_postgres.py
agent/tests/test_sql_candidate_token_budget.py
agent/tests/test_sql_candidate_e2e.py
agent/tests/test_sql_candidate_local_flow.py
agent/tests/test_model_runtime_core.py
agent/tests/test_model_runtime_config_and_http.py
```

使用已有应用 Python 环境，在 `agent/` 下运行测试；执行前应按项目评测规范关闭 `Settings` 与 `VideoIngestSettings` 的 dotenv 自动加载。测试不得读取企业配置或调用真实模型。启动工具可独立执行 `python3 agent/tools/start_sql_candidate_demo.py --help`；脚本自身仅使用标准库。

### 2026-11-07 实际验收

- 新建独立 PostgreSQL/Temporal/MinIO 卷，迁移成功；API、Web、Redis、数据库、Temporal 健康，三条队列的 Workflow 与 Activity poller 均就绪。
- 前端 372 项测试全部通过；Vue/TypeScript 类型检查和 `build:isolated` 构建通过。
- 镜像内后端 36 模块：925 passed、5 skipped、9 deselected；跳过/排除项为需要显式基础设施地址的测试，不计为通过。
- 安全评测 runner 依赖源码/Git 目录布局，未在镜像内正式运行；在源码虚拟环境另跑 27 项全部通过。其与日期模块合跑的46项包含19项重复验证，不重复累计。
- 真实 HTTP/SSE：新增候选4项及旧会话/知识/记忆/写作意图/SQL/热点28项，共32项通过。验证同一 run/query 绑定、实际只读查询、Temporal终态、分析报告、重放与权限拒绝。
- Edge 独立无头浏览器：聊天查数 → 5条视频 → 同运行热点及候选 SQL → 刷新历史 → 解释第一条新闻，全部通过；0 页面异常、0 HTTP 错误，新闻和指标快照未漂移。
- 另通过聊天调用 `analyze_hot_news_data`，实际隔离进程返回该榜单5条CTR分布。记录的一次129毫秒耗时仅为本机样例，不是性能基准。
- 最终镜像重新启动后，原 SQL、生成摘要、run/query 和两轮聊天历史保持不变；新提交的体育互动榜仍由 Worker 成功完成2条取数与分析。运行容器中的应用及修正后测试文件 SHA-256 与交付源码一致。
- 旧断言已校正：拒绝文案仍逐项验证人工审批边界；SQL大小写不影响语义；未批准的全天查询保持完整日期范围并返回 `unsupported/window_not_approved`，没有缩短为小时执行。

真实聊天运行：`9d059fb4-c3a5-4fc8-8725-068285b944d1`，SQL query：`b385513e-f174-4610-9855-d491a927aaec`。浏览器验收会话：`4706fd57-9411-4710-8ecf-c701108ba5b3`。这些均为本次隔离环境的合成记录，重建数据卷后不保证仍存在。

完整命令、镜像/源码摘要和截图在仓库 `output/sql-candidate-local-demo-20261107/`：`http-verification.json`、`backend-verification.json`、`ui-browser-report.json` 和最终 `verification.json`。最终报告以该目录的实际 JSON 证据为准。

本次写作验证到任务创建及 `research_review` 人工审查门，未批准或发布。Data Loop Worker 的就绪已验证，专用回放/审批/激活流程未运行；不要将本次Text2SQL验收扩展为所有业务能力已验收。

未完成的企业验收包括真实推理接口质量、真实数仓合同、负载与长稳。当前确定性候选用于验证工程接线，不能用于证明开源模板提高 SQL 正确率。正式候选收益还需冻结语义集、真实模型对照、成本/时延测量及人工发布审核。

当前取消或进程崩溃发生在预览保存前时，候选失败记录可能尚未持久化；生成期间的事务锁占用数据库连接，尚不能据此宣称生产容量与所有失败成本均已验收。
