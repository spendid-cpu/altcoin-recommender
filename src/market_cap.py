"""알트코인 시가총액(참고치) — 코인게코 공개 API에서 KRW 기준 시가총액을 받아 심볼로 매칭한다.

업비트는 시가총액을 안 주기 때문에 외부 소스가 필요하다. 심볼(티커)만으로 매칭하다 보니 같은 심볼을 쓰는
다른(무명) 프로젝트와 섞일 수 있다 — 시가총액이 큰 코인을 우선(정렬 결과의 첫 등장)으로 남겨서 그 위험을
줄인다. 자주 안 바뀌는 값이라 REFRESH_HOURS마다만 새로 받고, 실패해도 스캔 전체는 계속돼야 하므로 호출하는
쪽(pipeline)이 예외를 감싼다.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiohttp

from src import jsonutil, state_store

CACHE_KEY = "market_cap_json"
REFRESH_HOURS = 12
BASE_URL = "https://api.coingecko.com/api/v3/coins/markets"
PAGES = 5  # 페이지당 250개 = 최대 1250개 코인(시가총액 큰 순)


async def _fetch_page(session: aiohttp.ClientSession, page: int) -> list[dict]:
    params = {"vs_currency": "krw", "order": "market_cap_desc", "per_page": 250, "page": page}
    async with session.get(BASE_URL, params=params) as resp:
        if resp.status != 200:
            return []
        return await resp.json()


async def build(session: aiohttp.ClientSession) -> dict:
    caps: dict[str, float] = {}
    for page in range(1, PAGES + 1):
        try:
            data = await _fetch_page(session, page)
        except Exception as exc:
            print(f"[시가총액] {page}페이지 조회 실패(있는 데이터만 사용): {exc!r}")
            break
        if not data:
            break
        for c in data:
            sym = (c.get("symbol") or "").lower()
            cap = c.get("market_cap")
            if sym and cap and sym not in caps:  # 정렬이 시가총액 큰 순이라 먼저 나온 값이 대표값
                caps[sym] = float(cap)
        await asyncio.sleep(1.5)  # 코인게코 무료 API 속도 제한(분당 요청 수)을 지킨다
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "caps": caps}


async def refresh(session: aiohttp.ClientSession) -> None:
    cached = load_cached()
    if cached:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["generated_at"])
        if age < timedelta(hours=REFRESH_HOURS):
            return
    data = await build(session)
    if not data["caps"]:
        print("[시가총액] 갱신 실패(빈 응답) — 이전 값을 유지")
        return
    state_store.set_meta(CACHE_KEY, jsonutil.dumps(jsonutil.finite(data), ensure_ascii=False))
    print(f"[시가총액] 갱신: {len(data['caps'])}종목 매칭")


def load_cached() -> dict | None:
    raw = state_store.get_meta(CACHE_KEY)
    return json.loads(raw) if raw else None


def get(symbol: str) -> float | None:
    """market: 'KRW-WLFI' 형식이 아니라 'WLFI' 심볼만 넣는다."""
    cached = load_cached()
    if not cached:
        return None
    return cached["caps"].get(symbol.lower())
