"""신규 후보로 발굴된 시점부터 가격 움직임을 계속 기록한다.
설계 문서(대시보드 및 텔레그램 알림 섹션): '발굴 시점의 가격을 기록해두고 이후 일정 기간의
가격 움직임을 계속 추적 — 이 추적 데이터가 곧 백테스트 검증의 원본 데이터가 된다'를 구현한 것.

같은 SQLite 파일(state_store.DB_PATH)에 price_history 테이블을 둔다. 신규 후보 알림이 뜬
시점을 '진입가'로 한 번 기록하고(그때의 등급/점수도 함께), 이후 매 스캔 사이클마다
진입 후 TRACK_DAYS 이내인 종목의 현재가를 계속 스냅샷으로 남긴다. 이 데이터로 실제 라이브 신호의
+1h/+4h/+1d/+3d 수익률을 계산해 백테스트 결과와 비교할 수 있다.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from src import state_store

TRACK_DAYS = 3  # 진입 후 이 기간까지만 계속 추적 (백테스트 관찰 기간과 동일)


_deduped = False


def connect() -> sqlite3.Connection:
    global _deduped
    conn = state_store.connect_db()  # 테이블 생성 등 초기화 로직 재사용
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history ("
        "market TEXT, recorded_at TEXT, price REAL, is_entry INTEGER, "
        "PRIMARY KEY (market, recorded_at))"
    )
    # 예전 버전(등급/점수 컬럼 없음)으로 만든 DB도 그대로 쓸 수 있게 컬럼을 추가한다
    columns = {row[1] for row in conn.execute("PRAGMA table_info(price_history)")}
    if "grade" not in columns:
        conn.execute("ALTER TABLE price_history ADD COLUMN grade TEXT")
    if "score" not in columns:
        conn.execute("ALTER TABLE price_history ADD COLUMN score REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rec_exits ("
        "market TEXT, entered_at TEXT, ended_at TEXT, exit_price REAL, return_pct REAL, reason TEXT, "
        "PRIMARY KEY (market, entered_at))"
    )
    if not _deduped:
        _merge_duplicate_entries(conn)
        _deduped = True
    return conn


def _merge_duplicate_entries(conn: sqlite3.Connection) -> None:
    """추적 기간(TRACK_DAYS) 안에 같은 종목의 진입 기록이 또 있으면 중복 추천이므로 일반 스냅샷으로 바꾼다.
    (알림 중복 방지가 들어가기 전에 생긴 기록을 정리하는 용도. 여러 번 실행해도 결과는 같다.)"""
    rows = conn.execute(
        "SELECT market, recorded_at FROM price_history WHERE is_entry = 1 ORDER BY market, recorded_at"
    ).fetchall()
    window = timedelta(days=TRACK_DAYS)
    last_kept: dict[str, datetime] = {}
    duplicates = []
    for market, recorded_at in rows:
        at = datetime.fromisoformat(recorded_at)
        if market in last_kept and at - last_kept[market] < window:
            duplicates.append((market, recorded_at))
        else:
            last_kept[market] = at
    if duplicates:
        conn.executemany("UPDATE price_history SET is_entry = 0 WHERE market = ? AND recorded_at = ?", duplicates)
        conn.commit()


def record_entry(market: str, price: float, grade: str = "-", score: float | None = None) -> None:
    """신규 후보 발굴 시점의 가격(과 그때의 등급/점수)을 '진입가'로 기록한다."""
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO price_history (market, recorded_at, price, is_entry, grade, score) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (market, now, price, grade, score),
        )
        conn.commit()
    finally:
        conn.close()


def active_tracked_markets() -> list[str]:
    """지금 가격을 계속 따라가고 있는 종목: 진입 후 TRACK_DAYS 이내이고 아직 종료되지 않은 추천."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRACK_DAYS)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT p.market FROM price_history p WHERE p.is_entry = 1 AND p.recorded_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM rec_exits e WHERE e.market = p.market AND e.entered_at = p.recorded_at)",
            (cutoff,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def recent_recommendation_markets() -> list[str]:
    """최근 TRACK_DAYS 안에 추천한 적이 있는 종목 (종료 여부와 상관없이). 같은 종목을 이 기간 안에 다시 추천하지
    않는 중복 방지 기준으로 쓴다 — 익절로 일찍 끝난 종목이 곧바로 또 추천되는 것도 막는다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRACK_DAYS)).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT market FROM price_history WHERE is_entry = 1 AND recorded_at >= ?", (cutoff,)
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def record_exit(market: str, entered_at: datetime, exit_price: float, return_pct: float, reason: str) -> None:
    """추천 종료를 기록하고 종료 시점 가격을 마지막 스냅샷으로 남긴다 (차트가 종료 지점까지 이어지게)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO rec_exits (market, entered_at, ended_at, exit_price, return_pct, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (market, entered_at.isoformat(), now, exit_price, return_pct, reason),
        )
        conn.execute(
            "INSERT OR REPLACE INTO price_history (market, recorded_at, price, is_entry) VALUES (?, ?, ?, 0)",
            (market, now, exit_price),
        )
        conn.commit()
    finally:
        conn.close()


def load_recommendations() -> list[dict]:
    """진입 기록(is_entry=1) 하나가 추천 하나. 이후 스냅샷을 붙여 시간순(오래된 것 먼저)으로 반환한다.
    각 항목: market, entered_at(datetime), entry_price, grade, score, snaps[(datetime, price)],
    exit(종료된 추천이면 {ended_at, exit_price, return_pct, reason}, 아니면 None)"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT market, recorded_at, price, is_entry, grade, score FROM price_history ORDER BY market, recorded_at"
        ).fetchall()
        exit_rows = conn.execute(
            "SELECT market, entered_at, ended_at, exit_price, return_pct, reason FROM rec_exits"
        ).fetchall()
    finally:
        conn.close()
    exits = {
        (m, entered): {"ended_at": datetime.fromisoformat(ended), "exit_price": price, "return_pct": ret, "reason": reason}
        for m, entered, ended, price, ret, reason in exit_rows
    }

    recs: list[dict] = []
    current: dict | None = None
    current_market = None
    for market, recorded_at, price, is_entry, grade, score in rows:
        if market != current_market:
            current, current_market = None, market
        at = datetime.fromisoformat(recorded_at)
        if is_entry:
            current = {"market": market, "entered_at": at, "entry_price": price, "grade": grade or "-",
                       "score": score, "snaps": [], "exit": exits.get((market, recorded_at))}
            recs.append(current)
        elif current is not None:
            current["snaps"].append((at, price))
    recs.sort(key=lambda r: r["entered_at"])
    return recs


def record_snapshots(prices: dict[str, float]) -> None:
    """market -> 현재가 맵을 스냅샷으로 기록한다 (is_entry=0)."""
    if not prices:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO price_history (market, recorded_at, price, is_entry) VALUES (?, ?, ?, 0)",
            [(market, now, price) for market, price in prices.items()],
        )
        conn.commit()
    finally:
        conn.close()
