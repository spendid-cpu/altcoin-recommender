"""한 번의 스캔 사이클: BTC 추세 확인 -> (유리하면) 전체 스캔 -> 상태 비교 -> 알림.
scripts/run_scan.py(1회 실행)와 scripts/run_loop.py(계속 실행)가 공유해서 쓴다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from src import config, price_tracker, scan_log, state_store, telegram_client
from src.exchanges import upbit_client
from src.notifier import diff_alerts
from src.scanner import check_btc_trend, scan_all

KST = ZoneInfo("Asia/Seoul")


async def _send_heartbeat(session: aiohttp.ClientSession, text: str) -> None:
    if config.HEARTBEAT_ENABLED and telegram_client.is_configured():
        await telegram_client.send_message(session, text)


async def run_once(session: aiohttp.ClientSession) -> None:
    now = datetime.now(KST).strftime("%H:%M KST")

    favorable = await check_btc_trend(session)
    print(f"[BTC 추세] MA20 위 2일 이상 유지: {favorable}")
    if not favorable:
        print("BTC 추세 불리 -> 알트코인 추천 중지 (설계 문서: BTC 추세 필터)")
        # BTC 추세가 꺾이면 모든 후보가 무의미해지므로, 이전에 잡혀있던 상태를 전부 비워
        # 다음에 추세가 다시 좋아졌을 때 같은 종목이 '신규 후보'로 재알림되게 한다.
        alerts, new_states = diff_alerts([])
        state_store.save_all(new_states)
        # 이미 발굴해 추적 중인 종목은 BTC 추세와 상관없이 끝까지 따라간다
        await _track_prices(session)
        scan_log.record_scan(btc_favorable=False, candidates=0, alerts=0)
        await _send_heartbeat(session, f"[하트비트] {now} 스캔 완료 — BTC 추세 불리, 스캔 건너뜀")
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
            entry = " ★매수타점(15분)" if c.entry_ready else ""
            print(f"  {c.market:12s} [{c.grade}] 점수={c.total_score:5.1f}  통과 프레임=[{frames_str}]{vol}{entry}")

    # candidates가 비어 있어도 diff_alerts/save_all은 실행한다 — 이전에 후보였다가
    # 이번에 탈락한 종목의 저장 상태를 비워둬야 다음 재진입 때 '신규 후보'로 다시 잡힌다.
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

    await _track_prices(session)
    scan_log.record_scan(btc_favorable=True, candidates=len(candidates), alerts=len(alerts))

    await _send_heartbeat(
        session, f"[하트비트] {now} 스캔 완료 — 후보 {len(candidates)}개, 알림 {len(alerts)}건"
    )


async def _track_prices(session: aiohttp.ClientSession) -> None:
    """발굴 후 TRACK_DAYS 이내인 종목들의 현재가를 스냅샷으로 남긴다."""
    markets = price_tracker.active_tracked_markets()
    if not markets:
        return
    prices = await upbit_client.fetch_ticker_prices(session, markets)
    price_tracker.record_snapshots(prices)
    print(f"가격 추적 중인 종목 {len(markets)}개 스냅샷 기록")
