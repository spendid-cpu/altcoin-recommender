"""대조군(original)이 메인 알림일 때(config.ALERT_STRATEGY) 쓰는 묶음 알림. 대조군은 하루 한 번(일봉 마감 직후)
여러 종목이 한꺼번에 나오고 만료도 한꺼번에 오므로, 종목마다 메시지를 보내지 않고 한 통(길면 여러 통)으로 묶는다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, telegram_client
from src.formatting import fmt_price
from src.report import elapsed_text

KST = ZoneInfo("Asia/Seoul")
CHUNK_CHARS = 3500  # 텔레그램 한 통 4096자 제한 안


def _targets(price: float) -> str:
    parts = []
    if config.EXIT_TAKE_PROFIT_PCT is not None:
        parts.append(f"익절 {fmt_price(price * (1 + config.EXIT_TAKE_PROFIT_PCT / 100))}")
    if config.EXIT_STOP_LOSS_PCT is not None:
        parts.append(f"손절 {fmt_price(price * (1 - config.EXIT_STOP_LOSS_PCT / 100))}")
    return " / ".join(parts)


def entry_lines(entries: list[tuple[str, float, float]]) -> list[str]:
    """entries: (종목, 추천가, 점수) 목록 -> 종목별 한 줄."""
    return [f"{market}  {fmt_price(price)} · {_targets(price)} · 점수 {score:.1f}" for market, price, score in entries]


def entry_header(count: int, now: datetime) -> str:
    rules = []
    if config.EXIT_TAKE_PROFIT_PCT is not None:
        rules.append(f"익절 +{config.EXIT_TAKE_PROFIT_PCT:g}%")
    if config.EXIT_STOP_LOSS_PCT is not None:
        rules.append(f"손절 -{config.EXIT_STOP_LOSS_PCT:g}%")
    rules.append(f"{price_tracker.TRACK_DAYS:g}일 뒤 만료")
    return f"🆕 대조군 추천 {count}종목 · {now.astimezone(KST):%m-%d %H:%M} KST\n일봉 마감 기준 · " + " / ".join(rules)


def chunk(header: str, lines: list[str]) -> list[str]:
    """header를 앞에 붙여 CHUNK_CHARS 안으로 나눈다. 두 번째 통부터는 header 첫 줄에 '(이어서)'를 붙인다."""
    chunks, current = [], []
    size = len(header)
    for line in lines:
        if current and size + len(line) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], len(header) + 6
        current.append(line)
        size += len(line) + 2
    if current:
        chunks.append(current)
    out = []
    for i, part in enumerate(chunks):
        head = header if i == 0 else header.split("\n")[0] + " (이어서)"
        out.append(head + "\n\n" + "\n".join(part))
    return out


async def send_all(session: aiohttp.ClientSession, header: str, lines: list[str]) -> None:
    if not lines or not telegram_client.is_configured():
        return
    for text in chunk(header, lines):
        await telegram_client.send_message(session, text)


EXIT_ICON = {
    "take_profit": "💰 익절", "stop_loss": "🛑 손절", "trailing": "📉 되돌림", "expired": "⏰ 만료", "btc_exit": "🪙 BTC 이탈",
}


def exit_line(rec: dict, reason: str, exit_price: float, at: datetime, restored: bool = False) -> str:
    entry = rec["entry_price"]
    ret = (exit_price / entry - 1) * 100
    icon = "🔺" if ret > 0 else "🔻" if ret < 0 else "➖"
    note = " ⏪공백 복원" if restored else ""
    return (
        f"{EXIT_ICON.get(reason, reason)}  {rec['market']}  {icon} {ret:+.2f}% · {fmt_price(entry)} → {fmt_price(exit_price)}"
        f" · {elapsed_text(at - rec['entered_at'])} 보유{note}"
    )


def exit_header(count: int, now: datetime) -> str:
    return f"🏁 대조군 추천 종료 {count}건 · {now.astimezone(KST):%m-%d %H:%M} KST"
