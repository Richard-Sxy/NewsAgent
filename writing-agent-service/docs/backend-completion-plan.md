# NewsAgent 后端补齐计划（前端收官所需的全部后端工作）

编写时间：2026-09-10
编写依据：对 `writing-agent-service` 当前代码的实地核对（非估算），逐条给出行号级证据。
本文档是**执行交接文档**：交给另一个会话照做，每个步骤都给出文件路径、函数签名、
验收命令与常见失败。

> 本文件由分析会话产出，**未修改任何业务实现文件**。按 `PROJECT_CONTEXT.md` 第 14 节，
> 业务代码由用户亲自实现；本文档只提供结构与签名，不含完整业务实现。

---

## 0. 一句话结论

前端三个模块里，**只有「热点运营工作台」被后端完全阻塞**，其余两个（写作任务、Data Loop）
后端已可用。所以要「全部完成这个项目」，后端必须补的是：

1. **热点模块的 HTTP 层与 Worker 进程**——代码齐全但**没有任何 API，也没有进程能跑它**（P0）。
2. **写入方向的身份校验**——`/api/v1/jobs` 目前**零网关校验**（P1，安全问题）。
3. **CMS 发布网关配置**——写作链路最后一环返回 503（P2）。
4. **Memory 层 API**——领域层齐全，无对外接口（P3）。
5. **生产化与验收**（P4）。

---

## 1. 前端三模块的后端就绪度

| 前端模块 | 后端路由 | 后端领域层 | 就绪度 | 阻塞项 |
| --- | --- | --- | --- | --- |
| 写作任务工作台 | `app/api/jobs.py` 12 端点 + `events.py` SSE | 完整 | **可用** | 仅 `publish` 返回 503（P2） |
| Data Loop 评审台 | `app/api/data_loop.py` 17 端点 | 完整（11 个服务文件） | **可用** | 仅生产启用（P4） |
| 热点运营工作台 | **不存在** | 完整（模型/Schema/Workflow/Activity/编排/Agent 全有） | **0%** | P0 全部 |

核对证据：

- `app/api/` 下只有三个文件：`jobs.py`、`events.py`、`data_loop.py`。
- `app/api/__init__.py` 只导出三个 router；`app/main.py:59-61` 只注册三个 router。
- `grep -rn "hot_news" app/api/ app/main.py` → **零匹配**。
- 但热点领域层有 **22 个文件**：`app/models/hot_news.py`、`app/schemas/hot_news.py`、
  `app/workflows/hot_news.py`、`app/activities/hot_news.py`、`app/hot_news_bootstrap.py`、
  `app/services/hot_news_*.py`（6 个）、`app/services/data_loop/*` 等。

**这是整个项目最不对称的地方：热点是最完整的领域实现，也是唯一没有任何出口的模块。**

---

## 2. 缺口总表

| 编号 | 缺口 | 证据 | 阻塞面 | 优先级 |
| --- | --- | --- | --- | --- |
| G-A | 热点无 HTTP 路由 | `app/api/` 无 hot_news | 前端热点模块 100% | **P0** |
| G-B | 热点 Worker 无进程入口 | `app/hot_news_worker.py` 只有 `run_hot_news_worker()`，**无 `main()`** | 热点无法运行 | **P0** |
| G-C | 热点无 Temporal Schedule | 全仓无 `ScheduleClient`/`create_schedule` | 无持续调度 | **P0** |
| G-D | 热点无读取仓储 | `PostgresHotNewsRunStore` 只有 `get_completed()`（按幂等键），**无 list/get by id** | 列表页无法实现 | **P0** |
| G-E | `/api/v1/jobs` 零网关校验 | `app/api/jobs.py` 无 gateway token；仅 `X-Tenant-ID`+`X-User-ID`，二者均由客户端控制 | 越权读写任意租户 | **P1** |
| G-F | 无 IdP / SSO | 对比 `get_data_loop_principal` 只校验共享 Token | 企业登录 | **P1** |
| G-G | CMS 未配置 | `.env` 无 `CMS_PUBLISH_URL`/`CMS_PUBLISH_TOKEN`；`cms.py:33-34` 抛 `CmsNotConfiguredError` | `publish` 恒 503 | **P2** |
| G-H | Memory 无 API | `app/schemas/user_memory.py`、`hot_news_memory.py`、`app/repositories/user_memory.py`、`app/services/memory_*.py` 齐全，但无 router | 记忆层不可用 | **P3** |
| G-I | 无 Temporal Schedule 运维面 | 同上 | 周期运行 | **P4** |
| G-J | 镜像 tag 可变 | compose 用 `news-agent/writing-agent-service:dev` | 滚动发布事故 | **P4** |
| G-K | API/Worker 无 K8s 清单 | `deploy/k8s/` 仅前端 7 个文件 | 生产部署 | **P4** |
| G-L | 无热点指标 | `app/main.py:92` `/metrics` 只导出 jobs + outbox | 可观测性 | **P4** |
| G-M | 无热点进度事件 | `app/activities/hot_news.py` 无 Redis publish | 热点 SSE | **P4（可选）** |

