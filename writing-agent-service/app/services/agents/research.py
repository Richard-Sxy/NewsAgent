from typing import Any

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.research import ResearchPackage

""" 这边是 Research 智能体,输入job_id/topic/requirements """
class ResearchAgentRunner:
    def __init__(self, client: FastGPTClient, app_id: str) -> None:
        self.client = client
        self.app_id = app_id
    # 异步执行，主要是 job_id 和 topic 主题
    async def run(
        self,
        *,
        job_id: str,
        topic: str,
        requirements: dict[str, Any],
        previous_package: ResearchPackage | None = None,
        revision_instruction: str | None = None,
    ) -> AgentResult[ResearchPackage]:
        return await self.client.run_structured(
            app_id=self.app_id,
            payload={
                "job_id": job_id,
                "topic": topic,
                "requirements": requirements,
                "previous_research_package": (
                    previous_package.model_dump(mode="json")
                    if previous_package is not None
                    else None
                ),
                "revision_instruction": revision_instruction,
            },
            output_type=ResearchPackage,
            mode="research",
            # job_id/topic are trusted orchestration context, not model-authored data.
            output_defaults={"job_id": job_id, "topic": topic},
        )
