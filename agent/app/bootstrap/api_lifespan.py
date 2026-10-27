"""FastAPI 业务 lifespan 装配。

构建顺序：数据库 → 模型 Port/知识库 → Temporal/Redis → 编排与事件流 →
行为标签/读者画像/受众预览 → 知识索引 → 会话服务与 Data Loop 钩子。
关闭顺序与构建相反，receipt 缓存先于依赖清理关闭。
入口：app.main（薄入口，仅注册路由与健康检查）。
"""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256

from fastapi import FastAPI
from temporalio.client import Client
from redis.asyncio import Redis

from app.observability.telemetry import configure_telemetry
from app.observability.temporal import temporal_interceptors
from app.config import get_settings
from app.modules.knowledge.postgres_store import PostgresKnowledgeStore
from app.modules.knowledge.search_factory import build_knowledge_search_index, close_knowledge_search_index
from app.modules.knowledge.qa import NativeQAGenerationService
from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.db.session import Database
from app.modules.writing.orchestrator import OrchestratorService
from app.modules.writing.event_reader import RedisProgressReader
from app.modules.hot_news.application.hot_news_event_stream import RedisHotNewsEventStream
from app.storage.s3 import S3ArtifactStore
from app.clients.cms import CmsPublisher
from app.modules.conversation.service import ConversationAgentService
from app.modules.conversation.tools import ConversationTools
from app.modules.conversation.writing import ConversationWritingReader, ConversationWritingCommands
from app.modules.conversation.admission import AdmissionService
from app.modules.conversation.receipt_reads import ReceiptReadCoordinator
from app.modules.conversation.memory_hooks import ConversationMemoryHooks
from app.modules.conversation.feedback_hooks import ConversationFeedbackHooks
from app.modules.conversation.dataloop_hooks import ConversationDataLoopHooks
from app.modules.conversation.candidate_templates import candidate_templates
from app.modules.data_loop.bundle_runtime import NativeProductionBundleRuntimeRegistry
from app.modules.data_loop.bundle_contract import UnsupportedProductionBundleRuntimeError
from app.modules.data_analysis.factory import create_analysis_runner
from app.repositories.conversation import PostgresConversationRepository
from app.modules.hot_news.application.hot_news_query import HotNewsQueryService
from app.modules.data_loop.orchestrator import DataLoopOrchestrator
from app.modules.data_loop.artifacts import S3DataLoopArtifactStore
from app.modules.data_loop.dataset_freezer import (
    S3EvaluationDatasetArtifactStore,
)


