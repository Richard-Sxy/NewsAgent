"""ORM 模型聚合注册。

各业务模块的 ORM 已迁移到 app.modules.<module>.persistence；为避免
"迁移模型 → app.models.common → 本包 init → 迁移模型(半初始化)" 的导入环，
全部模型名称通过 __getattr__ 惰性再导出。依赖"import app.models 即注册
全部 ORM"的入口（alembic env、建表测试）需显式导入各模块的
persistence 包（导入即注册）。
"""

from importlib import import_module as _import_module

_MOVED_REEXPORTS = {
    "AgentRun": "app.modules.writing.persistence.agent_run",
    "WritingArtifact": "app.modules.writing.persistence.artifact",
    "WritingJob": "app.modules.writing.persistence.job",
    "WritingStep": "app.modules.writing.persistence.step",
    "OutboxEvent": "app.modules.writing.persistence.outbox",
    "AnalysisFeedbackCaseRecord": "app.modules.data_loop.persistence.analysis_feedback",
    "AnalysisFeedbackLabelRecord": "app.modules.data_loop.persistence.analysis_feedback",
    "PublicationOutcomeRecord": "app.modules.data_loop.persistence.analysis_feedback",
    "EvaluationDatasetRecord": "app.modules.data_loop.persistence.evaluation_dataset",
    "EvaluationDatasetCaseRecord": "app.modules.data_loop.persistence.evaluation_dataset",
    "ProductionBundleRecord": "app.modules.data_loop.persistence.production_bundle",
    "ConfigurationCandidateRecord": "app.modules.data_loop.persistence.production_bundle",
    "CandidateEvaluationRunRecord": "app.modules.data_loop.persistence.production_bundle",
    "PromotionDecisionRecord": "app.modules.data_loop.persistence.production_bundle",
    "ShortTermUserMemoryRecord": "app.modules.memory.persistence.user_memory",
    "LongTermMemoryCandidateRecord": "app.modules.memory.persistence.user_memory",
    "LongTermUserMemoryRecord": "app.modules.memory.persistence.user_memory",
    "MemoryPromotionRequestRecord": "app.modules.memory.persistence.user_memory",
    "HotNewsAnalysisRun": "app.modules.hot_news.persistence.hot_news",
    "HotEventRecord": "app.modules.hot_news.persistence.hot_event",
    "HotNewsDecision": "app.modules.hot_news.persistence.hot_news_decision",
}


def __getattr__(name: str):
    module_name = _MOVED_REEXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_import_module(module_name), name)


__all__ = list(_MOVED_REEXPORTS)