---

## 3. P0 —— 让热点链路上线（解锁前端第三个模块）

目标：前端 `HotNewsView.vue` 从占位变成真实可用，热点能被人手动触发、能看、能决策。

### P0-1 新增热点读取仓储方法

**位置**：`app/services/hot_news_run_store.py`（现有 `PostgresHotNewsRunStore` 类内追加）

现有能力只有 `get_completed(...)`（第 29 行，按 `idempotency_key` 精确命中，给 Activity 幂等用的）。
列表页需要两个新方法：

```python
async def list_runs(
    self,
    *,
    tenant_id: str,
    status: str | None = None,
    window_start_from: datetime | None = None,
    window_start_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[HotNewsAnalysisRun], int]:
    """按租户分页列出分析运行。返回 (行列表, 总数)。"""
    # select(HotNewsAnalysisRun).where(tenant_id == ...)
    #   .order_by(HotNewsAnalysisRun.window_start.desc(), id.desc())
    # 总数用 select(func.count()).select_from(...) 同一组 where 条件

async def get_run(
    self,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
) -> HotNewsAnalysisRun | None:
    """按租户 + run_id 取单条。tenant_id 必须参与 where，避免跨租户读取。"""
```

**可用字段**（`app/models/hot_news.py:21`，表名 `analysis_runs`）：
`id`、`tenant_id`、`idempotency_key`、`window_start`、`window_end`、
`production_bundle_version`、`workflow_version`、`status`、`fetched_record_count`、
`metric_snapshot_count`、`ranked_news_count`、`analyzed_news_count`、
`payload_schema_version`、`result_payload`（JSONB）、`completed_at`。

**注意**：`tenant_id` 在 `analysis_runs` 里是 `String(128)`，不是 UUID（与 `writing_jobs` 不同）。
API 层收到的 `X-Tenant-ID` 是 UUID 字符串，直接 `str()` 传入即可，**不要改成 UUID 类型比较**。

**常见失败**：漏了 `tenant_id` 过滤 → 跨租户泄漏。`list_runs` 的 count 查询忘了复用 where 条件
→ 分页总数与列表不匹配。

**验收**：写单测走真实 PostgreSQL（参考 `tests/` 下现有 DB 测试的 fixture），断言
① 两个租户各插 1 条，A 租户查不到 B 的；② `limit=1` 时 `total=2` 而 `items` 长度 1。

### P0-2 新增热点 API 响应 Schema

**新文件**：`app/schemas/hot_news_api.py`

前端 `frontend/src/views/HotNewsView.vue:15-26` 已声明期望契约（三个端点），
Schema 要与之对齐：

```python
class HotNewsRunSummary(BaseModel):
    run_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    status: str                    # 值来自 analysis_runs.status
    production_bundle_version: str
    workflow_version: str
    fetched_record_count: int
    metric_snapshot_count: int
    ranked_news_count: int
    analyzed_news_count: int
    payload_schema_version: str
    completed_at: datetime | None

class HotNewsRunListResponse(BaseModel):
    items: list[HotNewsRunSummary]
    total: int
    limit: int
    offset: int

class HotNewsRunDetailResponse(BaseModel):
    summary: HotNewsRunSummary
    report: HotNewsAnalysisReport        # 复用 app/schemas/hot_news.py:165
    validation_result: dict[str, Any]    # 来自 result_payload

class HotNewsDecisionRequest(BaseModel):
    # 字段直接对齐 app/schemas/hot_news_decision.py:14 RecordHotNewsDecisionCommand
    run_id: uuid.UUID
    news_id: str
    decision_type: Literal["accepted", "rejected", "deferred", "corrected"]
    reason: str
    correction_payload: dict[str, Any] = {}
    idempotency_key: str
    supersedes_decision_id: uuid.UUID | None = None

class HotNewsDecisionResponse(BaseModel):
    decision_id: uuid.UUID
    run_id: uuid.UUID
    news_id: str
    decision_type: str
    recorded_at: datetime
    idempotent_replay: bool
```

