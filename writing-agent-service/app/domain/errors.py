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
