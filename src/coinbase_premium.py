"""코인베이스 프리미엄 = 코인베이스 BTC-USD / 바이낸스 BTCUSDT - 1 (미국 현물 BTC 수요를 보는 참고 지표, 2026-10-16 사용자 결정으로 기록 시작).

백테스트(scratchpad ext_data_test.py의 C1, 일봉 저점 확인 신호 4,431건, 2024-03~2026-09): 추천 직전 24시간 평균이 상위 1/3(+0.018% 이상)일 때
하위 1/3(-0.062% 이하)보다 3일 수익 +1.23%p·손절터치 -4.7%p였고 앞/뒤 절반 모두 같은 방향이었지만, 99.17% 구간 [-1.14, +3.52]가 0을 포함해 '근거 없음'.
그래서 추천 판단·점수에는 쓰지 않고, 추천 순간 값(detail.cbp)을 기록해 라이브로 다시 확인하고 비트코인 탭에 보여준다.
- 1시간봉 종가끼리 비교한다(코인베이스 공개 API 1시간봉, 바이낸스 BTCUSDT 1시간봉). 24시간 평균 = 최근 마감 24개 평균으로 백테스트와 같은 정의.
- USD와 USDT의 가격 차이도 섞여 있어서 0이 기준점은 아니다(백테스트 기간 3분위 경계가 -0.062% / +0.018%).
- REFRESH_MIN분마다 다시 받는다(요청 2번). 실패하면 이전 값을 그대로 둔다."""

import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd

from src import jsonutil, state_store
from src.exchanges import binance_client

META_KEY = "coinbase_premium_json"
CB_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
REFRESH_MIN = 15
HIST_HOURS = 168
FRESH_HOURS = 3
LOW_CUT, HIGH_CUT = -0.00062, 0.00018  # 백테스트 3분위 경계 (24시간 평균)


def tier(avg24: float | None) -> str | None:
    if avg24 is None:
        return None
    return "high" if avg24 >= HIGH_CUT else "low" if avg24 <= LOW_CUT else "mid"


def export() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


def entry_value(now: datetime | None = None) -> dict | None:
    """추천 순간 기록용 {avg24, last, tier}. 값이 FRESH_HOURS보다 낡았으면 None (네트워크 호출 없음)."""
    x = export()
    now = now or datetime.now(timezone.utc)
    if not x or x.get("avg24") is None or now - datetime.fromisoformat(x["at"]) > timedelta(hours=FRESH_HOURS):
        return None
    return {"avg24": x["avg24"], "last": x["last"], "tier": x["tier"]}


async def refresh(session: aiohttp.ClientSession, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    prev = export()
    if prev and now - datetime.fromisoformat(prev["at"]) < timedelta(minutes=REFRESH_MIN):
        return prev
    async with session.get(CB_URL, params={"granularity": 3600}, headers={"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender)"},
                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        rows = await resp.json()  # [시작 시각(초), 저가, 고가, 시가, 종가, 거래량], 최신이 먼저
    now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    cb = {pd.Timestamp(r[0], unit="s"): float(r[4]) for r in rows if pd.Timestamp(r[0], unit="s") + pd.Timedelta(hours=1) <= now_naive}
    bn = await binance_client.fetch_ohlcv(session, "BTCUSDT", "1h", 200)  # 마감된 봉만, time = 마감 시각
    bn_start = {(t + pd.Timedelta(milliseconds=1) - pd.Timedelta(hours=1)).floor("h"): float(c) for t, c in zip(bn["time"], bn["close"])}
    hours = sorted(set(cb) & set(bn_start))[-HIST_HOURS:]
    if len(hours) < 18:
        print(f"[코인베이스 프리미엄] 맞춘 1시간봉이 {len(hours)}개뿐이라 이번엔 건너뜀")
        return prev
    prem = [cb[h] / bn_start[h] - 1 for h in hours]
    last24 = prem[-24:]
    avg24 = sum(last24) / len(last24)
    out = {"at": now.isoformat(), "avg24": round(avg24, 6), "last": round(prem[-1], 6), "last_hour": (hours[-1] + pd.Timedelta(hours=1)).isoformat() + "Z",
           "tier": tier(avg24), "cuts": [LOW_CUT, HIGH_CUT], "hist": [[(h + pd.Timedelta(hours=1)).isoformat() + "Z", round(p, 6)] for h, p in zip(hours, prem)]}
    state_store.set_meta(META_KEY, jsonutil.dumps(out, separators=(",", ":")))
    print(f"[코인베이스 프리미엄] 24시간 평균 {avg24 * 100:+.3f}% ({out['tier']}) · 최근 1시간 {prem[-1] * 100:+.3f}%")
    return out