**关键约束**：`DecisionType` 在 `app/schemas/hot_news_decision.py:6` 已经是
`Literal["accepted","rejected","deferred","corrected"]`——**不要重新定义**，直接 import 复用。
`RecordHotNewsDecisionCommand` 上的 `model_validator`（`corrected` 必须有
`correction_payload`，非 `corrected` 必须为空）已被后端强制，前端也要同名校验。

### P0-3 新增热点权限依赖

**位置**：`app/api/dependencies.py`（在 `DataLoopPermission` 附近，第 37 行起）

现有模式可直接镜像：`DataLoopPermission`（StrEnum）+ `DataLoopPrincipal`(dataclass) +
`get_data_loop_principal(...)`（校验 Bearer + `X-Tenant-ID` + `X-User-ID` +
`X-Data-Loop-Roles` 逗号分隔）+ `require_data_loop_permission(...)` 工厂。

热点侧新增：

```python
class HotNewsPermission(StrEnum):
    READ = "hot-news:read"
    DECIDE = "hot-news:decide"

@dataclass(frozen=True)
class HotNewsPrincipal:
    tenant_id: str      # 注意：热点用 str，不是 uuid.UUID
    user_id: uuid.UUID
    permissions: frozenset[str]

async def get_hot_news_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
    x_user_id: Annotated[uuid.UUID | None, Header(alias="X-User-ID")] = None,
    x_hot_news_roles: Annotated[str | None, Header(alias="X-Hot-News-Roles")] = None,
) -> HotNewsPrincipal: ...

def require_hot_news_permission(permission: HotNewsPermission): ...
```

**必须复用的校验语义**（照抄 `dependencies.py:118-155`）：
- 未配置共享 Token → **503**（不是 401）
- Bearer 不匹配 → **401** + `WWW-Authenticate` 头，用 `secrets.compare_digest` 比较
- 缺租户/用户 → 401；角色为空 → 403

**决策点（需你拍板）**：热点是否复用 `DATA_LOOP_GATEWAY_TOKEN` 这同一个共享凭据？
建议复用（一个网关一张凭据，减少配置面），但**角色头必须独立**为 `X-Hot-News-Roles`，
否则 Data Loop 的运营角色会顺带拿到热点权限。

### P0-4 新增热点 API 路由

**新文件**：`app/api/hot_news.py`

```python
router = APIRouter(prefix="/api/v1/hot-news", tags=["hot-news"])

@router.get("/runs", response_model=HotNewsRunListResponse)
async def list_hot_news_runs(
    principal: Annotated[HotNewsPrincipal, Depends(require_hot_news_permission(HotNewsPermission.READ))],
    store: Annotated[PostgresHotNewsRunStore, Depends(get_hot_news_run_store)],
    status: str | None = None,
    window_start_from: datetime | None = None,
    window_start_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> HotNewsRunListResponse: ...

@router.get("/runs/{run_id}", response_model=HotNewsRunDetailResponse)
async def get_hot_news_run(
    run_id: uuid.UUID,
    principal: Annotated[..., Depends(require_hot_news_permission(HotNewsPermission.READ))],
    store: ...,
) -> HotNewsRunDetailResponse:
    """404 与 403 的区分：先按 (tenant_id, run_id) 查，查不到统一返回 404，
    不要先查存在性再判租户，否则会泄漏'该 run 存在但不属于你'。"""

@router.post("/decisions", response_model=HotNewsDecisionResponse, status_code=201)
async def record_hot_news_decision(
    request: HotNewsDecisionRequest,
    principal: Annotated[..., Depends(require_hot_news_permission(HotNewsPermission.DECIDE))],
    service: Annotated[HotNewsDecisionService, Depends(get_hot_news_decision_service)],
) -> HotNewsDecisionResponse:
    """内部构造 RecordHotNewsDecisionCommand 交给 HotNewsDecisionService.record_decision
    （app/services/hot_news_decision.py:46）。tenant_id 只来自 principal，不接受请求体传入。
    异常映射：HotNewsDecisionTargetNotFoundError → 404
              HotNewsDecisionConflictError → 409
              HotNewsDecisionPersistenceError → 503"""
```

**接线**（三处，缺一处就是 404）：

1. `app/api/__init__.py`：`from app.api.hot_news import router as hot_news_router`，
   加入 `__all__`。
2. `app/main.py:59-61`：追加 `application.include_router(hot_news_router)`。
3. `app/main.py` 的 `lifespan`：把 `PostgresHotNewsRunStore` 与 `HotNewsDecisionService`
   实例挂到 `app.state`，再在 `dependencies.py` 加两个取值函数：

