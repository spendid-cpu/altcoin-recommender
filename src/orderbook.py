"""호가창(지금 걸려 있는 매수·매도 주문)을 스냅샷으로 받아 쏠림과 '벽'을 요약한다.

- 업비트: 묶음 단위(level)를 써서 현재가 위아래 약 ±3% 범위의 30단계 호가를 받는다. 추천 중인 종목은 매수/매도 잔량 쏠림을,
  BTC는 단계별 벽과 최근 몇 시간 동안 그 벽이 유지됐는지(지속성)를 본다.
- 바이낸스 BTCUSDT: 5000단계(현재가 위아래 약 1%)를 가격대로 묶어 가까운 쪽 벽을 본다.
- 호가는 순간 사진이라 몇 초 만에 취소되는 허수 주문이 많다. 그래서 벽은 지속성과 함께 보고, 참고 표시로만 쓴다
  (추천 규칙에는 안 씀). 진입 시점의 쏠림은 추천 기록(detail)에 남겨, 나중에 사전 등록 검증에 쓸 수 있게 한다."""

import asyncio
import json
import statistics
import time
from datetime import datetime, timezone

import aiohttp

from src import jsonutil, price_tracker, state_store
from src.exchanges import binance_client, upbit_client

META_KEY = "orderbook_json"
BTC_MARKET = "KRW-BTC"
HIST_HOURS = 6        # BTC 벽 지속성을 계산하려고 스냅샷을 이만큼 보관
WALL_X = 2.0          # 같은 쪽 단계 중앙값의 이 배수 이상이면 '벽'
TARGET_UNIT_PCT = 0.2  # 묶음 한 칸 폭을 현재가의 약 0.2%로 (30단계 -> 위아래 합쳐 약 ±3%)
CHUNK = 20            # 한 번에 조회하는 종목 수
HIST_STEP_MIN = 30     # 호가 매수 비중 이력 간격
HIST_POINTS = 24       # 시장당 보관 개수 (30분 x 24 = 12시간)
HIST_KEEP_HOURS = 12


def _connect():
    conn = state_store.connect_db()
    conn.execute("CREATE TABLE IF NOT EXISTS orderbook_hist (ts INTEGER NOT NULL, market TEXT NOT NULL, units TEXT NOT NULL, PRIMARY KEY (ts, market))")
    return conn


