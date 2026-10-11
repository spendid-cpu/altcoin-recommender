"""추천 퍼널 (2026-10-12): 한 번의 스캔에서 종목이 단계마다 몇 개 남고, 마지막에 왜 추천이 안 됐는지(제외 사유)를 기록한다 — 오늘 화면의 '왜 지금 추천이 안 나오나' 카드용.
표시 전용이고 추천 판단에는 안 쓴다. 최신 스캔과 최근 24시간(스캔 HISTORY_KEEP번) 요약을 meta에 저장한다.

단계: 스캔한 종목 -> 일봉 통과 -> 4시간 -> 1시간 -> 15분 -> 5분 저점(마지막 관문까지 온 종목).
5분 저점까지 온 종목은 아래 순서로 첫 번째로 해당하는 사유 하나로 나눈다:
  low_confirm(일봉 저점 미확인) > runup(최근 급등) > ma_confluence(4시간 이평선과 멂) > score(최소 점수 미달) > wave_gate(BTC 작은 파동 하락) > recent(최근 3일 안 이미 추천) > recommended(이번 스캔 신규 추천) > other"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from src import config, jsonutil, state_store

META_KEY = "scan_funnel_json"
HISTORY_KEEP = 96  # 15분 간격 스캔 24시간치
REASONS = ("low_confirm", "runup", "ma_confluence", "score", "wave_gate", "recent", "recommended", "other")


def classify(candidate, tracked: set, new_markets: set, gate_blocked: bool) -> str:
    """5분 저점까지 통과한 후보 하나의 최종 결과(위 순서의 첫 번째 사유)."""
    if candidate.low_confirm_excluded:
        return "low_confirm"
    if candidate.runup_excluded:
        return "runup"
    if candidate.ma_confluence_excluded:
        return "ma_confluence"
    if (candidate.total_score or 0) < config.MIN_RECOMMEND_SCORE:
        return "score"
    if gate_blocked:
        return "wave_gate"
    if candidate.market in new_markets:
        return "recommended"
    if candidate.market in tracked:
        return "recent"
    return "other"


def build(total: int, scan_stats: dict | None, candidates, tracked: set, new_markets: set, gate_blocked: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    frames = [c.cleared_frames for c in candidates]
    count = lambda name: sum(1 for f in frames if name in f)  # noqa: E731
    low_name = config.LOW_FRAME
    stages = {"scanned": total, "day": len(candidates), "4h": count("4h"), "1h": count("1h"), "15m": count("15m"), low_name: count(low_name)}
    full = [c for c in candidates if c.cleared_frames == [*config.FRAME_ORDER, low_name]]
    pairs = [[c.market, classify(c, tracked, new_markets, gate_blocked)] for c in full]
    reasons = Counter(r for _, r in pairs)
    return {
        "at": now.isoformat(), "stages": stages, "reasons": {k: reasons.get(k, 0) for k in REASONS if reasons.get(k)},
        "full": pairs, "scan": {k: v for k, v in (scan_stats or {}).items() if v},
        "rules": {"low_confirm": config.LOW_CONFIRM_MODE, "gate": config.BTC_WAVE_GATE, "gate_blocked": bool(gate_blocked), "btc_filter": config.BTC_FILTER_MODE},
    }


def _load() -> dict:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else {"latest": None, "history": []}


def record(funnel: dict) -> None:
    data = _load()
    hist = data.get("history", [])
    hist.append({"at": funnel["at"], "stages": funnel["stages"], "full": funnel["full"]})
    state_store.set_meta(META_KEY, jsonutil.dumps({"latest": funnel, "history": hist[-HISTORY_KEEP:]}, ensure_ascii=False, separators=(",", ":")))


def export(now: datetime | None = None) -> dict | None:
    """최신 퍼널 + 최근 24시간 요약(5분 저점까지 온 서로 다른 종목들의 최종 결과, 한 번이라도 추천됐으면 추천으로 센다)."""
    data = _load()
    if not data.get("latest"):
        return None
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    best: dict[str, str] = {}
    scans = 0
    for h in data.get("history", []):
        if datetime.fromisoformat(h["at"]) < cutoff:
            continue
        scans += 1
        for market, reason in h["full"]:
            if best.get(market) != "recommended":  # 한 번이라도 추천됐으면 추천, 아니면 가장 최근 결과
                best[market] = reason
    day = Counter(best.values())
    return {"latest": data["latest"], "day24": {"scans": scans, "markets": len(best), "reasons": {k: day.get(k, 0) for k in REASONS if day.get(k)}}}
