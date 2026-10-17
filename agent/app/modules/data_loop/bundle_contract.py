"""
定义当前运行环境不支持这个 Bundle 的公告异常
"""


class UnsupportedProductionBundleRuntimeError(ValueError):
    retryable = False


LOCAL_EXECUTION_FIELDS = (
    "metric_definition_version",
    "hot_score_policy_version",
    "reranker_policy_version",
    "output_schema_version",
    "validator_version",
    "memory_resolver_policy_version",
)
