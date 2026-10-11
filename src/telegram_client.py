"""텔레그램 봇 알림 발송."""

import aiohttp

from src import config

_API_BASE = "https://api.telegram.org"


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


async def send_message(session: aiohttp.ClientSession, text: str, reply_markup: dict | None = None) -> None:
    """텍스트 알림을 보낸다. reply_markup(예: 인라인 버튼 {"inline_keyboard": [[...]]})을 주면 메시지에 버튼이 붙는다."""
    if not is_configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 설정되지 않았습니다 (.env 확인)")
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()


async def get_updates(session: aiohttp.ClientSession, offset: int) -> list[dict]:
    """아직 못 받은 버튼 누름(callback_query)만 받는다. offset은 마지막으로 처리한 update_id + 1."""
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    payload = {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]}
    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()
        return (await resp.json()).get("result", [])


async def answer_callback(session: aiohttp.ClientSession, callback_id: str, text: str) -> None:
    """버튼 누름에 응답한다(누른 사람 화면에 짧게 뜨는 알림). 실패해도 처리 자체는 이미 끝났으므로 오류는 삼킨다."""
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        async with session.post(url, json={"callback_query_id": callback_id, "text": text}) as resp:
            resp.raise_for_status()
    except Exception as exc:
        print(f"  [텔레그램] 버튼 응답 실패(무시): {exc!r}")


async def clear_buttons(session: aiohttp.ClientSession, chat_id: str, message_id: int) -> None:
    """처리가 끝난 메시지에서 버튼을 없앤다 (같은 버튼을 두 번 누르지 않게)."""
    url = f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup"
    async with session.post(url, json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}) as resp:
        resp.raise_for_status()
