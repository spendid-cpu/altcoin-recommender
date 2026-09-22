"""추천 종목 추적 전용 사이클: 새 후보 발굴(전체 시장 스캔)은 하지 않고, 이미 추천된 종목의 가격만 자주
확인해 익절/손절/만료를 그만큼 촘촘하게 잡는다. pipeline.run_once(전체 스캔, 15분 간격)와 별도로 더 짧은
간격(5분, scripts/run_track.py)으로 돈다.

목적: 15분 간격으로만 보면 익절/손절 문턱을 넘은 뒤에도 다음 확인까지 가격이 계속 움직여, 실제 종료가가
목표치(±5%)에서 벗어난다 (라이브 실측: 손절 평균 -5.34%로 -0.34%p 초과, 익절 평균 +6.18%로 +1.18%p 초과,
최악 사례는 손절 -6.13%). 이 간극을 줄이는 게 목적이고, 승률 자체를 올리려는 게 아니라 정한 손절/익절
폭을 실제로 지키기 위한 리스크 관리다. 그래서 전체 시장 스캔 없이 가격 확인만 가볍게 반복한다.

비트코인 매크로 분석(대시보드 '비트코인 분석'·'비트코인 멀티 차트' 탭)도 여기서 같이 갱신한다 — 알트코인
추천/추적과는 독립이라 5분마다 돌아도 무방하고, MACRO_REFRESH_MINUTES(기본 4분)를 5분 틱보다 짧게 둬서
매번 실제로 새로 계산되게 했다."""

import aiohttp

from src import exits, macro_job, pipeline, report
from src.scanner import check_btc_trend


async def run_once(session: aiohttp.ClientSession) -> None:
    await macro_job.run(session)

    prices = await pipeline.track_prices(session)
    if not prices:
        print("추적 중인 추천이 없습니다.")
        return
    # score_line(종료 알림에 붙는 '점수 변화' 문구)의 BTC 필터 표기가 정확하도록 실제 추세를 확인한다.
    # (여기서는 새 후보를 스캔하지 않으므로 candidates=[]는 항상 빈 목록이다.)
    favorable = await check_btc_trend(session)
    closed = await exits.process_exits(session, prices, [], btc_filter_on=favorable)
    await report.maybe_send_report(session, prices, [], btc_filter_on=favorable)
    print(f"추적 {len(prices)}종목 확인, 종료 {closed}건")
