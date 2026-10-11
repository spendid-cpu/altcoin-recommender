"""한 번만 실행되는 데이터 정리.

2026-10-12 대조군(original) 전략을 삭제하면서, 쌓여 있던 대조군 추천 기록(진입·종료·모의 지정가·정정 이력)을 DB에서 지운다.
삭제 전 내용은 data/archive/original_strategy_20261012.json.gz에 보관했고(가격 스냅샷 제외), 그 이전 data/state.db는 git 이력에도 남아 있다.
meta 'original_purged_at'가 있으면 다시 하지 않는다. 스캔·추적 사이클 앞머리에서 부른다(같은 동시성 그룹이라 동시에 돌지 않는다)."""

from datetime import datetime, timezone

from src import state_store

PURGED_KEY = "original_purged_at"


def purge_original_once() -> dict | None:
    """대조군 기록을 지우고 지운 개수를 돌려준다. 이미 했으면 None."""
    if state_store.get_meta(PURGED_KEY):
        return None
    conn = state_store.connect_db()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {"entries": 0, "snapshots": 0}
        if "price_history" in tables:
            keys = conn.execute("SELECT market, recorded_at FROM price_history WHERE is_entry = 1 AND strategy = 'original'").fetchall()
            result["entries"] = len(keys)
            for table, c1, c2 in (("rec_exits", "market", "entered_at"), ("paper_orders", "market", "rec_at"),
                                  ("rec_exit_repairs", "market", "entered_at"), ("rec_half", "market", "entered_at")):
                if table in tables and keys:
                    conn.executemany(f"DELETE FROM {table} WHERE {c1} = ? AND {c2} = ?", keys)
            conn.execute("DELETE FROM price_history WHERE is_entry = 1 AND strategy = 'original'")
            # 추천이 하나도 남지 않은 종목의 가격 스냅샷은 더 쓸 곳이 없다
            cur = conn.execute("DELETE FROM price_history WHERE is_entry = 0 AND market NOT IN (SELECT market FROM price_history WHERE is_entry = 1)")
            result["snapshots"] = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    state_store.set_meta(PURGED_KEY, datetime.now(timezone.utc).isoformat())
    return result
