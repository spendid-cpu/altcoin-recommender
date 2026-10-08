"""공포·탐욕 지수(Fear & Greed, alternative.me 무료 공개 API): 오늘 화면 표시와 추천 순간 기록용. 추천 판단에는 쓰지 않는다.

배경(2026-10-09): 과거 대조군 신호 백테스트에서 지수가 75 이상(극단적 탐욕)일 때 진입한 신호의 3일 내 -5% 터치율이 74%로 나머지(38.5%)보다 높았지만
사전 기준(수익 개선)은 통과하지 못했고 에피소드가 11개뿐이라 확정이 아니다. 그래서 규칙은 안 바꾸고, 지수를 화면에 보여주고 추천 순간 값을
기록(detail.fng)해 새 데이터로 다시 검증한다. 지수는 하루에 한 번(00:00 UTC) 바뀌어서 REFRESH_MIN분마다만 받는다."""

import json
from datetime import datetime, timedelta, timezone

import aiohttp

from src import jsonutil, state_store

URL = "https://api.alternative.me/fng/?limit=45&format=json"
META_KEY = "fear_greed_json"
REFRESH_MIN = 60
STALE_DAYS = 3  # 이보다 오래된 값은 추천 기록에 쓰지 않는다


def export() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


def latest_value(now: datetime | None = None) -> int | None:
    """추천 순간에 기록할 값: 저장된 가장 최근 지수(너무 오래됐으면 None). 네트워크 호출 없음."""
    data = export()
    if not data or not data.get("latest"):
        return None
    now = now or datetime.now(timezone.utc)
    day = datetime.fromisoformat(data["latest"]["d"]).replace(tzinfo=timezone.utc)
    return int(data["latest"]["v"]) if now - day <= timedelta(days=STALE_DAYS) else None


def parse(payload: dict) -> dict | None:
    rows = []
    for x in payload.get("data") or []:
        try:
            v, ts = int(x["value"]), int(x["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= v <= 100:
            rows.append((ts, v, x.get("value_classification") or ""))
    if not rows:
        return None
    rows.sort()
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).date().isoformat()  # noqa: E731
    ts, v, cls = rows[-1]
    return {"latest": {"v": v, "c": cls, "d": day(ts)}, "hist": [[day(t), val] for t, val, _ in rows]}


async def refresh(session: aiohttp.ClientSession) -> None:
    prev = export()
    now = datetime.now(timezone.utc)
    if prev and prev.get("at") and now - datetime.fromisoformat(prev["at"]) < timedelta(minutes=REFRESH_MIN):
        return
    async with session.get(URL, headers={"User-Agent": "Mozilla/5.0 (compatible; altcoin-recommender dashboard)"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = parse(await resp.json(content_type=None))
    if data is None:
        print("[공포·탐욕] 응답이 비어 있어 이전 값 유지")
        return
    data["at"] = now.isoformat()
    state_store.set_meta(META_KEY, jsonutil.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"[공포·탐욕] {data['latest']['v']} ({data['latest']['c']}, {data['latest']['d']})")