async def supported_levels(session: aiohttp.ClientSession, markets: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i in range(0, len(markets), 50):
        data = await upbit_client._get_json(session, "orderbook/supported_levels", {"markets": ",".join(markets[i:i + 50])})
        for row in data:
            out[row["market"]] = [int(x) for x in row.get("supported_levels", [])]
    return out


def pick_level(price: float, levels: list[int]) -> int:
    """현재가의 TARGET_UNIT_PCT%에 가장 가까운 묶음 단위. 지원하는 단위가 없으면 0(묶지 않음)."""
    cands = [lv for lv in levels if lv > 0]
    if not cands or price <= 0:
        return 0
    target = price * TARGET_UNIT_PCT / 100
    return min(cands, key=lambda lv: abs(lv - target) / target)


def summarize(book: dict, level: int) -> dict:
    """업비트 호가(orderbook_units)를 금액(원) 기준으로 요약한다."""
    units = book["orderbook_units"]
    bids = [(float(u["bid_price"]), float(u["bid_size"]) * float(u["bid_price"])) for u in units]
    asks = [(float(u["ask_price"]), float(u["ask_size"]) * float(u["ask_price"])) for u in units]
    bid_total, ask_total = sum(v for _, v in bids), sum(v for _, v in asks)

    def wall(side: list[tuple[float, float]]) -> dict:
        med = statistics.median(v for _, v in side) or 0.0
        px, v = max(side, key=lambda x: x[1])
        return {"px": px, "krw": round(v), "x": round(v / med, 1) if med > 0 else 0.0}

    mid = (bids[0][0] + asks[0][0]) / 2
    return {
        "imb": round(bid_total / (bid_total + ask_total), 3) if bid_total + ask_total > 0 else 0.5,
        "bid_krw": round(bid_total), "ask_krw": round(ask_total),
        "bid_wall": wall(bids), "ask_wall": wall(asks),
        "range_pct": round((asks[-1][0] - bids[-1][0]) / mid * 100, 2), "level": level,
        "bids": bids, "asks": asks,
    }


async def fetch_summaries(session: aiohttp.ClientSession, prices: dict[str, float]) -> dict[str, dict]:
    """prices: {마켓: 현재가}. 마켓별 호가 요약. 일부가 실패해도 받은 것만 돌려준다."""
    markets = [m for m, p in prices.items() if p]
    if not markets:
        return {}
    levels = await supported_levels(session, markets)
    groups: dict[int, list[str]] = {}
    for m in markets:
        groups.setdefault(pick_level(prices[m], levels.get(m, [])), []).append(m)
    out: dict[str, dict] = {}
    for level, ms in groups.items():
        for i in range(0, len(ms), CHUNK):
            try:
                data = await upbit_client._get_json(session, "orderbook", {"markets": ",".join(ms[i:i + CHUNK]), "level": level})
            except Exception as exc:
                print(f"[호가] 업비트 조회 실패(level {level}, {len(ms[i:i + CHUNK])}종목, 건너뜀): {exc!r}")
                continue
            for book in data:
                if book.get("orderbook_units"):
                    out[book["market"]] = summarize(book, level)
            await asyncio.sleep(0.12)
    return out


def _slim(s: dict) -> dict:
    return {k: v for k, v in s.items() if k not in ("bids", "asks")}


def binance_summary(depth: dict) -> dict | None:
    bids, asks = depth["bids"], depth["asks"]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    step = min((5, 10, 20, 25, 50, 100, 200), key=lambda s: abs(s - mid * 0.0006))
    lo_px, hi_px = mid * 0.99, mid * 1.01
    bins: dict[int, list[float]] = {}
    for px, q in bids:
        if px >= lo_px:
            bins.setdefault(int(px // step), [0.0, 0.0])[0] += q
    for px, q in asks:
        if px <= hi_px:
            bins.setdefault(int(px // step), [0.0, 0.0])[1] += q
    rows = [{"lo": b * step, "hi": (b + 1) * step, "bid_btc": round(v[0], 3), "ask_btc": round(v[1], 3)} for b, v in sorted(bins.items())]
    near_bid = sum(q for px, q in bids if px >= mid * 0.995)
    near_ask = sum(q for px, q in asks if px <= mid * 1.005)
    return {
        "mid": round(mid, 2), "step": step, "bins": rows,
        "imb_near": round(near_bid / (near_bid + near_ask), 3) if near_bid + near_ask > 0 else 0.5,
        "bid_total_btc": round(sum(q for px, q in bids if px >= lo_px), 1), "ask_total_btc": round(sum(q for px, q in asks if px <= hi_px), 1),
        "span_pct": round((asks[-1][0] - bids[-1][0]) / mid * 100, 2),
    }


def _save_btc_history(conn, now_ms: int, s: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO orderbook_hist (ts, market, units) VALUES (?, ?, ?)",
                 (now_ms, BTC_MARKET, jsonutil.dumps({"bids": s["bids"], "asks": s["asks"], "level": s["level"]}, separators=(",", ":"))))
    conn.execute("DELETE FROM orderbook_hist WHERE ts < ?", (now_ms - HIST_HOURS * 3600_000,))


def _persistence(conn, current: dict) -> dict:
    """현재 스냅샷의 매수/매도 벽이 최근 HIST_HOURS 동안 몇 %의 스냅샷에서 벽이었는지."""
    rows = conn.execute("SELECT units FROM orderbook_hist WHERE market = ? ORDER BY ts", (BTC_MARKET,)).fetchall()
    snaps = [json.loads(r[0]) for r in rows]
    snaps = [s for s in snaps if s.get("level") == current["level"]]
    out = {}
    for side_key in ("bids", "asks"):
        walls_now = _wall_prices(current[side_key])
        hits = {px: 0 for px in walls_now}
        for sn in snaps:
            prev = _wall_prices(sn[side_key])
            for px in walls_now:
                hits[px] += px in prev
        out[side_key] = {px: round(hits[px] / len(snaps), 2) if snaps else None for px in walls_now}
    out["n"] = len(snaps)
    return out


def _wall_prices(side: list) -> set[float]:
    vals = [v for _, v in side]
    med = statistics.median(vals) if vals else 0
    return {float(p) for p, v in side if med > 0 and v >= med * WALL_X}


async def refresh(session: aiohttp.ClientSession) -> None:
    """BTC와 추적 중인 종목의 호가를 받아 저장한다 (대시보드·추천 카드용)."""
    markets = sorted({BTC_MARKET, *price_tracker.active_tracked_markets()})
    prices = await upbit_client.fetch_ticker_prices(session, markets)
    books = await fetch_summaries(session, prices)
    if not books:
        return
    now_ms = int(time.time() * 1000)
    conn = _connect()
    try:
        btc = books.get(BTC_MARKET)
        btc_out = None
        if btc:
            _save_btc_history(conn, now_ms, btc)
            conn.commit()
            per = _persistence(conn, btc)
            def rows(side_key, items):
                return [{"px": px, "krw": round(v), "wall": px in _wall_prices(btc[side_key]),
                         "persist": per[side_key].get(float(px))} for px, v in items]
            btc_out = {**_slim(btc), "price": prices.get(BTC_MARKET), "bids": rows("bids", btc["bids"]), "asks": rows("asks", btc["asks"]),
                       "snapshots": per["n"], "hist_hours": HIST_HOURS}
    finally:
        conn.close()
    binance = None
    try:
        binance = binance_summary(await binance_client.fetch_depth(session, "BTCUSDT", 5000))
    except Exception as exc:
        print(f"[호가] 바이낸스 호가 조회 실패(건너뜀): {type(exc).__name__}: {exc}")
    # 호가 매수 비중의 변화를 보려고 종목마다 30분 간격으로 최근 12시간치만 가볍게 남긴다 ([시각ms, 비중])
    try:
        prev_hist = (export() or {}).get("hist") or {}
    except Exception:
        prev_hist = {}
    hist = {}
    for m, sm in books.items():
        series = [pt for pt in prev_hist.get(m, []) if now_ms - pt[0] < HIST_KEEP_HOURS * 3600_000]
        if not series or now_ms - series[-1][0] >= HIST_STEP_MIN * 60_000 - 90_000:
            series.append([now_ms, sm["imb"]])
        hist[m] = series[-HIST_POINTS:]
    payload = {"at": datetime.now(timezone.utc).isoformat(), "btc": btc_out, "binance": binance,
               "markets": {m: _slim(s) for m, s in books.items() if m != BTC_MARKET}, "hist": hist}
    state_store.set_meta(META_KEY, jsonutil.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"[호가] 업비트 {len(books)}종목 · 바이낸스 {'OK' if binance else '실패'}")


def export() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None
