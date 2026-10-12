"""규칙 때문에 '막힌' 추천의 그림자 기록 (2026-10-12): 막지 않았다면 새 추천이 됐을 종목을 가상 진입으로 남겨 두고, 실제 추천과 같은 규칙(config.EXIT_STOP_LOSS_PCT 손절 / config.EXIT_TAKE_PROFIT_PCT 익절 / 3일 만료)으로
이후 성과를 따라간다. 지금 켜 둔 규칙들(작은 파동 하락 게이트, 일봉 저점 확인)은 백테스트로 이득이 입증되지 않았고 라이브에서는 반대 신호도 있었다 —
'막은 쪽이 정말 더 나빴나'를 라이브로 확인하는 가장 직접적인 방법이다.

- 실제 추천 기록(price_history)과 완전히 분리된 shadow_recs 표에만 쓴다. 알림·추천 목록·성과 집계·추적 대상에는 섞이지 않는다.
- 사유(reasons): low_confirm = 일봉 저점 확인 규칙(config.LOW_CONFIRM_MODE)에 걸림, wave_gate = BTC 작은 파동 하락 게이트가 막음 (둘 다일 수 있다).
- 같은 종목은 SHADOW_DEDUP_HOURS(추적 기간) 안에 다시 기록하지 않고, 이미 실제로 추천해 추적 중인 종목도 기록하지 않는다 (실제 추천의 3일 쿨다운과 같은 취급).
- 종료는 가격 확인 시점(스캔 15분·추적 5분) 기준이라 실제 추천과 같은 한계를 가진다. BTC 이탈 정리는 적용하지 않는다."""

import json
from datetime import datetime, timezone

import aiohttp

from src import config, price_tracker, state_store
from src.exchanges import upbit_client

SHADOW_DEDUP_HOURS = price_tracker.TRACK_DAYS * 24
EXPORT_LIMIT = 300
DDL = (
    "CREATE TABLE IF NOT EXISTS shadow_recs ("
    "market TEXT NOT NULL, recorded_at TEXT NOT NULL, price REAL NOT NULL, score REAL, reasons TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'open', last_price REAL, max_ret REAL, min_ret REAL, closed_at TEXT, exit_reason TEXT, return_pct REAL, "
    "PRIMARY KEY (market, recorded_at))"
)


def _connect():
    conn = state_store.connect_db()
    conn.execute(DDL)
    return conn


def reasons_for(candidate, gate_blocked: bool) -> list[str]:
    """이 후보가 규칙 때문에 막힌 사유들. 막히지 않았거나 원래 추천 대상이 아니면 빈 목록."""
    if not candidate.recommendable_ignoring_low or (candidate.total_score or 0) < config.MIN_RECOMMEND_SCORE:
        return []
    out = []
    if candidate.low_confirm_excluded:
        out.append("low_confirm")
    if gate_blocked:
        out.append("wave_gate")
    return out


def record(candidates, tracked: set, gate_blocked: bool, now: datetime | None = None) -> int:
    """이번 스캔에서 규칙 때문에 막힌 새 추천 후보를 그림자 진입으로 기록한다. 새로 기록한 건수를 돌려준다."""
    now = now or datetime.now(timezone.utc)
    conn = _connect()
    added = 0
    try:
        recent = {r[0] for r in conn.execute(
            "SELECT market, recorded_at FROM shadow_recs ORDER BY recorded_at DESC LIMIT 2000"
        ).fetchall() if (now - datetime.fromisoformat(r[1])).total_seconds() < SHADOW_DEDUP_HOURS * 3600}
        for c in candidates:
            reasons = reasons_for(c, gate_blocked)
            if not reasons or c.market in tracked or c.market in recent:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO shadow_recs (market, recorded_at, price, score, reasons) VALUES (?, ?, ?, ?, ?)",
                (c.market, now.isoformat(), float(c.current_price), round(float(c.total_score), 1), json.dumps(reasons)))
            recent.add(c.market)
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


async def update(session: aiohttp.ClientSession, now: datetime | None = None) -> int:
    """열려 있는 그림자 진입의 가격을 확인해 손절·익절·만료로 닫는다. 이번에 닫은 건수를 돌려준다. 실패해도 호출하는 쪽이 감싼다."""
    now = now or datetime.now(timezone.utc)
    conn = _connect()
    closed = 0
    try:
        rows = conn.execute(
            "SELECT market, recorded_at, price, max_ret, min_ret FROM shadow_recs WHERE status = 'open'"
        ).fetchall()
        if not rows:
            return 0
        prices = await upbit_client.fetch_ticker_prices(session, sorted({r[0] for r in rows}))
        sl, tp = config.EXIT_STOP_LOSS_PCT, config.EXIT_TAKE_PROFIT_PCT
        for market, rec_at, entry, max_ret, min_ret in rows:
            price = prices.get(market)
            age_sec = (now - datetime.fromisoformat(rec_at)).total_seconds()
            if price is None:
                if age_sec >= price_tracker.TRACK_DAYS * 86400 + 6 * 3600:  # 시세를 못 받는 채로 오래되면 마지막 값으로 정리
                    conn.execute("UPDATE shadow_recs SET status='closed', closed_at=?, exit_reason='expired' WHERE market=? AND recorded_at=?", (now.isoformat(), market, rec_at))
                    closed += 1
                continue
            ret = (price / entry - 1) * 100
            max_ret = ret if max_ret is None else max(max_ret, ret)
            min_ret = ret if min_ret is None else min(min_ret, ret)
            reason = None
            if sl is not None and ret <= -sl:
                reason = "stop_loss"
            elif tp is not None and ret >= tp:
                reason = "take_profit"
            elif age_sec >= price_tracker.TRACK_DAYS * 86400:
                reason = "expired"
            if reason:
                conn.execute(
                    "UPDATE shadow_recs SET status='closed', last_price=?, max_ret=?, min_ret=?, closed_at=?, exit_reason=?, return_pct=? WHERE market=? AND recorded_at=?",
                    (price, max_ret, min_ret, now.isoformat(), reason, ret, market, rec_at))
                closed += 1
            else:
                conn.execute("UPDATE shadow_recs SET last_price=?, max_ret=?, min_ret=?, return_pct=? WHERE market=? AND recorded_at=?",
                             (price, max_ret, min_ret, ret, market, rec_at))
        conn.commit()
    finally:
        conn.close()
    return closed


def export() -> list[dict]:
    """대시보드용: 최근 그림자 진입 EXPORT_LIMIT건(최신순). 표가 아직 없으면(처음 막힌 일이 없으면) 표를 만들지 않고 빈 목록."""
    conn = state_store.connect_db()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_recs'").fetchone():
            return []
        rows = conn.execute(
            "SELECT market, recorded_at, price, score, reasons, status, return_pct, max_ret, min_ret, exit_reason, closed_at "
            "FROM shadow_recs ORDER BY recorded_at DESC LIMIT ?", (EXPORT_LIMIT,)).fetchall()
    finally:
        conn.close()
    return [{"market": m, "at": at, "price": p, "score": s, "reasons": json.loads(rs), "status": st,
             "ret": None if r is None else round(r, 2), "max": None if mx is None else round(mx, 2), "min": None if mn is None else round(mn, 2),
             "exit": ex, "closed_at": ca} for m, at, p, s, rs, st, r, mx, mn, ex, ca in rows]
