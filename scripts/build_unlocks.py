"""업비트 KRW 종목의 토큰 언락(유통량이 늘어나는 시점) 표 data/unlocks.json을 만든다. 사용법: python scripts/build_unlocks.py

- 자료: DefiLlama 언락 데이터셋(defillama-datasets.llama.fi/emissions/{slug}, 공식 API는 유료지만 이 경로는 공개). 배분 항목(팀·투자자·커뮤니티·에코시스템 등)별 누적 해제량이
  일 단위로 미래까지 계산돼 있다. 항목들을 합친 누적 곡선 U(t)에서 향후 30·90일 해제량과, 유통량의 UNLOCK_JUMP_PCT% 이상이 한 번에 풀리는 날(다음 대형 언락)을 구한다.
- 업비트 종목과는 CoinGecko 코인 id로 맞춘다(DefiLlama의 gecko_id <-> CoinGecko coins/list의 심볼 + 가격 일치). 유통량·최대 공급량은 CoinGecko coins/markets.
- 한계: DefiLlama가 일정을 문서화한 약 370개 프로토콜만 있다(없으면 '정보 없음'). 스케줄상 해제량이라 실제 시장에 풀린 양·매도와는 다르고, 팀 물량 연장·소각 같은 변경은
  늦게 반영될 수 있다. 인플레이션(스테이킹 보상) 방출도 선형 해제로 잡힌다. 참고용이고 추천 규칙에는 쓰지 않는다. 주 1회 정도 다시 만들어 커밋한다.
"""

import asyncio
import bisect
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.exchanges import upbit_client

OUT = Path(__file__).resolve().parent.parent / "data" / "unlocks.json"
LLAMA = "https://defillama-datasets.llama.fi"
GECKO = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender unlock builder)", "Accept": "application/json"}
PRICE_TOL = 0.25
UNLOCK_JUMP_PCT = 0.5      # 유통량의 이 %를 넘게 한 번에 풀리면 '대형 언락'으로 센다
HORIZON_DAYS = 365
DAY = 86400


async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None, timeout: int = 90):
    for attempt in range(6):
        try:
            async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(min(int(resp.headers.get("Retry-After") or 30), 90) + 1)
                    continue
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 * (attempt + 1))
    return None


def cumulative(series: list[dict]) -> list[tuple[int, float]]:
    """배분 항목별 누적 해제량(timestamp, unlocked) 목록들을 합친 전체 누적 곡선. 항목마다 시각이 달라도 직전 값을 이어 받는다."""
    stamps = sorted({p["timestamp"] for s in series for p in s.get("data", [])})
    last = [0.0] * len(series)
    idx = [0] * len(series)
    out = []
    for ts in stamps:
        for k, s in enumerate(series):
            pts = s.get("data", [])
            while idx[k] < len(pts) and pts[idx[k]]["timestamp"] <= ts:
                last[k] = float(pts[idx[k]].get("unlocked") or 0)
                idx[k] += 1
        out.append((ts, sum(last)))
    return out


def at(cum: list[tuple[int, float]], ts: int) -> float:
    keys = [c[0] for c in cum]
    i = bisect.bisect_right(keys, ts) - 1
    return cum[i][1] if i >= 0 else 0.0


def upcoming(cum: list[tuple[int, float]], now: int, circ: float, price_krw: float | None) -> dict:
    """향후 30·90일 해제량과 다음 대형 언락(유통량 대비 UNLOCK_JUMP_PCT% 이상, 같은 날은 합침) 최대 3개."""
    base = at(cum, now)
    out = {"u30_pct": round((at(cum, now + 30 * DAY) - base) / circ * 100, 3), "u90_pct": round((at(cum, now + 90 * DAY) - base) / circ * 100, 3)}
    days: dict[int, float] = {}
    prev = base
    for ts, u in cum:
        if ts <= now:
            continue
        if ts > now + HORIZON_DAYS * DAY:
            break
        d = u - prev
        prev = u
        if d > 0:
            day = ts // DAY
            days[day] = days.get(day, 0.0) + d
    big = [(day, t) for day, t in sorted(days.items()) if t / circ * 100 >= UNLOCK_JUMP_PCT]
    out["next"] = [{"d": datetime.fromtimestamp(day * DAY, timezone.utc).date().isoformat(), "pct": round(t / circ * 100, 2),
                    "krw": round(t * price_krw) if price_krw else None} for day, t in big[:3]]
    return out


