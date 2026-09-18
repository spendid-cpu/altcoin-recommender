"""스캔을 15분마다(캔들 마감 직후) 계속 실행한다. Ctrl+C로 종료.
나중에 Railway 등에 배포할 때는 이 스크립트를 그대로 프로세스로 띄우면 된다."""

import asyncio
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.pipeline import run_once

INTERVAL_MINUTES = 15
BUFFER_SECONDS = 20  # 캔들 마감 직후 업비트 쪽 데이터 반영 시간을 위한 여유


def seconds_until_next_boundary(interval_minutes: int = INTERVAL_MINUTES, buffer_seconds: int = BUFFER_SECONDS) -> float:
    now = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = (now - epoch).total_seconds()
    interval = interval_minutes * 60
    next_boundary_epoch = (int(elapsed // interval) + 1) * interval
    next_boundary = epoch + timedelta(seconds=next_boundary_epoch)
    return (next_boundary - now).total_seconds() + buffer_seconds


async def main() -> None:
    print(f"스케줄러 시작: {INTERVAL_MINUTES}분마다 스캔 (캔들 마감 + {BUFFER_SECONDS}초 뒤)")
    async with aiohttp.ClientSession() as session:
        while True:
            started = datetime.now(timezone.utc)
            print(f"\n===== 스캔 시작 {started.strftime('%Y-%m-%d %H:%M:%S')} UTC =====")
            try:
                await run_once(session)
            except Exception:
                print("스캔 중 오류 발생, 이번 주기는 건너뛰고 다음 주기에 재시도합니다:")
                traceback.print_exc()

            wait_seconds = seconds_until_next_boundary()
            next_time = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            print(f"다음 스캔: {next_time.strftime('%Y-%m-%d %H:%M:%S')} UTC ({wait_seconds:.0f}초 뒤)")
            await asyncio.sleep(wait_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n스케줄러 종료")
