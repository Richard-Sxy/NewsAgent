# 热点模块最小启动补丁

> **历史启动补丁，状态更新于 2026-09-18：** 文中 Worker 入口、依赖工厂、Compose 服务、
> 热点 API、前端真实列表和 Schedule 管理已经进入仓库，并完成过本地 Tier 1/L2 验证。
> 本文继续作为复现与故障排查手册，不应再当作“尚待开发的代码清单”。企业 SDK、真实
> FastGPT App、生产 Schedule、Gateway/IdP 和生产基础设施仍需单独验收。

目标：让 `hot-news` 队列在主 compose 栈里**有消费者**，并让热点链路**真正产出数据**，  
使前端 `/hot-news` 能看到真实列表而不是空态。

本文只覆盖「最小可跑通」所需的改动，共 **5 处新增/修改 + 2 处配置**。  
Data Loop 与写作任务两个模块已经就绪，不在本文范围内。

---

## 0. 先读这一节：两个必须知道的约束

### 约束 1：热点 Worker 需要 4 个「企业数据 Port」，仓库里没有实现

`app/hot_news_bootstrap.py::create_hot_news_worker_runtime()` 需要调用方提供：

| 参数                     | 类型（`app/services/hot_news_orchestration.py` / `app/analytics/`） |
| ---------------------- | --------------------------------------------------------------- |
| `behavior_data_source` | `BehaviorDataSource`                                            |
| `baseline_provider`    | `HotNewsBaselineProvider`                                       |
| `content_repository`   | `NewsContentRepository`                                         |
| `policy`               | `HotNewsOrchestrationPolicy`                                    |

这四个的真实实现来自企业 RPC SDK，`app/clients/enterprise/dependencies.py`  
只提供了装配函数 `build_enterprise_hot_news_adapters(clients, config=...)`，  
**而 `clients` 需要企业生成的 SDK 实例**——本仓库没有。  
所以本文的方案是：**在运行期按环境变量解析一个「依赖工厂」**，把企业接线留在部署侧，  
仓库里只放管道。

### 约束 2：`examples` 的场景依赖被时间窗口钉死，不能配 Schedule

这一点容易踩坑，务必先看：

- `examples/hot_news_e2e_support.py` 的行为数据源是 `InMemoryBehaviorDataSource`，  
  它的 `fetch()` 按 `query.start <= event_time < query.end` 过滤  
  （`app/analytics/data_source.py:77`）。
- 场景文件 `examples/data/hot_news_scenario.json` 的 `current_window_start`  
  固定为 `2026-09-03T10:00:00+08:00`，`window_minutes=60`。
- 它的基线提供方 `ScenarioHotNewsBaselineProvider.get_baselines()` 会显式校验  
  `window_start/window_end` 与场景一致，否则抛  
  `ValueError("E2E request window does not match scenario window")`。

而 `HotNewsWindowDispatcherWorkflow` 每次触发计算的是 `[now - window_minutes, now)`。  
**`now` 永远对不上 `2026-09-03T10:00`，所以 Schedule 一触发就必然失败。**

**因此本文分两层：**

- **Tier 1（本次目标，可立即验证）**：用场景工厂 + **手工按场景窗口启动一次 workflow**，  
  不开 Schedule。产出 1 条 `analysis_runs` 记录，前端立刻可见。
- **Tier 2（生产）**：把工厂换成企业 Adapter 工厂，再打开 Schedule。

---

## 1. 代码改动清单

| # | 文件                                      | 动作                            |
| - | --------------------------------------- | ----------------------------- |
| 1 | `app/config.py`                         | 新增 1 个配置字段                    |
| 2 | `app/hot_news_dependencies.py`          | **新建**：依赖工厂解析器                |
| 3 | `app/hot_news_worker.py`                | 新增 `main()`                   |
| 4 | `examples/hot_news_scenario_factory.py` | **新建**：本地场景工厂（Tier 1 用）       |
| 5 | `Dockerfile`                            | 新增 `COPY examples ./examples` |
| 6 | `pyproject.toml`                        | `[project.scripts]` 新增 2 条    |
| 7 | `deploy/docker-compose.yml`             | 新增 2 个服务                      |
| 8 | `.env`                                  | 新增 2 个变量                      |

---

### 1.1 `app/config.py` — 新增配置字段

