"""주식 주봉 RSI 과매도 후보 스캔 1회 실행 (stock.yml이 부른다). 같은 마감 주봉은 한 번만 처리하고, 지수·진행 중 종목 현재가는 매번 갱신한다.
기본은 대시보드용 기록만 한다. 텔레그램은 환경변수 STOCK_NOTIFY=true일 때만 보낸다.
--force: 이미 처리한 주봉도 다시, --dry: DB·처리 표시 없이 계산·출력만."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src import stock_scan


async def main(force: bool, dry: bool) -> None:
    notify = (os.environ.get("STOCK_NOTIFY") or "").strip().lower() == "true"
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=60)) as session:
        print(await stock_scan.run(session, force=force, notify=notify, dry=dry))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.force, a.dry))
