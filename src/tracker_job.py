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

from src import config, exits, gap_replay, macro_job, manual_tp, migrations, pipeline, report, shadow
from src.scanner import check_btc_trend


async def run_once(session: aiohttp.ClientSession) -> None:
    try:  # 2026-10-12 대조군 삭제: 쌓여 있던 대조군 기록을 한 번만 지운다 (이미 했으면 바로 끝)
        purged = migrations.purge_original_once()
        if purged:
            print(f"[정리] 대조군 기록 삭제: 추천 {purged['entries']}건, 가격 스냅샷 {purged['snapshots']}건")
    except Exception as exc:
        print(f"[정리] 대조군 기록 삭제 실패(다음 사이클에 다시 시도): {exc!r}")
    try:  # 일봉 고점권 안내에 '+3% 이상' 조건이 생기기 전에 낮은 수익으로 기록된 진행 중 안내를 한 번만 지운다
        n_hz = migrations.cleanup_low_gain_high_zone_once(config.HIGH_ZONE_MIN_GAIN_PCT)
        if n_hz:
            print(f"[정리] 수익이 기준 미만인 일봉 고점권 안내 기록 {n_hz}건 삭제")
    except Exception as exc:
        print(f"[정리] 고점권 안내 정리 실패(다음 사이클에 다시 시도): {exc!r}")
    await macro_job.run(session)

    try:  # 2026-09-28 스캔 중단(43시간) 때 잘못 기록된 종료를 한 번 정정한다. 실패해도 추적은 계속한다
        fixed = await gap_replay.repair_recorded_gaps(session, exits.check_exit)
        if fixed:
            print(f"[공백 정정] 종료 기록 {fixed}건 정정 (원래 값은 rec_exit_repairs 표에 보관)")
    except Exception as exc:
        print(f"[공백 정정] 실패(이번 사이클은 건너뜀): {exc!r}")

    # 종료 알림의 BTC 필터 표기, BTC 이탈 정리, 모의 지정가 취소 판단에 모두 쓰이는 실제 추세.
    # (여기서는 새 후보를 스캔하지 않으므로 candidates=[]는 항상 빈 목록이다.)
    favorable = await check_btc_trend(session)
    try:  # 텔레그램 '수동 익절' 버튼은 추적할 추천이 없어도 받아서 처리한다
        await manual_tp.poll_actions(session)
    except Exception as exc:
        print(f"[수동 익절] 버튼 확인 실패(추적은 계속): {exc!r}")
    try:  # 규칙에 막힌 추천의 그림자 진입도 5분마다 가격을 확인한다 (실제 추천과 같은 간격)
        await shadow.update(session)
    except Exception as exc:
        print(f"[그림자 기록] 가격 확인 실패(추적은 계속): {exc!r}")
    prices = await pipeline.track_prices(session)
    if not prices:
        print("추적 중인 추천이 없습니다.")
        await pipeline._update_paper(session, favorable)
        return
    closed = await exits.process_exits(session, prices, [], btc_filter_on=favorable)
    try:  # 일봉이 처음 고점권에 닿은 추천을 한 번 안내한다 (종료된 건 제외하려고 종료 처리 뒤에 본다)
        await manual_tp.check_high_zone(session, prices)
    except Exception as exc:
        print(f"[일봉 고점권] 확인 실패(추적은 계속): {exc!r}")
    await report.maybe_send_report(session, prices, [], btc_filter_on=favorable)
    await pipeline._update_paper(session, favorable)
    print(f"추적 {len(prices)}종목 확인, 종료 {closed}건")
