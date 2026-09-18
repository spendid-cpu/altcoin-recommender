"""점수화 로직 파라미터. 설계 문서(점수화 로직 섹션) 기준 초기값 — 백테스트로 조정 예정."""

import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 스캔이 실제로 도는지 확인용 하트비트 메시지. 매 사이클(15분)마다 오니 평소엔 꺼두고,
# 배포 직후 스케줄러가 잘 도는지 확인할 때만 켠다 (GitHub Actions 저장소 변수 HEARTBEAT=true).
HEARTBEAT_ENABLED = os.environ.get("HEARTBEAT", "false").lower() == "true"

# 프레임 축(하드 게이트) 가중치: 일봉 > 4시간 > 1시간
FRAME_WEIGHTS = {"day": 3, "4h": 2, "1h": 1}
FRAME_ORDER = ("day", "4h", "1h")  # 게이트를 타는 순서 (상위 -> 하위)

# 주기 축(프레임별 가산점) 가중치: 장기만 유효.
# 단기는 트리거 감지 역할이라 별도 가중치가 아니라 TRIGGER_BONUS로 취급.
# 중기(mid)는 요인 분석(scripts/run_factor_analysis.py) 결과 뒷받침되지 않아 0으로 비활성화 —
# 특히 일봉 중기 전환은 오히려 역효과였음(True 36.2%/-1.23% vs False 67.6%/+2.73%, 552건 기준).
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

# 점수 등급: 요인 분석(scripts/run_factor_analysis.py)의 점수 4분위 백테스트 결과 기준.
# 13점 미만은 승률 46%대(사실상 동전던지기)라 등급 밖, 13점부터 승률/수익률이 뚜렷하게 갈림.
SCORE_GRADE_THRESHOLDS = {"A": 16, "B": 13}  # 이 값 이상이면 해당 등급, 미만이면 등급 없음("-")

UPBIT_MARKET = "KRW-BTC"
BINANCE_SYMBOL = "BTCUSDT"

# 업비트 시세(quotation) API 레이트리밋 대응 — 보수적으로 초당 5건
UPBIT_RATE_LIMIT_PER_SEC = 5
UPBIT_CONCURRENCY = 5
