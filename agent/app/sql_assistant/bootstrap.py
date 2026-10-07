"""组装运行所需的对象。初始化演示数仓、审计存储、任务绑定，再把模型客户端与数据库客户端注入 SqlAssistantService。当前入口限定在隔离点 e2e 环境。"""

from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.core import StructuredInferenceService
from app.sql_assistant.audit import PostgresQuerySnapshotStore, initialize_query_audit
from app.sql_assistant.hot_news_binding import HotNewsSqlBindingStore
from app.sql_assistant.scenarios import load_sql_scenarios
from app.sql_assistant.service import SqlAssistantService
from app.sql_assistant.warehouse import LocalPostgresSqlWarehouseClient, initialize_demo_warehouse, warehouse_contract


async def build_local_hot_news_sql_service(*, database, settings, model_config, model_ports) -> SqlAssistantService:
    if settings.environment != "e2e":
        raise ValueError("local hot-news SQL tool requires the isolated E2E environment")
    config = load_sql_scenarios(settings.sql_assistant_scenarios_path)
    model_config.agent_scene(config.model_scene)
    dataset_profile = getattr(settings, "sql_assistant_dataset_profile", "classic-v1")
    news_per_tenant = getattr(settings, "sql_assistant_news_per_tenant", 1200)
    if config.warehouse_schema_version != warehouse_contract(dataset_profile).version:
        raise ValueError("local SQL scenario warehouse version differs from its dataset profile")
    await initialize_demo_warehouse(database, environment=settings.environment, dataset_profile=dataset_profile,
                                    news_per_tenant=news_per_tenant)
    await initialize_query_audit(database, environment=settings.environment)
    bindings = HotNewsSqlBindingStore(database)
    await bindings.initialize(environment=settings.environment)
    return SqlAssistantService(
        agent=NativeStructuredAgentClient(
            StructuredInferenceService(inference=model_ports.inference, prompts=model_ports.prompts),
            model_config,
        ),
        store=PostgresQuerySnapshotStore(database),
        warehouse_factory=lambda tenant_id: LocalPostgresSqlWarehouseClient(
            database, tenant_id=tenant_id, dataset_profile=dataset_profile, news_per_tenant=news_per_tenant),
        scenarios_path=settings.sql_assistant_scenarios_path,
        model_provider=model_config.inference.provider,
        dataset_profile=dataset_profile,
        news_per_tenant=news_per_tenant,
        hot_news_bindings=bindings,
    )
