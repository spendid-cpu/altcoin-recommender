"""사이클 전략 추천의 절반 매도 신호와 종료 판단. 백테스트(65일)의 매도 규칙을 그대로 따른다.

  - 절반 매도 전: 추천가 대비 -CYCLE_STOP_PCT% 이하면 전량 손절
  - 4시간 단기 %K가 80 이상이고 그 4시간 봉 거래량이 직전 20개 평균의 2배 이상이면 절반 매도 신호 (진입 이후 마감된 봉만)
  - 절반 매도 뒤: 나머지 절반은 최고가 대비 -CYCLE_TRAIL_PCT% 트레일링 (신호가 뜬 그 스캔에서는 판단하지 않는다)
  - 진입 후 TRACK_DAYS(3일)가 지나면 남은 것 전부 정리
수익률은 두 몫의 평균(절반 매도 가격 수익률과 나머지 수익률의 평균)이다. 매도 권유가 아니라 정해둔 기준에 도달했다는 알림이다."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import config, cycle_signals, price_tracker, telegram_client
from src.exchanges import upbit_client
from src.formatting import fmt_pct, fmt_price
from src.report import elapsed_text

KST = ZoneInfo("Asia/Seoul")
REASONS = {
    "stop_loss": "🛑 손절 (전량)",
    "half_trailing": "📉 절반 매도 후 고점 대비 되돌림",
    "expired": "⏰ 추적 기간 만료 (전량 정리)",
    "half_expired": "⏰ 절반 매도 후 추적 기간 만료",
}
EXPIRY_NOTICE_WINDOW = timedelta(days=1)


def _peak(rec: dict, current: float, half_price: float | None) -> float:
    prices = [rec["entry_price"], current] + [p for _, p in rec["snaps"]]
    if half_price is not None:
        prices.append(half_price)
    return max(prices)


def blended_return(entry: float, current: float, half_price: float | None) -> float:
    ret = (current / entry - 1) * 100
    if half_price is None:
        return ret
    return 0.5 * ((half_price / entry - 1) * 100) + 0.5 * ret


def check_exit(rec: dict, current: float, now: datetime, half_price: float | None, just_halved: bool) -> str | None:
    """종료 사유(REASONS의 키) 또는 None."""
    entry = rec["entry_price"]
    if not entry:
        return None
    ret = (current / entry - 1) * 100
    if half_price is None and ret <= -config.CYCLE_STOP_PCT:
        return "stop_loss"
    if half_price is not None and not just_halved:
        peak = _peak(rec, current, half_price)
        if current <= peak * (1 - config.CYCLE_TRAIL_PCT / 100):
            return "half_trailing"
    if now - rec["entered_at"] >= timedelta(days=price_tracker.TRACK_DAYS):
        return "half_expired" if half_price is not None else "expired"
    return None


def build_half_message(rec: dict, price: float, now: datetime) -> str:
    entry = rec["entry_price"]
    return (
        f"✂️ 사이클 절반 매도 신호\n"
        f"{rec['market']}  {fmt_pct((price / entry - 1) * 100)}\n"
        f"💰 {fmt_price(entry)} → {fmt_price(price)} · {elapsed_text(now - rec['entered_at'])} 경과\n"
        f"4시간 단기 80+ 와 거래량 폭발이 함께 나왔습니다. 나머지 절반은 고점 대비 -{config.CYCLE_TRAIL_PCT:g}% 트레일링으로 이어집니다."
    )


def build_exit_message(rec: dict, reason: str, current: float, half_price: float | None, ret: float, now: datetime) -> str:
    entry = rec["entry_price"]
    peak = _peak(rec, current, half_price)
    entered_kst = rec["entered_at"].astimezone(KST).strftime("%m-%d %H:%M")
    lines = [
        f"🏁 사이클 추천 종료 · {REASONS[reason]}",
        f"{rec['market']}  {fmt_pct(ret)}" + ("  (절반 매도 반영)" if half_price is not None else ""),
        f"💰 {fmt_price(entry)} → {fmt_price(current)}",
    ]
    if half_price is not None:
        lines.append(f"✂️ 절반 매도 {fmt_price(half_price)} ({(half_price / entry - 1) * 100:+.2f}%) · 나머지 {(current / entry - 1) * 100:+.2f}%")
    lines += [
        f"🕐 {entered_kst} 추천 → {elapsed_text(now - rec['entered_at'])} 보유",
        f"🏔 최고 {(peak / entry - 1) * 100:+.2f}%",
    ]
    return "\n".join(lines)


async def _new_half_signal(session: aiohttp.ClientSession, rec: dict) -> bool:
    """진입 이후 마감된 4시간 봉에서 절반 매도 신호가 났는지."""
    h4 = await upbit_client.fetch_candles(session, rec["market"], "4h", count=200)
    if h4.empty:
        return False
    return bool(cycle_signals.half_sale_bars(h4, rec["entered_at"]))


async def process_cycle_exits(session: aiohttp.ClientSession, prices: dict[str, float]) -> tuple[int, int]:
    """절반 매도 신호를 기록·알리고 종료 규칙에 걸린 사이클 추천을 종료한다. (새 절반 매도 수, 종료 수)를 돌려준다."""
    now = datetime.now(timezone.utc)
    halves = closed = 0
    for rec in price_tracker.load_recommendations():
        if rec["strategy"] != "cycle" or rec["exit"] is not None:
            continue
        age = now - rec["entered_at"]
        if age < timedelta(days=price_tracker.TRACK_DAYS):
            current = prices.get(rec["market"])
            if current is None:
                continue  # 시세를 못 받았으면 다음 사이클에 판단
        else:
            current = rec["snaps"][-1][1] if rec["snaps"] else rec["entry_price"]

        half = rec["half"]
        just_halved = False
        # 손절이 먼저다 (백테스트와 같은 순서): 절반 매도 전에 -5%를 밟았다면 신호를 볼 필요 없이 전량 정리
        stopped = half is None and rec["entry_price"] and (current / rec["entry_price"] - 1) * 100 <= -config.CYCLE_STOP_PCT
        if half is None and not stopped and age < timedelta(days=price_tracker.TRACK_DAYS):
            try:
                if await _new_half_signal(session, rec):
                    price_tracker.record_half(rec["market"], rec["entered_at"], now, current)
                    half = {"at": now, "price": current}
                    just_halved = True
                    halves += 1
                    text = build_half_message(rec, current, now)
                    print(text)
                    if telegram_client.is_configured():
                        await telegram_client.send_message(session, text)
            except Exception as exc:
                print(f"  {rec['market']} 절반 매도 신호 확인 실패(다음 사이클에 다시): {exc}")

        half_price = half["price"] if half else None
        reason = check_exit(rec, current, now, half_price, just_halved)
        if reason is None:
            continue
        ret = blended_return(rec["entry_price"], current, half_price)
        price_tracker.record_exit(rec["market"], rec["entered_at"], current, ret, reason)
        closed += 1

        text = build_exit_message(rec, reason, current, half_price, ret, now)
        silent = reason in ("expired", "half_expired") and age >= timedelta(days=price_tracker.TRACK_DAYS) + EXPIRY_NOTICE_WINDOW
        print(text if not silent else f"(조용히 종료) {rec['market']} {reason}")
        if not silent and telegram_client.is_configured():
            await telegram_client.send_message(session, text)
    return halves, closed
