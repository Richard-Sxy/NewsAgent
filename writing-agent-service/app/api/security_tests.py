"""Operator-facing security dry-run endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import (
    HotNewsPermission,
    DataLoopPrincipal,
    require_hot_news_permission,
)
from app.schemas.security_test import (
    PromptInjectionTestRequest,
    PromptInjectionTestResponse,
)
from app.services.prompt_injection_harness import evaluate_prompt_injection_test


router = APIRouter(prefix="/api/v1/security-tests", tags=["security-tests"])

ReadPrincipal = Annotated[
    DataLoopPrincipal,
    Depends(require_hot_news_permission(HotNewsPermission.READ)),
]


@router.post("/prompt-injection", response_model=PromptInjectionTestResponse)
async def run_prompt_injection_test(
    request: PromptInjectionTestRequest,
    principal: ReadPrincipal,
) -> PromptInjectionTestResponse:
    return evaluate_prompt_injection_test(request, tenant_id=principal.tenant_id)
