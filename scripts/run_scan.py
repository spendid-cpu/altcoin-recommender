"""전체 스캔을 한 번만 실행한다. 계속 실행하려면 scripts/run_loop.py를 쓴다."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.pipeline import run_once


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await run_once(session)


if __name__ == "__main__":
    asyncio.run(main())
