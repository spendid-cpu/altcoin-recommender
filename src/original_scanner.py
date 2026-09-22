"""대조군(최초 커밋의 첫 버전 규칙) 스캔. 개선판과 성과를 나란히 비교하려고 첫 커밋의 추천 규칙을 그대로 병행 운영한다.
절대 규칙을 바꾸지 않는다 — 비교의 기준점이다.

  - 추천: 일봉 게이트만 통과하면 추천 (일봉 단기 스토RSI가 24시간 안에 저점권 최초 도달 또는 저점권 골든크로스)
  - 스토RSI 설정: 최초 버전의 것(stoch_rsi.ORIGINAL_PERIOD_SETS, 트레이딩뷰와 순서가 반대) 그대로
  - BTC 필터: 일봉 종가가 MA20 위에서 2일 이상 (config.ORIGINAL_BTC_HOLD_DAYS)
  - 종목당 3일에 한 번만 추천, 일봉 거래대금 3억원 미만은 제외 (모두 최초부터 있던 규칙)
개선판 스캔이 이미 받아 둔 일봉 캔들을 재사용하므로 추가 API 호출이 없다. 텔레그램으로는 알리지 않고 기록만 한다."""

from src import config
from src.indicators.stoch_rsi import ORIGINAL_PERIOD_SETS
from src.scoring import CandidateResult, FrameResult, score_frame

RULE_TEXT = "일봉 조건을 통과하면 추천 (최초 알고리즘: 최초 스토RSI 설정, BTC 종가 MA20 위 2일 유지)"


def scan_original(cache: dict, markets: list[str], exclude: set[str]) -> list[tuple[str, FrameResult]]:
    """(종목, 일봉 결과) 목록. exclude(최근에 이미 추천한 종목)는 뺀다. cache는 scanner.scan_all이 채운 (종목, 프레임) -> 캔들."""
    out = []
    for market in markets:
        day = cache.get((market, "day"))
        if day is None or day.empty:
            continue
        if day["value"].iloc[-1] < config.MIN_DAILY_TRADE_VALUE_KRW:
            continue
        if market in exclude:
            continue
        result = score_frame("day", day["close"], period_sets=ORIGINAL_PERIOD_SETS)
        if result.passed_gate:
            out.append((market, result))
    out.sort(key=lambda mr: mr[1].score, reverse=True)
    return out


def breakdown(result: FrameResult) -> dict:
    """대시보드 '추천 근거'에 쓰는 기록 (개선판과 같은 형식)."""
    detail = CandidateResult(market="", total_score=result.score, frames=[result]).breakdown()
    detail["rule"] = RULE_TEXT
    return detail
