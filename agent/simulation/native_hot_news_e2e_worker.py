"""Run the local Temporal hot-news worker with Python-owned model Ports."""

import asyncio

from app.hot_news_worker import run_hot_news_worker
from app.config import get_settings
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.factory import build_model_runtime_ports
from app.db.session import Database
from app.modules.knowledge.postgres_store import PostgresKnowledgeStore
from app.modules.knowledge.search_factory import build_knowledge_search_index, close_knowledge_search_index
from app.model_runtime.knowledge_factory import build_native_knowledge_search
from simulation.native_hot_news_e2e_knowledge import seed_native_e2e_knowledge
from simulation.hot_news_e2e_support import build_hot_news_e2e_dependencies
from simulation.native_hot_news_sql_support import build_native_hot_news_sql_dependencies, seed_native_sql_knowledge
from app.modules.hot_news.application.active_bundle_hot_news import HotNewsRunDependencies
from app.sql_assistant.bootstrap import build_local_hot_news_sql_service
from app.domain.errors import HotNewsDataQualityError
from app.sql_assistant.warehouse import DEMO_TENANT_ID, ISOLATION_TENANT_ID, STREAMED_PROFILES, demo_news_ids


class ProfileScopedKnowledgeIndex:
    """Restrict local v2 recall to the fixture's approved news ID directory."""

    def __init__(self, store, *, allowed_news_ids, legacy_news_ids=()):
        self.store = store
        self.allowed_news_ids = frozenset(allowed_news_ids)
        self.legacy_news_ids = frozenset(legacy_news_ids)

    async def search(self, *, tenant_id, exclude_news_ids, **arguments):
        if tenant_id not in {str(DEMO_TENANT_ID), str(ISOLATION_TENANT_ID)}:
            raise HotNewsDataQualityError("SQL knowledge query crossed synthetic tenant scope")
        hits = await self.store.search(
            tenant_id=tenant_id, exclude_news_ids=exclude_news_ids | self.legacy_news_ids,
            **arguments,
        )
        return [hit for hit in hits if hit.news_id in self.allowed_news_ids]


class ProfileScopedContentRepository:
    """Only hydrate external recall IDs that belong to the local SQL fixture."""

    def __init__(self, store, *, tenant_id: str, allowed_news_ids) -> None:
        if not tenant_id.strip():
            raise ValueError("synthetic content tenant_id cannot be empty")
        self.store = store
        self.tenant_id = tenant_id
        self.allowed_news_ids = frozenset(allowed_news_ids)

    async def batch_get_by_news_ids(self, *, tenant_id: str, news_ids: tuple[str, ...]):
        # Check identity before filtering: an empty/foreign candidate list does
        # not grant access to another tenant's synthetic content repository.
        if tenant_id != self.tenant_id:
            raise HotNewsDataQualityError("SQL content query crossed its synthetic tenant scope")
        requested = tuple(dict.fromkeys(
            news_id for news_id in news_ids if news_id in self.allowed_news_ids
        ))
        if not requested:
            return {}
        contents = await self.store.batch_get_by_news_ids(
            tenant_id=tenant_id, news_ids=requested,
        )
        if set(contents) - set(requested):
            raise HotNewsDataQualityError("synthetic content repository returned unrequested news_ids")
        if any(content.news_id != news_id for news_id, content in contents.items()):
            raise HotNewsDataQualityError("synthetic content repository returned mismatched news identity")
        return contents


async def run() -> None:
    settings = get_settings()
    config = load_model_runtime_config(settings.model_runtime_config_path)
    ports = build_model_runtime_ports(config, environment=settings.environment)
    dependencies = build_hot_news_e2e_dependencies()
    database = Database(settings)
    search_index = None
    try:
        sql_service = await build_local_hot_news_sql_service(
            database=database, settings=settings, model_config=config, model_ports=ports,
        )
        knowledge_store = PostgresKnowledgeStore(
            database=database,
            embedding=ports.embedding,
            embedding_version=config.embedding.model_routes[0],
            embedding_batch_size=config.embedding.max_batch_size,
        )
        scaled = sql_service.dataset_profile in STREAMED_PROFILES
        if not scaled:
            await seed_native_e2e_knowledge(dependencies, store=knowledge_store)

        async def build_sql_run_dependencies(request):
            await sql_service.hot_news_bindings.require(
                run_key=request.idempotency_key, query_id=request.sql_query_id,
                tenant_id=request.tenant_id, user_id=request.sql_query_user_id,
                production_bundle_version=request.production_bundle_version,
                scope_sha256=request.sql_query_scope_sha256,
                window_start=request.window_start, window_end=request.window_end,
            )

            async def execute_for_run(query_id):
                return await sql_service.execute_for_hot_news(query_id, request=request)

            sql_dependencies = await build_native_hot_news_sql_dependencies(
                database=database, sql_service=sql_service, query_id=request.sql_query_id,
                tenant_id=request.tenant_id, user_id=request.sql_query_user_id,
                production_bundle_version=request.production_bundle_version,
                execute_for_run=execute_for_run,
            )
            if sql_dependencies.window_start != request.window_start or sql_dependencies.window_end != request.window_end:
                raise HotNewsDataQualityError("SQL tool does not target this hot-news window")
            await seed_native_sql_knowledge(sql_dependencies, store=knowledge_store)
            content_repository = sql_dependencies.content_repository
            if search_index is not knowledge_store:
                content_repository = ProfileScopedContentRepository(
                    content_repository,
                    tenant_id=request.tenant_id,
                    allowed_news_ids=demo_news_ids(
                        sql_service.dataset_profile, news_per_tenant=sql_service.news_per_tenant,
                    ),
                )
            return HotNewsRunDependencies(
                metric_source=sql_dependencies.metric_source,
                baseline_provider=sql_dependencies.baseline_provider,
                content_repository=content_repository,
                policy=sql_dependencies.policy,
            )
        search_index = build_knowledge_search_index(
            config_path=settings.knowledge_search_config_path, postgres_store=knowledge_store,
        )
        # Fixture ID filtering belongs to the synthetic PostgreSQL corpus. An
        # explicitly selected external snapshot has its own tenant/ID boundary.
        index = ProfileScopedKnowledgeIndex(
            search_index,
            allowed_news_ids=demo_news_ids(sql_service.dataset_profile, news_per_tenant=sql_service.news_per_tenant),
            legacy_news_ids=dependencies.news_ids,
        ) if scaled and search_index is knowledge_store else search_index
        knowledge_search = build_native_knowledge_search(
            config=config, embedding=ports.embedding, database=database, index=index,
        )
        await run_hot_news_worker(
            behavior_data_source=dependencies.behavior_data_source,
            baseline_provider=dependencies.baseline_provider,
            content_repository=dependencies.content_repository,
            policy=dependencies.policy,
            knowledge_search=knowledge_search,
            inference_port=ports.inference,
            prompt_registry=ports.prompts,
            settings=settings,
            run_dependencies_factory=build_sql_run_dependencies,
        )
    finally:
        await close_knowledge_search_index(search_index)
        await database.close()
        await ports.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
