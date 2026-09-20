"""이전 스캔 상태와 비교해 '상태가 바뀐' 종목만 알림 대상으로 골라낸다 (중복 알림 방지).

상태에는 지금 후보인지(cleared_frames, entry_ready ...)와 별개로 '이미 알린 단계'(alerted_frames,
alerted_entry)를 따로 둔다. 추천 후 추적 기간(price_tracker.TRACK_DAYS) 안에 있는 종목은 후보에서 잠깐
빠졌다가 다시 들어와도 이미 알린 단계를 기억하고 있어서 같은 알림이 반복되지 않는다.
"""

from dataclasses import dataclass

from src import config, price_tracker, state_store
from src.formatting import fmt_price, frames_text, grade_badge
from src.scoring import CandidateResult

_EMPTY_STATE = {
    "cleared_frames": [], "entry_ready": False, "grade": "-", "current_price": 0.0, "score": None,
    "recommendable": False, "low_15m": False,
    "alerted_frames": [], "alerted_entry": False,
}


@dataclass
class Alert:
    market: str
    kind: str  # "new_candidate" | "frame_advance" | "entry_ready"
    message: str


def _message(title: str, market: str, cur: dict) -> str:
    score = f"{cur['score']:.1f}" if cur.get("score") is not None else "—"
    return (
        f"{title} · {grade_badge(cur['grade'])}\n"
        f"{market} @ {fmt_price(cur['current_price'])}\n"
        f"점수 {score} · 통과: {frames_text(cur['cleared_frames'])}"
    )


def _snapshot(candidate: CandidateResult) -> dict:
    return {
        "cleared_frames": candidate.cleared_frames,
        "entry_ready": candidate.entry_ready,
        "grade": candidate.grade,
        "current_price": candidate.current_price,
        "score": round(candidate.total_score, 1),
        "recommendable": candidate.recommendable,
        "low_15m": candidate.low_15m,
    }


def _normalize(state: dict | None) -> dict:
    """옛 형식(alerted_* 없음)의 저장 상태는 '지금 상태 = 이미 알린 상태'로 간주한다."""
    if state is None:
        return dict(_EMPTY_STATE)
    normalized = {**_EMPTY_STATE, **state}
    if "alerted_frames" not in state:
        normalized["alerted_frames"] = list(state.get("cleared_frames", []))
    if "alerted_entry" not in state:
        normalized["alerted_entry"] = bool(state.get("entry_ready", False))
    return normalized


def diff_alerts(candidates: list[CandidateResult]) -> tuple[list[Alert], dict[str, dict]]:
    """이번 스캔 결과 vs 저장된 이전 상태를 비교해 (알림 목록, 다음에 저장할 전체 상태)를 반환한다.

    - 추천 대상(CandidateResult.recommendable: 기본은 일봉/4시간/1시간 통과 + 15분 저점)이 아닌 후보는 대시보드에만
      보이고 알림도 기준선도 만들지 않는다. 15분 저점에 오면 그때 신규 추천으로 알린다.
    - 신규 추천: 아직 아무것도 알리지 않은 종목이고, 최근 추천한 적이 없고, 점수가 MIN_RECOMMEND_SCORE 이상일 때만 알린다.
    - 프레임 확장 / 15분 타점: 이미 알린 단계보다 더 진행됐을 때만 알린다 (후보에서 빠졌다 돌아와도 반복 안 함).
    - 후보에서 빠졌을 때: 최근 추천한 종목이면 알린 단계를 유지하고, 아니면 초기화해서 다음 재진입을 새 추천으로 본다.
    """
    previous = state_store.load_all()
    tracked = set(price_tracker.recent_recommendation_markets())
    current_by_market = {c.market: _snapshot(c) for c in candidates}

    alerts: list[Alert] = []
    new_states: dict[str, dict] = {}

    for market in set(previous) | set(current_by_market):
        prev = _normalize(previous.get(market))
        cur = current_by_market.get(market)

        if cur is None:
            state = dict(_EMPTY_STATE)
            if market in tracked:
                state["alerted_frames"] = prev["alerted_frames"]
                state["alerted_entry"] = prev["alerted_entry"]
            new_states[market] = state
            continue

        alerted_frames = prev["alerted_frames"]
        alerted_entry = prev["alerted_entry"]

        if not cur["recommendable"]:
            # 추천 시점(15분 저점)을 기다리는 후보. 최근 추천한 종목이면 이미 알린 단계를 기억해 둔다
            keep = market in tracked
            new_states[market] = {
                **cur,
                "alerted_frames": alerted_frames if keep else [],
                "alerted_entry": alerted_entry if keep else False,
            }
            continue

        if not alerted_frames:
            if (cur["score"] or 0) < config.MIN_RECOMMEND_SCORE:
                # 추천 기준에 못 미치는 후보는 대시보드에만 보이고, 알림 기준선도 만들지 않는다
                # (나중에 점수가 기준을 넘으면 그때 신규 추천으로 알린다)
                new_states[market] = {**cur, "alerted_frames": [], "alerted_entry": False}
                continue
            if market not in tracked:
                alerts.append(Alert(market, "new_candidate", _message("🆕 [수정판] 신규 추천", market, cur)))
            alerted_frames = cur["cleared_frames"]  # 알림을 생략해도 기준선은 갱신한다
        elif len(cur["cleared_frames"]) > len(alerted_frames):
            alerts.append(Alert(market, "frame_advance", _message("📈 [수정판] 단계 상승", market, cur)))
            alerted_frames = cur["cleared_frames"]

        if cur["entry_ready"] and not alerted_entry:
            alerts.append(Alert(market, "entry_ready", _message("🎯 [수정판] 15분 매수 타점", market, cur)))
            alerted_entry = True

        new_states[market] = {**cur, "alerted_frames": alerted_frames, "alerted_entry": alerted_entry}

    return alerts, new_states
