# 前端企业级部署方案

> 状态更新：2026-09-18。骨架已落地（`frontend/` + `deploy/k8s/`），
> 热点模块后端只读接口与决策接口已实现并接入前端真实页面；企业 CMS 由
> `deploy/mock-cms` 替身支撑发布链路；尚未替换线上页面。
> 本文档说明目标架构、鉴权模型、迁移步骤与验收标准。
>
> 第 1 节保留的是迁移前基线，其中“热点使用内置示例数据、后端没有热点路由”等描述
> 已经过期；当前前端能力以 `frontend/README.md` 为准。企业 SSO/Gateway、API/Worker 的
> 完整 K8s 发布、RUM/告警和生产灰度仍未完成。

## 1. 现状

`writing-agent-service/app/web/` 下有三个页面，全部由 FastAPI 以 `FileResponse` 同源托管：

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `console.html` | 1180 行 / 66 KB | 三模块合一，零构建的原生 JS |
| `review.html` | — | 早期审批控制台，职责已被 console 覆盖 |
| `research-package.html` | — | 早期资料包可视化，同上 |

三个模块的真实状态并不一致：

- **写作任务工作台**：真实调用 `/api/v1/jobs`，含 SSE 进度流与人工门禁。
- **Data Loop 评审台**：真实调用 `/api/v1/data-loop`。
- **热点运营工作台**：**渲染前端内置示例数据**。后端只有 `app/analytics` 的计算逻辑，
  没有对应的只读路由，榜单与热度分量无法审计。

身份处理是当前最大的技术债：租户、用户、角色、网关共享 Token 全部由运营在页面上手填并存入
`localStorage`。而 `app/api/dependencies.py::get_data_loop_principal` 的设计意图是
"仅信任可信网关注入的 Header"，浏览器直连等于把这道信任边界放在了用户可控的位置。

另外，项目没有 `CORSMiddleware` 也没有 `StaticFiles`，前端无法在不改动后端的前提下拆成独立域名。

## 2. 目标架构

请求自上而下四层：

```
浏览器 ──▶ 接入层（Nginx / Ingress：TLS、安全头、WAF、限流）
        ──▶ 前端应用层（Vue 3 + TS + Vite 构建产物，带内容哈希）
        ──▶ 接口层（BFF / 网关：解析登录态，注入身份 Header）
        ──▶ 现有 FastAPI 服务层（/api/v1/jobs、/api/v1/data-loop，契约不变）
```

四个关键决策（已确认）：

1. **技术栈**：Vue 3 + TypeScript + Vite，输出纯静态产物，不引入常驻 Node 运行时。
2. **鉴权**：企业网关统一鉴权。前端不实现登录页，不持有任何共享凭据。
3. **发布形态**：K8s，Ingress + Deployment，多副本滚动发布。
4. **本轮范围**：方案与工程骨架，不迁移既有页面。

## 3. 鉴权模型：从浏览器上移到网关

| | 现状 | 目标 |
| --- | --- | --- |
| 租户 | 页面手填 → `localStorage` → `X-Tenant-ID` | 网关从会话解析后注入 |
| 用户 | 页面手填 → `X-User-ID` | 同上 |
| 角色 | 页面手填（默认 `data-loop:admin`） | 网关注入 `X-Data-Loop-Roles` |
| 共享 Token | 存在浏览器里 | 只存在于网关，经 Secret 注入 |
| 浏览器持有 | 身份 + 凭据 | 仅会话 Cookie |

后端**不需要任何改动**：`dependencies.py` 本来就把这四个头当作"可信网关注入"来消费，
`events.py` 的 SSE 也只需要 `X-Tenant-ID` 一个头。

配套的安全前提（写在 Ingress 注解里，但很容易漏）：

```nginx
more_clear_input_headers "X-Tenant-ID" "X-User-ID" "X-Data-Loop-Roles";
more_clear_input_headers "Authorization";
```

不先清掉客户端传入的同名头，任何浏览器都能自称任意租户。
后端对 Data Loop 仍会独立校验共享 Bearer Token，但那是第二道防线，不能替代第一道。

## 4. 工程结构

```
frontend/
  package.json  tsconfig.json  vite.config.ts  eslint.config.js
  Dockerfile                    # node 构建 → nginx-unprivileged 运行
  nginx/default.conf            # 静态托管、缓存策略、SPA 回退、/healthz
  public/config.json            # 运行时配置，由 ConfigMap 覆盖
  src/
    api/       http.ts（统一请求层）· jobs.ts · dataLoop.ts · events.ts（SSE）
    config/     runtime.ts（替代 localStorage 的运行时配置）
    stores/     app.ts · jobs.ts
    layouts/    AppShell.vue
    views/      HotNewsView · JobsView · DataLoopView · NotFoundView
    router/  styles/  utils/
deploy/k8s/
  frontend-deployment.yaml  frontend-service.yaml  frontend-configmap.yaml
  frontend-ingress.yaml     frontend-hpa.yaml      frontend-networkpolicy.yaml
  kustomization.yaml
```

