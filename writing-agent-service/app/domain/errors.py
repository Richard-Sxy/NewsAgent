class FastGPTError(RuntimeError):
    """FastGPT 调用失败的基类。"""

    retryable = False

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id

""" 网络异常 """
class FastGPTNetworkError(FastGPTError):
    retryable = True

""" 超时异常 """
class FastGPTTimeoutError(FastGPTError):
    retryable = True

""" 速率异常限制 """
class FastGPTRateLimitError(FastGPTError):
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.retry_after = retry_after

""" 服务异常 """
class FastGPTServerError(FastGPTError):
    retryable = True

""" 验证登陆异常 """
class FastGPTAuthenticationError(FastGPTError):
    pass

""" 请求异常 """
class FastGPTRequestError(FastGPTError):
    pass

""" 回答异常 """
class FastGPTResponseError(FastGPTError):
    pass

""" Aitifact 对象存储操作失败。 """
class ArtifactStoreError(RuntimeError):
    retryable = True

""" Artifact 内容与数据库记录的哈希值不一致。 """
class ArtifactIntegrityError(ArtifactStoreError):
    retryable = False

""" Agent结构化输出异常 """
class AgentOutputValidationError(FastGPTError):

    def __init__(
        self,
        message: str,
        *,
        raw_content: str,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.raw_content = raw_content


class HotNewsAnalysisAttemptError(AgentOutputValidationError):
    """A model-quality failure carrying the exact trusted input snapshot.

    Data Loop collectors use this envelope to retain a reproducible bad case.
    It deliberately remains a validation error so existing retry policy stays
    non-retryable.
    """

    def __init__(
        self,
        message: str,
        *,
        analysis_input: object,
        raw_content: str,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            raw_content=raw_content,
            request_id=request_id,
        )
        self.analysis_input = analysis_input


class HotNewsDataQualityError(RuntimeError):
    """热点运行缺少可信内容或收到不一致的确定性数据。"""

    retryable = False


class HotNewsPersistenceError(RuntimeError):
    """热点运行的 PostgreSQL 持久化操作失败。"""

    retryable = True
