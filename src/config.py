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
EXIT_STOP_LOSS_PCT = _env_float("EXIT_STOP_LOSS_PCT", None)  # 추천가 대비 -X% 도달 시 종료 (기본 끔: -3~-5%는 평균을 낮췄다)
EXIT_TRAIL_ARM_PCT = _env_float("EXIT_TRAIL_ARM_PCT", None)  # 최고 수익이 +X%를 넘으면 되돌림 감시 시작
EXIT_TRAIL_DD_PCT = _env_float("EXIT_TRAIL_DD_PCT", None)  # 고점 대비 -Y% 되돌리면 종료 (ARM과 함께 지정)

# 비트코인 매크로 분석(피보나치 지지·저항 + 스토캐스틱 RSI 현황). 대시보드용 분석은 이 간격(분)마다 새로 계산한다
# (스캔이 15분마다 돌아도 분석은 시간당 한 번 — 4시간봉/1시간봉이 그 이상 자주 바뀌지 않는다).
# 발송 시각 정각에 딱 맞춰 돌지는 않아서 55분으로 잡아 실제 갱신이 시간당 한 번이 되게 한다.
MACRO_REFRESH_MINUTES = _env_float("MACRO_REFRESH_MINUTES", 55) or 55
# 매일 이 시각(한국시간, 시) 이후 첫 스캔에서 비트코인 매크로 브리핑을 텔레그램으로 보낸다 (기본 8시).
# 끄려면 값을 'off'로 둔다 (GitHub Actions 저장소 변수 MACRO_BRIEFING_HOUR). 숫자가 아닌 값은 기본값으로 본다.
_raw_hour = (os.environ.get("MACRO_BRIEFING_HOUR") or "").strip().lower()
MACRO_BRIEFING_HOUR: int | None = None if _raw_hour in ("off", "none", "false") else (
    int(_raw_hour) if _raw_hour.isdigit() and 0 <= int(_raw_hour) <= 23 else 8
)

# 프레임 축(하드 게이트) 가중치: 일봉 > 4시간 > 1시간
FRAME_WEIGHTS = {"day": 3, "4h": 2, "1h": 1}
FRAME_ORDER = ("day", "4h", "1h")  # 게이트를 타는 순서 (상위 -> 하위)

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
FRAME_HOURS = {"day": 24, "4h": 4, "1h": 1}

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

UPBIT_MARKET = "KRW-BTC"
BINANCE_SYMBOL = "BTCUSDT"

# 업비트 시세(quotation) API 레이트리밋 대응 — 보수적으로 초당 5건
UPBIT_RATE_LIMIT_PER_SEC = 5
UPBIT_CONCURRENCY = 5
