"""이전 스캔 상태와 비교해 '상태가 바뀐' 종목만 알림 대상으로 골라낸다 (중복 알림 방지)."""

from dataclasses import dataclass

from src import state_store
from src.scoring import CandidateResult

_EMPTY_STATE = {"cleared_frames": [], "entry_ready": False, "grade": "-", "current_price": 0.0, "score": None}


@dataclass
class Alert:
    market: str
    kind: str  # "new_candidate" | "frame_advance" | "entry_ready"
    message: str


def _fmt_price(price: float) -> str:
    if price >= 100:
        return f"{price:,.0f}원"
    if price >= 1:
        return f"{price:,.2f}원"
    return f"{price:.4f}원"


def _snapshot(candidate: CandidateResult) -> dict:
    return {
        "cleared_frames": candidate.cleared_frames,
        "entry_ready": candidate.entry_ready,
        "grade": candidate.grade,
        "current_price": candidate.current_price,
        "score": round(candidate.total_score, 1),
    }


def diff_alerts(candidates: list[CandidateResult]) -> tuple[list[Alert], dict[str, dict]]:
    """이번 스캔 결과 vs 저장된 이전 상태를 비교해 (알림 목록, 다음에 저장할 전체 상태)를 반환한다.
    day 게이트조차 못 넘은 종목은 candidates에 없으므로, 이전에 후보였다가 이번에 탈락한 종목은
    빈 상태로 명시적으로 되돌려 다음 재진입 시 '신규 후보'로 다시 잡히게 한다.
    """
    previous = state_store.load_all()
    current_by_market = {c.market: _snapshot(c) for c in candidates}

    alerts: list[Alert] = []
    new_states: dict[str, dict] = {}

    all_markets = set(previous) | set(current_by_market)
    for market in all_markets:
        prev = previous.get(market, _EMPTY_STATE)
        cur = current_by_market.get(market, _EMPTY_STATE)
        new_states[market] = cur

        price_str = _fmt_price(cur["current_price"])

        if not prev["cleared_frames"] and cur["cleared_frames"]:
            alerts.append(Alert(market, "new_candidate",
                                 f"[{cur['grade']}] {market} 신규 후보 진입 @ {price_str} "
                                 f"(프레임: {', '.join(cur['cleared_frames'])})"))
        elif len(cur["cleared_frames"]) > len(prev["cleared_frames"]):
            alerts.append(Alert(market, "frame_advance",
                                 f"[{cur['grade']}] {market} 프레임 확장 @ {price_str}: {', '.join(cur['cleared_frames'])}"))

        if cur["entry_ready"] and not prev.get("entry_ready", False):
            alerts.append(Alert(market, "entry_ready", f"[{cur['grade']}] {market} 15분봉 매수 타점 발생 @ {price_str}"))

    return alerts, new_states
