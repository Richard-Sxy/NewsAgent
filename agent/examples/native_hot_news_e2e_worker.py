"""Run the local Temporal hot-news worker with Python-owned model Ports."""

import asyncio

from app.hot_news_worker import run_hot_news_worker
from app.config import get_settings
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.factory import build_model_runtime_ports
from app.db.session import Database
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.tiered_vector import TieredVectorKnowledgeSearchClient, VectorTier
from examples.native_hot_news_e2e_knowledge import seed_native_e2e_knowledge
from examples.hot_news_e2e_support import build_hot_news_e2e_dependencies
from examples.native_hot_news_sql_support import build_native_hot_news_sql_dependencies, seed_native_sql_knowledge
from app.services.active_bundle_hot_news import HotNewsRunDependencies
from app.sql_assistant.bootstrap import build_local_hot_news_sql_service
from app.domain.errors import HotNewsDataQualityError
from app.sql_assistant.warehouse import DEMO_TENANT_ID, ISOLATION_TENANT_ID, demo_news_ids


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


async def run() -> None:
    settings = get_settings()
    config = load_model_runtime_config(settings.model_runtime_config_path)
    ports = build_model_runtime_ports(config, environment=settings.environment)
    dependencies = build_hot_news_e2e_dependencies()
    database = Database(settings)
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
        scaled = sql_service.dataset_profile in {"enterprise-v2", "public-headlines-v3"}
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
            return HotNewsRunDependencies(
                metric_source=sql_dependencies.metric_source,
                baseline_provider=sql_dependencies.baseline_provider,
                content_repository=sql_dependencies.content_repository,
                policy=sql_dependencies.policy,
            )
        index = ProfileScopedKnowledgeIndex(
            knowledge_store,
            allowed_news_ids=demo_news_ids(sql_service.dataset_profile, news_per_tenant=sql_service.news_per_tenant),
            legacy_news_ids=dependencies.news_ids,
        ) if scaled else knowledge_store
        knowledge_search = TieredVectorKnowledgeSearchClient(
            embedder=ModelQueryEmbedder(
                ports.embedding, version=config.embedding.model_routes[0]
            ),
            index=index,
            tiers=(VectorTier.HOT,),
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
        await database.close()
        await ports.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