两个刻意的设计：

- **构建期与运行期分离**。镜像里不含任何环境地址，`config.json` 由 ConfigMap 挂载覆盖，
  同一份镜像可以部署到测试与生产，改配置只需重启 Pod。
- **类型不靠猜**。`src/api/types.ts` 是人工维护的稳定子集，字段逐一对齐后端 Pydantic Schema；
  无法确定的嵌套结构刻意保留 `JsonObject` 并标注 `TODO(gen:api)`，接入前先跑
  `npm run gen:api` 从 `/openapi.json` 生成全量权威类型。

## 5. 迁移步骤

### P0 — 前置补齐（阻塞项）

1. ~~后端补热点榜只读接口~~（2026-09-10 已完成，`app/api/hot_news.py`）：
   - `GET /api/v1/hot-news/runs`：运行列表（窗口、Bundle、计数）
   - `GET /api/v1/hot-news/runs/{id}`：榜单、热度分量、分析摘要与决策记录
   - `POST /api/v1/hot-news/decisions`：运营决策（幂等，拒绝/修正原子进入 Data Loop 反馈）
   鉴权按已确认决策：**与 Data Loop 复用 `DATA_LOOP_GATEWAY_TOKEN` 共享 Bearer，
   角色头独立为 `X-Hot-News-Roles`**（`hot-news:read` / `hot-news:decide` / `hot-news:admin`）。
2. 提供 SSO 网关，并确定 `auth-url` 校验通过后返回的身份头名称。
3. 把 API 部署到 K8s 并暴露 Service（仓库目前只有 compose 的 `api`）。

### P1 — 骨架联调

4. `npm install && npm run dev`，开发服务器用 `/api` 代理直连本地 FastAPI；
   临时用反向代理补齐身份头，验证写作与 Data Loop 两个模块的真实链路。
5. `npm run gen:api` 生成全量类型，逐步替换 `types.ts` 中标 `TODO(gen:api)` 的部分。
6. 构建镜像并跑通 `deploy/k8s` 清单，确认探针、滚动更新、HPA 生效。

### P2 — 功能补齐

7. 按后端接口实现热点榜页面（不再使用假数据）。
8. 补齐 Data Loop 的评测集冻结、候选晋升、Bundle 回滚三个表单。
9. 接入错误上报（`config.json` 的 `errorReportingDsn` 已留挂载点）。

### P3 — 切换与清理

10. 灰度：Ingress 先切 `/`，观察一段时间后停用 `/console`、`/review`、`/research-package`。
11. 删除 `app/web/console.html` 与另外两个页面，移除 `app/main.py` 中的三个路由注册。
12. 更新 `PROJECT_CONTEXT.md` 与 `DEPLOYMENT.md`。

## 6. 验收清单

- [ ] `npm run build` 零类型错误，产物含内容哈希，`index.html` 不缓存
- [ ] 浏览器不持有任何身份头与共享 Token；删除手填入口
- [ ] 伪造 `X-Tenant-ID` 请求被网关剥离，返回 401/403
- [ ] SSE 进度流经 Ingress 不被缓冲，断线后按 `Last-Event-ID` 续读无丢事件
- [ ] 同镜像切换到不同 `config.json` 后行为正确，无需重新构建
- [ ] 容器非 root、根文件系统只读、出站仅 DNS
- [ ] 滚动更新期间无 5xx，`/healthz` 探针不误杀
- [ ] 热点模块要么接真实接口，要么在导航中不出现

## 7. 未决事项与风险

| 项 | 说明 |
| --- | --- |
| 热点模块 | 后端接口和真实页面已存在；企业 Adapter、真实模型与生产数据验收仍是阻塞项 |
| 网关实现 | `auth-url` / `auth-signin` 是占位域名；若企业用 OIDC 直连而非 oauth2-proxy，注解写法需调整 |
| 旧页面处置 | `review.html`、`research-package.html` 是否允许随切换一并删除，需确认 |
| 多租户切换 | 当前架构假设"一个部署一个租户"。若同一控制台要跨租户操作，需要在网关注入租户列表并在前端做切换，属于新需求 |
| 前端可观测性 | 目前只有挂载点，未接入 RUM / 错误上报 |
