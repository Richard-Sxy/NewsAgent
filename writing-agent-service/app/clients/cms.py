import uuid
from typing import Any

import httpx

from app.config import Settings


class CmsNotConfiguredError(RuntimeError):
    pass


class CmsPublishError(RuntimeError):
    pass


class CmsPublisher:
    """固定目标的企业 CMS 网关；目标地址只能来自部署配置，避免 SSRF。"""

    def __init__(self, settings: Settings) -> None:
        self.url = settings.cms_publish_url
        self.token = settings.cms_publish_token
        self.timeout = settings.cms_publish_timeout_seconds

    async def publish(
        self,
        *,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        channel: str,
        article: dict[str, Any],
    ) -> str:
        if not self.url:
            raise CmsNotConfiguredError("CMS 发布网关尚未配置")
        headers = {
            "Idempotency-Key": f"news-writing:{tenant_id}:{job_id}:{channel}",
            "X-Tenant-ID": str(tenant_id),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.url,
                    headers=headers,
                    json={"job_id": str(job_id), "channel": channel, "article": article},
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CmsPublishError("CMS 发布失败或返回格式无效") from exc
        external_id = payload.get("publication_id") or payload.get("id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise CmsPublishError("CMS 响应缺少 publication_id")
        return external_id