在 `hot_news_schedule_definitions_json` 那一行下面（当前第 48 行后）插入：

```python
    # 热点 Worker 的依赖装配入口，格式 "package.module:callable"，工厂签名固定为
    # (Settings) -> 依赖束（需含 behavior_data_source / baseline_provider /
    # content_repository / policy 四个属性）。未配置时热点 Worker 拒绝启动
    # （fail-closed），避免生产链路在本地假数据上静默运行。
    hot_news_dependencies_factory: str | None = None
```


### 1.2 `app/hot_news_dependencies.py` — 新建

```python
"""热点 Worker 的依赖装配入口。

热点编排层只依赖四个领域 Port（行为数据源、基线提供方、内容仓储、编排策略），
它们的真实实现来自企业 RPC SDK，不属于本仓库。因此这里只在运行期按配置解析
一个「依赖工厂」，把企业 Adapter 的装配留在部署侧：

    HOT_NEWS_DEPENDENCIES_FACTORY=your_package.hot_news_wiring:build_dependencies

工厂签名固定为 ``(Settings) -> 依赖束``。未配置时直接抛错，不提供任何隐式默认值，
避免生产链路悄悄跑在本地假数据上。
"""

from __future__ import annotations

import importlib
from typing import Protocol

from app.analytics.data_source import BehaviorDataSource
from app.analytics.news_content import NewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.services.hot_news_orchestration import 
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)

REQUIRED_ATTRIBUTES = (
    "behavior_data_source",
    "baseline_provider",
    "content_repository",
    "policy",
)


class HotNewsDependencyConfigError(RuntimeError):
    """依赖工厂未配置、路径非法，或返回值不满足依赖束契约。"""


class HotNewsDependencyBundle(Protocol):
    """热点 Worker 启动所需的最小依赖束。

    ``examples.hot_news_e2e_support.HotNewsE2EDependencies`` 天然满足该协议；
    企业侧工厂只需返回一个带这四个属性的对象即可（``knowledge_search`` 可选）。
    """

    behavior_data_source: BehaviorDataSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: NewsContentRepository
    policy: HotNewsOrchestrationPolicy
    knowledge_search: KnowledgeSearchClient | None


def resolve_dependency_factory(target: str):
    """把 ``"package.module:callable"`` 解析为可调用对象。"""

    module_path, separator, attribute = target.partition(":")
    if not separator or not module_path.strip() or not attribute.strip():
        raise HotNewsDependencyConfigError(
            "HOT_NEWS_DEPENDENCIES_FACTORY 必须是 'package.module:callable' 形式，"
            f"当前值为 {target!r}"
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise HotNewsDependencyConfigError(
            f"无法导入模块 {module_path!r}：{exc}"
        ) from exc

    factory = getattr(module, attribute, None)
    if factory is None:
        raise HotNewsDependencyConfigError(
            f"模块 {module_path!r} 中不存在属性 {attribute!r}"
        )
    if not callable(factory):
        raise HotNewsDependencyConfigError(
            f"HOT_NEWS_DEPENDENCIES_FACTORY 解析出的 {target!r} 不可调用"
        )
    return factory


def build_hot_news_dependencies(settings: Settings) -> HotNewsDependencyBundle:
    """按配置装配热点 Worker 依赖；未配置则 fail-closed。"""

    target = (settings.hot_news_dependencies_factory or "").strip()
    if not target:
        raise HotNewsDependencyConfigError(
            "HOT_NEWS_DEPENDENCIES_FACTORY 未配置，热点 Worker 拒绝启动。"
            "生产环境指向企业 Adapter 工厂；本地联调可指向 "
            "examples.hot_news_scenario_factory:build_from_scenario。"
        )

    factory = resolve_dependency_factory(target)
    try:
        bundle = factory(settings)
    except TypeError as exc:
        raise HotNewsDependencyConfigError(
            f"{target} 调用失败：工厂签名必须接受一个位置参数 Settings（{exc}）"
        ) from exc

    missing = [name for name in REQUIRED_ATTRIBUTES if not hasattr(bundle, name)]
    if missing:
        raise HotNewsDependencyConfigError(
            f"{target} 返回的对象缺少必需属性：{', '.join(missing)}"
        )
    return bundle
```


### 1.3 `app/hot_news_worker.py` — 新增 `main()`

