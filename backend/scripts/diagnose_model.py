import asyncio
import json
import os
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings


async def main() -> None:
    settings = get_settings()
    if not settings.anthropic_auth_token:
        print("模型令牌未配置")
        return

    token = settings.anthropic_auth_token.get_secret_value()
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if settings.anthropic_auth_mode.lower() == "x-api-key":
        headers["x-api-key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 32,
        "temperature": 0,
        "messages": [{"role": "user", "content": "只回复 OK"}],
    }
    if os.getenv("DIAGNOSE_DISABLE_THINKING", "").lower() in {"1", "true", "yes"}:
        payload["thinking"] = {"type": "disabled"}
    url = f"{settings.anthropic_base_url.rstrip('/')}/v1/messages"
    print(
        json.dumps(
            {
                "url": url,
                "model": settings.anthropic_model,
                "auth_mode": settings.anthropic_auth_mode,
                "thinking_disabled": "thinking" in payload,
            },
            ensure_ascii=False,
        )
    )
    try:
        async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        print(f"网络请求失败：{type(exc).__name__}: {exc}")
        return

    print(f"HTTP {response.status_code}")
    try:
        body = response.json()
        print(json.dumps(body, ensure_ascii=False, indent=2)[:3000])
    except ValueError:
        print(response.text[:1000])

    if response.status_code >= 400:
        models_url = f"{settings.anthropic_base_url.rstrip('/')}/v1/models"
        async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
            models_response = await client.get(models_url, headers=headers)
        print(f"GET /v1/models -> HTTP {models_response.status_code}")
        try:
            models_body = models_response.json()
            model_ids = [
                item.get("id")
                for item in models_body.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
            if model_ids:
                print(json.dumps({"available_model_ids": model_ids}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(models_body, ensure_ascii=False, indent=2)[:3000])
        except ValueError:
            print(models_response.text[:1000])


if __name__ == "__main__":
    asyncio.run(main())
