"""OKX에서 실제로 체결된 BTC 선물 청산 기록을 모아 두고 대시보드용으로 요약한다.

- 추정 모형(미결제약정으로 청산가를 가정해 만드는 '예상 청산 지도')은 사전 등록 평가에서 같은 봉 변동폭·OI를 통제하면
  설명력이 없어(2026-10-03, 편상관 +0.015, 95% 구간 [-0.048, +0.079]) 쓰지 않는다. 여기서는 실제 체결된 청산만 다룬다.
- 바이낸스 선물(451)과 Bybit(403)은 GitHub 러너(미국 IP)에서 막혀 OKX 공개 API(liquidation-orders)를 쓴다. OKX 한 거래소 분이라
  전체 시장의 일부다. OKX는 최근 약 하루치만 주므로 스캔마다 받아서 직접 쌓는다.
- 원자료는 RAW_DAYS일, 시간별 합계는 HOURLY_DAYS일 보관한다 (state.db가 git에 커밋되므로 용량을 작게 유지).
- 추천 규칙에는 쓰지 않는 참고 표시다."""

import asyncio
import time
from datetime import datetime, timezone

import aiohttp

from src import state_store

URL = "https://www.okx.com/api/v5/public/liquidation-orders"
UNDERLYING = "BTC-USDT"
CT_VAL_BTC = 0.01  # BTC-USDT-SWAP 1계약 = 0.01 BTC
RAW_DAYS = 7
HOURLY_DAYS = 90
HOUR_MS = 3600_000
PAGE_LIMIT = 100
BACKFILL_PAGES = 30   # 처음(DB가 비었을 때) OKX가 주는 만큼(약 하루치) 받는다
REGULAR_PAGES = 8     # 평소에는 저장된 마지막 시각을 만날 때까지만 받는다 (큰 급락 때도 상한이 있어 폭주하지 않는다)


def _connect():
    conn = state_store.connect_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS liquidations ("
        "ts INTEGER NOT NULL, side TEXT NOT NULL, px REAL NOT NULL, btc REAL NOT NULL, PRIMARY KEY (ts, side, px, btc))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS liq_hourly ("
        "hour INTEGER PRIMARY KEY, long_usd REAL NOT NULL, short_usd REAL NOT NULL, n INTEGER NOT NULL)"
    )
    return conn


async def _fetch_pages(session: aiohttp.ClientSession, stop_ts: int, max_pages: int) -> list[dict]:
    rows: list[dict] = []
    after = None
    for _ in range(max_pages):
        params = {"instType": "SWAP", "uly": UNDERLYING, "state": "filled", "limit": PAGE_LIMIT}
        if after is not None:
            params["after"] = after
        async with session.get(URL, params=params) as resp:
            if resp.status != 200:
                break
            data = await resp.json(content_type=None)
        details = [d for blk in (data.get("data") or []) for d in (blk.get("details") or [])]
        if not details:
            break
        rows.extend(details)
        oldest = min(int(d["ts"]) for d in details)
        if oldest <= stop_ts or (after is not None and oldest >= after):
            break
        after = oldest
        await asyncio.sleep(0.2)
    return rows