**（1）文件顶部补两个 import。** 现在第 1-19 行是：

```python
from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker
```

改成：

```python
from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker
```

再在 `from app.hot_news_bootstrap import create_hot_news_worker_runtime` 下面加一行：

```python
from app.hot_news_dependencies import build_hot_news_dependencies
```

**（2）文件末尾追加 `main()`。** 现在文件以 `await runtime.close()` 结束（第 64 行），追加：

```python


def main() -> None:
    """容器入口：解析依赖工厂并常驻消费 hot-news 队列。

    依赖装配失败时直接退出非 0，不降级、不回退到假数据。
    """
    settings = get_settings()
    dependencies = build_hot_news_dependencies(settings)
    asyncio.run(
        run_hot_news_worker(
            behavior_data_source=dependencies.behavior_data_source,
            baseline_provider=dependencies.baseline_provider,
            content_repository=dependencies.content_repository,
            policy=dependencies.policy,
            knowledge_search=getattr(dependencies, "knowledge_search", None),
            settings=settings,
        )
    )


if __name__ == "__main__":
    main()
```


### 1.4 `examples/hot_news_scenario_factory.py` — 新建

Tier 1 的工厂实现。它把 E2E 依赖束转成带 `knowledge_search` 的完整依赖束——  
补这一步是必要的，否则 `create_hot_news_runtime` 会退回到  
`FastGPTKnowledgeSearchClient`（走真实 FastGPT 知识库检索接口）。

```python
"""把仓库内的离线场景装配为热点 Worker 依赖束（仅限本地联调）。

生产环境不要使用：行为数据、基线与正文全部来自
``examples/data/hot_news_scenario.json``，不具备任何企业数据语义。

注意：本工厂产出的行为源与基线提供方都**绑定场景固定窗口**
（``2026-09-03T10:00:00+08:00``，60 分钟），因此只能配合
``python -m examples.start_hot_news_workflow`` 这类「按场景窗口手工触发」的方式使用，
**不能**接入 ``HOT_NEWS_SCHEDULE_DEFINITIONS_JSON`` 的周期调度。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.data_source import InMemoryBehaviorDataSource
from app.analytics.news_content import InMemoryNewsContentRepository
from app.clients.knowledge_base import KnowledgeSearchClient
from app.config import Settings
from app.services.hot_news_orchestration import (
    HotNewsBaselineProvider,
    HotNewsOrchestrationPolicy,
)
from examples.hot_news_e2e_knowledge import OfflineScenarioKnowledgeSearchClient
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies


@dataclass(frozen=True, slots=True)
class ScenarioHotNewsDependencies:
    """满足 HotNewsDependencyBundle 协议的本地场景依赖束。"""

    behavior_data_source: InMemoryBehaviorDataSource
    baseline_provider: HotNewsBaselineProvider
    content_repository: InMemoryNewsContentRepository
    policy: HotNewsOrchestrationPolicy
    knowledge_search: KnowledgeSearchClient


def build_from_scenario(settings: Settings) -> ScenarioHotNewsDependencies:
    """HOT_NEWS_DEPENDENCIES_FACTORY 的本地联调实现。

    ``settings`` 未使用，保留它是为了满足统一工厂签名。
    """

    _ = settings
    dependencies = build_hot_news_e2e_dependencies()
    return ScenarioHotNewsDependencies(
        behavior_data_source=dependencies.behavior_data_source,
        baseline_provider=dependencies.baseline_provider,
        content_repository=dependencies.content_repository,
        policy=dependencies.policy,
        knowledge_search=OfflineScenarioKnowledgeSearchClient(
            dependencies.content_repository,
            dependencies.news_ids,
        ),
    )
```

### 1.5 `Dockerfile` — 新增一行

E2E 镜像 `Dockerfile.e2e` 第 26 行有 `COPY examples ./examples`，主镜像没有。  
不加这行，1.4 的工厂在容器里导入会失败。

在 `COPY app ./app`（第 13 行）之后插入：

```dockerfile
COPY examples ./examples
```

> Tier 2 改用企业工厂后，如果该工厂不在 `examples/` 下，可以去掉这一行。

### 1.6 `pyproject.toml` — 新增两条脚本

把第 32-35 行：

