"""Opt-in assembly shared by API, hotspot Worker and indexing Worker."""
from dataclasses import dataclass
import math
import os

from app.knowledge.tier_lifecycle import KnowledgeLifecycleService
from app.knowledge.tier_lifecycle_config import load_knowledge_lifecycle_config
from app.knowledge.tier_lifecycle_index import GuardedTieredIndex
from app.knowledge.tier_lifecycle_store import TierLifecycleStore
from app.knowledge.tier_lifecycle_query import LifecycleKnowledgeSearch
from app.knowledge.search_config import load_knowledge_search_config
from app.model_runtime.query_embedding import ModelQueryEmbedder
from app.retrieval.tiered_vector import VectorTier


@dataclass
class LifecycleRuntime:
    service: KnowledgeLifecycleService
    index: GuardedTieredIndex
    query: LifecycleKnowledgeSearch

    async def validate(self):
        """Startup preflight does not imply Worker consumption or model quality."""
        await self.service.ledger.list_pending(limit=1)
        await self.service.ledger.list_news(limit=1, include_retracted=True)
        return await self.index.validate_collections(
            expected_dimensions=self.service.config.expected_embedding_dimensions,
            query_max_candidates=self.service.config.query_max_candidates)


def build_lifecycle_runtime(*, lifecycle_config_path, search_config_path, model_config,
                            embedding, database, environment):
    cfg = load_knowledge_lifecycle_config(lifecycle_config_path)
    endpoint = model_config.embedding
    if cfg.embedding_version != endpoint.model_routes[0]:
        raise ValueError("lifecycle embedding_version must match model runtime route")
    if endpoint.expected_embedding_dimensions is not None and endpoint.expected_embedding_dimensions != cfg.expected_embedding_dimensions:
        raise ValueError("lifecycle/model embedding dimensions differ")
    if endpoint.provider == "local" and endpoint.local_embedding_dimensions != cfg.expected_embedding_dimensions:
        raise ValueError("lifecycle/local embedding dimensions differ")
    if cfg.embedding_batch_size > endpoint.max_batch_size:
        raise ValueError("lifecycle embedding batch exceeds endpoint limit")
    search = load_knowledge_search_config(search_config_path) if search_config_path else None
    if search is None or search.backend == "postgres":
        from app.knowledge.tier_lifecycle_local_index import LocalLifecycleVectorIndex
        backend = LocalLifecycleVectorIndex(database=database, max_search_rows=cfg.max_search_rows,
                                            environment=environment)
    elif search.backend == "milvus":
        from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
        connection = search.milvus
        if set(connection.collections) != set(VectorTier):
            raise ValueError("lifecycle requires three distinct hot/warm/cold collections")
        ef = connection.search_params.get("ef")
        if "ef" in connection.search_params and (type(ef) is not int or ef < cfg.query_max_candidates):
            raise ValueError("HNSW ef must cover lifecycle query_max_candidates including refill")
        token = os.environ.get(connection.token_env) if connection.token_env else None
        if connection.token_env and (not token or not token.strip()):
            raise ValueError("Milvus token environment variable is missing or empty")
        backend = MilvusTieredVectorIndex.connect(uri=connection.uri, token=token,
            database=connection.database, collections=connection.collections,
            timeout_seconds=connection.timeout_seconds, metric_type=connection.metric_type,
            search_params=connection.search_params, consistency_level="Strong",
            rpc_max_concurrency=cfg.query_max_concurrency * 3)
    else:
        raise ValueError("immutable BGE snapshots cannot be lifecycle write targets")
    ledger = TierLifecycleStore(database=database)
    rounds = math.ceil(math.log(max(1, cfg.query_max_candidates/cfg.query_candidate_limit), cfg.query_refill_factor))
    index = GuardedTieredIndex(index=backend, ledger=ledger, max_candidates=cfg.query_max_candidates,
                              refill_factor=cfg.query_refill_factor, max_refill_rounds=rounds)
    service = KnowledgeLifecycleService(ledger=ledger, index=index, embedding=embedding, config=cfg)
    query = LifecycleKnowledgeSearch(index=index, ledger=ledger, config=cfg,
        embedder=ModelQueryEmbedder(embedding, version=cfg.embedding_version))
    index.lifecycle_service = service
    index.lifecycle_query = query
    runtime = LifecycleRuntime(service=service, index=index, query=query)
    index.lifecycle_runtime = runtime
    return runtime
