from typing import Any

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.schemas.research import ResearchPackage
from app.schemas.writing import (
    ArticleAssemblyInput,
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
    SectionWritingInput,
)

"""这边是写作Agent，输入job_id/research_package/requirements"""
class WriterAgentRunner:
    def __init__(self, client: FastGPTClient, app_id: str) -> None:
        self.client = client
        self.app_id = app_id

    # 生成文章提纲
    async def create_outline(
        self,
        *,
        job_id: str,
        research_package: ResearchPackage,
        requirements: dict[str, Any],
        previous_outline: ArticleOutline | None = None,
        revision_instruction: str | None = None,
    ) -> AgentResult[ArticleOutline]:
        return await self.client.run_structured(
            app_id=self.app_id,
            payload={
                "job_id": job_id,
                "research_package": research_package.model_dump(mode="json"),    # 转成JSON对象
                "requirements": requirements,
                "previous_outline": (
                    previous_outline.model_dump(mode="json")                     # 转成JSON对象
                    if previous_outline is not None
                    else None
                ),
                "revision_instruction": revision_instruction,
            },
            output_type=ArticleOutline,
            mode="outline",
        )

    # 返回一个结构化章节。
    async def write_section(
        self,
        writing_input: SectionWritingInput,
    ) -> AgentResult[ArticleSection]:
        mode = "revise" if writing_input.revision_instruction else "section"
        return await self.client.run_structured(
            app_id=self.app_id,
            payload=writing_input,
            output_type=ArticleSection,
            mode=mode,
        )
    
    # 把前面的多个章节拼成一篇完整文章文稿。
    async def assemble(
        self,
        assembly_input: ArticleAssemblyInput,
    ) -> AgentResult[ArticleDraft]:
        return await self.client.run_structured(
            app_id=self.app_id,
            payload=assembly_input,
            output_type=ArticleDraft,
            mode="assemble",
        )
