"""watchdog.yml(30분마다, scan.yml/track.yml과 별개의 동시성 그룹)이 부르는 1회 실행 진입점."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src import watchdog


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await watchdog.check(session)


if __name__ == "__main__":
    asyncio.run(main())
