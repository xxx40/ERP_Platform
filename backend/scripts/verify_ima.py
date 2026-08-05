import argparse
import asyncio
import sys
from typing import Any

from app.adapters.ima import ImaAdapter
from app.core.config import get_settings
from app.core.errors import AppError


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Verify IMA knowledge search.")
    parser.add_argument(
        "--query",
        default="苍穹开发",
        help="Search keyword or question. Use a keyword that can hit in IMA UI.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print sanitized IMA response structure for troubleshooting.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.ima_configured:
        print(
            "IMA is not configured. Please set IMA_CLIENT_ID, "
            "IMA_API_KEY and IMA_KNOWLEDGE_BASE_IDS in .env."
        )
        return

    adapter = ImaAdapter(settings)
    if args.debug:
        await debug_search(adapter, args.query)
        return

    try:
        chunks = await adapter.search(args.query, "verify-ima")
    except AppError as exc:
        print(f"IMA search failed: {exc.code} {exc.message}")
        return

    print(f"IMA search success. chunks={len(chunks)}")
    for index, chunk in enumerate(chunks[:5], start=1):
        print(f"\n[{index}] {chunk.title}")
        print(f"provider={chunk.metadata.get('provider')}")
        print(f"score={chunk.score}")
        print(f"evidence_eligible={chunk.metadata.get('evidence_eligible')}")
        if chunk.content:
            print(chunk.content[:300])
        else:
            print("content_excerpt=<empty; discovery-only result>")


async def debug_search(adapter: ImaAdapter, query: str) -> None:
    print(f"query={query}")
    print(f"configured_kb_count={len(adapter.settings.ima_kb_ids)}")
    print(f"query_variants={adapter._query_variants(query)}")

    for kb_id in adapter.settings.ima_kb_ids:
        print(f"\n=== knowledge_base_id={kb_id} ===")
        for variant in adapter._query_variants(query):
            print(f"\n--- variant={variant} ---")
            try:
                body = await adapter._post(
                    "openapi/wiki/v1/search_knowledge",
                    {"query": variant, "knowledge_base_id": kb_id, "cursor": ""},
                    "verify-ima-debug",
                )
            except AppError as exc:
                print(f"request_failed={exc.code} {exc.message}")
                continue

            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            info_list = data.get("info_list") if isinstance(data, dict) else None
            print(f"info_list_len={len(info_list) if isinstance(info_list, list) else 'not-list'}")
            if not isinstance(info_list, list):
                continue

            for index, row in enumerate(info_list[:5], start=1):
                if not isinstance(row, dict):
                    print(f"[{index}] non_dict_row={type(row).__name__}")
                    continue
                normalized = adapter._normalize(
                    {**row, "_knowledge_base_id": kb_id, "_query_variant": variant}
                )
                print(f"[{index}] title={safe(row.get('title'))}")
                print(f"    row_keys={sorted(row.keys())}")
                print(
                    "    string_field_lengths="
                    + str(
                        {
                            key: len(value)
                            for key, value in row.items()
                            if isinstance(value, str) and key != "media_id"
                        }
                    )
                )
                print(f"    normalized={'yes' if normalized else 'no'}")


def safe(value: Any, limit: int = 180) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


if __name__ == "__main__":
    asyncio.run(main())
