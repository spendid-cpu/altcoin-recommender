"""비트코인 MVRV Z-score (CoinMetrics 커뮤니티 공개 자료): 추천 순간 기록(detail.btc_ctx.mvrv_z)용. 추천 판단에는 쓰지 않는다.

Z = (시가총액 - 실현시가총액) / 시가총액의 누적 표준편차 (실현시가총액 = 시가총액 / MVRV). 하루에 한 번만 바뀌어서 REFRESH_HOURS마다만 받는다
(전체 이력이 필요한 누적 표준편차라 한 번에 약 1MB). 실패하면 이전 값을 그대로 둔다."""

import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd

from src import jsonutil, state_store

META_KEY = "btc_mvrv_json"
REFRESH_HOURS = 12
URL = ("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc&metrics=CapMVRVCur,CapMrktCurUSD"
       "&frequency=1d&start_time=2010-07-18&page_size=10000")


def compute(rows: list[dict]) -> dict | None:
    """CoinMetrics 행들 -> 가장 최근 날의 MVRV와 Z-score. 자료가 모자라면 None."""
    df = pd.DataFrame(rows)
    if df.empty or "CapMVRVCur" not in df or "CapMrktCurUSD" not in df:
        return None
    df["mvrv"] = pd.to_numeric(df["CapMVRVCur"], errors="coerce")
    df["mc"] = pd.to_numeric(df["CapMrktCurUSD"], errors="coerce")
    df = df.dropna(subset=["mvrv", "mc"]).reset_index(drop=True)
    if len(df) < 365:
        return None
    df["z"] = (df["mc"] - df["mc"] / df["mvrv"]) / df["mc"].expanding().std()
    last = df.iloc[-1]
    return {"date": str(pd.to_datetime(last["time"]).date()), "mvrv": round(float(last["mvrv"]), 3), "z": round(float(last["z"]), 3)}


async def refresh(session: aiohttp.ClientSession) -> None:
    raw = state_store.get_meta(META_KEY)
    now = datetime.now(timezone.utc)
    if raw:
        try:
            if now - datetime.fromisoformat(json.loads(raw)["at"]) < timedelta(hours=REFRESH_HOURS):
                return
        except (KeyError, ValueError):
            pass
    rows, url = [], URL
    for _ in range(5):  # 이력이 1만 행을 넘을 때만 다음 페이지가 있다
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender)"}, timeout=aiohttp.ClientTimeout(total=40)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        rows += data.get("data", [])
        url = data.get("next_page_url")
        if not url:
            break
    out = compute(rows)
    if out is None:
        print("[MVRV] 자료가 모자라 갱신 건너뜀")
        return
    out["at"] = now.isoformat()
    state_store.set_meta(META_KEY, jsonutil.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"[MVRV] Z-score {out['z']} (MVRV {out['mvrv']}, {out['date']})")
