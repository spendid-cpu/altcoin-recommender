"""텔레그램 chat_id 확인용. 먼저 텔레그램 앱에서 만든 봇에게 아무 메시지나 한 번 보낸 뒤 이 스크립트를 실행한다."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src import config


async def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN이 .env에 없습니다. 먼저 .env에 토큰을 넣어주세요.")
        return

    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

    updates = data.get("result", [])
    if not updates:
        print("메시지가 없습니다. 텔레그램에서 봇을 검색해 아무 메시지나 보낸 뒤 다시 실행해주세요.")
        return

    chat = updates[-1]["message"]["chat"]
    print(f"chat_id = {chat['id']}  (이름: {chat.get('first_name', chat.get('title', ''))})")
    print(".env의 TELEGRAM_CHAT_ID에 이 값을 넣으세요.")


if __name__ == "__main__":
    asyncio.run(main())