async def refresh(session: aiohttp.ClientSession) -> int:
    """새로 체결된 청산을 받아 저장하고, 시간별 합계를 갱신한다. 새로 저장한 건수를 돌려준다."""
    conn = _connect()
    try:
        latest = conn.execute("SELECT COALESCE(MAX(ts), 0) FROM liquidations").fetchone()[0]
        rows = await _fetch_pages(session, latest, BACKFILL_PAGES if latest == 0 else REGULAR_PAGES)
        added = 0
        for d in rows:
            try:
                ts, side, px, btc = int(d["ts"]), d["posSide"], float(d["bkPx"]), float(d["sz"]) * CT_VAL_BTC
            except (KeyError, ValueError, TypeError):
                continue
            if side not in ("long", "short") or ts <= 0:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO liquidations (ts, side, px, btc) VALUES (?, ?, ?, ?)", (ts, side, px, btc))
            added += cur.rowcount
        # 원자료 보관 기준은 정시로 맞춘다 — 남은 시간대는 항상 통째로 있어서 합계를 안전하게 다시 계산할 수 있다
        now_ms = int(time.time() * 1000)
        raw_cut = (now_ms - RAW_DAYS * 86400_000) // HOUR_MS * HOUR_MS
        conn.execute("DELETE FROM liquidations WHERE ts < ?", (raw_cut,))
        conn.execute(
            "INSERT OR REPLACE INTO liq_hourly (hour, long_usd, short_usd, n) "
            "SELECT ts / ? * ?, SUM(CASE WHEN side='long' THEN btc*px ELSE 0 END), SUM(CASE WHEN side='short' THEN btc*px ELSE 0 END), COUNT(*) "
            "FROM liquidations GROUP BY ts / ?", (HOUR_MS, HOUR_MS, HOUR_MS))
        conn.execute("DELETE FROM liq_hourly WHERE hour < ?", (now_ms - HOURLY_DAYS * 86400_000,))
        conn.commit()
        if added:
            print(f"[청산] OKX 실제 청산 {added}건 저장 (받은 {len(rows)}건)")
        return added
    finally:
        conn.close()


def _nice_width(price: float) -> float:
    raw = price * 0.001
    mag = 10 ** int(len(str(int(max(raw, 1)))) - 1)
    for step in (1, 2, 5, 10):
        if raw <= step * mag:
            return float(step * mag)
    return float(10 * mag)


def export(price: float | None = None) -> dict | None:
    """대시보드용 요약. 저장된 기록이 없으면 None."""
    conn = _connect()
    try:
        raw = conn.execute("SELECT ts, side, px, btc FROM liquidations ORDER BY ts").fetchall()
        hourly = conn.execute("SELECT hour, long_usd, short_usd, n FROM liq_hourly ORDER BY hour").fetchall()
    finally:
        conn.close()
    if not raw:
        return None
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    ref_price = price or raw[-1][2]
    width = _nice_width(ref_price)
    bins: dict[int, list[float]] = {}
    for ts, side, px, btc in raw:
        b = int(px // width)
        slot = bins.setdefault(b, [0.0, 0.0])
        slot[0 if side == "long" else 1] += btc
    # 가격대가 너무 멀리 퍼지면 현재가 ±8% 안쪽만 보여 준다 (그 밖은 합계로만)
    lo_px, hi_px = ref_price * 0.92, ref_price * 1.08
    shown = [{"lo": b * width, "hi": (b + 1) * width, "long_btc": round(v[0], 3), "short_btc": round(v[1], 3)}
             for b, v in sorted(bins.items()) if hi_px >= (b + 1) * width and b * width >= lo_px - width]
    now_ms = int(time.time() * 1000)
    last24 = [r for r in raw if r[0] >= now_ms - 86400_000]
    usd = lambda rows, side: sum(btc * px for ts, sd, px, btc in rows if sd == side)
    top = sorted(last24 or raw, key=lambda r: r[3] * r[2], reverse=True)[:5]
    return {
        "since": iso(raw[0][0]), "until": iso(raw[-1][0]), "raw_days": RAW_DAYS, "bin_width": width,
        "bins": shown,
        "hours": [{"t": iso(h), "long_usd": round(lu), "short_usd": round(su), "n": n} for h, lu, su, n in hourly[-(RAW_DAYS * 24 + 24):]],
        "last24h": {"long_usd": round(usd(last24, "long")), "short_usd": round(usd(last24, "short")), "n": len(last24)},
        "top": [{"t": iso(ts), "side": sd, "px": px, "btc": round(btc, 3), "usd": round(btc * px)} for ts, sd, px, btc in top],
        "source": "OKX BTC-USDT 무기한 선물 · 실제 체결된 청산",
    }
