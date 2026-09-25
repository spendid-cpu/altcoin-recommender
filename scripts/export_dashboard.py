"""대시보드용 JSON(dashboard/dashboard.json)을 만든다.
state.db(후보 상태, 가격 추적, 스캔 기록)와 바이낸스 BTC 일봉을 읽어 dashboard/index.html이 그릴 데이터를 정리한다.

사용법: python scripts/export_dashboard.py [--out 경로]
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import pandas as pd

from src import config, jsonutil, macro_job, price_tracker, scan_log
from src.btc_trend import days_above_ma, is_trend_favorable
from src.exchanges import binance_client

KST = ZoneInfo("Asia/Seoul")
HORIZONS_HOURS = (1, 4, 24, 72)
MAX_RECOMMENDATIONS = 400  # 성과 집계에 쓰는 전체 추천 수 (오래된 것은 가격 경로를 뺀다)
SERIES_KEPT = 60  # 가격 경로(차트용)를 담는 최근 추천 수
BTC_DAYS_SHOWN = 45
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "dashboard" / "dashboard.json"


async def build_btc(session: aiohttp.ClientSession) -> dict:
    try:
        df = await binance_client.fetch_klines(session, config.BINANCE_SYMBOL, "1d", 100)
    except Exception as exc:  # 바이낸스 조회가 실패해도 나머지 화면은 그려야 한다
        return {"error": f"{type(exc).__name__}: {exc}"}

    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    ma20 = df["close"].rolling(20).mean()
    shown = df.assign(ma20=ma20).dropna().tail(BTC_DAYS_SHOWN)
    return {
        "symbol": config.BINANCE_SYMBOL,
        "favorable": is_trend_favorable(df["close"]),
        "days_above": days_above_ma(df["close"]),
        "hold_days_required": config.BTC_HOLD_DAYS,
        "close": round(float(df["close"].iloc[-1]), 2),
        "prev_close": round(float(df["close"].iloc[-2]), 2),
        "ma20": round(float(ma20.iloc[-1]), 2),
        "last_candle_open": bool(df["time"].iloc[-1] > now),
        "series": [
            {"date": (row.time - pd.Timedelta(seconds=1)).strftime("%Y-%m-%d"), "close": round(float(row.close), 2),
             "ma20": round(float(row.ma20), 2)}
            for row in shown.itertuples()
        ],
    }


def _nearest_within(snaps: list[tuple[datetime, float]], target: datetime, tolerance: timedelta) -> float | None:
    best = min(snaps, key=lambda s: abs(s[0] - target), default=None)
    if best is None or abs(best[0] - target) > tolerance:
        return None
    return best[1]


def build_recommendations(now: datetime) -> list[dict]:
    """price_history의 진입 기록(is_entry=1) 하나가 '추천' 하나. 이후 스냅샷을 붙여 수익률을 계산한다."""
    out = []
    for rec in price_tracker.load_recommendations():
        entry_at, entry_price = rec["entered_at"], rec["entry_price"]
        snaps = rec["snaps"]
        last_at, last_price = (snaps[-1] if snaps else (entry_at, entry_price))
        pct = lambda p: round((p / entry_price - 1) * 100, 2) if entry_price else None  # noqa: E731
        horizons = {}
        for hours in HORIZONS_HOURS:
            target = entry_at + timedelta(hours=hours)
            value = None
            if target <= now:
                # 스캔 간격이 들쭉날쭉할 수 있어 허용 오차를 목표 시각의 25%(최소 30분)로 둔다
                tolerance = timedelta(hours=max(0.5, hours * 0.25))
                near = _nearest_within(snaps, target, tolerance)
                value = pct(near) if near is not None else None
            horizons[f"{hours}h"] = value
        age_hours = (now - entry_at).total_seconds() / 3600
        ended = rec["exit"]
        out.append({
            "exit": None if ended is None else {
                "reason": ended["reason"],
                "ended_at": ended["ended_at"].isoformat(),
                "exit_price": ended["exit_price"],
                "return_pct": round(ended["return_pct"], 2),
            },
            "market": rec["market"],
            "entered_at": entry_at.isoformat(),
            "entry_price": entry_price,
            "grade": rec["grade"],
            "score": rec["score"],
            "detail": rec.get("detail"),
            "last_price": last_price,
            "last_at": last_at.isoformat(),
            "return_pct": pct(last_price),
            "strategy": rec["strategy"],
            "horizons": horizons,
            "active": ended is None and age_hours < price_tracker.TRACK_DAYS * 24,
            "series": [[0, 0.0]] + [
                [round((t - entry_at).total_seconds() / 3600, 2), pct(p)] for t, p in snaps
            ],
        })
    out.sort(key=lambda r: r["entered_at"], reverse=True)
    out = out[:MAX_RECOMMENDATIONS]
    for rec in out[SERIES_KEPT:]:  # 오래된 추천은 차트에 안 쓰므로 가격 경로를 덜어 파일을 작게 유지한다
        rec["series"] = []
    return out


def build_summary(recs: list[dict]) -> dict:
    """개선판 추천의 요약 (대조군과의 비교는 화면이 추천 목록에서 전략별로 계산한다)."""
    recs = [r for r in recs if r["strategy"] == "legacy"]
    summary = {"total": len(recs)}
    for key, label in (("24h", "1d"), ("72h", "3d")):
        values = [r["horizons"][key] for r in recs if r["horizons"].get(key) is not None]
        summary[f"n_{label}"] = len(values)
        summary[f"win_rate_{label}"] = round(sum(1 for v in values if v > 0) / len(values) * 100, 1) if values else None
        summary[f"avg_{label}"] = round(sum(values) / len(values), 2) if values else None
    return summary


def build_candidates(latest_by_market: dict[str, dict]) -> tuple[list[dict], str | None]:
    conn = price_tracker.connect()
    try:
        rows = conn.execute("SELECT market, state_json, updated_at FROM market_state").fetchall()
    finally:
        conn.close()

    candidates = []
    for market, state_json, updated_at in rows:
        state = json.loads(state_json)
        if not state.get("cleared_frames"):
            continue
        rec = latest_by_market.get(market)
        candidates.append({
            "market": market,
            "grade": state.get("grade", "-"),
            "score": state.get("score"),
            "frames": state["cleared_frames"],
            "entry_ready": bool(state.get("entry_ready")),
            "low_15m": bool(state.get("low_15m")),
            "recommendable": bool(state.get("recommendable")),
            "runup_excluded": bool(state.get("runup_excluded")),
            "recent_runup_pct": state.get("recent_runup_pct"),
            "current_price": state.get("current_price"),
            "entry_price": rec["entry_price"] if rec else None,
            "entered_at": rec["entered_at"] if rec else None,
            "return_pct": rec["return_pct"] if rec else None,
            "rec_exit": rec["exit"]["reason"] if rec and rec.get("exit") else None,  # 이미 추천한 종목이면 종료 사유(익절 등)
        })
    candidates.sort(key=lambda c: (c["score"] is None, -(c["score"] or 0)))
    last_update = max((r[2] for r in rows), default=None)
    return candidates, last_update


async def main(out_path: Path) -> None:
    now = datetime.now(timezone.utc)
    async with aiohttp.ClientSession() as session:
        btc = await build_btc(session)

    recs = build_recommendations(now)
    latest_by_market: dict[str, dict] = {}
    for rec in recs:  # recs는 최신순이라 종목별 첫 항목이 가장 최근 추천 (후보 목록은 개선판 기준)
        if rec["strategy"] == "legacy":
            latest_by_market.setdefault(rec["market"], rec)
    candidates, state_updated_at = build_candidates(latest_by_market)

    scans = scan_log.recent(30)
    data = {
        "generated_at": now.astimezone(KST).isoformat(),
        "last_scan_at": scans[0]["ran_at"] if scans else state_updated_at,
        "btc": btc,
        "macro": macro_job.load_cached(),  # 비트코인 분석 탭 (스캔이 시간당 한 번 갱신해 둔 저장본)
        "candidates": candidates,
        "recommendations": recs,
        "summary": build_summary(recs),
        "scans": scans,
        "track_days": price_tracker.TRACK_DAYS,
        "exit_rules": {
            "take_profit_pct": config.EXIT_TAKE_PROFIT_PCT,
            "stop_loss_pct": config.EXIT_STOP_LOSS_PCT,
            "trail_arm_pct": config.EXIT_TRAIL_ARM_PCT,
            "trail_dd_pct": config.EXIT_TRAIL_DD_PCT,
            "btc_exit": config.BTC_EXIT_ENABLED,
            "track_days": price_tracker.TRACK_DAYS,
        },
        "grade_thresholds": config.SCORE_GRADE_THRESHOLDS,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(jsonutil.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"대시보드 데이터 저장: {out_path} (후보 {len(candidates)}개, 추천 기록 {len(recs)}건, 스캔 기록 {len(scans)}건)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    asyncio.run(main(args.out))