@asynccontextmanager
async def _business_lifespan(app: FastAPI):
    settings = get_settings()
    database = Database(settings)
    model_ports = None
    knowledge_search_index = None
    if settings.model_runtime_backend == "native":
        if settings.model_runtime_config_path is None:
            raise ValueError("MODEL_RUNTIME_CONFIG_PATH is required")
        model_config = load_model_runtime_config(settings.model_runtime_config_path)
        model_ports = build_model_runtime_ports(
            model_config, environment=settings.environment
        )
        app.state.model_config = model_config
        app.state.model_ports = model_ports
        knowledge_store = PostgresKnowledgeStore(
            database=database,
            embedding=model_ports.embedding,
            embedding_version=model_config.embedding.model_routes[0],
            embedding_batch_size=model_config.embedding.max_batch_size,
        )
        app.state.knowledge_store = knowledge_store
        model_config.agent_scene("qa_generation")
        app.state.qa_service = NativeQAGenerationService(
            agent=NativeStructuredAgentClient(
                StructuredInferenceService(
                    inference=model_ports.inference,
                    prompts=model_ports.prompts,
                ),
                model_config,
            ),
            store=knowledge_store,
        )
    temporal = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=temporal_interceptors(),
    )
    redis = Redis.from_url(str(settings.redis_url), decode_responses=False)
    app.state.settings = settings
    app.state.database = database
    app.state.orchestrator = OrchestratorService(temporal, settings)
    app.state.data_loop_orchestrator = DataLoopOrchestrator(temporal, settings)
    app.state.conversation_admission = (AdmissionService(client=temporal, settings=settings)
        if settings.conversation_async_admission_enabled else None)
    app.state.conversation_receipt_reads = ReceiptReadCoordinator()
    app.state.temporal = temporal
    app.state.redis = redis
    app.state.event_reader = RedisProgressReader(redis, settings)
    app.state.hot_news_event_stream = RedisHotNewsEventStream(redis, settings)
    app.state.artifact_store = S3ArtifactStore(settings)
    app.state.evaluation_dataset_artifact_store = (
        S3EvaluationDatasetArtifactStore(settings)
    )
    app.state.data_loop_artifact_store = S3DataLoopArtifactStore(settings)
    app.state.cms_publisher = CmsPublisher(settings)
    analysis_runner = create_analysis_runner(settings) if settings.conversation_enabled else None
    audience_service = None
    app.state.audience_service = None
    reader_profile_service = None
    app.state.reader_profile_service = None
    try:
        from app.modules.behavior_tags.runtime import build_behavior_runtime
        app.state.behavior_tags_runtime = build_behavior_runtime(settings)
        from app.modules.reader_profiles.factory import build_reader_profile_service
        reader_profile_service = build_reader_profile_service(settings)
        app.state.reader_profile_service = reader_profile_service
        if settings.audience_enabled:
            from app.modules.audience.factory import build_audience_client
            from app.modules.audience.service import AudiencePreviewService
            from app.modules.audience.taxonomy import load_topic_catalog
            from app.modules.hot_news.adapters.hot_news_run_store import PostgresHotNewsRunStore
            if settings.audience_backend == "behavior_package" and app.state.behavior_tags_runtime is not None:
                catalog = app.state.behavior_tags_runtime.policy.catalog
            elif not settings.audience_taxonomy_path:
                raise ValueError("Audience integration requires AUDIENCE_TAXONOMY_PATH")
            else:
                catalog = load_topic_catalog(settings.audience_taxonomy_path)
            audience_service = AudiencePreviewService(catalog=catalog,
                client=build_audience_client(settings, catalog, behavior_runtime=app.state.behavior_tags_runtime),
                run_store=PostgresHotNewsRunStore(database),
                source_mode="synthetic" if settings.audience_backend in {"synthetic", "behavior_package"} else "enterprise",
                timeout_seconds=settings.audience_timeout_seconds,
                max_snapshot_age_seconds=settings.audience_max_snapshot_age_seconds)
            app.state.audience_service = audience_service
        if model_ports is not None:
            # 构建知识库查询索引
            if settings.knowledge_lifecycle_config_path:
                from app.modules.knowledge.tier_lifecycle_factory import build_lifecycle_runtime
                lifecycle = build_lifecycle_runtime(
                    lifecycle_config_path=settings.knowledge_lifecycle_config_path,
                    search_config_path=settings.knowledge_search_config_path,
                    model_config=model_config, embedding=model_ports.embedding,
                    semantic_config_path=settings.knowledge_semantics_config_path,
                    inference=model_ports.inference,
                    database=database, environment=settings.environment)
                knowledge_search_index = lifecycle.index
                await lifecycle.validate()
                app.state.knowledge_lifecycle = lifecycle
                app.state.knowledge_store = lifecycle.service
            else:
                knowledge_search_index = build_knowledge_search_index(
                    config_path=settings.knowledge_search_config_path,
                    postgres_store=knowledge_store,
                )
        if settings.conversation_enabled:
            dataloop_hooks = None
            try:
                registry = NativeProductionBundleRuntimeRegistry.validation_only(
                    model_ports.prompts, settings.hot_news_runtime_manifest_json,
                    allowed_model_routes=model_config.inference.model_routes,
                )
                dataloop_hooks = ConversationDataLoopHooks(database=database,
                    orchestrator=app.state.data_loop_orchestrator, runtime_registry=registry,
                    admission=app.state.conversation_admission,
                    candidate_templates=candidate_templates(settings.hot_news_runtime_manifest_json, registry))
            except (ValueError, UnsupportedProductionBundleRuntimeError):
                # A deployment without a valid evaluation manifest still keeps
                # its ordinary conversation tools; these hooks remain hidden.
                pass
            binding = model_config.agent_scene("conversation")
            memory_binding = model_config.agent_scene("conversation_memory")
            memory_prompt = model_ports.prompts.resolve(scene=memory_binding.scene, version=memory_binding.prompt_version)
            prompt = model_ports.prompts.resolve(scene=binding.scene, version=binding.prompt_version)
            app.state.conversation_service = ConversationAgentService(
                repository=PostgresConversationRepository(database),
                model=NativeStructuredAgentClient(
                    StructuredInferenceService(inference=model_ports.inference, prompts=model_ports.prompts),
                    model_config,
                    timeout_seconds=min(settings.conversation_turn_timeout_seconds, model_config.inference.timeout_seconds),
                ),
                tools=ConversationTools(
                    hot_news=HotNewsQueryService(database=database), knowledge_store=knowledge_search_index,
                    embedding=model_ports.embedding, embedding_version=model_config.embedding.model_routes[0],
                    knowledge_enabled=settings.conversation_knowledge_enabled,
                    analysis_runner=analysis_runner,
                    writing_reader=ConversationWritingReader(database=database, admission=app.state.conversation_admission),
                    writing_commands=ConversationWritingCommands(database=database, orchestrator=app.state.orchestrator,
                        admission=app.state.conversation_admission),
                    memory_hooks=ConversationMemoryHooks(database=database),
                    feedback_hooks=ConversationFeedbackHooks(database=database),
                    dataloop_hooks=dataloop_hooks,
                ),
                context_max_chars=settings.conversation_context_max_chars,
                context_recent_chars=settings.conversation_context_recent_chars,
                context_summary_chars=settings.conversation_context_summary_chars,
                context_threshold_ratio=settings.conversation_context_threshold_ratio,
                context_max_compaction_attempts=settings.conversation_context_max_compaction_attempts,
                max_tool_calls=settings.conversation_max_tool_calls,
                turn_timeout_seconds=settings.conversation_turn_timeout_seconds,
                local_simulation=model_config.inference.provider == "local",
                runtime_metadata={"scene": binding.scene, "prompt_version": binding.prompt_version,
                                  "async_admission": {"enabled": settings.conversation_async_admission_enabled,
                                      "budget": settings.conversation_async_admission_budget,
                                      "queue_limit": settings.conversation_async_admission_queue_limit,
                                      "scope": "conversation_async_hooks"},
                                  "model_provider": model_config.inference.provider,
                                  "model_route": binding.model_route,
                                  "memory_prompt_version": memory_binding.prompt_version,
                                  "memory_model_route": memory_binding.model_route,
                                  "memory_prompt_sha256": sha256(memory_prompt.system_prompt.encode()).hexdigest(),
                                  "prompt_sha256": sha256(prompt.system_prompt.encode()).hexdigest()},
            )
        yield
    finally:
        if reader_profile_service is not None:
            await reader_profile_service.close()
        if audience_service is not None:
            await audience_service.close()
        await app.state.conversation_receipt_reads.close()
        await close_knowledge_search_index(knowledge_search_index)
        if analysis_runner is not None and hasattr(analysis_runner, "close"):
            await analysis_runner.close()
        await redis.aclose()
        if model_ports is not None:
            await model_ports.close()
        await database.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    telemetry = configure_telemetry("newsagent-api")
    try:
        async with _business_lifespan(app):
            yield
    finally:
        await asyncio.to_thread(telemetry.shutdown)
