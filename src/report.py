"""추천한 종목의 현황 리포트. 추천가 대비 현재가, 고점 대비 하락, 발굴 시 점수와 현재 점수의 변화를
정기적으로 텔레그램으로 보낸다 (익절 여부를 스스로 판단할 때 참고하는 숫자들이고, 자동 매매나 매도 권유가 아니다)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, state_store, telegram_client
from src.formatting import fmt_pct, fmt_price, frames_text, strategy_tag
from src.scoring import CandidateResult

KST = ZoneInfo("Asia/Seoul")
LAST_REPORT_KEY = "last_report_at"
MAX_LISTED = 12  # 텔레그램 메시지 길이 제한(4096자) 안에서 보여줄 종목 수


def elapsed_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}분"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}시간 {rest}분"
    return f"{hours // 24}일 {hours % 24}시간"


def score_line(rec: dict, cand: CandidateResult | None, btc_filter_on: bool) -> str:
    """발굴 때 점수와 지금 점수의 변화 한 줄 (현황 리포트와 종료 알림에서 같이 쓴다)."""
    start = f"{rec['score']:.1f}" if rec["score"] is not None else "—"
    if cand is not None:
        tail = f" · 5분 저점 {'✅' if cand.low_15m else '대기'}{' · 타점 ✅' if cand.entry_ready else ''}"
        return f"📐 점수 {start} → {cand.total_score:.1f} ({cand.grade}) · {frames_text(cand.cleared_frames)}{tail}"
    if btc_filter_on:
        return f"📐 점수 {start} → ⚠️ 조건 이탈 (일봉 게이트 미통과)"
    return f"📐 점수 {start} → 확인 불가 (BTC 필터 꺼짐)"


def active_recommendations(now: datetime) -> list[dict]:
    """지금 추적 중인 추천 (추적 기간 안이고 아직 종료되지 않은 것)."""
    cutoff = now - timedelta(days=price_tracker.TRACK_DAYS)
    return [r for r in price_tracker.load_recommendations()
            if r["entered_at"] >= cutoff and r["exit"] is None and r["strategy"] != "original"]  # 최초는 조용히 기록만


def build_report(
    now: datetime,
    recs: list[dict],
    prices: dict[str, float],
    candidates: list[CandidateResult],
    btc_filter_on: bool,
) -> str | None:
    if not recs:
        return None
    candidate_by_market = {c.market: c for c in candidates}
    blocks = []
    # 수익률이 높은 순으로 보여준다
    rows = []
    for rec in recs:
        market, entry = rec["market"], rec["entry_price"]
        current = prices.get(market) or (rec["snaps"][-1][1] if rec["snaps"] else entry)
        peak = max([entry, current] + [p for _, p in rec["snaps"]])
        rows.append((rec, current, peak, (current / entry - 1) * 100 if entry else 0.0))
    rows.sort(key=lambda r: r[3], reverse=True)

    for rec, current, peak, ret in rows[:MAX_LISTED]:
        entry = rec["entry_price"]
        peak_ret = (peak / entry - 1) * 100 if entry else 0.0
        from_peak = (current / peak - 1) * 100 if peak else 0.0
        entered_kst = rec["entered_at"].astimezone(KST).strftime("%m-%d %H:%M")

        if rec["strategy"] == "cycle":
            half = rec["half"]
            state_line = (f"✂️ 절반 매도 {fmt_price(half['price'])} ({(half['price'] / entry - 1) * 100:+.2f}%) · 나머지 트레일링 중"
                          if half else "✂️ 절반 매도 신호 대기 (4시간 단기 80+ 와 거래량 폭발)")
        else:
            state_line = score_line(rec, candidate_by_market.get(rec["market"]), btc_filter_on)
        blocks.append(
            f"{strategy_tag(rec)}  {rec['market']}  {fmt_pct(ret)}\n"
            f"🕐 {entered_kst} 추천 ({elapsed_text(now - rec['entered_at'])} 경과)\n"
            f"💰 {fmt_price(entry)} → {fmt_price(current)}\n"
            f"🏔 최고 {peak_ret:+.2f}% · 고점 대비 {from_peak:+.2f}%\n"
            f"{state_line}"
        )

    extra = f"\n\n외 {len(rows) - MAX_LISTED}종목은 대시보드에서 확인하세요." if len(rows) > MAX_LISTED else ""
    header = f"📊 추천 현황 · {now.astimezone(KST).strftime('%m-%d %H:%M')} KST · 추적 {len(rows)}종목"
    return header + "\n\n" + "\n\n".join(blocks) + extra


def report_due(now: datetime) -> bool:
    if config.REPORT_INTERVAL_HOURS <= 0:
        return False
    last = state_store.get_meta(LAST_REPORT_KEY)
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(hours=config.REPORT_INTERVAL_HOURS)


async def maybe_send_report(
    session: aiohttp.ClientSession,
    prices: dict[str, float],
    candidates: list[CandidateResult],
    btc_filter_on: bool,
) -> bool:
    """간격이 지났고 추적 중인 추천이 있으면 리포트를 보낸다. 보냈으면 True."""
    now = datetime.now(timezone.utc)
    if not report_due(now):
        return False
    text = build_report(now, active_recommendations(now), prices, candidates, btc_filter_on)
    if text is None:
        return False
    print(text)
    if telegram_client.is_configured():
        await telegram_client.send_message(session, text)
    # 텔레그램이 꺼져 있어도 발송 시각은 기록해 콘솔 출력이 매 스캔마다 반복되지 않게 한다
    state_store.set_meta(LAST_REPORT_KEY, now.isoformat())
    return True
