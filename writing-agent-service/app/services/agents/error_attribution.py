"""FastGPT 错误归因 Agent Runner 及参照完整性校验。"""

from __future__ import annotations

from app.clients.fastgpt import AgentResult, FastGPTClient
from app.domain.errors import AgentOutputValidationError
from app.schemas.error_attribution import (
    ErrorAttributionInput,
    ErrorAttributionReport,
)


_CATEGORY_ASSETS: dict[str, frozenset[str]] = {
    "input_data_gap": frozenset({"input_pipeline", "none"}),
    "retrieval_miss": frozenset({"retrieval_policy", "none"}),
    "ranking_rule": frozenset({"reranker_policy", "none"}),
    "prompt_instruction": frozenset({"analysis_prompt", "none"}),
    "schema_contract": frozenset({"output_schema", "none"}),
    "validator_rule": frozenset({"validator", "none"}),
    "model_reasoning": frozenset({"analysis_prompt", "none"}),
    "operation_policy": frozenset({"operation_policy", "none"}),
}


class ErrorAttributionValidator:
    """在 Pydantic Schema 之外校验模型不能伪造 Case/证据引用。"""

    def validate(
        self,
        *,
        attribution_input: ErrorAttributionInput,
        result: AgentResult[ErrorAttributionReport],
    ) -> AgentResult[ErrorAttributionReport]:
        report = result.value
        if report.dataset_id != attribution_input.dataset_id:
            self._raise("dataset_id does not match attribution input", result)
        if report.dataset_sha256 != attribution_input.dataset_sha256:
            self._raise(
                "dataset_sha256 does not match attribution input",
                result,
            )

        input_cases = {case.case_id: case for case in attribution_input.cases}
        output_case_ids = {item.case_id for item in report.attributions}
        if output_case_ids != set(input_cases):
            missing = sorted(set(input_cases) - output_case_ids)
            unknown = sorted(output_case_ids - set(input_cases))
            self._raise(
                f"attribution case refs mismatch: missing={missing}, "
                f"unknown={unknown}",
                result,
            )

        category_by_case: dict[str, str] = {}
        for attribution in report.attributions:
            source_case = input_cases[attribution.case_id]
            allowed_evidence = {
                evidence.news_id
                for evidence in source_case.analysis_input.related_news
            }
            unknown_evidence = (
                set(attribution.evidence_news_ids) - allowed_evidence
            )
            if unknown_evidence:
                self._raise(
                    f"case {attribution.case_id} references unknown evidence: "
                    f"{sorted(unknown_evidence)}",
                    result,
                )
            if attribution.affected_asset not in _CATEGORY_ASSETS[
                attribution.category
            ]:
                self._raise(
                    f"case {attribution.case_id} maps category "
                    f"{attribution.category} to incompatible asset "
                    f"{attribution.affected_asset}",
                    result,
                )
            category_by_case[attribution.case_id] = attribution.category

        for pattern in report.cross_case_patterns:
            unknown_cases = set(pattern.case_ids) - set(input_cases)
            if unknown_cases:
                self._raise(
                    "cross-case pattern references unknown cases: "
                    f"{sorted(unknown_cases)}",
                    result,
                )
            mismatched = [
                case_id
                for case_id in pattern.case_ids
                if category_by_case[case_id] != pattern.category
            ]
            if mismatched:
                self._raise(
                    "cross-case pattern category does not match primary "
                    f"attributions: {sorted(mismatched)}",
                    result,
                )
            if pattern.affected_asset not in _CATEGORY_ASSETS[pattern.category]:
                self._raise(
                    f"pattern maps category {pattern.category} to "
                    f"incompatible asset {pattern.affected_asset}",
                    result,
                )

        return result

    @staticmethod
    def _raise(
        message: str,
        result: AgentResult[ErrorAttributionReport],
    ) -> None:
        raise AgentOutputValidationError(
            f"error attribution output failed business validation: {message}",
            raw_content=result.raw_content,
            request_id=result.request_id,
        )


class ErrorAttributionAgentRunner:
    """复用 FastGPTClient.run_structured 运行只读错误归因。"""

    def __init__(
        self,
        client: FastGPTClient,
        app_id: str | None,
        *,
        validator: ErrorAttributionValidator | None = None,
    ) -> None:
        if app_id is None or not app_id.strip():
            raise ValueError("FASTGPT_ERROR_ATTRIBUTION_APP_ID cannot be empty")
        self._client = client
        self._app_id = app_id.strip()
        self._validator = validator or ErrorAttributionValidator()

    async def run(
        self,
        attribution_input: ErrorAttributionInput,
    ) -> AgentResult[ErrorAttributionReport]:
        result = await self._client.run_structured(
            app_id=self._app_id,
            payload=attribution_input,
            output_type=ErrorAttributionReport,
            mode="hot_news_error_attribution",
        )
        return self._validator.validate(
            attribution_input=attribution_input,
            result=result,
        )
