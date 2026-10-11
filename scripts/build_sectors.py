"""업비트 KRW 종목의 섹터(테마) 분류표 data/sectors.json을 만든다. 사용법: python scripts/build_sectors.py

- CoinGecko 카테고리(1,000개가 넘어 너무 잘다)를 THEMES의 20여 개 테마로 묶어서, 카테고리 멤버를 업비트 종목과 '심볼 + 가격 일치'로 맞춘다
  (같은 티커를 쓰는 다른 코인 걸러내기, src/market_cap.py와 같은 방식).
- data/sector_overrides.json(사람이 직접 고치는 보정 파일)의 add/remove를 마지막에 적용한다. 양자·최근 서사처럼 CoinGecko 분류보다 시장 서사가 빠른 것,
  새로 상장해 분류가 없는 종목, 틀린 분류는 여기서 고친다. 한 종목이 여러 섹터에 속할 수 있다.
- 표시 전용이다(대시보드 '섹터' 탭). 추천 규칙에는 쓰지 않는다. 분류는 가끔(새 상장·서사가 바뀔 때) 다시 만들어서 커밋한다.
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from src.exchanges import upbit_client

OUT = Path(__file__).resolve().parent.parent / "data" / "sectors.json"
OVERRIDES = Path(__file__).resolve().parent.parent / "data" / "sector_overrides.json"
CACHE = Path(__file__).resolve().parent.parent / ".sector_cache.json"  # 카테고리 id -> {심볼: [가격,...]} (git에는 안 올린다)
GECKO = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender sector builder)", "Accept": "application/json"}
PRICE_TOL = 0.25
MAX_PAGES = 2  # 카테고리당 시총 상위 500개까지(업비트 종목은 대부분 이 안에 든다)

# 테마 id -> (표시 이름, 한 줄 설명, CoinGecko 카테고리 id 목록). 없는 카테고리 id는 건너뛴다.
THEMES: dict[str, tuple[str, str, list[str]]] = {
    "ai": ("AI", "인공지능·에이전트", ["artificial-intelligence", "ai-agents", "ai-applications", "ai-framework"]),
    "robotics": ("로보틱스", "로봇·자율 시스템", ["robotics"]),
    "rwa": ("RWA·토큰화", "실물자산·국채·금 토큰화", ["real-world-assets-rwa", "rwa-protocol", "tokenized-gold"]),
    "quantum": ("양자내성", "양자컴퓨터에 안전한 서명·구조 서사", ["quantum-resistant"]),
    "zk": ("ZK", "영지식 증명", ["zero-knowledge-zk"]),
    "l2": ("레이어2·모듈러", "확장·데이터 가용성", ["layer-2", "modular-blockchain"]),
    "l1": ("레이어1", "독립 메인넷", ["layer-1"]),
    "privacy": ("프라이버시", "익명성·기밀 거래", ["privacy-coins", "privacy-blockchain"]),
    "defi": ("디파이", "탈중앙 금융 전반", ["decentralized-finance-defi", "decentralized-exchange", "lending-borrowing"]),
    "staking": ("스테이킹·리스테이킹", "유동성 스테이킹", ["liquid-staking", "restaking"]),
    "oracle": ("오라클", "외부 데이터 연결", ["oracle"]),
    "depin": ("DePIN·스토리지", "탈중앙 물리 인프라·저장", ["depin", "storage"]),
    "meme": ("밈", "밈코인", ["meme-token"]),
    "gaming": ("게임·메타버스", "게임파이·메타버스", ["gaming", "gaming-platform", "metaverse"]),
    "social": ("소셜·미디어", "소셜파이", ["socialfi"]),
    "interop": ("상호운용·브리지", "체인 간 연결", ["interoperability"]),
    "exchange": ("거래소 토큰", "중앙화 거래소 발행 토큰", ["centralized-exchange-token-cex"]),
    "cosmos_eco": ("코스모스 생태계", "IBC 체인", ["cosmos-ecosystem"]),
}


async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    """무료 한도가 빡빡해서(몇 건 뒤 429) 응답이 알려 주는 Retry-After만큼 기다렸다가 다시 보낸다."""
    for attempt in range(8):
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 429:
                wait = int(resp.headers.get("Retry-After") or 30)
                await asyncio.sleep(min(wait, 90) + 1)
                continue
            resp.raise_for_status()
            return await resp.json(content_type=None)
    raise RuntimeError(f"429 반복: {url}")


def match(symbol_prices: dict[str, list[float]], market: str, price: float | None) -> bool:
    if not price:
        return False
    return any(abs(p / price - 1) <= PRICE_TOL for p in symbol_prices.get(market[4:].lower(), []) if p)


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        known = {c["category_id"] for c in await get_json(s, f"{GECKO}/coins/categories/list")}
        markets = [m for m in await upbit_client.fetch_markets(s)]
        prices = await upbit_client.fetch_ticker_prices(s, markets)
        print(f"업비트 KRW {len(markets)}종목, 가격 {len(prices)}개, CoinGecko 카테고리 {len(known)}개")
        members: dict[str, dict[str, list[float]]] = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() and "--refetch" not in sys.argv else {}
        wanted = sorted({c for _, _, cats in THEMES.values() for c in cats})
        missing = [c for c in wanted if c not in known]
        print("없는 카테고리 id(건너뜀):", missing)
        for cid in [c for c in wanted if c in known and c not in members]:
            sym: dict[str, list[float]] = {}
            for page in range(1, MAX_PAGES + 1):
                rows = await get_json(s, f"{GECKO}/coins/markets", {"vs_currency": "krw", "category": cid, "order": "market_cap_desc", "per_page": 250, "page": page})
                for r in rows:
                    if r.get("current_price"):
                        sym.setdefault((r.get("symbol") or "").lower(), []).append(float(r["current_price"]))
                await asyncio.sleep(1.0)
                if len(rows) < 250:
                    break
            members[cid] = sym
            CACHE.write_text(json.dumps(members, ensure_ascii=False), encoding="utf-8")
            print(f"  {cid:<40} {sum(len(v) for v in sym.values()):4d}개 코인", flush=True)

    themes = {}
    for tid, (name, desc, cats) in THEMES.items():
        ms = sorted({m for m in markets for c in cats if c in members and match(members[c], m, prices.get(m))})
        themes[tid] = {"name": name, "desc": desc, "markets": ms, "source": [c for c in cats if c in members]}
    ov = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    for tid, lst in (ov.get("add") or {}).items():
        if tid not in themes:
            themes[tid] = {"name": (ov.get("names") or {}).get(tid, tid), "desc": (ov.get("descs") or {}).get(tid, "직접 지정"), "markets": [], "source": []}
        themes[tid]["markets"] = sorted(set(themes[tid]["markets"]) | {m for m in lst if m in markets})
        themes[tid]["manual_add"] = [m for m in lst if m in markets]
    for tid, lst in (ov.get("remove") or {}).items():
        if tid in themes:
            themes[tid]["markets"] = [m for m in themes[tid]["markets"] if m not in set(lst)]
    covered = {m for t in themes.values() for m in t["markets"]}
    out = {"built_at": datetime.now(timezone.utc).isoformat(), "source": "CoinGecko 카테고리 + data/sector_overrides.json",
           "themes": themes, "unclassified": sorted(set(markets) - covered)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장 {OUT} | 분류됨 {len(covered)}/{len(markets)}종목, 미분류 {len(out['unclassified'])}종목")
    for tid, t in sorted(themes.items(), key=lambda kv: -len(kv[1]["markets"])):
        print(f"  {t['name']:<14} {len(t['markets']):3d}종목: {' '.join(m[4:] for m in t['markets'][:12])}{' …' if len(t['markets']) > 12 else ''}")


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
    print(f"{time.time() - t0:.0f}초")
