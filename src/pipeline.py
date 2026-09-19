"""한 번의 스캔 사이클: BTC 추세 확인 -> (유리하면) 전체 스캔 -> 상태 비교 -> 알림.
scripts/run_scan.py(1회 실행)와 scripts/run_loop.py(계속 실행)가 공유해서 쓴다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from src import config, exits, macro_job, price_tracker, report, scan_log, state_store, telegram_client
from src.exchanges import upbit_client
from src.notifier import diff_alerts
from src.scanner import check_btc_trend, scan_all

KST = ZoneInfo("Asia/Seoul")


async def _send_heartbeat(session: aiohttp.ClientSession, text: str) -> None:
    if config.HEARTBEAT_ENABLED and telegram_client.is_configured():
        await telegram_client.send_message(session, text)


async def run_once(session: aiohttp.ClientSession) -> None:
    now = datetime.now(KST).strftime("%H:%M KST")

    # 비트코인 매크로 분석 갱신(시간당 1회)과 아침 브리핑. 알트코인 추천/추적과는 독립이라 BTC 추세와 상관없이 돈다.
    await macro_job.run(session)

    favorable = await check_btc_trend(session)
    print(f"[BTC 추세] 일봉 종가 MA20 위 {config.BTC_HOLD_DAYS}일 이상 유지: {favorable}")
    if not favorable:
        print("BTC 추세 불리 -> 알트코인 추천 중지 (설계 문서: BTC 추세 필터)")
        # BTC 추세가 꺾이면 현재 후보는 모두 없는 것으로 본다. 추적 기간이 끝난 종목은 상태를 비워 다음에
        # 추세가 돌아왔을 때 새 추천으로 알리고, 추적 중인 종목은 이미 알린 단계를 유지해 중복 알림을 막는다.
        alerts, new_states = diff_alerts([])
        state_store.save_all(new_states)
        # 이미 발굴해 추적 중인 종목은 BTC 추세와 상관없이 끝까지 따라가고 현황도 계속 보낸다
        prices = await _track_prices(session)
        await exits.process_exits(session, prices, [], btc_filter_on=False)
        await report.maybe_send_report(session, prices, [], btc_filter_on=False)
        scan_log.record_scan(btc_favorable=False, candidates=0, alerts=0)
        await _send_heartbeat(session, f"💓 {now} 스캔 완료 — BTC 추세 불리, 스캔 건너뜀")
        return

    print("업비트 KRW 마켓 스캔 중...")
    candidates = await scan_all(session)

    if not candidates:
        print("일봉 게이트를 통과한 종목이 없습니다.")
    else:
        print(f"\n후보 {len(candidates)}개 (점수 높은 순)\n")
        for c in candidates[:20]:
            frames_str = " > ".join(c.cleared_frames) if c.cleared_frames else "-"
            vol = " +거래량" if c.volume_bonus else ""
            entry = (" ★15분저점" if c.low_15m else "") + (" ★매수타점(15분)" if c.entry_ready else "")
            entry += " →추천" if c.recommendable else ""
            print(f"  {c.market:12s} [{c.grade}] 점수={c.total_score:5.1f}  통과 프레임=[{frames_str}]{vol}{entry}")

    # candidates가 비어 있어도 diff_alerts/save_all은 실행한다 — 이번에 후보에서 빠진 종목의 상태를
    # 정리해야 하기 때문이다 (추적이 끝난 종목은 초기화, 추적 중인 종목은 알린 단계 유지).
    alerts, new_states = diff_alerts(candidates)
    state_store.save_all(new_states)

    print(f"\n알림 대상 {len(alerts)}건 (상태가 바뀐 종목만)")
    if not telegram_client.is_configured():
        print("(텔레그램 미설정 -> 콘솔에만 출력, .env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID를 넣으면 전송됨)")
    candidate_by_market = {c.market: c for c in candidates}
    for a in alerts:
        print(f"  [{a.kind}] {a.message}")
        if telegram_client.is_configured():
            await telegram_client.send_message(session, a.message)
        if a.kind == "new_candidate":
            # 발굴 시점의 가격/등급/점수를 '진입 기록'으로 남긴다 — 이후 계속 추적해 실제 수익률을 검증하는 원본 데이터.
            c = candidate_by_market[a.market]
            price_tracker.record_entry(a.market, c.current_price, c.grade, round(c.total_score, 1))

    prices = await _track_prices(session)
    await exits.process_exits(session, prices, candidates, btc_filter_on=True)
    await report.maybe_send_report(session, prices, candidates, btc_filter_on=True)
    scan_log.record_scan(btc_favorable=True, candidates=len(candidates), alerts=len(alerts))

    await _send_heartbeat(
        session, f"💓 {now} 스캔 완료 — 후보 {len(candidates)}개, 알림 {len(alerts)}건"
    )


async def _track_prices(session: aiohttp.ClientSession) -> dict[str, float]:
    """발굴 후 TRACK_DAYS 이내인 종목들의 현재가를 스냅샷으로 남기고, 그 가격표를 돌려준다."""
    markets = price_tracker.active_tracked_markets()
    if not markets:
        return {}
    prices = await upbit_client.fetch_ticker_prices(session, markets)
    price_tracker.record_snapshots(prices)
    print(f"가격 추적 중인 종목 {len(markets)}개 스냅샷 기록")
    return prices
