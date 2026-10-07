"""定义公共异常。包括模型输出校验失败、产物哈希不一致、热点数据质量问题、SQL护栏失败和持久化失败，并标记是否允许重试。"""

class ModelError(RuntimeError):
    """Provider-neutral model boundary failure."""

    retryable = False

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


""" Aitifact 对象存储操作失败。 """
class ArtifactStoreError(RuntimeError):
    retryable = True

""" Artifact 内容与数据库记录的哈希值不一致。 """
class ArtifactIntegrityError(ArtifactStoreError):
    retryable = False

""" Agent结构化输出异常 """
class AgentOutputValidationError(ModelError):

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


class Text2SqlGuardError(HotNewsDataQualityError):
    """模板或模型生成的 SQL 未通过只读白名单护栏，禁止进入数仓执行。"""

    retryable = False


class HotNewsPersistenceError(RuntimeError):
    """热点运行的 PostgreSQL 持久化操作失败。"""

    retryable = True
