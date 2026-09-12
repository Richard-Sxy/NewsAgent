"""FastGPT Text2SQL SQL 生成 Runner。

Runner 只负责选择 App、组装 payload 并复用统一客户端做结构化校验；它不执行
SQL、不重试、不做降级。生成的候选 SQL 必须交给 ``SqlGuard`` 校验后才能执行。
"""

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.text2sql import Text2SqlGenerationInput, Text2SqlPlan


class Text2SqlAgentRunner:
    """调用指定 FastGPT App，返回经过 Pydantic 校验的候选 SQL。"""

    def __init__(self, client: FastGPTClient, app_id: str | None) -> None:
        if app_id is None or not app_id.strip():
            raise ValueError("FASTGPT_TEXT2SQL_APP_ID cannot be empty")
        self.client = client
        self.app_id = app_id.strip()

    async def run(
        self,
        generation_input: Text2SqlGenerationInput,
    ) -> AgentResult[Text2SqlPlan]:
        # 这边就是调用 app_id 的大模型 提示词什么的都设置在那边了
        return await self.client.run_structured(
            app_id=self.app_id,
            payload=generation_input,
            output_type=Text2SqlPlan,
            mode="text2sql",
        )
