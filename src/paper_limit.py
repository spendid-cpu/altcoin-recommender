"""지지선 지정가 매수 모의(페이퍼) 실험 — 실제 주문은 내지 않고 기록만 한다.

개선판이 종목을 추천하면 (1) 그 가격에 바로 산 것(기존 추천 기록)과 (2) 최근 1시간봉 스윙 저점(지지) 중 현재가 -1%~-6% 사이의
가장 높은 자리에 지정가를 건 것을 나란히 기록해 몇 주 뒤 실제 라이브에서 비교한다. 120일 백테스트(지지 변형 S, 손절 -5%)가 근거인데
통계적 우위는 확인되지 않았고(기준선 대비 초과 +0.22% [-0.18,+0.56]), 체결 선택 편향 때문에 라이브 검증이 필요하다.

규칙: 지정가는 PAPER_ORDER_HOURS(24시간) 동안 유효하고 가격이 지정가 이하가 되는 첫 확인 시점에 지정가로 체결된 것으로 본다.
체결 뒤에는 개선판과 같은 종료 규칙(익절/손절 config.EXIT_*, BTC 이탈 정리, 체결 후 3일 만료)을 체결가 기준으로 적용한다.
대기 중에 BTC 필터가 꺼지면 주문을 취소한다(백테스트는 체결 직후 정리되는 것으로 처리해 실질적으로 같다).
"""

from datetime import datetime, timedelta, timezone

import aiohttp

from src import config, price_tracker
from src.exchanges import upbit_client

LEVEL_MIN_DEPTH_PCT = 1.0  # 현재가 대비 이만큼(%) 이상 아래
LEVEL_MAX_DEPTH_PCT = 6.0  # 이만큼(%) 이내
MAX_ROWS_EXPORTED = 400


def _connect():
    conn = price_tracker.connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_orders ("
        "market TEXT, rec_at TEXT, placed_at TEXT, ref_price REAL, level REAL, status TEXT, "
        "filled_at TEXT, fill_price REAL, ended_at TEXT, exit_price REAL, exit_reason TEXT, return_pct REAL, "
        "PRIMARY KEY (market, rec_at))"
    )
    return conn


def pick_level(support_lows: list[float], price: float) -> float | None:
    """현재가 -1%~-6% 안에 있는 지지 저점 중 가장 높은(현재가에 가까운) 것."""
    lo, hi = price * (1 - LEVEL_MAX_DEPTH_PCT / 100), price * (1 - LEVEL_MIN_DEPTH_PCT / 100)
    inside = [x for x in support_lows if lo <= x <= hi]
    return max(inside) if inside else None


def place(market: str, rec_at: datetime, price: float, support_lows: list[float]) -> None:
    """추천 시점에 모의 지정가를 기록한다. 자리가 없으면 no_level로 남겨 커버리지를 센다."""
    if not config.PAPER_LIMIT_ENABLED:
        return
    level = pick_level(support_lows, price)
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO paper_orders (market, rec_at, placed_at, ref_price, level, status) VALUES (?, ?, ?, ?, ?, ?)",
            (market, rec_at.isoformat(), now, price, level, "pending" if level is not None else "no_level"),
        )
        conn.commit()
    finally:
        conn.close()


def _open_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT market, rec_at, placed_at, level, status, filled_at, fill_price FROM paper_orders "
        "WHERE status IN ('pending', 'open')"
    ).fetchall()


async def update(session: aiohttp.ClientSession, btc_favorable: bool) -> None:
    """체결·종료 판단. 5분/15분 사이클마다 부른다. 실패해도 사이클을 막지 않도록 호출하는 쪽에서 감싼다."""
    if not config.PAPER_LIMIT_ENABLED:
        return
    conn = _connect()
    try:
        rows = _open_rows(conn)
        if not rows:
            return
        prices = await upbit_client.fetch_ticker_prices(session, sorted({r[0] for r in rows}))
        now = datetime.now(timezone.utc)
        for market, rec_at, placed_at, level, status, filled_at, fill_price in rows:
            try:
                price = prices.get(market)
                if status == "pending":
                    if not btc_favorable:
                        _close(conn, market, rec_at, now, None, "btc_off", "cancelled", 0.0)
                        continue
                    if price is not None and price <= level:
                        conn.execute(
                            "UPDATE paper_orders SET status='open', filled_at=?, fill_price=? WHERE market=? AND rec_at=?",
                            (now.isoformat(), level, market, rec_at))
                        status, filled_at, fill_price = "open", now.isoformat(), level
                    elif now - datetime.fromisoformat(placed_at) >= timedelta(hours=config.PAPER_ORDER_HOURS):
                        _close(conn, market, rec_at, now, None, "timeout", "cancelled", 0.0)
                        continue
                if status == "open" and price is not None:
                    reason = _exit_reason(price, fill_price, datetime.fromisoformat(filled_at), now, btc_favorable)
                    if reason:
                        _close(conn, market, rec_at, now, price, reason, "closed", (price / fill_price - 1) * 100)
            except Exception as exc:
                print(f"  {market} 모의 주문 처리 실패(이번 사이클은 건너뜀): {exc!r}")
        conn.commit()
    finally:
        conn.close()


def _exit_reason(price: float, fill: float, filled_at: datetime, now: datetime, btc_favorable: bool) -> str | None:
    ret = (price / fill - 1) * 100
    if config.EXIT_STOP_LOSS_PCT is not None and ret <= -config.EXIT_STOP_LOSS_PCT:
        return "stop_loss"
    if config.BTC_EXIT_ENABLED and not btc_favorable:
        return "btc_exit"
    if config.EXIT_TAKE_PROFIT_PCT is not None and ret >= config.EXIT_TAKE_PROFIT_PCT:
        return "take_profit"
    if now - filled_at >= timedelta(days=price_tracker.TRACK_DAYS):
        return "expired"
    return None


def _close(conn, market, rec_at, now, price, reason, status, ret) -> None:
    conn.execute(
        "UPDATE paper_orders SET status=?, ended_at=?, exit_price=?, exit_reason=?, return_pct=? WHERE market=? AND rec_at=?",
        (status, now.isoformat(), price, reason, ret, market, rec_at))


def export_rows() -> list[dict]:
    """대시보드용: 최근 모의 주문 기록."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT market, rec_at, placed_at, ref_price, level, status, filled_at, fill_price, ended_at, exit_price, "
            "exit_reason, return_pct FROM paper_orders ORDER BY rec_at DESC LIMIT ?", (MAX_ROWS_EXPORTED,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
