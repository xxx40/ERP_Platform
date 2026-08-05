import asyncio

from app.adapters.model import ModelAdapter
from app.core.config import get_settings


async def main() -> None:
    adapter = ModelAdapter(get_settings())
    result = await adapter.answer_general("请用一句话说明只读企业 Agent 的作用。")
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
