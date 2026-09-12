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

## 界面说明（按钮与选项）

顶部 `AppShell` 提供 `热点运营` / `写作任务` / `Data Loop` 三个入口与环境徽标；
任一接口返回 401/403 时，主区顶部出现"未通过网关鉴权"提示条。以下按模块列出每个按钮与选项的含义。

### 热点运营 `views/HotNewsView.vue`

**运行列表**（`GET /hot-news/runs`）

- `刷新运行列表`：重新拉取当前租户的运行记录。
- 列：`窗口` / `Production Bundle` / `上榜` / `已分析` / `完成时间`；行末 `查看` 打开该运行榜单。
- 若 `config.json` 的 `enableHotNews=false`，顶部显示模块未开放提示。

**榜单与热度分量**（`GET /hot-news/runs/{run_id}`）

- `分析` / `收起`：展开该条 news_id 的 LLM 分析摘要（趋势判断、主导驱动、证据 news_id、
  FastGPT request_id、限制、运营建议 JSON）。
- `决策`：把该行 news_id 预填到下方决策表单。
- 热度列：总热度 + 四分量 `点击/消费/互动/增长`；置信度为分析结果的 `overall_confidence`。

**记录热点运营决策**（`POST /hot-news/decisions`，`HotNewsDecisionForm.vue`）

| 字段 | 选项 / 含义 |
| --- | --- |
| `news_id` | 要决策的新闻，点榜单"决策"自动带入 |
| `决策` | 采纳 `accepted` / 驳回 `rejected` / 延后 `deferred` / 修正 `corrected` |
| `问题类型` | 分析有误 / 论点缺少支撑 / 指标不一致 / 证据不一致 / 缺少证据 / 置信度偏低 / 检索漏召 / 检索误召 / 结构不合法 / 违反策略 / 效果未达标 / 其他 |
| `严重度` | 低 `low` / 中 `medium` / 高 `high` / 严重 `critical` |
| `覆盖的决策 ID` | 可选，用于覆盖（supersede）一条旧决策 |
| `原因` | 必填 |
| `修正载荷` | 仅 `修正` 时出现，JSON 对象必填 |

- `提交决策`：`驳回` 以危险色二次确认；驳回/修正会进入 Data Loop 反馈链路。

### 写作任务 `views/JobsView.vue`

**新建写作任务**（`POST /jobs`，`JobCreateCard.vue`）

| 字段 / 按钮 | 含义 |
| --- | --- |
| `选题` | 必填，文章主题 |
| `产出类型` | 辅助写作（含成稿）`assisted_writing` / 仅资料包 `research_package` |
| `补充要求` | 切换一个 JSON 文本框，可留空 |
| `创建任务` | 带幂等键提交，成功后自动选中新任务 |

**任务列表**

- `刷新`；`关键词`（回车触发）；`状态`：全部状态 / 待人工处理 `waiting_human` /
  撰写中 `drafting` / 评审中 `reviewing` / 终审通过 `final_approved` / 已发布 `published` /
  失败 `failed` / 已取消 `cancelled`；`每页` 10/20/50；`仅待人工处理`（默认勾选）；`搜索`。
- 点击表格行选中任务；列：`选题`（附任务 ID）/ `状态` / `进度` / `更新时间`。

**任务详情** `JobInspector.vue`

- 头部：状态徽标、事件流连接状态徽标、`刷新`。
- 指标：当前步骤、整体进度、章节进度、评审轮次、检索重试。
- 标签页：
  - `概览`：任务/租户/产出类型/Temporal 工作流/时间 + `requirements` JSON。
  - `进度`：阶段、已完成步骤、评审轮次、等待门禁、最近产物 URI + 原始快照。
  - `研究指标`：逻辑键、产物版本 + 指标 JSON。
  - `资料包`：`导出资料包` + 资料包 JSON。
  - `恢复`：`恢复令牌`、`恢复动作`（按检查点继续 / 重新检索）、补充指令、`执行恢复`（仅在可恢复时可用）、恢复点 JSON。
  - `事件流`：`EventStreamViewer.vue`——`关键词`、`事件类型`、`清空筛选`；事件倒序展示、可展开原始载荷；底部 `重新连接事件流`。
- `人工决策`：
  - `门禁`：资料门禁 `research` / 大纲门禁 `outline` / 评审门禁 `review` / 终审门禁 `final`（按任务状态自动推断）。
  - `动作`：通过 `approve` / 打回修改 `revise` / 补充检索 `research` / 取消任务 `cancel`。
  - `指令`：`打回修改`、`补充检索` 必填；`取消任务` 二次确认后提交。
  - `提交决策`。
