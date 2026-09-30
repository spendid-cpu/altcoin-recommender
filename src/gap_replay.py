"""스캔·추적이 멈춘 공백 동안의 가격 움직임을 5분봉 종가로 복원해 익절/손절/만료 판정을 바로잡는다.

exits.check_exit는 '지금 가격' 한 점만 본다. 그래서 추적이 43시간 멈췄다 재개되면 그동안 이미 닿았다가 되돌아간
-5%/+5%를 놓치고, 재개 시점 가격 하나로 종료를 판정한다(2026-09-28 사고: 손절 5건 누락, +9.12% 가짜 익절).
- find_gap_exit: 추적 중 마지막 두 기록 사이가 GAP_MIN보다 벌어졌으면 그 구간을 5분봉으로 다시 훑는다 (exits.process_exits가 호출).
- repair_recorded_gaps: 이미 잘못 기록된 과거 종료를 같은 방식으로 한 번 정정한다 (tracker_job이 호출, 원래 값은 rec_exit_repairs에 남김).
판정은 정상 시에 스냅샷으로 하던 것과 같은 성격(5분 간격의 시점 가격)이라 5분봉 '종가'를 쓴다 — 고가·저가는 쓰지 않는다."""

from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, state_store
from src.exchanges import upbit_client

KST = ZoneInfo("Asia/Seoul")
GAP_MIN = timedelta(minutes=30)  # 정상 사이클 간격(5분·15분)이 GitHub 지연으로 벌어지는 정도(측정상 최대 ~20분)보다 넉넉히 크게
CANDLE = timedelta(minutes=5)
MAX_CANDLES = 1200  # 약 4일치. 이보다 오래된 공백은 복원하지 않는다
REPAIR_KEY = "gap_repair_2026_09_28"
REPAIR_SINCE = datetime(2026, 9, 25, tzinfo=timezone.utc)  # 이 시각 이후까지 살아 있던 추천만 정정 대상
REPAIR_MAX_TRIES = 3

Hit = tuple[datetime, float, str]  # (종료 시각, 종료가, 사유)


def replay_hit(rec: dict, candles, w0: datetime, w1: datetime, check: Callable) -> Hit | None:
    """w0 이후 마감(끝 시각 > w0)되어 w1까지(끝 시각 <= w1) 이어진 5분봉을 차례로 보며 종료 규칙에 처음 걸린 지점.
    check는 exits.check_exit와 같은 시그니처다. 공백 중의 BTC 이탈은 복원할 수 없어서 btc_filter_on=True로 본다(가격 규칙만)."""
    view = {**rec, "snaps": [s for s in rec["snaps"] if s[0] <= w0]}
    for row in candles.itertuples():
        end = datetime.fromisoformat(row.time).replace(tzinfo=KST) + CANDLE
        if end <= w0:
            continue
        if end > w1:
            break
        price = float(row.close)
        reason = check(view, price, end, True)
        if reason:
            return end, price, reason
        view["snaps"].append((end, price))
    return None


async def _candles(session: aiohttp.ClientSession, market: str, w0: datetime, now: datetime):
    n = min(int((now - w0) / CANDLE) + 3, MAX_CANDLES)
    return await upbit_client.fetch_candles(session, market, "5m", n, closed_only=True)


async def find_gap_exit(session: aiohttp.ClientSession, rec: dict, now: datetime, check: Callable) -> Hit | None:
    """진행 중 추천의 마지막 기록 구간에 공백이 있었다면 그 구간에서 이미 종료 규칙에 걸렸는지 복원해 본다.
    호출 시점에는 이번 사이클의 스냅샷이 이미 기록돼 있다 — 추적 기간 안이면 그것과 그 직전 기록 사이가 검사 구간이고,
    추적 기간이 끝난 추천이면 마지막 기록에서 만료 시각까지가 검사 구간이다."""
    points = [rec["entered_at"]] + [t for t, _ in rec["snaps"]]
    deadline = rec["entered_at"] + timedelta(days=price_tracker.TRACK_DAYS)
    if now < deadline:
        if len(points) < 2:
            return None
        w0, w1, gap_end = points[-2], points[-1], points[-1]
    else:
        w0, w1, gap_end = points[-1], deadline + CANDLE, deadline  # 만료 시각을 넘긴 첫 봉까지 본다
    if gap_end - w0 <= GAP_MIN:
        return None
    return replay_hit(rec, await _candles(session, rec["market"], w0, now), w0, w1, check)


