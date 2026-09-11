# NewsAgent 前端（企业版）

Vue 3 + TypeScript + Vite 的运营控制台，替代原先由 FastAPI 同源托管的单文件 `console.html`。

## 设计前提

1. **鉴权在网关**。浏览器只带会话 Cookie，不再手填租户 / 用户 / 角色 / 网关 Token。
   后端的 `X-Tenant-ID`、`X-User-ID`、`X-Data-Loop-Roles`、`Authorization` 全部由网关注入。
   `src/` 下**没有任何一行代码设置身份头**——本地开发需要的身份头只在开发代理里补（见下）。
2. **构建期与运行期分离**。镜像里不含任何环境地址；`public/config.json` 由 ConfigMap 覆盖，
   同一份镜像可部署到测试与生产。
3. **API 契约以后端为唯一事实来源**。`src/api/types.ts` 是人工维护的稳定子集，
   全量权威类型通过 `npm run gen:api` 从 `/openapi.json` 生成。

## 本地开发

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173
```

`/api` 会自动代理到 `http://127.0.0.1:8000`（可用 `VITE_DEV_PROXY_TARGET` 覆盖）。

### 本地没有网关，身份头从哪来

后端强制要求身份头，而本地没有企业网关。为不改动生产不变式，身份头**只在开发代理层注入**，
配置在 `.env.development`：

```
VITE_DEV_TENANT_ID=11111111-1111-4111-8111-111111111111
VITE_DEV_USER_ID=22222222-2222-4222-8222-222222222222
VITE_DEV_DATA_LOOP_ROLES=data-loop:admin
VITE_DEV_HOT_NEWS_ROLES=hot-news:admin
VITE_DEV_GATEWAY_TOKEN=local-dev-token
```

> 这两个 UUID 必须与后端场景/测试使用的 `E2E_TENANT_ID`（`examples/hot_news_e2e_support.py`）
> 保持一致，否则接口会 200 但返回空列表 —— 因为查的是另一个租户的数据。

要对接别处后端时改这几个值即可，也可以删掉它们 —— 删掉就模拟"没有网关"，接口会返回 401/403，
正好用来验证前端的鉴权失败提示。

几个必须知道的细节：

- `X-Tenant-ID` 与 `X-User-ID` 后端要求是 **UUID**，写成 `operator` 这类字符串会 422。
- `X-Data-Loop-Roles` 是**逗号分隔的权限串**，取值见
  `app/api/dependencies.py::DataLoopPermission`；填 `data-loop:admin` 可通吃全部端点。
- `X-Hot-News-Roles` 是热点控制台的独立角色头，取值见
  `app/api/dependencies.py::HotNewsPermission`（`hot-news:read` / `hot-news:decide` /
  `hot-news:admin`）。它与 Data Loop **复用同一个共享网关 Token**，但权限独立授予。
- `VITE_DEV_GATEWAY_TOKEN` 必须等于后端 `.env` 里的 `DATA_LOOP_GATEWAY_TOKEN`。

## 命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 开发服务器（注入本地身份头，SSE 代理已关闭缓冲） |
| `npm run build` | typecheck + 生产构建 |
| `npm run preview` | 预览生产产物，**不注入身份头**，用于模拟真实网关环境 |
| `npm run preview:local` | 预览生产产物，但按开发模式加载环境变量，注入本地身份头 |
| `npm run typecheck` | `vue-tsc` 严格模式类型检查 |
| `npm run lint` / `lint:fix` | ESLint（flat config） |
| `npm run format` | Prettier |
| `npm run gen:api` | 从后端 OpenAPI 生成全量类型到 `src/api/schema.d.ts` |

## 目录

```
src/
  api/          类型化接口层：http / jobs(12 端点) / dataLoop(17 端点) / hotNews(3 端点) / events(SSE) / types
  components/   通用组件：NaToastHost / NaConfirmDialog / NaPaginator / NaStateBlock / NaJsonBlock
  components/jobs/  写作任务：创建卡片 / 任务表格 / 检查器 / 事件流查看器
  components/hotnews/  热点：榜单表格（热度分量+分析摘要）/ 运营决策表单
  composables/  useAsyncTask（统一 loading + 错误提示）
  config/       运行时配置加载（替代 localStorage）
  stores/       Pinia：app（配置与未授权态）、jobs（任务与事件流）、toast、confirm
  layouts/      AppShell 布局
  views/        热点 / 写作 / Data Loop(6 个子面板) / 404
  router/       路由与标题（Data Loop 为嵌套子路由）
  styles/       base.css（浅色主题）
  utils/        format / labels / validate / errors / download
nginx/          容器内 nginx 配置
```

## 交互约定

- 全部破坏性写操作（取消任务、发布、激活恢复、数据集冻结、生产包回滚、标签审批）
  一律先经过 `useConfirmStore` 的二次确认。
- 请求结果与错误统一走 `useToastStore`，不再由各页面各自维护 `message` 字符串。
- 载入 / 失败 / 空三态统一走 `NaStateBlock`，任何面板都不会出现空白页。
- 事件流支持按关键词与事件类型筛选，且渲染的是**全部**已接收事件（按时间倒序），
  不再是只看最后一条。
- 写接口统一由 `newIdempotencyKey()` 生成幂等键，重复点击不会产生重复业务对象。

## 尚未完成

- ~~热点榜读接口未在后端实现~~（2026-09-10 已接入）：`HotNewsView` 现在真实调用
  `GET /api/v1/hot-news/runs`、`GET /api/v1/hot-news/runs/{id}` 与
  `POST /api/v1/hot-news/decisions`；榜单、热度分量与分析摘要全部来自
  `analysis_runs` 持久化快照，不渲染任何假数据。
- `src/api/types.ts` 中标注 `TODO(gen:api)` 的类型是刻意保留的宽松类型，接入前需先跑生成脚本。
- 企业错误上报（`errorReportingDsn`）只留了挂载点，未接入具体平台。
- API / Worker 侧尚无 K8s 清单，见 `../deploy/k8s/README.md` 与 `../docs/frontend-enterprise-plan.md`。