```toml
[project.scripts]
writing-agent-worker = "app.worker:main"
writing-agent-outbox-worker = "app.outbox_worker:main"
writing-agent-data-loop-worker = "app.data_loop_worker:main"
```

改为：

```toml
[project.scripts]
writing-agent-worker = "app.worker:main"
writing-agent-outbox-worker = "app.outbox_worker:main"
writing-agent-data-loop-worker = "app.data_loop_worker:main"
writing-agent-hot-news-worker = "app.hot_news_worker:main"
writing-agent-hot-news-scheduler = "app.hot_news_scheduler:main"
```


### 1.7 `deploy/docker-compose.yml` — 新增两个服务

插到 `data-loop-worker`（第 161-173 行）之后、`outbox-worker` 之前。

```yaml
  # 热点监控 Worker：消费 TEMPORAL_HOT_NEWS_TASK_QUEUE（默认 hot-news）。
  # 依赖由 HOT_NEWS_DEPENDENCIES_FACTORY 在运行期解析，未配置则启动即失败。
  hot-news-worker:
    <<: *writing-agent-service
    depends_on:
      migrate:
        condition: service_completed_successfully
      temporal:
        condition: service_started
    command: ["python", "-m", "app.hot_news_worker"]
    deploy:
      resources:
        limits:
          cpus: "${HOT_NEWS_WORKER_CPU_LIMIT:-2.0}"
          memory: "${HOT_NEWS_WORKER_MEMORY_LIMIT:-2G}"

  # Schedule provisioning：一次性任务，按 HOT_NEWS_SCHEDULE_DEFINITIONS_JSON
  # 幂等创建/更新每个租户组的 Schedule，成功后退出。定义为空时直接返回 0。
  hot-news-scheduler:
    <<: *writing-agent-service
    restart: "no"
    depends_on:
      migrate:
        condition: service_completed_successfully
      temporal:
        condition: service_started
      hot-news-worker:
        condition: service_started
    command: ["python", "-m", "app.hot_news_scheduler", "ensure"]
```

### 1.8 `.env` — 新增两个变量

```ini
# 热点 Worker 依赖工厂。Tier 1 用本地场景；Tier 2 换成企业 Adapter 工厂，
# 形如 your_package.hot_news_wiring:build_dependencies
HOT_NEWS_DEPENDENCIES_FACTORY=examples.hot_news_scenario_factory:build_from_scenario

# 热点周期调度定义。留空 [] 表示不创建任何 Schedule（Tier 1 请保持为空）。
# tier 2 示例：
# HOT_NEWS_SCHEDULE_DEFINITIONS_JSON=[{"tenant_group":"local","tenant_id":"11111111-1111-4111-8111-111111111111","production_bundle_version":"bundle-v1","window_minutes":60,"interval_minutes":60,"paused":false}]
HOT_NEWS_SCHEDULE_DEFINITIONS_JSON=[]
```

---


## 2. Tier 1 启动顺序（今天就能验证）

前置：`HOT_NEWS_RUNTIME_MANIFEST_JSON` 已配置（现有 `.env` 里已有 1 条完整的  
`ProductionBundleSpec`）。该清单就是 Worker 的**不可变运行清单**——注册表按  
`bundle_spec_sha256(spec)` 建索引，所以第 3 步 bootstrap 提交的 `spec` 必须与  
清单里那一条**内容完全一致**，否则 `ensure_supported()` 会拒绝  
（`app/services/production_bundle_runtime.py:126`）。

> 观察项（不阻塞 Tier 1）：当前 `.env` 的 `FASTGPT_HOT_NEWS_APP_ID` 与清单里的  
> `fastgpt_app_id` **不一致**。前者只在 `create_hot_news_runtime` 里做 fail-closed  
> 守卫与默认值，实际执行用的是活动生产包里那条 spec 的 `fastgpt_app_id`；  
> 配合 mock FastGPT 时任意 app id 都能通。切真实 FastGPT 前建议核对这两处。