def _record_repair(rec: dict, hit: Hit) -> None:
    end, price, reason = hit
    ex = rec["exit"]
    conn = price_tracker.connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rec_exit_repairs (market TEXT, entered_at TEXT, old_ended_at TEXT, old_exit_price REAL, "
            "old_return_pct REAL, old_reason TEXT, new_ended_at TEXT, new_exit_price REAL, new_return_pct REAL, new_reason TEXT, "
            "repaired_at TEXT, PRIMARY KEY (market, entered_at))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO rec_exit_repairs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["market"], rec["entered_at"].isoformat(),
             ex["ended_at"].isoformat() if ex else None, ex["exit_price"] if ex else None,
             ex["return_pct"] if ex else None, ex["reason"] if ex else None,
             end.astimezone(timezone.utc).isoformat(), price, (price / rec["entry_price"] - 1) * 100, reason,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    price_tracker.record_exit(rec["market"], rec["entered_at"], price, (price / rec["entry_price"] - 1) * 100, reason, ended_at=end)


async def repair_recorded_gaps(session: aiohttp.ClientSession, check: Callable) -> int:
    """이미 남아 있는 잘못된 종료 기록(공백 뒤 재개 시점 가격으로 판정된 것, 공백 중 종료 규칙에 닿았는데 진행 중으로 남은 것)을
    5분봉으로 복원한 값으로 정정한다. 한 번만 성공하면 끝나고(meta 키), 다시 돌려도 이미 정정된 건 건드리지 않는다.
    정정 전 값은 rec_exit_repairs 표에 남는다. 알림은 보내지 않는다. 정정한 개수를 돌려준다."""
    if state_store.get_meta(REPAIR_KEY) == "done":
        return 0
    tries = int(state_store.get_meta(REPAIR_KEY + "_tries") or 0)
    if tries >= REPAIR_MAX_TRIES:
        return 0
    state_store.set_meta(REPAIR_KEY + "_tries", str(tries + 1))

    now = datetime.now(timezone.utc)
    fixed, failed = 0, False
    for rec in price_tracker.load_recommendations():
        ex = rec["exit"]
        deadline = rec["entered_at"] + timedelta(days=price_tracker.TRACK_DAYS)
        if deadline < REPAIR_SINCE or rec["market"] in config.EXCLUDED_MARKETS or (ex and ex["reason"] == "excluded"):
            continue
        points = [rec["entered_at"]] + [t for t, _ in rec["snaps"]]
        gaps = [(a, b) for a, b in zip(points, points[1:]) if b - a > GAP_MIN]
        if ex is None and now >= deadline and deadline - points[-1] > GAP_MIN:
            gaps.append((points[-1], deadline + CANDLE))
        if not gaps:
            continue
        try:
            hit = None
            for a, b in gaps:
                hit = replay_hit(rec, await _candles(session, rec["market"], a, now), a, b, check)
                if hit:
                    break
        except Exception as exc:
            print(f"  [공백 정정] {rec['market']} 5분봉 조회 실패(다음 사이클에 다시 시도): {exc!r}")
            failed = True
            continue
        if hit is None or (ex and hit[0] >= ex["ended_at"]):
            continue
        _record_repair(rec, hit)
        old = f"{ex['return_pct']:+.2f}% {ex['reason']}" if ex else "진행 중"
        print(f"  [공백 정정] {rec['market']} {rec['entered_at'].astimezone(KST):%m-%d %H:%M} 추천: {old} -> "
              f"{(hit[1] / rec['entry_price'] - 1) * 100:+.2f}% {hit[2]} ({hit[0].astimezone(KST):%m-%d %H:%M})")
        fixed += 1
    if not failed:
        state_store.set_meta(REPAIR_KEY, "done")
    return fixed