- `发布与导出`：`发布渠道`（默认 `default`）、`发布到 CMS`（二次确认）、`导出成稿`。

### Data Loop `views/DataLoopView.vue`

页面顶部六个子标签页：反馈案例 / 评测运行 / 运营决策 / 评估数据集 / 生产包 / 配置候选。

**反馈案例** `FeedbackCasesPanel.vue`

- 列表：`刷新`；`状态筛选`：已收集 `collected` / 待标注 `needs_label` / 已标注 `labeled` /
  已排除 `excluded` / 已冻结 `frozen`；点击行选中案例。
- 已有标签表：`版本号`输入、`审核意见`输入、`审批` / `二审通过` / `退回修改`（退回必填意见）。
- 新增标签：`标签内容` JSON、`期望上一版本`（乐观锁，可选）、`提交标签`。

**评测运行** `RunsPanel.vue`

- `启动评测运行` 字段：`窗口开始` / `窗口结束`（本地时间）、`数据集名称`、`数据集版本`、
  `黄金数据集 ID`、`高风险回归数据集 ID`、`候选配置 ID`、`上一个候选 ID`（可选）、
  `评估策略版本`；按钮 `启动运行`。
- `快照与门禁`：`工作流 ID`、`查询快照`、`激活恢复`（危险色、二次确认的写操作）；
  快照展示 `阶段 / 门禁 / 等待审批 / 数据集`。
- 门禁决策：`动作`（通过 `approve` / 驳回 `reject`）、`原因`、`提交门禁决策`。

**运营决策** `DecisionsPanel.vue`

- `记录运营决策`：`run_id`、`news_id`、`决策`、`问题类型`、`严重度`、`原因`、
  `覆盖的决策 ID`、`修正载荷`（修正时）、`提交决策`；选项含义同热点决策表单。
- `从反馈案例带入`：`重新加载`，选择一条反馈案例自动填充 run_id / news_id / 问题类型 / 严重度。

**评估数据集** `DatasetsPanel.vue`

- `冻结评估数据集`：`数据集名称`、`数据集版本`、`数据层`
  （黄金集 `golden` / 新鲜坏例 `fresh_bad_case` / 高风险回归 `high_risk_regression`）、
  `数据截止时间`、`描述`、`反馈案例 ID`（每行一个）、`带入已标注案例`、`冻结数据集`
  （危险色，冻结后不可修改）。
- `查询数据集`：`数据集 ID`、`查询`。

**生产包** `BundlesPanel.vue`

- `当前生效生产包`：`刷新`。
- `引导生产包`：`生产包版本`、`生产包规格` JSON、`引导生产包`。
- `回滚生产包`：`目标生产包 ID`、`回滚原因`、`执行回滚`（危险色，直接影响线上，二次确认）。

**配置候选** `CandidatesPanel.vue`

- `提交配置候选`：`基线生产包 ID`、`候选版本`、`提案原因`、`候选规格` JSON、
  `结构化差异` JSON 数组、`提交候选`。
- `查询候选详情`：`候选 ID`、`查询`。

### 通用交互

- 破坏性写操作（取消任务、发布、激活恢复、数据集冻结、生产包回滚、标签审批/退回）
  一律先经 `useConfirmStore` 二次确认。
- 载入 / 失败 / 空三态统一走 `NaStateBlock`，面板不会出现空白页。
- 所有写操作由 `newIdempotencyKey()` 生成幂等键，重复点击不产生重复业务对象。
- JSON 输入框校验失败只弹 toast，不发起请求。

## 尚未完成

- ~~热点榜读接口未在后端实现~~（2026-09-10 已接入）：`HotNewsView` 现在真实调用
  `GET /api/v1/hot-news/runs`、`GET /api/v1/hot-news/runs/{id}` 与
  `POST /api/v1/hot-news/decisions`；榜单、热度分量与分析摘要全部来自
  `analysis_runs` 持久化快照，不渲染任何假数据。
- `src/api/types.ts` 中标注 `TODO(gen:api)` 的类型是刻意保留的宽松类型，接入前需先跑生成脚本。
- 企业错误上报（`errorReportingDsn`）只留了挂载点，未接入具体平台。
- API / Worker 侧尚无 K8s 清单，见 `../deploy/k8s/README.md` 与 `../docs/frontend-enterprise-plan.md`。