```bash
cd writing-agent-service

# 1) 重建镜像（含新的 main()、依赖模块、examples/）
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 \
  docker compose --env-file .env -f deploy/docker-compose.yml build api

# 2) 起热点 Worker，确认它没有因为缺工厂而退出
docker compose --env-file .env -f deploy/docker-compose.yml up -d hot-news-worker
docker logs -f news-agent-hot-news-worker-1
# 预期：无 traceback；Temporal UI (http://127.0.0.1:8233) 的
#       default namespace → Task Queues → hot-news 能看到 Poller

# 3) 引导该租户的「生产包」——ActiveProductionBundleHotNewsService 必须有
#    active bundle 才能解析 production_bundle_version，否则 run() 直接抛错。
#
#    请求体是 {"bundle_version": str, "spec": ProductionBundleSpec}，
#    其中 spec 必须与 HOT_NEWS_RUNTIME_MANIFEST_JSON 里那一条内容完全一致。
#    最省事的做法：从 .env 里把清单抽出来，直接拼请求体。
T="11111111-1111-4111-8111-111111111111"
U="22222222-2222-4222-8222-222222222222"

python3 - <<'PY' > /tmp/bootstrap-bundle.json
import json, pathlib, re
env = pathlib.Path(".env").read_text(encoding="utf-8")
manifest = json.loads(
    re.search(r"^HOT_NEWS_RUNTIME_MANIFEST_JSON=(.*)$", env, re.M).group(1)
)
print(json.dumps({"bundle_version": "bundle-v1", "spec": manifest[0]},
                 ensure_ascii=False))
PY

curl -s -X POST "http://127.0.0.1:8000/api/v1/data-loop/production-bundles/bootstrap" \
  -H "X-Tenant-ID: $T" -H "X-User-ID: $U" \
  -H "X-Data-Loop-Roles: data-loop:admin" \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  --data @/tmp/bootstrap-bundle.json
# 预期 201 Created，body 形如 {"bundle": {...}, "created": true}。
# 若报 "production bundle snapshot is not registered ..." → spec 与清单不一致。
# 也可以走前端：Data Loop → 生产包 → 「引导生产包」，把同一个 spec JSON 粘进输入框。

# 4) 按场景固定窗口手工触发一次热点运行。
#    用 compose run 起临时容器，确保用的是重建后的镜像与环境变量。
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm hot-news-worker python -m examples.start_hot_news_workflow
# 预期：打印 starting_hot_news_workflow 与 hot_news_workflow_completed，
#       其中 status/completed 字段表示本次是否落库成功。

# 5) 验证接口与前端
curl -s -H "X-Tenant-ID: $T" -H "X-User-ID: $U" \
  -H "X-Hot-News-Roles: hot-news:admin" -H "Authorization: Bearer local-dev-token" \
  "http://127.0.0.1:8000/api/v1/hot-news/runs?limit=5"
# 预期：runs 数组非空，含 run_id / window / hot_score 等字段

cd frontend && npm run dev    # 打开 http://127.0.0.1:5173/hot-news
```

**注意第 4 步的幂等性**：`start_hot_news_workflow` 用  
`HotNewsRunRequest.idempotency_key` 作为 Workflow ID，并带  
`WorkflowIDReusePolicy.ALLOW_DUPLICATE`。重复执行不会报错，但  
`HotNewsRunStore.get_completed` 会命中幂等键并**重放**既有结果，  
不会产生第二条 `analysis_runs`。若要造出多条数据，需要改场景窗口。

---


## 3. Tier 2 切到生产

1. 写企业工厂（返回的对象只需带那四个属性，`knowledge_search` 可选）：
   ```python
   # your_package/hot_news_wiring.py
   from dataclasses import dataclass

   from app.clients.enterprise.dependencies import (
       EnterpriseHotNewsAdapterConfig, build_enterprise_hot_news_adapters,
   )
   from app.services.hot_news_orchestration import HotNewsOrchestrationPolicy

   @dataclass(frozen=True, slots=True)
   class HotNewsDependencies:
       behavior_data_source: object
       baseline_provider: object
       content_repository: object
       policy: HotNewsOrchestrationPolicy
       knowledge_search: object | None = None

   def build_dependencies(settings):
       clients = build_your_rpc_clients(settings)   # 企业 SDK 实例，本仓库不提供
       adapters = build_enterprise_hot_news_adapters(
           clients,
           config=EnterpriseHotNewsAdapterConfig(
               behavior_dataset="...",
               production_bundle_version="bundle-v1",
               baseline_policy_version="...",
               retrieval_policy_version="...",
           ),
       )
       return HotNewsDependencies(
           behavior_data_source=adapters.behavior_data_source,
           baseline_provider=adapters.baseline_provider,
           content_repository=adapters.content_repository,
           policy=HotNewsOrchestrationPolicy(...),
           knowledge_search=adapters.related_news_search,
       )
   ```
