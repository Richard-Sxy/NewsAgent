"""Python-owned push-strategy Agent runner."""

from app.model_runtime.agent_client import StructuredAgentClient
from app.model_runtime.result import AgentResult
from app.schemas.push import PushStrategyCandidate, PushStrategyInput


class PushStrategyAgentRunner:
    def __init__(self, client: StructuredAgentClient, app_id: str | None) -> None:
        if app_id is None or not app_id.strip():
            raise ValueError("push-strategy execution identity cannot be empty")
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
