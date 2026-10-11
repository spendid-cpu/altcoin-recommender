"""BTC 작은 파동이 하락 중이면 알트매크로 추천의 '신규 추천'을 보류한다 (2026-10-11 사용자 결정으로 알림에 바로 적용).

근거와 한계:
- 사전 등록 백테스트(대조군 신호 3,112건, 2024-03~2026-09, 7일 블록 부트스트랩): 'W2 = 작은 파동이 하락 파동이고 반등이
  하락 무마까지는 가지 않은 상태'에서 진입한 신호가 -5%를 먼저 건드린 비율이 +21.8%p 높았고(구간 +5.1~+38.7, 99.17%) 3일 수익률도 낮았다
  (-2.47%p). 앞/뒤 절반 모두 같은 방향. 다만 분기별로는 11개 중 8개만 같은 방향이었고, 백테스트 밖 라이브 구간(2026-09-20~10-07,
  종료 141건, 진입일 15일)에서는 오히려 반대였다(W2 해당 구간 손절 13.6% vs 나머지 45.1%). 그래서 규칙이 아니라 '시험 중인 보류'로 다루고,
  막은 새 추천은 src/shadow.py의 그림자 진입으로 남겨 이후 성과를 실제 추천과 같은 규칙으로 따라간다. 대조군(original)은 이 게이트를 받지 않는다(기준점 고정).
- 하락 '시작'을 예측하지는 않는다. 작은 파동(좌우 4캔들·최소 3%)에서 직전 고점으로부터 3% 이상 내려오고, 마지막으로 마감된 4시간봉 종가가
  그 하락 파동의 0.618 되돌림 아래에 있으면 '하락 파동'으로 본다. 더 일찍 잡는 변형(2%·1.5% 파동, 조정 구간 포함)은 같은 방식으로 시험했지만
  기준을 더 잘 통과하지 못했다(2%는 통과했으나 W2보다 약함, 1.5%·조정 구간 포함은 근거 없음).

모드(저장소 변수 BTC_WAVE_GATE): off = 끔, down = W2(기본), bear = W1(작은 파동 bias가 bear: 저점 갱신 중·하락 지속).
상태를 못 구하면(매크로 분석 없음·낡음) 막지 않는다(fail-open). 이미 추천한 종목의 추적·종료·현황 리포트는 그대로 돈다."""

import json
from datetime import datetime, timezone

from src import config, jsonutil, macro_job, state_store

META_KEY = "btc_wave_gate_json"
NOTICE_KEY = "btc_wave_gate_notice_at"
NOTICE_GAP_HOURS = 4  # 상태가 4시간봉 경계에서 오락가락해도 변경 알림은 이 간격 안에 두 번 보내지 않는다
STALE_MINUTES = 90  # 매크로 분석이 이보다 낡으면 상태를 모른다고 보고 막지 않는다
MODES = ("off", "down", "bear")

MODE_TEXT = {
    "down": "작은 파동이 하락 파동(반등 구간 포함, 하락 무마 제외)",
    "bear": "작은 파동 하락 우세(저점 갱신 중·하락 지속)",
}


def is_blocked(small: dict | None, mode: str) -> bool:
    """작은 파동 상태가 보류 조건인지 (백테스트 W1/W2와 같은 정의)."""
    if not small or mode == "off":
        return False
    if mode == "bear":
        return small.get("bias") == "bear"
    return small.get("direction") == "down" and small.get("bias") != "bull"


def _slim(wave: dict | None) -> dict | None:
    if not wave:
        return None
    keys = ("direction", "code", "bias", "label", "position", "lines", "start", "end")
    return {k: wave[k] for k in keys if k in wave}


def compute(macro: dict | None, mode: str, now: datetime) -> dict:
    """저장된 BTC 매크로 분석으로 게이트 상태를 만든다 (네트워크 호출 없음)."""
    base = {"mode": mode, "at": now.isoformat(), "blocked": False, "known": False, "small": None, "large": None, "basis_time": None, "note": None}
    if mode == "off":
        base["note"] = "게이트 꺼짐"
        return base
    if not macro:
        base["note"] = "BTC 분석 없음 — 보류하지 않음"
        return base
    try:
        age = (now - datetime.fromisoformat(macro["generated_at"])).total_seconds() / 60
    except (KeyError, ValueError):
        age = None
    waves = (macro.get("fib") or {}).get("waves") or {}
    small, large = waves.get("small"), waves.get("large")
    if age is None or age > STALE_MINUTES or not small:
        base["note"] = "BTC 분석이 낡았거나 작은 파동이 없음 — 보류하지 않음"
        return base
    base.update({"known": True, "blocked": is_blocked(small, mode), "small": _slim(small), "large": _slim(large),
                 "basis_time": (macro.get("fib") or {}).get("basis_time")})
    return base


def load() -> dict | None:
    raw = state_store.get_meta(META_KEY)
    return json.loads(raw) if raw else None


def evaluate(now: datetime | None = None) -> tuple[dict, bool]:
    """(현재 게이트 상태, 직전 스캔 대비 막힘 여부가 바뀌었는지)를 돌려주고 저장한다."""
    now = now or datetime.now(timezone.utc)
    prev = load()
    gate = compute(macro_job.load_cached(), config.BTC_WAVE_GATE, now)
    if prev and prev.get("blocked") == gate["blocked"]:
        gate["since"] = prev.get("since") or prev.get("at")
    else:
        gate["since"] = now.isoformat()
    state_store.set_meta(META_KEY, jsonutil.dumps(gate, ensure_ascii=False, separators=(",", ":")))
    changed = bool(prev) and bool(prev.get("known")) == gate["known"] and bool(prev.get("blocked")) != gate["blocked"]
    return gate, changed


def transition_message(gate: dict) -> str:
    small = gate.get("small") or {}
    if gate["blocked"]:
        return (f"🚦 BTC 작은 파동 {small.get('label', '하락')} — 알트매크로 신규 추천을 잠시 보류해요\n"
                f"기준: {MODE_TEXT.get(gate['mode'], gate['mode'])}. 이미 추천한 종목의 추적·종료 알림은 그대로예요. (시험 중인 보류 규칙)")
    return (f"🟢 BTC 작은 파동이 하락 구간을 벗어났어요({small.get('label', '-')}) — 알트매크로 신규 추천을 다시 시작해요")


def notice_due(now: datetime | None = None) -> bool:
    """상태 변경 알림을 지금 보내도 되는지(스팸 방지). 보내도 되면 시각을 기록한다."""
    now = now or datetime.now(timezone.utc)
    last = state_store.get_meta(NOTICE_KEY)
    if last and (now - datetime.fromisoformat(last)).total_seconds() < NOTICE_GAP_HOURS * 3600:
        return False
    state_store.set_meta(NOTICE_KEY, now.isoformat())
    return True


def export() -> dict | None:
    """대시보드용: 현재 게이트 상태. (막힌 추천의 사후 성과는 src/shadow.py가 따로 기록한다.)"""
    return load()