2. `.env` 把 `HOT_NEWS_DEPENDENCIES_FACTORY` 换成上面这个路径。
3. 配 `HOT_NEWS_SCHEDULE_DEFINITIONS_JSON`，然后：
   ```bash
   docker compose --env-file .env -f deploy/docker-compose.yml \
     run --rm hot-news-scheduler
   ```
   生产环境把这一步换成 K8s `Job`（或 Helm pre-install hook），不要常驻重启。

---

## 4. 验收清单

- [ ] `docker logs news-agent-hot-news-worker-1` 无 traceback，Temporal UI 能看到 `hot-news` 队列 Poller
- [ ] `GET /api/v1/hot-news/runs` 返回非空 `runs`
- [ ] 前端 `http://127.0.0.1:5173/hot-news` 列表出现记录，点开能看到报告详情
- [ ] （Tier 2）`python -m app.hot_news_scheduler list` 能列出已配置的租户组
- [ ] （Tier 2）`HOT_NEWS_DEPENDENCIES_FACTORY` 指向企业工厂后，`hot-news-worker` 仍能启动

---

## 5. 容易失败的地方

| 现象                                                                                                     | 原因                                                  | 处置                                                                                       |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Worker 启动即退出，日志含 `HOT_NEWS_DEPENDENCIES_FACTORY 未配置`                                                   | 第 1.8 步没做，**或该变量没写进 compose 的 `environment:`**（`.env` 只做 `${}` 插值，不进容器） | 补 `.env` + 在 `x-writing-agent-env` 里加 `HOT_NEWS_DEPENDENCIES_FACTORY: ${HOT_NEWS_DEPENDENCIES_FACTORY:-}` |
| Worker 退出，`ModuleNotFoundError: examples`                                                              | 第 1.5 步 `COPY examples` 没加                          | 补 `Dockerfile` 后重建镜像                                                                     |
| `FASTGPT_HOT_NEWS_APP_ID is required`                                                                  | 该变量为空                                               | `create_hot_news_runtime` 在 `app/hot_news_bootstrap.py:112-113` fail-closed              |
| bootstrap 报 `production bundle snapshot is not registered in this worker's immutable runtime manifest` | 提交的 `spec` 与 `HOT_NEWS_RUNTIME_MANIFEST_JSON` 内容不一致 | 用第 3 步的脚本从 `.env` 直接生成请求体，不要手写 spec                                                      |
| 第 4 步报 `E2E request window does not match scenario window`                                             | 用了自定义窗口，或误开了 Schedule                               | Tier 1 必须走 `examples.start_hot_news_workflow`，并把 `HOT_NEWS_SCHEDULE_DEFINITIONS_JSON` 留空 |
| `failed to resolve the tenant's active production bundle`                                              | 第 3 步没做，或 `tenant_id` 不一致                           | 先 bootstrap，并确认 `tenant_id` 与场景一致（`11111111-1111-4111-8111-111111111111`）                |
| 重复执行第 4 步不产生新数据                                                                                        | 幂等键命中，属设计行为                                         | 需要多条数据就改场景窗口，或切 Tier 2                                                                   |

---

## 6. 实跑 Tier 1 后补的 3 个修复（2026-09-11 实测）

按上面 §2 走完后，链路会在第 4 步失败。实测补齐以下三处后才真正跑通。

### 6.1 `HOT_NEWS_DEPENDENCIES_FACTORY` 必须写进 compose 的 `environment:`

`.env` 里的变量只用于 compose 文件的 `${}` 插值，**不会自动进入容器**，
本 compose 也没有 `env_file:`。只在 `.env` 里写 → 容器内该变量为 `None` → worker fail-closed 退出。

在 `deploy/docker-compose.yml` 的 `x-writing-agent-env` 锚点里补：

```yaml
  HOT_NEWS_DEPENDENCIES_FACTORY: ${HOT_NEWS_DEPENDENCIES_FACTORY:-}
```

### 6.2 `feedback_cases.analysis_output_snapshot` 的 JSONB 语义（真实 bug）

