"""텔레그램 봇 알림 발송."""

import aiohttp

from src import config

_API_BASE = "https://api.telegram.org"


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


async def send_message(session: aiohttp.ClientSession, text: str) -> None:
    if not is_configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않았습니다 (.env 확인)")
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()
