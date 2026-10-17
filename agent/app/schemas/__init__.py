"""新闻写作 Agent 的结构化数据契约。

各业务契约已迁移到 app.modules.<module>.contracts；本包仅通过
__getattr__ 惰性再导出旧名称，避免与迁移模块形成导入环。
"""

from importlib import import_module as _import_module

_MOVED_REEXPORTS = {
    "AnalysisReason": "app.modules.hot_news.contracts.hot_news",
    "HotNewsAnalysisInput": "app.modules.hot_news.contracts.hot_news",
    "HotNewsAnalysisReport": "app.modules.hot_news.contracts.hot_news",
    "HotNewsMetrics": "app.modules.hot_news.contracts.hot_news",
    "HotScoreComponents": "app.modules.hot_news.contracts.hot_news",
    "OperationSuggestion": "app.modules.hot_news.contracts.hot_news",
    "RelatedNewsContext": "app.modules.hot_news.contracts.hot_news",
    "RelatedNewsEvidence": "app.modules.hot_news.contracts.hot_news",
    "ArticleAssemblyInput": "app.modules.writing.contracts.writing",
    "ArticleDraft": "app.modules.writing.contracts.writing",
    "ArticleOutline": "app.modules.writing.contracts.writing",
    "ArticleSection": "app.modules.writing.contracts.writing",
    "SectionWritingInput": "app.modules.writing.contracts.writing",
    "ResearchPackage": "app.modules.writing.contracts.research",
    "ReviewInput": "app.modules.writing.contracts.review",
    "ReviewReport": "app.modules.writing.contracts.review",
    "ArtifactDescriptor": "app.modules.writing.contracts.checkpoint",
    "ArtifactReference": "app.modules.writing.contracts.checkpoint",
    "CheckpointCommit": "app.modules.writing.contracts.checkpoint",
    "FailureCommit": "app.modules.writing.contracts.checkpoint",
    "FailureOutcome": "app.modules.writing.contracts.checkpoint",
    "ResumePoint": "app.modules.writing.contracts.checkpoint",
}


def __getattr__(name: str):
    module_name = _MOVED_REEXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_import_module(module_name), name)


__all__ = list(_MOVED_REEXPORTS)