```python
def get_hot_news_run_store(request: Request) -> PostgresHotNewsRunStore:
    return request.app.state.hot_news_run_store

def get_hot_news_decision_service(request: Request) -> HotNewsDecisionService:
    return request.app.state.hot_news_decision_service
```

**构造 `HotNewsDecisionService` 时注意**：它的 `__init__` 需要注入依赖
（见 `app/services/hot_news_decision.py:33`，包含 feedback collector）。
在 `lifespan` 里必须把 feedback collector 一并构造，**不能传 None**，否则决策落不了 Feedback Case。
参考 `dependencies.py` 现有的 `get_analysis_feedback_collector()`。

### P0-5 让热点 Worker 能作为进程启动

**问题**：`app/hot_news_worker.py` 只有 `run_hot_news_worker(...)`，且需要外部注入
`behavior_data_source`、`baseline_provider`、`content_repository`、`policy` 四个依赖。
**没有 `main()`，也没人组装这四个依赖**。所以 `TEMPORAL_HOT_NEWS_TASK_QUEUE=hot-news`
这个队列**永远没有消费者**（compose 里配了队列名，但 compose 里没有对应服务）。

**要做三件事：**

**(a) 新增组合根**——在 `app/hot_news_bootstrap.py` 里加一个函数，把
`build_enterprise_hot_news_adapters(...)`（`app/clients/enterprise/dependencies.py:65`）
的产物转成 `run_hot_news_worker` 需要的四个参数：

```python
def create_hot_news_worker_from_settings(
    settings: Settings,
) -> Awaitable[None]:
    """从 settings 组装企业 Adapter，返回可直接 asyncio.run 的协程。"""
    # 1. adapters = build_enterprise_hot_news_adapters(EnterpriseHotNewsAdapterConfig(...))
    # 2. return run_hot_news_worker(
    #        behavior_data_source=adapters.behavior_data,
    #        baseline_provider=adapters.baseline,
    #        content_repository=adapters.news_content,
    #        policy=HotNewsOrchestrationPolicy(...),
    #        knowledge_search=...,
    #        settings=settings,
    #    )
```

`EnterpriseHotNewsAdapterConfig` 的字段见 `app/clients/enterprise/dependencies.py:42`；
`app/clients/enterprise/README.md` 是这套适配器的对接说明，先读它。

**(b) 补入口**——`app/hot_news_worker.py` 末尾：

```python
def main() -> None:
    asyncio.run(create_hot_news_worker_from_settings(get_settings()))
```

**(c) 注册为可安装命令 + compose 服务**

- `pyproject.toml` 的 `[project.scripts]`（现有 3 条）追加：
  `writing-agent-hot-news-worker = "app.hot_news_worker:main"`
- `deploy/docker-compose.yml` 追加服务 `hot-news-worker`，**照抄
  `outbox-worker` 的段落**（第 173 行附近）：同一镜像、`command: python -m app.hot_news_worker`、
  `environment: *writing-agent-env`、`read_only: true`、`cap_drop: [ALL]`、`init: true`、
  同一 network、同一 depends_on。**唯一区别是它需要企业 RPC 凭据**。

**常见失败**：
- 忘记注册 script → `docker compose` 里 `command` 报 `No module named`。
- 组合根里把 `HotNewsOrchestrationPolicy` 写成默认值 → 策略版本与 Production Bundle 不一致，
  Activity 侧校验会拒。策略参数必须来自 Active Production Bundle，
  见 `app/services/active_bundle_hot_news.py`。
- `hot_news_runtime_manifest_json` 为 `"[]"` → `ProductionBundleRuntimeRegistry.from_json`
  （`app/hot_news_bootstrap.py:177`）拿不到 bundle。本地必须像现在这样从 `.env` 注入一条。

**验收命令**：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d hot-news-worker
docker compose -f deploy/docker-compose.yml logs --tail=40 hot-news-worker
# 预期：Temporal client 连上、Worker 开始 poll "hot-news" 队列，无 traceback
```

然后在 Temporal UI（`http://localhost:8080`）确认 `hot-news` 队列出现 **Poller**。

### P0-6 热点持续调度（Temporal Schedule）

现状：全仓 `grep -rn "ScheduleClient|create_schedule|ScheduleActionStartWorkflow"` → **零匹配**。
`HotNewsMonitorWorkflow`（`app/workflows/hot_news.py:25`）是**有界单次运行**，
没有任何东西周期触发它。

**新增文件建议**：`app/schedule_bootstrap.py`

