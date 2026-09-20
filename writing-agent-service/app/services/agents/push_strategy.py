"""FastGPT 推送策略 Agent Runner。"""

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.push import PushStrategyCandidate, PushStrategyInput


class PushStrategyAgentRunner:
    def __init__(self, client: FastGPTClient, app_id: str | None) -> None:
        if app_id is None or not app_id.strip():
            raise ValueError("FASTGPT_PUSH_STRATEGY_APP_ID cannot be empty")
        self._client = client
        self._app_id = app_id.strip()

    async def run(
        self,
        strategy_input: PushStrategyInput,
    ) -> AgentResult[PushStrategyCandidate]:
        return await self._client.run_structured(
            app_id=self._app_id,
            payload=strategy_input,
            output_type=PushStrategyCandidate,
            mode="push_strategy_generation",
        )
