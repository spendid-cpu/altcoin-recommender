"""추천 종목 추적을 한 번만 실행한다 (전체 시장 스캔 없이 가격만 확인). 5분마다 반복 실행하는 건
GitHub Actions 워크플로(track.yml)가 한다. 새 후보를 찾으려면 scripts/run_scan.py를 쓴다."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.tracker_job import run_once


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        await run_once(session)


if __name__ == "__main__":
    asyncio.run(main())
