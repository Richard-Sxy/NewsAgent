from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.review import ReviewInput, ReviewReport

""" 这边是 Reviewer 智能体，输入：review_input """
class ReviewerAgentRunner:
    def __init__(self, client: FastGPTClient, app_id: str) -> None:
        self.client = client
        self.app_id = app_id

    async def run(
        self,
        review_input: ReviewInput,
    ) -> AgentResult[ReviewReport]:
        return await self.client.run_structured(
            app_id=self.app_id,
            payload=review_input,
            output_type=ReviewReport,
            mode="review",
        )
