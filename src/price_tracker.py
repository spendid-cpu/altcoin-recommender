"""신규 후보로 발굴된 시점부터 가격 움직임을 계속 기록한다.
설계 문서(대시보드 및 텔레그램 알림 섹션): '발굴 시점의 가격을 기록해두고 이후 일정 기간의
가격 움직임을 계속 추적 — 이 추적 데이터가 곧 백테스트 검증의 원본 데이터가 된다'를 구현한 것.

같은 SQLite 파일(state_store.DB_PATH)에 price_history 테이블을 둔다. 신규 후보 알림이 뜬
시점을 '진입가'로 한 번 기록하고, 이후 매 스캔 사이클(15분)마다 진입 후 TRACK_DAYS 이내인
종목의 현재가를 계속 스냅샷으로 남긴다. 이 데이터로 나중에 실제 라이브 신호의 +1h/+4h/+1d/+3d
수익률을 계산해 백테스트 결과와 비교할 수 있다.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from src import state_store

TRACK_DAYS = 3  # 진입 후 이 기간까지만 계속 추적 (백테스트 관찰 기간과 동일)


def _connect() -> sqlite3.Connection:
    conn = state_store.connect_db()  # 테이블 생성 등 초기화 로직 재사용
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_history ("
        "market TEXT, recorded_at TEXT, price REAL, is_entry INTEGER, "
        "PRIMARY KEY (market, recorded_at))"
    )
    return conn


def record_entry(market: str, price: float) -> None:
    """신규 후보 발굴 시점의 가격을 '진입가'로 기록한다."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO price_history (market, recorded_at, price, is_entry) VALUES (?, ?, ?, 1)",
            (market, now, price),
        )
        conn.commit()
    finally:
        conn.close()


def active_tracked_markets() -> list[str]:
    """진입 기록이 TRACK_DAYS 이내인 종목만 반환한다 (그 이후는 추적 종료)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRACK_DAYS)).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT market FROM price_history WHERE is_entry = 1 AND recorded_at >= ?",
            (cutoff,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def record_snapshots(prices: dict[str, float]) -> None:
    """market -> 현재가 맵을 스냅샷으로 기록한다 (is_entry=0)."""
    if not prices:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO price_history (market, recorded_at, price, is_entry) VALUES (?, ?, ?, 0)",
            [(market, now, price) for market, price in prices.items()],
        )
        conn.commit()
    finally:
        conn.close()
