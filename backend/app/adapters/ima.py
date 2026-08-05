import hashlib
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    ExternalServiceError,
    ServiceNotConfiguredError,
    ServiceTimeoutError,
    UnauthorizedError,
    UpstreamQuotaExceededError,
)
from app.schemas.chat import DocumentChunk


class ImaAdapter:
    provider_id = "ima"
    QUOTA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self.max_query_concurrency = 1
        self._quota_blocked_until: datetime | None = None

    @property
    def configured_collection_ids(self) -> list[str]:
        return self.settings.ima_kb_ids

    async def search(
        self,
        query: str,
        request_id: str,
        *,
        collection_ids: list[str] | None = None,
    ) -> list[DocumentChunk]:
        if not self.settings.ima_configured:
            raise ServiceNotConfiguredError("IMA knowledge base")
        self._raise_if_quota_blocked()

        effective_collection_ids = (
            self.settings.ima_kb_ids
            if collection_ids is None
            else collection_ids
        )
        if not effective_collection_ids:
            raise UnauthorizedError("当前身份没有获授权的 IMA 知识库。")
        ranked_rows: dict[str, tuple[int, int, dict[str, Any]]] = {}
        for knowledge_base_id in effective_collection_ids:
            evidence_media_ids: set[str] = set()
            for variant_rank, variant in enumerate(self._query_variants(query)):
                cursor = ""
                seen_cursors: set[str] = set()
                result_offset = 0
                for _ in range(self.settings.ima_max_pages):
                    body = await self._post(
                        "openapi/wiki/v1/search_knowledge",
                        {
                            "query": variant,
                            "knowledge_base_id": knowledge_base_id,
                            "cursor": cursor,
                        },
                        request_id,
                    )
                    data = body.get("data") if isinstance(body, dict) else None
                    info_list = data.get("info_list") if isinstance(data, dict) else None
                    if not isinstance(info_list, list):
                        break

                    for page_rank, row in enumerate(info_list):
                        if not isinstance(row, dict):
                            continue
                        result_rank = result_offset + page_rank
                        media_id = str(row.get("media_id") or "")
                        row_key = media_id or hashlib.sha1(
                            f"{knowledge_base_id}:{row.get('title')}:{result_rank}".encode(
                                "utf-8"
                            )
                        ).hexdigest()
                        if row_key in ranked_rows:
                            continue
                        row["_knowledge_base_id"] = knowledge_base_id
                        row["_query_variant"] = variant
                        row["_variant_rank"] = variant_rank
                        row["_result_rank"] = result_rank
                        row["_score"] = max(0.0, 1.0 - variant_rank * 0.08 - result_rank * 0.005)
                        ranked_rows[row_key] = (variant_rank, result_rank, row)
                        if str(row.get("highlight_content") or "").strip():
                            evidence_media_ids.add(row_key)

                    result_offset += len(info_list)
                    is_end = bool(data.get("is_end")) if isinstance(data, dict) else True
                    next_cursor = str(data.get("next_cursor") or "") if isinstance(data, dict) else ""
                    if is_end or not next_cursor or next_cursor in seen_cursors:
                        break
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor

                if (
                    len(evidence_media_ids) >= self.settings.ima_evidence_target
                    or len(ranked_rows) >= self.settings.ima_search_limit
                ):
                    break

        rows = [
            row
            for _, _, row in sorted(
                ranked_rows.values(),
                key=lambda item: (item[0], item[1]),
            )
        ][: self.settings.ima_search_limit]
        return [chunk for row in rows if (chunk := self._normalize(row))]

    async def _post(
        self,
        api_path: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        headers = {
            "ima-openapi-clientid": self.settings.ima_client_id.get_secret_value(),
            "ima-openapi-apikey": self.settings.ima_api_key.get_secret_value(),
            "ima-openapi-ctx": "erp_assistant=runtime",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        url = f"{self.settings.ima_base_url.rstrip('/')}/{api_path}"
        try:
            if self._client:
                response = await self._client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.ima_timeout_seconds,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.settings.ima_timeout_seconds,
                    )
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError("IMA knowledge base") from exc
        except httpx.HTTPError as exc:
            raise ExternalServiceError("IMA knowledge base") from exc

        if response.status_code in {401, 403}:
            raise UnauthorizedError(
                "IMA knowledge base denied this request. Check OpenAPI credentials and KB permission."
            )
        if response.is_error:
            raise ExternalServiceError("IMA knowledge base")
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError("IMA knowledge base") from exc
        business_code = body.get("code")
        if business_code == 220021:
            self._quota_blocked_until = self._next_quota_reset()
            raise UpstreamQuotaExceededError(
                "IMA knowledge base",
                str(body.get("msg") or body.get("message") or "").strip() or None,
            )
        if business_code != 0:
            raise ExternalServiceError("IMA knowledge base")
        return body

    def _raise_if_quota_blocked(self) -> None:
        if self._quota_blocked_until is None:
            return
        now = datetime.now(self.QUOTA_TIMEZONE)
        if now >= self._quota_blocked_until:
            self._quota_blocked_until = None
            return
        reset_at = self._quota_blocked_until.strftime("%Y-%m-%d %H:%M %Z")
        raise UpstreamQuotaExceededError(
            "IMA knowledge base",
            f"已触发本地配额熔断，预计 {reset_at} 后恢复尝试。",
        )

    @classmethod
    def _next_quota_reset(cls) -> datetime:
        now = datetime.now(cls.QUOTA_TIMEZONE)
        return datetime.combine(
            now.date() + timedelta(days=1),
            time.min,
            tzinfo=cls.QUOTA_TIMEZONE,
        )

    @staticmethod
    def _normalize(row: dict[str, Any]) -> DocumentChunk | None:
        title = str(row.get("title") or "").strip()
        content = ImaAdapter._clean_highlight(str(row.get("highlight_content") or "").strip())
        media_id = str(row.get("media_id") or "").strip()
        if not title or not media_id:
            return None
        evidence_eligible = bool(content)
        chunk_hash = hashlib.sha1(f"ima:{media_id}:{content}".encode("utf-8")).hexdigest()[:12]
        return DocumentChunk(
            chunk_id=f"ima-{chunk_hash}",
            knowledge_id=media_id,
            title=title,
            filename=title,
            source_url=None,
            content=content,
            score=row.get("_score"),
            metadata={
                "provider": "ima",
                "authority_level": "external_general",
                "authority_priority": 50,
                "media_id": media_id,
                "knowledge_base_id": row.get("_knowledge_base_id"),
                "parent_folder_id": row.get("parent_folder_id"),
                "query_variant": row.get("_query_variant"),
                "variant_rank": row.get("_variant_rank"),
                "result_rank": row.get("_result_rank"),
                "media_type": row.get("media_type"),
                # IMA search may return a matching title without any content
                # excerpt.  Keep that hit for diagnostics/discovery, but never
                # let a title-only record become answer evidence.
                "evidence_eligible": evidence_eligible,
            },
        )

    @staticmethod
    def _clean_highlight(value: str) -> str:
        cleaned = re.sub(r"</?em>", "", value, flags=re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _query_variants(query: str) -> list[str]:
        normalized = re.sub(r"[？?，,。；;：:！!\s]+", " ", query).strip()
        variants = [normalized or query.strip()]

        replacements = {
            "收料": "收货",
            "入库": "收货",
            "审核": "",
            "应该": "",
            "怎么": "",
            "如何": "",
            "完成": "",
            "后": "",
        }
        rewritten = normalized
        for source, target in replacements.items():
            rewritten = rewritten.replace(source, target)
        rewritten = re.sub(r"\s+", " ", rewritten).strip()
        if rewritten and rewritten not in variants:
            variants.append(rewritten)

        terms: list[str] = []
        for term in (
            "采购订单",
            "采购业务",
            "收货",
            "收料",
            "下推",
            "入库",
            "服务插件",
            "表单插件",
            "苍穹开发",
            "RequestContext",
        ):
            if term in query or term in rewritten:
                terms.append("收货" if term == "收料" else term)

        compact_terms = " ".join(dict.fromkeys(terms))
        if compact_terms and compact_terms not in variants:
            variants.append(compact_terms)

        for term in terms:
            if term and term not in variants:
                variants.append(term)

        return variants[:5]