```python
async def ensure_hot_news_schedule(settings: Settings) -> str:
    """幂等地创建/更新热点窗口 Schedule。返回 schedule_id。

    幂等维度（对齐 PROJECT_CONTEXT 第 4 节）：
      tenant_id + window_start + window_end + production_bundle_version

    Schedule 用 ScheduleActionStartWorkflow(
        HotNewsMonitorWorkflow.run, args=[HotNewsWindowRequest(...)],
        id=f"hot-news:{tenant_id}:{window_start.isoformat()}",
        task_queue=settings.temporal_hot_news_task_queue)

    IntervalSpec 建议 1 小时；catchup_window 设为窗口长度的 2 倍；
    必须设 ScheduleOverlapPolicy.SKIP 或 BUFFER_ONE，否则慢运行时窗口会重叠。
    """
```

**入口**：新增 `app/schedule_runner.py`（或并入 `hot_news_worker.py` 的 `main()` 前置步骤），
在 Worker 启动前调用一次 `ensure_hot_news_schedule`。

**注意**：Schedule 的 payload 只放租户、窗口、Workflow 版本、Production Bundle ID，
**不要放数据本身**（PROJECT_CONTEXT 第 8 节）。

**决策点**：一个 Schedule 覆盖多租户，还是每租户一个？
建议**每租户一个**，这样单租户的数据源故障不会连带影响其他租户，
且租户级暂停/限流更自然。租户清单来源需要你确认（配置项？还是 `tenants` 表？）。

### P0-7 前端接线（收尾，工作量很小）

前端已经有完整的基础设施，只需把占位换成真实调用：

1. `frontend/src/api/types.ts` 增加 P0-2 的三个响应类型（与后端字段同名）。
2. 新增 `frontend/src/api/hotNews.ts`，三个函数：`listRuns` / `getRun` / `recordDecision`，
   照抄 `frontend/src/api/dataLoop.ts` 的结构（它已经是 17 个端点的成熟范例）。
3. 重写 `frontend/src/views/HotNewsView.vue`：现在 82 行、纯占位（第 36 行有
   `na-badge--sample` "后端接口未接入" 徽标，第 15-26 行是声明的期望契约）。
   改成「运行列表（分页 + 状态筛选）+ 运行详情（报告 + 校验结果）+ 决策表单」。
   直接复用已有组件：`NaPaginator`、`NaStateBlock`、`NaJsonBlock`、`useAsyncTask`、
   toast、confirm。
4. 在 `frontend/vite.config.ts` 的开发代理里，把 `X-Hot-News-Roles` 一并注入
   （和现有 `VITE_DEV_*` 身份头同处），否则本地 403。
5. 跑一次 `npm run gen:api`，用 `/openapi.json` 收紧张量类型，
   顺便清掉 `types.ts` 里剩余的 `TODO(gen:api)`。

**验收**：`npm run typecheck && npm run lint && npm run build` 全绿；
本地页面能看到真实运行列表（本地无 Schedule 时可手工触发一次 Workflow 造数据）。

---

## 4. P1 —— 身份校验收口（安全问题，不是功能问题）

### P1-1 给 `/api/v1/jobs` 补上网关校验

**现状（已实测）**：

```
curl -H "X-Tenant-ID: <任意UUID>" -H "X-User-ID: <任意UUID>" /api/v1/jobs  →  200
```

`app/api/jobs.py` 里 `grep -c "gateway|get_data_loop_principal"` → **0**。
所有端点只用 `get_tenant_id` / `get_user_id` 两个依赖（第 175-176 行等），
而这两个值**完全由客户端通过请求头提供，没有任何凭据背书**。

对比：Data Loop 的 `get_data_loop_principal` 会校验共享 Bearer Token（`dependencies.py:138-143`）。
**同一套系统里，读写方向的安全强度不一致。**

**影响**：任何能访问到 8000 端口的调用方，改一个请求头就能：
读任意租户的稿件、提交任意租户的审批决策、触发任意租户的发布。
在 K8s 里如果 API Service 被同集群其他 Pod 访问到，这就是完整越权。

**做法**：与 P1-2/P0-3 用同一模式——新增 `WritingPermission` 枚举 +
`get_writing_principal`，把 `jobs.py` 12 个端点的 `Depends(get_tenant_id)` 换成
`Depends(require_writing_permission(...))`。

**兼容性风险（重要）**：这是**破坏性变更**。现有前端、`NEXT_STEPS.txt` 里的 curl 脚本、
`tests/` 里的接口测试、`docs/` 里的 runbook **全都只发两个头**。
建议做法：新增配置项 `WRITING_GATEWAY_TOKEN`，
**为空时保持当前宽松行为**（并在启动日志打 WARNING），非空时强制校验。
这样本地开发和已上线环境都不会被打断，生产环境只需注入一个配置。

