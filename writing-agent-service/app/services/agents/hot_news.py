"""FastGPT 热点分析 Agent Runner。"""

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport


class HotNewsAnalysisAgentRunner:
    """选择热点分析 App，并复用统一客户端执行结构化调用。"""

    def __init__(self, client: FastGPTClient, app_id: str | None) -> None:
        if app_id is None or not app_id.strip():
            raise ValueError("FASTGPT_HOT_NEWS_APP_ID cannot be empty")
        self.client = client
        self.app_id = app_id.strip()

    async def run(
        self,
        analysis_input: HotNewsAnalysisInput,
    ) -> AgentResult[HotNewsAnalysisReport]:
        """调用真实 FastGPT App，并返回经过 Pydantic 校验的报告。

        调用约束：
        1. 调用 ``self.client.run_structured``，不要重新实现 HTTP 请求。
        2. ``payload`` 传入 ``analysis_input``。
        3. ``output_type`` 使用 ``HotNewsAnalysisReport``。
        4. ``mode`` 固定为 ``hot_news_analysis``。
        5. 不在 Runner 内重试、吞异常或静默切换规则结果。
        """

        return await self.client.run_structured(
            app_id=self.app_id,
            payload=analysis_input,
            output_type=HotNewsAnalysisReport,
            mode="hot_news_analysis",
        )
