import asyncio
import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings


def _result_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    return len(results) if isinstance(results, list) else None


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "title":
            self._inside_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title += data


def _safe_url(value: str | httpx.URL) -> str:
    parts = urlsplit(str(value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _html_title(response: httpx.Response) -> str | None:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        return None
    parser = _TitleParser()
    parser.feed(response.text[:10000])
    title = " ".join(parser.title.split())
    return title[:200] or None


async def main() -> None:
    settings = get_settings()
    if not settings.confluence_configured:
        print(
            "Confluence 配置不完整：请在 .env 中设置 "
            "CONFLUENCE_BASE_URL、CONFLUENCE_ROOT_PAGE_ID、"
            "CONFLUENCE_ACCESS_TOKEN。"
        )
        return

    base_url = settings.confluence_base_url.rstrip("/")
    page_id = settings.confluence_root_page_id.strip()
    headers = {
        "Authorization": (
            f"Bearer {settings.confluence_access_token.get_secret_value()}"
        ),
        "Accept": "application/json",
    }
    checks: dict[str, Any] = {
        "base_url": base_url,
        "root_page_id_configured": True,
        "token_configured": True,
    }

    async with httpx.AsyncClient(
        headers=headers,
        timeout=settings.confluence_timeout_seconds,
        follow_redirects=True,
    ) as client:
        page_response = await client.get(
            f"{base_url}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,space"},
        )
        checks["page_http_status"] = page_response.status_code
        if page_response.status_code in {401, 403}:
            checks["result"] = "unauthorized"
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return
        if page_response.status_code == 404:
            checks["result"] = "page_or_endpoint_not_found"
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return
        page_response.raise_for_status()
        page_payload = page_response.json()
        body = page_payload.get("body") if isinstance(page_payload, dict) else None
        storage = body.get("storage") if isinstance(body, dict) else None
        storage_value = storage.get("value") if isinstance(storage, dict) else None
        version = page_payload.get("version") if isinstance(page_payload, dict) else None
        checks["page_body_available"] = bool(storage_value)
        checks["page_body_length"] = len(storage_value or "")
        checks["page_version_available"] = bool(
            isinstance(version, dict) and version.get("number") is not None
        )

        children_response = await client.get(
            f"{base_url}/rest/api/content/{page_id}/child/page",
            params={"limit": 5},
        )
        checks["children_http_status"] = children_response.status_code
        child_rows: list[dict[str, Any]] = []
        if children_response.is_success:
            children_payload = children_response.json()
            checks["sample_child_page_count"] = _result_count(children_payload)
            if isinstance(children_payload, dict):
                child_rows = [
                    row
                    for row in children_payload.get("results", [])
                    if isinstance(row, dict) and row.get("id")
                ]

        sample_page_ids = [page_id, *(str(row["id"]) for row in child_rows)]
        downloadable = None
        attachment_statuses: list[int] = []
        total_sample_attachments = 0
        for sample_page_id in sample_page_ids:
            attachments_response = await client.get(
                f"{base_url}/rest/api/content/{sample_page_id}/child/attachment",
                params={"limit": 5, "expand": "version"},
            )
            attachment_statuses.append(attachments_response.status_code)
            if not attachments_response.is_success:
                continue
            attachments_payload = attachments_response.json()
            count = _result_count(attachments_payload) or 0
            total_sample_attachments += count
            rows = attachments_payload.get("results", [])
            downloadable = downloadable or next(
                (
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("_links"), dict)
                    and row["_links"].get("download")
                ),
                None,
            )
            if downloadable:
                break

        checks["sample_pages_checked_for_attachments"] = len(attachment_statuses)
        checks["attachment_http_statuses"] = attachment_statuses
        checks["sample_attachment_count"] = total_sample_attachments
        if downloadable:
            page_links = page_payload.get("_links", {})
            download_base = page_links.get("base") or base_url
            download_url = urljoin(
                f"{download_base.rstrip('/')}/",
                downloadable["_links"]["download"],
            )
            download_response = await client.get(download_url)
            checks["attachment_download_http_status"] = download_response.status_code
            checks["attachment_download_final_url"] = _safe_url(
                download_response.url
            )
            checks["attachment_download_redirects"] = [
                {
                    "status_code": item.status_code,
                    "target": _safe_url(
                        urljoin(str(item.url), item.headers.get("location", ""))
                    ),
                }
                for item in download_response.history
            ]
            checks["attachment_download_content_type"] = download_response.headers.get(
                "content-type"
            )
            checks["attachment_download_content_disposition"] = (
                download_response.headers.get("content-disposition")
            )
            checks["attachment_download_bytes"] = len(download_response.content)
            checks["attachment_download_html_title"] = _html_title(download_response)
            content_type = download_response.headers.get("content-type", "").lower()
            checks["attachment_binary_available"] = bool(
                download_response.status_code == 200
                and download_response.content
                and "text/html" not in content_type
                and "application/json" not in content_type
            )
        else:
            checks["attachment_download_tested"] = False
            checks["attachment_binary_available"] = False

    checks["result"] = (
        "source_ready"
        if checks.get("attachment_binary_available")
        else "page_ready_attachment_download_failed"
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.TimeoutException:
        print(json.dumps({"result": "timeout"}, ensure_ascii=False, indent=2))
    except httpx.HTTPStatusError as exc:
        print(
            json.dumps(
                {
                    "result": "http_error",
                    "status_code": exc.response.status_code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (httpx.RequestError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "result": "request_or_response_error",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