**验收**：注入 Token 后，不带 `Authorization` 的请求返回 401；带错 Token 返回 401；
带对的返回 200。且写一条测试断言"未配置 Token 时仍兼容旧行为"。

### P1-2 / P1-3 企业 IdP / SSO

`get_data_loop_principal` 的注释写得很清楚：网关必须在转发前
**剥离客户端自带的身份头，再注入自己的值**。这一层现在完全不存在。

需要你提供的信息（我无法替你决定）：
- 企业用的是哪套 IdP（OIDC / CAS / 自建 SSO）？
- 网关是 ingress-nginx + `auth-url`，还是已有的 API 网关？
- `X-Tenant-ID` 从哪里来——IdP 声明里的字段，还是我们自己维护的用户→租户映射表？

前端侧已经按"网关注入"设计好了（`src/api/http.ts` 不设置任何身份头），
`deploy/k8s/frontend-ingress.yaml` 里也预留了 `auth-url` / `auth-signin` 的位置，
**这部分前端不需要改，只缺网关侧的真实配置**。

---

## 5. P2 —— 打通写作链路最后一环（CMS 发布）

**现状**：`app/clients/cms.py:33-34`，`self.url` 为空即抛 `CmsNotConfiguredError`，
`app/api/jobs.py:480` 映射为 **503**。`.env` 里确实没有 `CMS_PUBLISH_URL` / `CMS_PUBLISH_TOKEN`。

**这意味着**：前端「写作任务工作台」的发布按钮必然报错，
`JobStatus.FINAL_APPROVED → PUBLISHED` 这一跳在生产路径上从未跑过。

**要做的事**：

1. `.env` 注入两个键：
   - `CMS_PUBLISH_URL`（企业 CMS 网关的固定地址）
   - `CMS_PUBLISH_TOKEN`（Bearer 凭据，走 Secret 管理，不要进 git）
2. **与企业 CMS 侧对齐契约**。现在 `cms.py` 假定的是：
   - 请求：`POST {url}`，头 `Idempotency-Key: news-writing:{tenant}:{job}:{channel}` +
     `X-Tenant-ID` + `Authorization: Bearer`，
     体 `{"job_id": str, "channel": str, "article": {...}}`
   - 响应：JSON，取 `publication_id` 或 `id` 作为外部发布号（`cms.py:52`）
   如果企业 CMS 的契约不是这样，改的是 `cms.py` 这一个文件，**不要改 `jobs.py`**。
3. `channel` 的取值集合需要确认（现在是从请求体透传的 `str`，
   建议在 `app/schemas/job.py` 收成 `Literal`）。
4. **幂等语义必须落到 CMS 侧**：服务端只在 `Idempotency-Key` 上保证了同一请求的键稳定，
   真正的去重要靠 CMS 支持这个头。如果 CMS 不支持，发布语义是
   **有界 at-least-once**，不能声称 exactly-once（与 `PROJECT_CONTEXT` 第 10 节对
   FastGPT 的同类声明保持一致）。

**验收**：
```bash
# 假设已有一个 FINAL_APPROVED 的 job
curl -X POST -H "Content-Type: application/json" \
  -H "X-Tenant-ID: $TENANT_ID" -H "X-User-ID: $USER_ID" \
  -d '{"channel":"web"}' \
  http://127.0.0.1:8000/api/v1/jobs/$JOB_ID/publish
# 预期：200，含 external_publication_id；重复调用第二次返回 idempotent_replay=true
```
本地没有真实 CMS 时，用 `tools/mock_fastgpt.py` 同样的思路起一个返回
`{"publication_id":"PUB-xxx"}` 的替身即可闭环。

---

## 6. P3 —— Memory 层对外接口

**现状**：领域层已完整——`app/schemas/user_memory.py`、`app/schemas/hot_news_memory.py`、
`app/repositories/user_memory.py`、`app/services/memory_context.py`、
`memory_promotion.py` + `memory_promotion_application.py`、`memory_write_application.py`、
`memory_prompt.py`、`memory_aware_hot_news_analysis.py`。
四张表（`short_term_user_memories`、`long_term_memory_candidates`、
`long_term_user_memories`、`memory_promotion_requests`）与 `0009` 迁移已建立。

**缺的**：`app/api/` 下没有 memory 路由。`PROJECT_CONTEXT` 第 13 节原文：
"Repository、审批人 RBAC/ABAC 授权、API、审计事件、完整幂等事务和真实数据库迁移验证仍未接入"
（其中 Repository 现已存在）。

