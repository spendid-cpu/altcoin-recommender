"""추천한 종목의 현황 리포트. 추천가 대비 현재가, 고점 대비 하락, 발굴 시 점수와 현재 점수의 변화를
정기적으로 텔레그램으로 보낸다 (익절 여부를 스스로 판단할 때 참고하는 숫자들이고, 자동 매매나 매도 권유가 아니다)."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, state_store, telegram_client
from src.formatting import fmt_pct, fmt_price, frames_text, strategy_tag
from src.scoring import CandidateResult

KST = ZoneInfo("Asia/Seoul")
LAST_REPORT_KEY = "last_report_at"
LAST_FULL_REPORT_KEY = "last_full_report_at"
SNAPSHOT_KEY = "report_snapshot"
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
            if r["entered_at"] >= cutoff and r["exit"] is None and r["strategy"] != "original"]  # 대조군은 조용히 기록만


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


def full_report_due(now: datetime) -> bool:
    """전체 현황을 보낼 차례인가. 전체 간격이 변화 요약 간격 이하이면 매번 전체를 보낸다."""
    if config.REPORT_FULL_INTERVAL_HOURS <= config.REPORT_INTERVAL_HOURS:
        return True
    last = state_store.get_meta(LAST_FULL_REPORT_KEY)
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(hours=config.REPORT_FULL_INTERVAL_HOURS)


def rec_key(rec: dict) -> str:
    return f"{rec['market']}|{rec['entered_at'].isoformat()}"


def current_returns(recs: list[dict], prices: dict[str, float]) -> dict[str, float]:
    """추적 중인 추천별 추천가 대비 현재 수익률(%). 지난 리포트와 비교하는 기준이 된다."""
    out = {}
    for rec in recs:
        entry = rec["entry_price"]
        current = prices.get(rec["market"]) or (rec["snaps"][-1][1] if rec["snaps"] else entry)
        out[rec_key(rec)] = (current / entry - 1) * 100 if entry else 0.0
    return out


def build_delta_report(
    now: datetime, since: datetime, recs: list[dict], returns: dict[str, float], previous: dict[str, float],
) -> str | None:
    """지난 리포트 이후 바뀐 것만: 신규/종료 건수(개별 알림은 이미 나갔으니 건수만), 추천가 대비 수익률이 크게 움직인 종목,
    전체 평균. 알릴 변화가 없으면 None."""
    all_recs = [r for r in price_tracker.load_recommendations() if r["strategy"] != "original"]
    new_n = sum(1 for r in all_recs if r["entered_at"] > since)
    ended = [r["exit"]["reason"] for r in all_recs if r["exit"] and r["exit"]["ended_at"] > since]
    labels = {"take_profit": "익절", "stop_loss": "손절", "btc_exit": "BTC 이탈", "expired": "만료", "trailing": "되돌림"}
    ended_text = ", ".join(f"{labels.get(k, k)} {ended.count(k)}" for k in dict.fromkeys(ended))

    movers = []
    for rec in recs:
        key = rec_key(rec)
        if key in previous:
            delta = returns[key] - previous[key]
            if abs(delta) >= config.REPORT_MOVE_PCT:
                movers.append((abs(delta), rec, returns[key], previous[key], delta))
    movers.sort(key=lambda m: m[0], reverse=True)

    if not new_n and not ended and not movers:
        return None
    lines = [f"🔔 추천 변화 요약 · {now.astimezone(KST).strftime('%m-%d %H:%M')} KST · 지난 리포트 {elapsed_text(now - since)} 전 이후"]
    summary = []
    if new_n:
        summary.append(f"신규 추천 {new_n}건")
    if ended:
        summary.append(f"종료 {len(ended)}건 ({ended_text})")
    if summary:
        lines.append("🆕 " + " · ".join(summary) + " (개별 알림으로 이미 전달)")
    if movers:
        lines.append("")
        lines.append(f"📈 추천가 대비 수익률이 {config.REPORT_MOVE_PCT:g}%p 이상 움직인 종목")
        for _, rec, now_ret, prev_ret, delta in movers[:8]:
            lines.append(f"{strategy_tag(rec)} {rec['market']} {fmt_pct(now_ret)} (지난 리포트 {fmt_pct(prev_ret)}, {delta:+.2f}%p)")
        if len(movers) > 8:
            lines.append(f"외 {len(movers) - 8}종목")
    if returns:
        avg_now = sum(returns.values()) / len(returns)
        prev_vals = [previous[k] for k in returns if k in previous]
        prev_text = f" (지난 리포트 {fmt_pct(sum(prev_vals) / len(prev_vals))})" if prev_vals else ""
        lines.append("")
        lines.append(f"📊 추적 {len(returns)}종목 · 평균 {fmt_pct(avg_now)}{prev_text}")
    lines.append(f"전체 현황은 {config.REPORT_FULL_INTERVAL_HOURS:g}시간마다 보내드려요.")
    return "\n".join(lines)


async def maybe_send_report(
    session: aiohttp.ClientSession,
    prices: dict[str, float],
    candidates: list[CandidateResult],
    btc_filter_on: bool,
) -> bool:
    """변화 요약 간격(REPORT_INTERVAL_HOURS)이 지나면 리포트를 만든다. 전체 간격(REPORT_FULL_INTERVAL_HOURS)이 지났으면
    전체 현황, 아니면 지난 리포트 이후 변화만 담고 변화가 없으면 보내지 않는다. 보냈으면 True."""
    now = datetime.now(timezone.utc)
    if not report_due(now):
        return False
    recs = active_recommendations(now)
    returns = current_returns(recs, prices)
    full = full_report_due(now)
    if full:
        text = build_report(now, recs, prices, candidates, btc_filter_on)
    else:
        last = state_store.get_meta(LAST_REPORT_KEY)
        raw = state_store.get_meta(SNAPSHOT_KEY)
        text = build_delta_report(now, datetime.fromisoformat(last), recs, returns, json.loads(raw) if raw else {}) if last else None
    if text is None:
        return False  # 보낼 게 없으면 발송 시각을 갱신하지 않아, 다음 사이클에 변화가 생기면 그때 보낸다
    print(text)
    if telegram_client.is_configured():
        try:
            await telegram_client.send_message(session, text)
        except Exception as exc:
            # 전송 실패(네트워크 문제, 메시지 길이 등)로 이번 사이클 전체(상태 저장·배포)가 막히면 안 된다
            print(f"[리포트] 전송 실패(다음 사이클에 다시 시도): {exc!r}")
    # 텔레그램이 꺼져 있어도 발송 시각은 기록해 콘솔 출력이 매 스캔마다 반복되지 않게 한다
    state_store.set_meta(LAST_REPORT_KEY, now.isoformat())
    if full:
        state_store.set_meta(LAST_FULL_REPORT_KEY, now.isoformat())
    state_store.set_meta(SNAPSHOT_KEY, json.dumps(returns))
    return True
