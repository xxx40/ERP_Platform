import argparse
import asyncio
import sys
from typing import Any

import httpx

from app.core.config import get_settings


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="List IMA knowledge bases.")
    parser.add_argument(
        "--query",
        default="",
        help="Knowledge base name keyword. Empty string lists accessible knowledge bases.",
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.ima_client_id or not settings.ima_api_key:
        print("IMA credentials are missing. Please set IMA_CLIENT_ID and IMA_API_KEY in .env.")
        return

    headers = {
        "ima-openapi-clientid": settings.ima_client_id.get_secret_value(),
        "ima-openapi-apikey": settings.ima_api_key.get_secret_value(),
        "ima-openapi-ctx": "erp_assistant=list_kbs",
        "Content-Type": "application/json",
    }
    url = f"{settings.ima_base_url.rstrip('/')}/openapi/wiki/v1/search_knowledge_base"
    payload = {"query": args.query, "cursor": "", "limit": args.limit}

    async with httpx.AsyncClient(timeout=settings.ima_timeout_seconds) as client:
        response = await client.post(url, headers=headers, json=payload)

    print(f"HTTP {response.status_code}")
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        print(response.text[:500])
        return

    print(f"code={body.get('code')} msg={body.get('msg')}")
    if body.get("code") != 0:
        return

    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    info_list = data.get("info_list") if isinstance(data, dict) else []
    if not isinstance(info_list, list) or not info_list:
        print("No knowledge base found. Try a different --query keyword.")
        return

    print(f"found={len(info_list)}")
    for index, item in enumerate(info_list, start=1):
        if not isinstance(item, dict):
            continue
        kb_id = item.get("kb_id") or item.get("id") or item.get("knowledge_base_id")
        name = item.get("kb_name") or item.get("name") or item.get("title")
        print(f"\n[{index}] name={name}")
        print(f"    id={kb_id}")
        if item.get("description"):
            print(f"    description={item.get('description')}")
        if item.get("content_count") is not None:
            print(f"    content_count={item.get('content_count')}")
        if item.get("role_type") is not None:
            print(f"    role_type={item.get('role_type')}")
        extra_keys = sorted(
            key
            for key in item.keys()
            if key
            not in {
                "kb_id",
                "id",
                "knowledge_base_id",
                "kb_name",
                "name",
                "title",
                "description",
                "content_count",
                "role_type",
            }
        )
        if extra_keys:
            print(f"    extra_keys={', '.join(extra_keys)}")


if __name__ == "__main__":
    asyncio.run(main())
