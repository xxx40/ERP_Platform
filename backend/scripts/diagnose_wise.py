import asyncio
import json
import sys

import httpx

from app.core.config import get_settings


async def main() -> None:
    query = " ".join(sys.argv[1:]).strip() or "采购订单审核后应该如何完成收料？"
    settings = get_settings()
    if not settings.wise_api_key or not settings.wise_kb_ids:
        print("WISE API Key 或知识库 ID 未配置")
        return

    headers = {
        "X-API-Key": settings.wise_api_key.get_secret_value(),
        "Content-Type": "application/json",
        "X-Request-ID": "wise-diagnose",
    }
    payload = {
        "arguments": {
            "queries": [query],
            "knowledge_base_ids": settings.wise_kb_ids,
        }
    }
    url = (
        f"{settings.wise_base_url.rstrip('/')}"
        "/agent-tools/knowledge_search/invoke"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=payload)

    print(
        json.dumps(
            {
                "url": url,
                "query": query,
                "knowledge_base_count": len(settings.wise_kb_ids),
            },
            ensure_ascii=False,
        )
    )
    print(f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        print(response.text[:1000])
        return

    result = body.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    rows = data.get("results") if isinstance(data, dict) else None
    summary = {
        "top_level_keys": list(body.keys()),
        "ok": body.get("ok"),
        "data_type": type(data).__name__,
        "data_count": len(rows) if isinstance(rows, list) else None,
    }
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            summary["first_item_keys"] = list(first.keys())
            summary["first_title"] = first.get("knowledge_title") or first.get("knowledge_filename")
            summary["first_content_length"] = len(str(first.get("content") or ""))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