`collect_analysis_failure` 故意传 `analysis_output_snapshot=None`（校验失败时丢弃原始输出），
但 SQLAlchemy 的 `JSONB` 默认把 Python `None` 序列化成 **JSON 字面量 `'null'`**，
而约束 `ck_feedback_cases_output_snapshot_object` 是：

```sql
CHECK (analysis_output_snapshot IS NULL OR jsonb_typeof(analysis_output_snapshot) = 'object')
```

`jsonb_typeof('null'::jsonb) = 'null'` → 被拒，报
`CheckViolation: new row for relation "feedback_cases" violates check constraint ...`。

修复（`app/models/analysis_feedback.py` 与 `app/models/evaluation_dataset.py` 各一处）：

```diff
-        JSONB, nullable=True
+        JSONB(none_as_null=True), nullable=True
```

**凡是 `nullable=True` 的 JSONB 列都必须加 `none_as_null=True`**，否则永远存不进 SQL NULL。
验证方法（无需造数）：

```python
from sqlalchemy import func, literal, select
expr = func.jsonb_typeof(literal(None, Col.type))
# 返回 None（而非 'null'）即为正确
```

### 6.3 mock FastGPT 缺 `HotNewsAnalysisReport` 分支（真实 bug）

`tools/mock_fastgpt.py` 是通用 schema faker，`fix_cross_field_refs` 只处理了写作链的 5 个模型。
热点报告输出 `news_id="mock-id"`，被 `HotNewsAnalysisValidator._validate_news_id` 拒收：

```
output news_id does not match trusted input: expected='20260827A0C5VA00', actual='mock-id'
```

> ⚠️ 这个真实原因**不会出现在日志里**：`HotNewsActivities.run_hot_news_window` 的
> `except` 分支会先尝试写 feedback case，写库失败后把 `reported_error` 覆盖成 DB 错误，
> 所以看到的只有 `CheckViolation`。先用 6.2 修掉写库，或写探针直接调
> `runtime.hot_news_runtime.service.run(request)` 才能看到原始校验错误。

修复：`extract_context` 补 `news_id` / `_related_news_ids` / `_memory_ids`，
并在 `fix_cross_field_refs` 增加 `HotNewsAnalysisReport` 分支（整体构造而非随机填充，
因为它的身份/证据/无证据三类约束相互耦合）。

该文件是 **bind mount** 进 `fastgpt-app` 容器的（`-v tools/mock_fastgpt.py:/mock_fastgpt.py:ro`），
所以改完 **只需 `docker restart fastgpt-app`，不用重建镜像**；
但 `app/` 下的改动（6.2）被打包进镜像，必须 rebuild：

```bash
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 \
  docker compose --env-file .env -f deploy/docker-compose.yml build api
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

### 6.4 前端 dev 代理的租户与后端数据不是同一个（会让 UI "200 但空列表"）

`frontend/.env.development` 原先写的是 `11111111-1111-1111-1111-111111111111`，
而场景/测试用的是 `11111111-1111-4111-8111-111111111111`（见
`examples/hot_news_e2e_support.py::E2E_TENANT_ID`）。两者不是同一个 UUID，
接口返回 **200 但 `runs` 为空**，极易误判成"后端没数据"。

```diff
-VITE_DEV_TENANT_ID=11111111-1111-1111-1111-111111111111
-VITE_DEV_USER_ID=22222222-2222-2222-2222-222222222222
+VITE_DEV_TENANT_ID=11111111-1111-4111-8111-111111111111
+VITE_DEV_USER_ID=22222222-2222-4222-8222-222222222222
```

改完必须**重启 dev server**（vite 启动时读 env）。

### 6.5 跑通后的期望结果

| 检查项 | 期望 |
| --- | --- |
| 触发命令输出 | `hot_news_workflow_completed`，`status=completed`，`analyzed_news_count=3` |
| `GET /api/v1/hot-news/runs` | `runs` 数组 1 条，`status=completed` |
| `GET /api/v1/hot-news/runs/{run_id}` | `ranked_news` 3 条 |
| `GET /api/v1/data-loop/feedback-cases` | 3 条，`source_type=low_confidence`（mock 给的 confidence=0.5 低于阈值） |
| 前端 `http://127.0.0.1:5173/hot-news` | 列表出现 1 条运行记录，点开有排行与报告 |
