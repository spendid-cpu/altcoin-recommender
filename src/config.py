"""점수화 로직 파라미터. 설계 문서(점수화 로직 섹션) 기준 초기값 — 백테스트로 조정 예정."""

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 스캔이 실제로 도는지 확인용 하트비트 메시지. 매 사이클(15분)마다 오니 평소엔 꺼두고,
# 배포 직후 스케줄러가 잘 도는지 확인할 때만 켠다 (GitHub Actions 저장소 변수 HEARTBEAT=true).
HEARTBEAT_ENABLED = os.environ.get("HEARTBEAT", "false").lower() == "true"

# 추천 후 추적 중인 종목의 현황(추천가 대비 수익률, 점수 변화 등)을 텔레그램으로 보내는 간격(시간).
# 0이면 끈다. 스캔이 돌 때만 확인하므로 실제 발송은 이 간격이 지난 뒤 첫 스캔에서 나간다.
# GitHub Actions 저장소 변수 REPORT_INTERVAL_HOURS로 바꿀 수 있다.
try:
    REPORT_INTERVAL_HOURS = float(os.environ.get("REPORT_INTERVAL_HOURS") or "1")
except ValueError:
    REPORT_INTERVAL_HOURS = 1.0

def _env_float(name: str, default: float | None) -> float | None:
    """환경변수를 실수로 읽는다. 비어 있거나 0/off/none이면 '사용 안 함'(None)."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw == "":
        return default
    if raw in ("0", "off", "none", "false"):
        return None
    try:
        return float(raw)
    except ValueError:
        return default


# 추천으로 알리고 추적을 시작하는 최소 점수. 0이면 일봉 게이트를 통과한 후보는 전부 추천한다.
# 마감 캔들 기준으로 바로잡은 백테스트(scripts/run_exit_backtest.py)에서 기준을 올려도 성과가 좋아지지는 않았고
# (점수 3+ 평균 +2.00%, 13+ +1.62%, 16+ +1.51% / 추천 후 3일) 알림 수만 줄었다. 알림이 너무 많으면 이 값을 올린다
# (GitHub Actions 저장소 변수 MIN_RECOMMEND_SCORE).
MIN_RECOMMEND_SCORE = _env_float("MIN_RECOMMEND_SCORE", 0) or 0

# 추천 종료 규칙 (종료 시 텔레그램으로 최종 결과를 알리고 추적을 끝낸다). 어느 것이든 저장소 변수로 바꾼다.
# 백테스트(점수 13+ 추천 119건, 20일치 한 가지 장세): 익절 +5% 평균 +2.15% 승률 65.5% 평균보유 50시간,
# 3일 보유 +1.62% 승률 55.5%. 다만 3일 보유와의 차이는 통계적으로 구분되지 않는 수준이다.
# 점수 하락/조건 이탈로 종료하는 규칙은 평균 +0.06~+0.3%로 오히려 나빠서 넣지 않았다(신호가 약해져도 가격은 계속 올랐다).
EXIT_TAKE_PROFIT_PCT = _env_float("EXIT_TAKE_PROFIT_PCT", 5.0)  # 추천가 대비 +X% 도달 시 종료 (0/off면 끔)
# 손절은 프로그램이 쓸모 있는지 재기 위해 익절과 같은 폭(-5%)으로 둔다: 익절 수 : 손절 수 비율이 곧 성과 지표가 된다(대시보드 '성과' 탭).
# 다만 이전 백테스트(20일)에서는 손절을 -3~-5%로 걸면 평균이 낮아졌으니, 매매 규칙으로 최적이라는 뜻은 아니다. 끄려면 'off'.
EXIT_STOP_LOSS_PCT = _env_float("EXIT_STOP_LOSS_PCT", 5.0)  # 추천가 대비 -X% 도달 시 종료 (0/off면 끔)
EXIT_TRAIL_ARM_PCT = _env_float("EXIT_TRAIL_ARM_PCT", None)  # 최고 수익이 +X%를 넘으면 되돌림 감시 시작
EXIT_TRAIL_DD_PCT = _env_float("EXIT_TRAIL_DD_PCT", None)  # 고점 대비 -Y% 되돌리면 종료 (ARM과 함께 지정)

# 비트코인 매크로 분석(지지·저항 + 스토캐스틱 RSI 현황). 대시보드용 분석은 이 간격(분)마다 새로 계산한다.
# 스캔이 15분마다 도니 10분으로 두면 매 스캔마다 갱신된다 (현재가와 15분 사이의 변화를 놓치지 않으려고 시간당 → 15분으로 줄였다).
# 값을 키우면 그만큼 덜 자주 갱신한다 (저장소 변수 MACRO_REFRESH_MINUTES).
MACRO_REFRESH_MINUTES = _env_float("MACRO_REFRESH_MINUTES", 10) or 10
# 매일 이 시각(한국시간, 시) 이후 첫 스캔에서 비트코인 매크로 브리핑을 텔레그램으로 보낸다 (기본 8시).
# 끄려면 값을 'off'로 둔다 (GitHub Actions 저장소 변수 MACRO_BRIEFING_HOUR). 숫자가 아닌 값은 기본값으로 본다.
_raw_hour = (os.environ.get("MACRO_BRIEFING_HOUR") or "").strip().lower()
MACRO_BRIEFING_HOUR: int | None = None if _raw_hour in ("off", "none", "false") else (
    int(_raw_hour) if _raw_hour.isdigit() and 0 <= int(_raw_hour) <= 23 else 8
)

# BTC 추세 필터: 바이낸스 BTCUSDT 일봉 종가가 MA20 위에서 이 일수만큼 연속 마감돼야 알트코인 추천을 켠다.
# 처음에는 2일이었지만 '종가가 MA20 위면 추천'으로 완화했다 (GitHub Actions 저장소 변수 BTC_HOLD_DAYS로 되돌릴 수 있다).
try:
    BTC_HOLD_DAYS = max(1, int(os.environ.get("BTC_HOLD_DAYS") or "1"))
except ValueError:
    BTC_HOLD_DAYS = 1

# 프레임 축(하드 게이트) 가중치: 일봉 > 4시간 > 1시간
# 15분봉은 마지막 관문(LOW_FRAME)이라 가장 가벼운 0.5점. 일봉 > 4시간 > 1시간 > 15분 순으로 무게를 둔다.
FRAME_WEIGHTS = {"day": 3, "4h": 2, "1h": 1, "15m": 0.5}
FRAME_ORDER = ("day", "4h", "1h")  # 게이트를 타는 순서 (상위 -> 하위)

# 추천 시점: 일봉/4시간/1시간 게이트를 모두 통과한 종목이 15분봉 저점에 있을 때만 추천한다.
# 15분 저점 = 단기 %K가 저점권(OVERSOLD_THRESHOLD 이하)에 있거나, 최근 LOW_FRAME_LOOKBACK_HOURS 안에 저점권 최초 도달/
# 저점권에서의 골든크로스가 있었던 경우. 예전처럼 일봉 게이트만 통과해도 추천하려면 저장소 변수
# RECOMMEND_ONLY_AT_15M_LOW=false.
LOW_FRAME = "15m"
LOW_FRAME_LOOKBACK_HOURS = 0.75  # 15분봉 3개
RECOMMEND_ONLY_AT_15M_LOW = os.environ.get("RECOMMEND_ONLY_AT_15M_LOW", "true").strip().lower() != "false"

# 주기 축(프레임별 가산점) 가중치.
# 단기는 트리거 감지 역할이라 별도 가중치가 아니라 TRIGGER_BONUS로 취급.
# 중기(mid)는 0으로 비활성화 — 일봉 중기 전환은 미래 정보를 걷어낸 백테스트에서도 일관되게 역효과였다
# (True 34.3%/-0.98% vs False 72.9%/+3.37%, 유동성 필터 적용 481건 / 추천 후 3일).
# 장기(long)와 골든크로스 보너스는 마감 시각 기준으로 바로잡은 백테스트에서 뚜렷한 효과가 확인되지 않았다.
# 값은 그대로 두지만 근거가 약하다 (이전 백테스트의 효과는 미래 정보 누출이었다).
PERIOD_BONUS_WEIGHTS = {"long": 3, "mid": 0}

# 트리거(단기 스토)가 "최초 도달"이 아니라 "골든크로스"일 때 추가 점수
# (바닥 찍고 상방 전환하는 시점에 더 높은 점수를 준다는 설계 원칙)
GOLDEN_CROSS_BONUS = 2

# 게이트 임계값: 단기 스토 %K가 이 값 이하면 "유의미한 구간"으로 본다
OVERSOLD_THRESHOLD = 20

# 프레임별 캔들 1개의 시간(시간 단위) — 유효기간을 캔들 개수로 환산할 때 사용
FRAME_HOURS = {"day": 24, "4h": 4, "1h": 1, "15m": 0.25}

# 조건 유효기간: 게이트가 통과된 후 몇 시간까지 '아직 유효한 상태'로 볼지.
# 백테스트(scripts/run_backtest.py) 결과 24시간이 3일 뒤 수익률/승률 기준으로 가장 좋았음
# (6시간은 표본이 너무 적어 신뢰하기 어려움, 72/168시간은 오래된 게이트가 섞여 희석됨).
VALIDITY_WINDOW_HOURS = 24

# 거래량 조건 (1시간봉 기준). 요인 분석 결과 배수 임계값(1.5~4)을 다 시도해봐도
# 승률/수익률에 뚜렷한 차이가 없어(오히려 높은 배수일수록 약간 나빠짐) 점수 반영은 0으로 비활성화.
# 감지 자체(has_volume_spike, 스캐너의 volume_bonus 표시)는 그대로 두고 신호로만 참고.
VOLUME_LOOKBACK = 20  # 최근 N개 캔들 평균과 비교
VOLUME_MULTIPLIER = 2  # 평균 대비 X배 이상이면 "유의미한 하락거래량" (표시용, 점수에는 미반영)
VOLUME_BONUS = 0

# 거래대금(유동성) 가산점은 넣지 않는다 — 요인 분석 결과 오히려 거래대금이 가장 낮은 5분위
# (일봉 거래대금 13M~457M원)가 승률 67.0%/+4.29%(3일)로 가장 좋았고, 거래대금이 클수록
# 딱히 더 나아지지 않았음(가장 큰 5분위 56.5%/+0.98%). 저유동성 종목이 백테스트 상 유리해
# 보여도 실제 체결 슬리피지는 반영 안 된 수치라, 가산점 대신 아래 최소 유동성 '필터'로만 사용.
MIN_DAILY_TRADE_VALUE_KRW = 300_000_000  # 일봉 거래대금 3억원 미만은 체결이 어려워 후보에서 제외

# 점수 등급 표시(A/B/없음). 표시용 구분일 뿐 성과를 보장하지 않는다: 마감 시각 기준으로 바로잡은 백테스트에서
# 점수가 높을수록 성과가 좋아지는 관계는 확인되지 않았다 (추천 후 3일 평균: 3+ +2.00%, 13+ +1.62%, 16+ +1.51%).
SCORE_GRADE_THRESHOLDS = {"A": 16, "B": 13}  # 이 값 이상이면 해당 등급, 미만이면 등급 없음("-")

# 사이클 전략(사용자 판단 방식)을 기존 전략과 나란히 돌려 성과를 비교한다. 끄려면 저장소 변수 CYCLE_ENABLED=false.
# 규칙은 cycle_signals.py 첫머리에 있고, 65일 백테스트에서 확정한 값 그대로다 (임의로 바꾸지 않는다 — 바꾸면 비교가 무의미해진다).
CYCLE_ENABLED = os.environ.get("CYCLE_ENABLED", "true").strip().lower() != "false"
CYCLE_STOP_PCT = 5.0  # 절반 매도 전 손절 (전량)
CYCLE_TRAIL_PCT = 5.0  # 절반 매도 뒤 나머지 절반의 고점 대비 트레일링 폭
CYCLE_H4_CANDLES = 450  # 4시간 이력 (백테스트와 같은 길이: 65일 + 여유)

# 최초 알고리즘(첫 버전)을 병행 운영해 수정판·사이클 전략과 성과를 비교한다. 끄려면 저장소 변수 ORIGINAL_ENABLED=false.
# 규칙: 일봉 게이트만 통과하면 추천 + 최초 스토RSI 설정 + BTC 일봉 종가 MA20 위 2일 유지. 조용히(텔레그램 없이) 기록만 한다.
# 종료 규칙은 최초에는 없었지만 성과를 재려고 수정판과 같은 익절 +5% / 손절 -5% / 3일을 쓴다. 마감된 캔들만 쓰는 것도 수정판과 같다
# (최초 버전의 진행 중 캔들 사용은 신호가 스캔마다 바뀌는 오류였다).
ORIGINAL_ENABLED = os.environ.get("ORIGINAL_ENABLED", "true").strip().lower() != "false"
ORIGINAL_BTC_HOLD_DAYS = 2

UPBIT_MARKET = "KRW-BTC"
BINANCE_SYMBOL = "BTCUSDT"

# 업비트 시세(quotation) API 레이트리밋 대응 — 보수적으로 초당 5건
UPBIT_RATE_LIMIT_PER_SEC = 5
UPBIT_CONCURRENCY = 5
