import asyncio
import sys
from uuid import uuid4

from app.adapters.wise import WiseAdapter
from app.core.config import get_settings


async def main() -> None:
    query = " ".join(sys.argv[1:]).strip() or "采购订单审核后应该如何完成收料？"
    chunks = await WiseAdapter(get_settings()).search(
        query,
        uuid4().hex,
    )
    print(f"查询：{query}")
    print(f"WISE 返回 {len(chunks)} 个有效片段")
    for chunk in chunks[:5]:
        print(f"- {chunk.title} | score={chunk.score}")


if __name__ == "__main__":
    asyncio.run(main())