**建议的最小面**（对齐第 13 节"首个最小闭环"）：
- `GET  /api/v1/memory/short-term`（按 tenant/user/job 过滤）
- `POST /api/v1/memory/short-term`
- `POST /api/v1/memory/promotions`（提交晋升请求）
- `POST /api/v1/memory/promotions/{id}/approve`（**审批，必须独立权限**）
- `GET  /api/v1/memory/long-term`（按作用域过滤：tenant/team/section/role/user）

权限必须与 Data Loop 隔离：审批人角色不能和被审批人角色是同一个。
作用域层级 `tenant_id + team_id + section_id + role_id + user_id` 是硬约束，
**个人记忆永远不能覆盖合规/品牌/安全等上层硬约束**（第 13 节原文）。

**注意**：`app/services/memory_context.py` 的 Resolver 已实现作用域过滤与稳定冲突排序，
API 层**不要重新实现排序**，直接调用 Resolver。

**本轮是否需要做**：Memory 是"持续改进"能力，**不阻塞前端三个模块**。
建议放到热点链路打通之后。

---

## 7. P4 —— 生产化与验收

### P4-1 镜像不可变

`deploy/docker-compose.yml:35` 用 `news-agent/writing-agent-service:dev`。
`DEPLOYMENT.md` 要求"API、Worker、Outbox Worker 必须使用同一个不可变镜像 digest"。
→ CI 构建后按 digest 引用；本地 compose 用 `dev` 无所谓，生产清单必须换。

### P4-2 K8s 清单（当前只有前端）

`deploy/k8s/` 只有 7 个前端文件。需要补：
- `api` Deployment + Service + HPA
- `temporal-worker`、`data-loop-worker`、`outbox-worker`、**`hot-news-worker`** 四个 Deployment
  （**无 Service**，它们不接流量；**无 HPA**，用副本数控制消费能力）
- `migrate` 的 **Job**（替代 compose 里的一次性 `migrate` 服务，用 pre-deploy hook 触发）
- `schedule-runner` 的 Job 或由 `api` 启动时调用（对应 P0-6）
- ConfigMap + Secret（用 `ExternalName`/Secret 引用托管 PG、Redis、对象存储）
- **Ingress 必须拆两个**：`/` 静态 + `/api` 鉴权。ingress-nginx 的 `auth-url` 注解
  **按 Ingress 生效而不是按 path**，合成一个会连静态资源一起拦。
- Ingress 上对 `/api/v1/jobs/{id}/events` **单独关 `proxy-buffering`**，
  `proxy-read-timeout` 拉到 3600s。否则 SSE 会被缓冲，前端看到的是
  "卡在 0% 然后突然 100%"。
- 注入身份头前必须 `more_clear_input_headers`，否则客户端可伪造 `X-Tenant-ID`。

### P4-3 对象存储生产治理

`depends_on: minio` 那套是本地开发。生产要确认：Object Lock（评测数据集不可变）、
生命周期策略、跨区复制、条件写与 SHA-256 核验
（`PROJECT_CONTEXT` 第 10 节提到 S3 条件写与哈希核验已补齐，需要真实存储验证）。

### P4-4 指标扩展

`app/main.py:92` 的 `/metrics` 只导出 `news_agent_jobs` 和 `news_agent_outbox_events`。
`app/observability/hot_news.py` 已有 `HotNewsRunMetrics`，但没有暴露到 `/metrics`。
建议追加：热点运行计数/时长/失败分类、Data Loop 决策计数、
Temporal Workflow 失败数、各 worker 的心跳。

### P4-5 真实环境验收（不能跳过）

- **真实 FastGPT App**：`FASTGPT_HOT_NEWS_APP_ID` 对应的 App 需要配置
  System Prompt 与输出 Schema 约束。当前单元测试用 MockTransport，
  **真实 App 从未联通**。
- **企业数据 Adapter**：`app/clients/enterprise/*` 全部是 RPC 契约，
  没有企业侧服务就无法验证。`docs/enterprise-rpc-integration-checklist.md` 是清单。
- **48 小时审批超时的 time-skipping CI**：Temporal 的 time-skipping 测试环境验收。
- **L3 端到端**：`PROJECT_CONTEXT` 明确"L2 只代表本地依赖闭环，
  L3 真实模型、企业 Gateway/IdP 和企业 Adapter 仍需发布前验收"。
- `pytest` 中 3 项 opt-in E2E 默认跳过，需要专门的依赖栈才能跑。

---

## 8. P5 —— 仓库卫生（低风险，但建议尽早）

1. **`.env` 被 git 跟踪**（`PROJECT_CONTEXT` 第 9 节）。如果里面有真实凭据，
   需要 `git rm --cached` 并**轮换所有已泄漏的密钥**。同时把
   `.venv`、`__pycache__`、`egg-info` 从跟踪中移除。
