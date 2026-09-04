import httpx

from config import settings


class FastGPTTrainingService:
    def __init__(
        self,
        request_get=None,
        raw_dataset_id: str | None = None,
        qa_dataset_id: str | None = None,
    ):
        self.base_url = settings.fastgpt_base_url.rstrip("/")
        self.api_key = settings.fastgpt_api_key
        self.timeout = settings.fastgpt_timeout
        self.request_get = request_get or httpx.get
        self.raw_dataset_id = raw_dataset_id or settings.fastgpt_dataset_id
        self.qa_dataset_id = qa_dataset_id or settings.fastgpt_qa_dataset_id

    def _get(self, path: str, dataset_id: str) -> dict:
        if not dataset_id:
            raise ValueError("FastGPT datasetId 不能为空。")
        if not self.api_key:
            raise ValueError("FastGPT API Key 不能为空。")

        try:
            response = self.request_get(
                self.base_url + path,
                params={
                    "datasetId": dataset_id,
                },
                headers={
                    "Authorization": (
                        f"Bearer {self.api_key}"
                    ),
                },
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"FastGPT连接失败：{exc}"
            ) from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"FastGPT HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "FastGPT训练状态响应不是有效JSON。"
            ) from exc

        if not isinstance(result, dict):
            raise RuntimeError(
                "FastGPT训练状态返回格式错误。"
            )

        if result.get("code") != 200:
            raise RuntimeError(
                result.get(
                    "message",
                    "FastGPT训练状态返回格式错误。"
                )
            )
        
        data = result.get("data")

        if not isinstance(data, dict):
            raise RuntimeError(
                "FastGPT训练状态返回格式错误。"
            )
        
        return data

    def get_dataset_status(
        self,
        dataset_id: str,
    ) -> dict:
        queue = self._get(
            (
                "/api/core/dataset/training/"
                "getDatasetTrainingQueue"
            ),
            dataset_id,
        )

        error_result = self._get(
            (
                "/api/core/dataset/training/"
                "hasDatasetTrainingError"
            ),
            dataset_id,
        )

        training_count = int(
            queue.get("trainingCount", 0)
        )
        rebuilding_count = int(
            queue.get("rebuildingCount", 0)
        )
        has_error = bool(
            error_result.get("hasError", False)
        )

        if has_error:
            status = "error"
        elif (
            training_count > 0
            or rebuilding_count > 0
        ):
            status = "training"
        else:
            status = "ready"

        return {
            "status": status,
            "training_count": training_count,
            "rebuilding_count": rebuilding_count,
            "has_error": has_error,
        }

    def check_all(self) -> dict:
        raw_dataset = self.get_dataset_status(
            self.raw_dataset_id
        )
        qa_dataset = self.get_dataset_status(
            self.qa_dataset_id
        )

        statuses = {
            raw_dataset["status"],
            qa_dataset["status"],
        }

        if "error" in statuses:
            overall_status = "error"
        elif "training" in statuses:
            overall_status = "training"
        else:
            overall_status = "ready"
        
        return {
            "status": overall_status,
            "raw_dataset": raw_dataset,
            "qa_dataset": qa_dataset,
        }
