import asyncio
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    ExternalServiceError,
    ServiceNotConfiguredError,
    ServiceTimeoutError,
    UnauthorizedError,
)
from app.schemas.chat import DocumentChunk


class WiseAdapter:
    provider_id = "wise"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self.max_query_concurrency = settings.wise_query_concurrency

    @property
    def configured_collection_ids(self) -> list[str]:
        return self.settings.wise_kb_ids

    async def search(
        self,
        query: str,
        request_id: str,
        *,
        collection_ids: list[str] | None = None,
    ) -> list[DocumentChunk]:
        if not self.settings.wise_configured:
            raise ServiceNotConfiguredError("WISE 知识库")

        headers = {
            "X-API-Key": self.settings.wise_api_key.get_secret_value(),
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        effective_collection_ids = (
            self.settings.wise_kb_ids
            if collection_ids is None
            else collection_ids
        )
        if not effective_collection_ids:
            raise UnauthorizedError("当前身份没有获授权的 WISE 知识库。")
        payload = {
            "arguments": {
                "queries": [query],
                "knowledge_base_ids": effective_collection_ids,
            }
        }
        url = (
            f"{self.settings.wise_base_url.rstrip('/')}"
            "/agent-tools/knowledge_search/invoke"
        )

        for attempt in range(3):
            try:
                if self._client:
                    response = await self._client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=30,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=30,
                        )
            except httpx.TimeoutException as exc:
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                raise ServiceTimeoutError("WISE 知识库") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                raise ExternalServiceError("WISE 知识库") from exc

            if response.status_code in {401, 403}:
                raise UnauthorizedError("WISE 知识库拒绝了本次访问，请联系系统维护人员。")
            if response.is_error:
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                raise ExternalServiceError("WISE 知识库")

            try:
                body = response.json()
            except ValueError as exc:
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                raise ExternalServiceError("WISE 知识库") from exc

            result = body.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            rows = data.get("results") if isinstance(data, dict) else None
            if (
                body.get("ok") is False
                or not isinstance(result, dict)
                or result.get("success") is False
                or not isinstance(rows, list)
            ):
                if attempt < 2:
                    await asyncio.sleep(0.4)
                    continue
                raise ExternalServiceError("WISE 知识库")
            return [chunk for row in rows if (chunk := self._normalize(row))]

        raise ExternalServiceError("WISE 知识库")

    @staticmethod
    def _normalize(row: Any) -> DocumentChunk | None:
        if not isinstance(row, dict):
            return None
        content = str(row.get("matched_content") or row.get("content") or "").strip()
        title = str(row.get("knowledge_title") or row.get("knowledge_filename") or "").strip()
        chunk_id = str(row.get("chunk_id") or row.get("id") or "").strip()
        if not content or not title or not chunk_id:
            return None

        raw_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        metadata = {
            **raw_metadata,
            "provider": "wise",
            "authority_level": "enterprise_project",
            "authority_priority": 100,
        }
        score_value = row.get(
            "ordering_score",
            row.get("score", row.get("raw_relevance_score")),
        )
        try:
            score = float(score_value) if score_value is not None else None
        except (TypeError, ValueError):
            score = None

        return DocumentChunk(
            chunk_id=chunk_id,
            knowledge_id=str(row.get("knowledge_id") or "") or None,
            title=title,
            filename=str(row.get("knowledge_filename") or "") or None,
            source_url=str(row.get("knowledge_source") or "") or None,
            content=content,
            score=score,
            updated_at=str(raw_metadata.get("source_updated_at") or "") or None,
            metadata=metadata,
        )
