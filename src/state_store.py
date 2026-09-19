"""마켓별 직전 신호 상태를 SQLite에 저장해 중복 알림을 방지한다."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# DATA_DIR 환경변수가 있으면 그쪽을 쓴다 (Railway 등 배포 환경에서 퍼시스턴트 볼륨을 마운트할 때 사용).
# 없으면 로컬 개발 기본값인 프로젝트 루트의 data/ 폴더를 쓴다.
_DATA_DIR = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else Path(__file__).resolve().parent.parent / "data"
DB_PATH = _DATA_DIR / "state.db"


def connect_db() -> sqlite3.Connection:
    """같은 SQLite 파일에 다른 테이블(예: price_tracker.price_history)을 추가할 때도 이 함수를 재사용한다."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS market_state ("
        "market TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def get_meta(key: str) -> str | None:
    """작은 키-값 저장소 (예: 마지막 현황 리포트 발송 시각)."""
    conn = connect_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_meta(key: str, value: str) -> None:
    conn = connect_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def load_all() -> dict[str, dict]:
    conn = connect_db()
    try:
        rows = conn.execute("SELECT market, state_json FROM market_state").fetchall()
        return {market: json.loads(state_json) for market, state_json in rows}
    finally:
        conn.close()


def save_all(states: dict[str, dict]) -> None:
    """market -> state dict. 상태가 없는(빈) 마켓도 명시적으로 넘기면 그 마켓 행이 초기화된다."""
    now = datetime.now(timezone.utc).isoformat()
    conn = connect_db()
    try:
        conn.executemany(
            "INSERT INTO market_state (market, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(market) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
            [(m, json.dumps(s), now) for m, s in states.items()],
        )
        conn.commit()
    finally:
        conn.close()
