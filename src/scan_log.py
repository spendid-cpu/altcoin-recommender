"""스캔 실행 기록. 대시보드에서 '마지막 스캔이 언제였나 / 실제 실행 간격이 얼마나 되나'를 보여주는 용도
(GitHub Actions 예약 실행이 밀리는 걸 눈으로 확인할 수 있게 한다)."""

import sqlite3
from datetime import datetime, timedelta, timezone

from src import state_store

KEEP_DAYS = 14


def connect() -> sqlite3.Connection:
    conn = state_store.connect_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scan_log ("
        "ran_at TEXT PRIMARY KEY, btc_favorable INTEGER, candidates INTEGER, alerts INTEGER)"
    )
    return conn


def record_scan(btc_favorable: bool, candidates: int, alerts: int) -> None:
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO scan_log (ran_at, btc_favorable, candidates, alerts) VALUES (?, ?, ?, ?)",
            (now.isoformat(), int(btc_favorable), candidates, alerts),
        )
        conn.execute("DELETE FROM scan_log WHERE ran_at < ?", ((now - timedelta(days=KEEP_DAYS)).isoformat(),))
        conn.commit()
    finally:
        conn.close()


def recent(limit: int = 30) -> list[dict]:
    """최신순으로 최대 limit개."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ran_at, btc_favorable, candidates, alerts FROM scan_log ORDER BY ran_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"ran_at": r[0], "btc_favorable": bool(r[1]), "candidates": r[2], "alerts": r[3]} for r in rows
    ]