2. `tencent-news-crawler/crawler/tencent_news.py` 的 `_parse_article_id` 附近有缩进错误。
3. 根 `README.md` 对热点模块进展的描述落后于代码。
4. `app/api/events.py` 的 SSE 只覆盖 `/api/v1/jobs`；热点没有进度事件
   （`app/activities/hot_news.py` 里没有任何 Redis publish）。热点 SSE 属于可选增强。

---

## 9. 全项目 Definition of Done

前端三个模块全部可交互、后端全部可验证时，才算「全部完成」：

**热点模块**
- [ ] `GET /api/v1/hot-news/runs` 返回真实分页数据（含租户隔离）
- [ ] `GET /api/v1/hot-news/runs/{run_id}` 返回报告 + 校验结果
- [ ] `POST /api/v1/hot-news/decisions` 落库并产生 Feedback Case
- [ ] `hot-news-worker` 容器稳定运行，Temporal UI 能看到 Poller
- [ ] Schedule 按窗口周期触发，重叠策略正确
- [ ] 前端 `HotNewsView` 接真实接口，占位徽标移除

**写作模块**
- [ ] 12 个端点全部有前端入口（现状：3 个）
- [ ] `publish` 在配置了 CMS 后返回 `external_publication_id`
- [ ] 事件流可筛选、可滚动、支持断点续传

**Data Loop**
- [ ] 17 个端点全部有前端入口（现状：已完成）
- [ ] 生产环境迁移演练、Object Lock、真实 Adapter 验收

**横向**
- [ ] 写入方向有网关校验（不再是裸 `X-Tenant-ID`）
- [ ] IdP / SSO 接入，网关剥离并注入身份头
- [ ] 镜像按 digest 部署，CI 有 typecheck/test/build/scan
- [ ] K8s 清单覆盖 api + 4 个 worker + migrate Job + Schedule
- [ ] `/metrics` 覆盖热点、Data Loop、worker 心跳
- [ ] 真实 FastGPT App、企业 RPC、48h 超时 time-skipping 三项验收通过

---

## 10. 需要你决策的四个点

在另一个会话开工前，先确认这四件事，否则中途会返工：

| # | 决策点 | 影响 |
| --- | --- | --- |
| 1 | 热点是否复用 `DATA_LOOP_GATEWAY_TOKEN`？角色头是否独立为 `X-Hot-News-Roles`？ | P0-3 权限模型 |
| 2 | Schedule 是每租户一个还是全局一个？租户清单从哪里来？ | P0-6 调度设计 |
| 3 | `/api/v1/jobs` 补网关校验时，"未配置 Token 保持宽松"这个兼容策略是否接受？ | P1 是否破坏现有测试与脚本 |
| 4 | 企业 CMS 的真实契约（端点、字段、`publication_id` 命名、是否支持 `Idempotency-Key`） | P2 是否需要改 `cms.py` |

---

## 附：关键文件索引（执行时直接跳）

| 用途 | 路径 |
| --- | --- |
| 路由注册 | `app/main.py:59-61` |
| 路由导出 | `app/api/__init__.py` |
| 身份/权限依赖（照抄模板） | `app/api/dependencies.py:37-185` |
| Data Loop 路由（17 端点范例） | `app/api/data_loop.py` |
| 写作路由（12 端点） | `app/api/jobs.py` |
| 热点运行表 | `app/models/hot_news.py:21`（表 `analysis_runs`） |
| 热点运行存储 | `app/services/hot_news_run_store.py:23` |
| 热点决策服务 | `app/services/hot_news_decision.py:32` |
| 热点决策契约 | `app/schemas/hot_news_decision.py:6,14` |
| 热点报告契约 | `app/schemas/hot_news.py:165` |
| 热点 Worker（缺 main） | `app/hot_news_worker.py` |
| 热点组合根 | `app/hot_news_bootstrap.py:156` |
| 热点工作流 | `app/workflows/hot_news.py:25` |
| 企业 Adapter 组装 | `app/clients/enterprise/dependencies.py:65` |
| CMS 客户端 | `app/clients/cms.py` |
| 前端热点占位页 | `frontend/src/views/HotNewsView.vue` |
| 前端接口层范例 | `frontend/src/api/dataLoop.ts` |
| 前端开发代理（注入身份头） | `frontend/vite.config.ts` |
| 企业 RPC 对接清单 | `docs/enterprise-rpc-integration-checklist.md` |
| Data Loop L2 验收手册 | `docs/data-loop-e2e-runbook.md` |
