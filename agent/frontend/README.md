# NewsAgent 前端

Vue 3 + TypeScript + Vite 的运营控制台，包含热点、写作和 Data Loop 页面。

## 边界

- 生产鉴权由网关注入，浏览器代码不保存企业令牌，也不主动构造身份头。
- 本地开发身份只由 Vite 代理注入；生产构建不包含这些值。
- API 契约以后端 OpenAPI 为事实来源，`src/api/types.ts` 只维护页面所需稳定子集。
- 模型与知识库由 Python 后端负责，前端不直接调用任何模型服务。

## 本地全链路启动

先启动后端：

```bash
cd writing-agent-service
docker compose -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
```

再启动前端：

```bash
cd frontend
npm ci
npm run dev
```

`npm ci` 只需首次安装或锁文件变更后执行。开发服务器自动读取
[`dev-server/local.yml`](dev-server/local.yml)：端口 `5174`、后端地址
`http://127.0.0.1:28000`、本地模拟开关、演示租户/用户/角色和公开本地令牌。
修改 YAML 后重启 `npm run dev`，无需再粘贴一串环境变量。

打开 `http://127.0.0.1:5174/hot-news#query-tools`，选择示例问题，保留默认一小时窗口，
点击“启动热点 Agent”。Text2SQL 是热点内部工具，不是独立查询页面。链路为：

```text
Vue
  → Vite 代理注入本地身份
  → FastAPI
  → Text2SQL受限计划、SQL Guard、原子绑定run_key
  → Temporal Workflow/Worker
  → PostgreSQL只读候选与同小时聚合指标/基线
  → Python 指标与热度
  → Python Agent + 本地 InferencePort
  → Schema/业务校验
  → PostgreSQL/MinIO
  → 查询 API/SSE
```

SQL、候选输入、热点排名和分析保存于同一个运行，查看历史运行不会拼接最新SQL查询。
详细调用链、场景YAML、冻结Schema、重试和测试见 [热点SQL模拟说明](../docs/text2sql-local-demo.md)。

新闻知识在 Python 中完成分块、Embedding、索引和关联检索。本地确定性模型替身只用于
复现测试，不代表企业模型质量。

## 前端 YAML 配置调用链

`npm run dev` → `vite.config.ts` → `loadLocalDevSettings()` → `js-yaml` 解析
`dev-server/local.yml` → `parseLocalDevYaml()` 严格校验版本、类型、端口、UUID、角色
→ Vite 启动 HTTP 服务并代理 `/api` → Python API → 页面展示业务结果。
只有 `simulation.enabled` 转换为浏览器的模拟功能开关；身份和令牌只存在于 Node 代理层。

- `server.host` / `server.port`：前端监听地址和端口。
- `proxy.target`：Python 后端地址。
- `simulation.enabled`：热点页面的本地模拟入口。
- `gateway.enabled` / `tenant_id` / `user_id` / 两类 `roles` / `token`：本地代理身份。
- `schema_version`：当前仅支持 `1`，未知字段或格式错误会阻止启动。

终端显式设置的 `VITE_DEV_*` 和 `VITE_LOCAL_SIMULATION` 环境变量可以覆盖 YAML；
旧 `.env` 文件里的这些本地配置不再参与开发启动，避免把请求发送到旧后端。
空身份值会移除对应头，`VITE_DEV_GATEWAY_ENABLED=0` 关闭全部模拟身份。
`assertLocalDevBoundary()` 会校验最终监听地址（包含 CLI `--host`），注入本地身份时
前端和代理目标都必须是环回地址。配置目录禁止通过开发服务器下载。

生产构建、生产模式和所有 `preview`（包括 `preview:local`）不加载这份 YAML，
不注入本地身份、不启用模拟入口；私有 `VITE_DEV_*` 不暴露为浏览器环境变量。
企业模型密钥仍由 Python 后端/企业网关管理，不能填入这份提交到仓库的演示 YAML。
生产发布继续使用 `public/config.json` 和真实网关，不要把开发服务器用于生产。

测试入口是 `npm test`：Node 内置测试验证 YAML/覆盖规则，Vite API 在隔离临时项目中
验证构建、预览、浏览器环境变量、HTTP 文件保护和环回测试代理，不依赖企业接口。
开发配置依赖 Node、Vite 和 `js-yaml`，不增加任何外部服务依赖。
企业网关与真实模型的联调验收仍为 TODO；本配置不替代这些验收。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 启动开发服务器 |
| `npm test` | YAML 配置与隔离边界回归 |
| `npm run typecheck` | TypeScript/Vue 类型检查 |
| `npm run lint` | ESLint 检查 |
| `npm run build` | 类型检查并生成生产静态资源 |
| `npm run gen:api` | 从后端 OpenAPI 重新生成完整 API 类型 |

`X-Tenant-ID` 与 `X-User-ID` 必须是 UUID；热点权限使用 `X-Hot-News-Roles`，Data Loop
权限使用 `X-Data-Loop-Roles`。这些头只能由可信网关或本地开发代理产生。

停止后端但保留数据卷：

```bash
docker compose -f deploy/docker-compose.native-e2e.yml down
```
