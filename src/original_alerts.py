"""대조군(original)이 메인 알림일 때(config.ALERT_STRATEGY) 쓰는 묶음 알림. 대조군은 하루 한 번(일봉 마감 직후)
여러 종목이 한꺼번에 나오고 만료도 한꺼번에 오므로, 종목마다 메시지를 보내지 않고 한 통(길면 여러 통)으로 묶는다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from src import config, original_scanner, price_tracker, telegram_client
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


REPEAT_MARK_DAYS = 7  # 이 기간 안에 이미 추천했던 종목이면 '재추천'으로 표시한다 (표시만, 추천 규칙은 그대로)
_PRIOR_LABEL = {"take_profit": "익절", "stop_loss": "손절", "expired": "만료", "trailing": "되돌림", "btc_exit": "BTC 이탈"}


def prior_entries(recs: list[dict], markets: list[str], now: datetime) -> dict[str, dict]:
    """종목별로 직전 대조군 추천이 REPEAT_MARK_DAYS일 안에 있었으면 {days, label}. recs는 price_tracker.load_recommendations() 결과.
    새 진입을 기록하기 전에 불러야 한다."""
    wanted = set(markets)
    latest: dict[str, dict] = {}
    for r in recs:
        if r["strategy"] != "original" or r["market"] not in wanted or (r["exit"] and r["exit"]["reason"] == "excluded"):
            continue
        if r["market"] not in latest or r["entered_at"] > latest[r["market"]]["entered_at"]:
            latest[r["market"]] = r
    out = {}
    for market, r in latest.items():
        days = (now - r["entered_at"]).total_seconds() / 86400
        if days > REPEAT_MARK_DAYS:
            continue
        if r["exit"]:
            label = f"{_PRIOR_LABEL.get(r['exit']['reason'], '종료')} {r['exit']['return_pct']:+.1f}%"
        else:
            last = r["snaps"][-1][1] if r["snaps"] else r["entry_price"]
            label = f"진행 중 {(last / r['entry_price'] - 1) * 100:+.1f}%" if r["entry_price"] else "진행 중"
        out[market] = {"days": days, "label": label}
    return out


def entry_lines(entries: list[tuple]) -> list[str]:
    """entries: (종목, 추천가, 점수, 변동폭%, 직전 추천 정보 또는 None[, 추천 순간 호가 매수 비중 0~1]) 목록 -> 종목별 한 줄.
    새 추천을 먼저, 재추천을 뒤에 둔다. 변동폭·재추천·호가 표식은 참고용이고 추천 대상은 그대로다."""
    lines = []
    for market, price, score, vol_pct, prior, *rest in sorted(entries, key=lambda e: e[4] is not None):
        ob_imb = rest[0] if rest else None
        vol = f" · 변동폭 {vol_pct:.1f}%" if vol_pct is not None else ""
        ob = f" · 호가 매수 {ob_imb * 100:.0f}%" if ob_imb is not None else ""
        rep = f" · 🔁 {prior['days']:.0f}일 전에도 추천({prior['label']})" if prior else ""
        lines.append(f"{original_scanner.vol_icon(vol_pct) or '▫️'} {market}  {fmt_price(price)} · {_targets(price)} · 점수 {score:.1f}{vol}{ob}{rep}")
    return lines


def entry_header(count: int, now: datetime, repeat: int = 0, has_book: bool = False) -> str:
    rules = []
    if config.EXIT_TAKE_PROFIT_PCT is not None:
        rules.append(f"익절 +{config.EXIT_TAKE_PROFIT_PCT:g}%")
    if config.EXIT_STOP_LOSS_PCT is not None:
        rules.append(f"손절 -{config.EXIT_STOP_LOSS_PCT:g}%")
    rules.append(f"{price_tracker.TRACK_DAYS:g}일 뒤 만료")
    return (
        f"🆕 대조군 추천 {count}종목{f' (새 추천 {count - repeat} · 재추천 {repeat})' if repeat else ''} · {now.astimezone(KST):%m-%d %H:%M} KST\n일봉 마감 기준 · " + " / ".join(rules)
        + f"\n🔹 변동폭 낮음 · 🔸 높음 (14일 평균 일중 등락폭, 기준 {config.VOL_MARK_PCT:g}%) — 표시만이고 추천 대상은 그대로예요"
        + ("\n📚 호가 매수 N% = 추천 순간 호가창(현재가 위아래 30단계)에서 사려는 주문의 비중 · 순간 사진이라 참고만" if has_book else "")
        + (f"\n🔁 최근 {REPEAT_MARK_DAYS}일 안에 이미 추천했던 종목이에요 (3일 제한이 풀려 일봉 조건을 다시 통과)" if repeat else "")
    )


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
    vol_mark = original_scanner.vol_icon((rec.get("detail") or {}).get("vol14_pct"))
    return (
        f"{EXIT_ICON.get(reason, reason)}  {vol_mark}{rec['market']}  {icon} {ret:+.2f}% · {fmt_price(entry)} → {fmt_price(exit_price)}"
        f" · {elapsed_text(at - rec['entered_at'])} 보유{note}"
    )


def exit_header(count: int, now: datetime) -> str:
    return f"🏁 대조군 추천 종료 {count}건 · {now.astimezone(KST):%m-%d %H:%M} KST"
