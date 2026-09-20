"""기존 전략 추천의 종료 판단과 알림. 종료 규칙(익절, 손절, 고점 대비 되돌림, 기간 만료)에 걸린 추천은 최종 결과를
텔레그램으로 알리고 추적을 끝낸다. 규칙의 값은 config.EXIT_* (저장소 변수)로 바꾼다.
매도 권유가 아니라 정해둔 기준에 도달했다는 알림이다."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, telegram_client
from src.formatting import fmt_pct, fmt_price, grade_badge
from src.report import elapsed_text, score_line
from src.scoring import CandidateResult

KST = ZoneInfo("Asia/Seoul")
REASONS = {
    "take_profit": "💰 익절 기준 도달",
    "stop_loss": "🛑 손절 기준 도달",
    "trailing": "📉 고점 대비 되돌림",
    "expired": "⏰ 추적 기간 만료",
}
# 이 기간보다 오래전에 이미 만료된 추천(기능 도입 전 기록 등)은 알림 없이 조용히 종료 처리한다
EXPIRY_NOTICE_WINDOW = timedelta(days=1)


def check_exit(rec: dict, current: float, now: datetime) -> str | None:
    """종료 사유(REASONS의 키) 또는 None. 같은 시점에 여러 개가 걸리면 손절 > 되돌림 > 익절 > 만료 순."""
    entry = rec["entry_price"]
    if not entry:
        return None
    ret = (current / entry - 1) * 100
    peak = max([entry, current] + [p for _, p in rec["snaps"]])
    peak_ret = (peak / entry - 1) * 100

    if config.EXIT_STOP_LOSS_PCT is not None and ret <= -config.EXIT_STOP_LOSS_PCT:
        return "stop_loss"
    if config.EXIT_TRAIL_ARM_PCT is not None and config.EXIT_TRAIL_DD_PCT is not None:
        if peak_ret >= config.EXIT_TRAIL_ARM_PCT and (current / peak - 1) * 100 <= -config.EXIT_TRAIL_DD_PCT:
            return "trailing"
    if config.EXIT_TAKE_PROFIT_PCT is not None and ret >= config.EXIT_TAKE_PROFIT_PCT:
        return "take_profit"
    if now - rec["entered_at"] >= timedelta(days=price_tracker.TRACK_DAYS):
        return "expired"
    return None


def build_exit_message(
    rec: dict, reason: str, exit_price: float, now: datetime, cand: CandidateResult | None, btc_filter_on: bool
) -> str:
    entry = rec["entry_price"]
    ret = (exit_price / entry - 1) * 100
    peak = max([entry, exit_price] + [p for _, p in rec["snaps"]])
    entered_kst = rec["entered_at"].astimezone(KST).strftime("%m-%d %H:%M")
    return (
        f"🏁 [수정판] 추천 종료 · {REASONS[reason]}\n"
        f"{grade_badge(rec['grade'])}  {rec['market']}  {fmt_pct(ret)}\n"
        f"💰 {fmt_price(entry)} → {fmt_price(exit_price)}\n"
        f"🕐 {entered_kst} 추천 → {elapsed_text(now - rec['entered_at'])} 보유\n"
        f"🏔 최고 {(peak / entry - 1) * 100:+.2f}%\n"
        f"{score_line(rec, cand, btc_filter_on)}"
    )


async def process_exits(
    session: aiohttp.ClientSession,
    prices: dict[str, float],
    candidates: list[CandidateResult],
    btc_filter_on: bool,
) -> int:
    """종료 규칙에 걸린 추천을 종료 처리하고 알림을 보낸다. 종료한 개수를 돌려준다."""
    now = datetime.now(timezone.utc)
    candidate_by_market = {c.market: c for c in candidates}
    closed = 0
    for rec in price_tracker.load_recommendations():
        if rec["exit"] is not None or rec["strategy"] not in ("legacy", "original"):
            continue  # 사이클 전략 추천은 cycle_exits가 다룬다
        age = now - rec["entered_at"]
        if age < timedelta(days=price_tracker.TRACK_DAYS):
            current = prices.get(rec["market"])
            if current is None:
                continue  # 이번 스캔에서 시세를 못 받았으면 다음 사이클에 판단
        else:
            # 추적이 이미 끝난 종목이라 현재가를 따로 받지 않는다 — 마지막으로 기록한 가격이 만료 시점 가격이다
            current = rec["snaps"][-1][1] if rec["snaps"] else rec["entry_price"]

        reason = check_exit(rec, current, now)
        if reason is None:
            continue
        ret = (current / rec["entry_price"] - 1) * 100
        price_tracker.record_exit(rec["market"], rec["entered_at"], current, ret, reason)
        closed += 1

        text = build_exit_message(rec, reason, current, now, candidate_by_market.get(rec["market"]), btc_filter_on)
        # 최초 알고리즘 추천은 기록만 한다 (텔레그램에 알리지 않음)
        silent = rec["strategy"] == "original" or (
            reason == "expired" and age >= timedelta(days=price_tracker.TRACK_DAYS) + EXPIRY_NOTICE_WINDOW)
        print(text if not silent else f"(조용히 종료) {rec['market']} {reason}")
        if not silent and telegram_client.is_configured():
            await telegram_client.send_message(session, text)
    return closed