async def main() -> None:
    now = int(time.time())
    async with aiohttp.ClientSession() as s:
        markets = await upbit_client.fetch_markets(s)
        prices = await upbit_client.fetch_ticker_prices(s, markets)
        det = await upbit_client._fetch_market_details(s)
        names = {x["market"]: x.get("english_name") for x in det if x["market"].startswith("KRW-")}
        print(f"업비트 KRW {len(markets)}종목")

        coin_list = await get_json(s, f"{GECKO}/coins/list")
        by_symbol: dict[str, set[str]] = {}
        for c in coin_list:
            by_symbol.setdefault((c.get("symbol") or "").lower(), set()).add(c["id"])
        wanted = {cid for m in markets for cid in by_symbol.get(m[4:].lower(), ())}
        print(f"CoinGecko 코인 {len(coin_list)}개, 업비트 심볼에 해당하는 후보 id {len(wanted)}개")

        slugs = await get_json(s, f"{LLAMA}/emissionsProtocolsList") or []
        sem = asyncio.Semaphore(5)
        protos: dict[str, dict] = {}  # gecko id -> {name, cum}

        async def one(slug: str):
            async with sem:
                j = await get_json(s, f"{LLAMA}/emissions/{slug}", timeout=120)
            if not j:
                return
            gid = j.get("gecko_id")
            tok = (j.get("metadata") or {}).get("token") or ""
            if not gid and tok.startswith("coingecko:"):
                gid = tok.split(":", 1)[1]
            if not gid or gid not in wanted:
                return
            series = (j.get("documentedData") or {}).get("data") or []
            if series:
                protos[gid] = {"name": j.get("name") or slug, "slug": slug, "cum": cumulative(series)}

        t0 = time.time()
        await asyncio.gather(*(one(sl) for sl in slugs))
        print(f"DefiLlama 프로토콜 {len(slugs)}개 중 업비트 후보와 겹치는 것 {len(protos)}개 ({time.time() - t0:.0f}초)")

        ids = sorted(protos)
        info: dict[str, dict] = {}
        for i in range(0, len(ids), 200):
            rows = await get_json(s, f"{GECKO}/coins/markets", {"vs_currency": "krw", "ids": ",".join(ids[i:i + 200]), "per_page": 250}) or []
            for r in rows:
                info[r["id"]] = r
            await asyncio.sleep(2)

    items = {}
    for m in markets:
        sym = m[4:].lower()
        for gid in by_symbol.get(sym, ()):
            if gid not in protos or gid not in info:
                continue
            r, pr = info[gid], prices.get(m)
            cp = r.get("current_price")
            if not pr or not cp or abs(cp / pr - 1) > PRICE_TOL:   # 같은 티커의 다른 코인 걸러내기
                continue
            circ, mx = r.get("circulating_supply"), r.get("max_supply") or r.get("total_supply")
            if not circ:
                continue
            u = upcoming(protos[gid]["cum"], now, float(circ), float(cp))
            items[m] = {"name": protos[gid]["name"], "gecko": gid, "circ": circ, "max": mx,
                        "locked_pct": round((1 - circ / mx) * 100, 1) if mx and mx >= circ else None, **u}
            break
    out = {"built_at": datetime.now(timezone.utc).isoformat(), "source": "DefiLlama 언락 데이터셋 + CoinGecko 공급량",
           "jump_pct": UNLOCK_JUMP_PCT, "items": items}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장 {OUT} | 업비트 종목 {len(items)}개에 언락 정보 ({len(items)}/{len(markets)})")
    soon = sorted(((v["next"][0]["d"], m, v["next"][0]["pct"]) for m, v in items.items() if v["next"]))
    for d, m, p in soon[:15]:
        print(f"  다음 대형 언락 {d} {m[4:]:<8} 유통량의 {p}%")
    big30 = sorted(((v["u30_pct"], m) for m, v in items.items()), reverse=True)[:10]
    print("  향후 30일 해제 큰 순:", " ".join(f"{m[4:]} {p:.1f}%" for p, m in big30))


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
    print(f"{time.time() - t0:.0f}초")
