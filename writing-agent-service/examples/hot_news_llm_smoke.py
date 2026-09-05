"""真实调用 FastGPT 热点分析 Agent。"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dotenv import load_dotenv

from app.clients.fastgpt import FastGPTClient
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsMetrics,
    HotScoreComponents,
)
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner
from app.services.hot_news_analysis import HotNewsAnalysisValidator

"""读取环境变量"""
def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    load_dotenv()

    """设置简单的命名空间"""
    client_settings = SimpleNamespace(
        fastgpt_base_url=required_env("FASTGPT_BASE_URL"),
        fastgpt_api_key=required_env("FASTGPT_API_KEY"),
    )
    app_id = required_env("FASTGPT_HOT_NEWS_APP_ID")

    now = datetime.now(timezone.utc)

    analysis_input = HotNewsAnalysisInput(
        news_id="smoke-news-001",
        title="某科技公司发布新一代人工智能产品",
        summary="该产品发布后获得大量用户关注。",
        content_excerpt=(
            "该公司正式发布新一代人工智能产品，"
            "当前新闻的点击、有效消费和互动指标明显上升。"
        ),
        content_type="article",
        window_start=now - timedelta(hours=1),
        window_end=now,
        hot_score=0.72,
        metrics=HotNewsMetrics(
            impressions=1000,
            clicks=120,
            ctr=0.12,
            unique_users=900,
            effective_consumptions=80,
            interactions=30,
        ),
        score_components=HotScoreComponents(
            click=0.24,
            consumption=0.25,
            interaction=0.14,
            growth=0.09,
        ),
        # 第一次 smoke test 不传关联证据，方便验证无证据限制。
        related_news=[],
        analysis_policy_version="hot-news-analysis-v1",
    )

    client = FastGPTClient(client_settings)

    try:
        runner = HotNewsAnalysisAgentRunner(
            client,
            app_id,
        )

        result = await runner.run(analysis_input)

        # Pydantic 校验后，再执行输入输出交叉校验。
        HotNewsAnalysisValidator().validate(
            analysis_input=analysis_input,
            result=result,
        )

        print("FastGPT request_id:", result.request_id)
        print("Token usage:", result.usage)
        print(result.value.model_dump_json(indent=2))

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())