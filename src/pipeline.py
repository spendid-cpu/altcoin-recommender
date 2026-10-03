"""한 번의 스캔 사이클: BTC 추세 확인 -> (유리하면) 전체 스캔 -> 상태 비교 -> 알림.
scripts/run_scan.py(1회 실행)와 scripts/run_loop.py(계속 실행)가 공유해서 쓴다."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import config, cycle, exits, macro_job, market_cap, orderbook, original_alerts, original_scanner, paper_limit, price_tracker, report, scan_log, state_store, telegram_client
from src.scoring import find_support_lows
from src.exchanges import upbit_client
from src.notifier import diff_alerts
from src.scanner import btc_days_above, check_btc_trend, scan_all

KST = ZoneInfo("Asia/Seoul")


async def _send_heartbeat(session: aiohttp.ClientSession, text: str) -> None:
    if config.HEARTBEAT_ENABLED and telegram_client.is_configured():
        try:
            await telegram_client.send_message(session, text)
        except Exception as exc:
            print(f"[하트비트] 전송 실패(무시): {exc!r}")


async def run_once(session: aiohttp.ClientSession) -> None:
    now = datetime.now(KST).strftime("%H:%M KST")

    # 비트코인 매크로 분석 갱신(시간당 1회)과 아침 브리핑. 알트코인 추천/추적과는 독립이라 BTC 추세와 상관없이 돈다.
    await macro_job.run(session)

    # 알트 사이클 탭용 시가총액(참고치). 12시간에 한 번만 실제로 받아오고(market_cap.refresh 내부 판단),
    # 실패해도 알트코인 스캔이 막히면 안 된다.
    try:
        await market_cap.refresh(session)
    except Exception as exc:
        print(f"[시가총액] 갱신 실패(이번 사이클은 건너뜀): {exc!r}")

    favorable = await check_btc_trend(session)
    print(f"[BTC 추세] 일봉 종가 MA20 위 {config.BTC_HOLD_DAYS}일 이상 유지: {favorable}")
    if not favorable:
        print("BTC 추세 불리 -> 알트코인 추천 중지 (설계 문서: BTC 추세 필터)")
        # BTC 추세가 꺾이면 현재 후보는 모두 없는 것으로 본다. 추적 기간이 끝난 종목은 상태를 비워 다음에
        # 추세가 돌아왔을 때 새 추천으로 알리고, 추적 중인 종목은 이미 알린 단계를 유지해 중복 알림을 막는다.
        alerts, new_states = diff_alerts([])
        state_store.save_all(new_states)
        # 이미 발굴해 추적 중인 종목은 BTC 추세와 상관없이 끝까지 따라가고 현황도 계속 보낸다
        prices = await track_prices(session)
        await exits.process_exits(session, prices, [], btc_filter_on=False)
        await _update_paper(session, False)
        await report.maybe_send_report(session, prices, [], btc_filter_on=False)
        scan_log.record_scan(btc_favorable=False, candidates=0, alerts=0)
        await _send_heartbeat(session, f"💓 {now} 스캔 완료 — BTC 추세 불리, 스캔 건너뜀")
        return

    print("업비트 KRW 마켓 스캔 중...")
    markets = await upbit_client.fetch_markets(session)
    candle_cache: dict = {}  # 개선판이 받은 일봉 캔들을 기준선·사이클 탭이 재사용한다
    candidates = await scan_all(session, markets, candle_cache)

    try:
        await cycle.refresh(session, candle_cache, markets)
    except Exception as exc:  # 사이클 탭 계산 실패로 알트코인 추천 자체가 막히면 안 된다
        print(f"[사이클] 계산 실패(이번 사이클은 건너뜀): {exc!r}")

    if not candidates:
        print("일봉 게이트를 통과한 종목이 없습니다.")
    else:
        print(f"\n후보 {len(candidates)}개 (점수 높은 순)\n")
        for c in candidates[:20]:
            frames_str = " > ".join(c.cleared_frames) if c.cleared_frames else "-"
            vol = " +거래량" if c.volume_bonus else ""
            entry = (" ★5분저점" if c.low_15m else "") + (" ★매수타점(5분)" if c.entry_ready else "")
            entry += " →추천" if c.recommendable else ""
            print(f"  {c.market:12s} [{c.grade}] 점수={c.total_score:5.1f}  통과 프레임=[{frames_str}]{vol}{entry}")

    # candidates가 비어 있어도 diff_alerts/save_all은 실행한다 — 이번에 후보에서 빠진 종목의 상태를
    # 정리해야 하기 때문이다 (추적이 끝난 종목은 초기화, 추적 중인 종목은 알린 단계 유지).
    alerts, new_states = diff_alerts(candidates)
    state_store.save_all(new_states)

    legacy_alerts_on = config.ALERT_STRATEGY == "legacy"
    print(f"\n알림 대상 {len(alerts)}건 (상태가 바뀐 종목만)" + ("" if legacy_alerts_on else " — 개선판은 알림 없이 기록만(ALERT_STRATEGY=original)"))
    if not telegram_client.is_configured():
        print("(텔레그램 미설정 -> 콘솔에만 출력, .env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID를 넣으면 전송됨)")
    candidate_by_market = {c.market: c for c in candidates}
    for a in alerts:
        print(f"  [{a.kind}] {a.message}")
        # 알림 하나(전송 실패 등)에서 난 오류로 나머지 알림·진입 기록이 막히면 안 된다
        try:
            if legacy_alerts_on and telegram_client.is_configured():
                await telegram_client.send_message(session, a.message)
        except Exception as exc:
            print(f"  알림 전송 실패({a.market}, 기록은 계속 진행): {exc!r}")
        if a.kind == "new_candidate":
            try:
                # 발굴 시점의 가격/등급/점수를 '진입 기록'으로 남긴다 — 이후 계속 추적해 실제 수익률을 검증하는 원본 데이터.
                c = candidate_by_market[a.market]
                rec_at = price_tracker.record_entry(a.market, c.current_price, c.grade, round(c.total_score, 1), c.breakdown())
                paper_limit.place(a.market, rec_at, c.current_price, c.support_lows)
            except Exception as exc:
                print(f"  {a.market} 진입 기록 실패(이번 사이클은 건너뜀): {exc!r}")

    await _run_baseline_scan(session, markets, candle_cache)

    prices = await track_prices(session)
    await exits.process_exits(session, prices, candidates, btc_filter_on=True)
    await _update_paper(session, True)
    await report.maybe_send_report(session, prices, candidates, btc_filter_on=True)
    scan_log.record_scan(btc_favorable=True, candidates=len(candidates), alerts=len(alerts))

    await _send_heartbeat(
        session, f"💓 {now} 스캔 완료 — 후보 {len(candidates)}개, 알림 {len(alerts) if legacy_alerts_on else 0}건"
    )


async def _update_paper(session: aiohttp.ClientSession, btc_favorable: bool) -> None:
    """지지선 지정가 모의 실험 갱신. 실패해도 스캔/추적 사이클은 계속 진행한다."""
    try:
        await paper_limit.update(session, btc_favorable)
    except Exception as exc:
        print(f"모의 지정가 갱신 실패(사이클은 계속): {exc!r}")


async def _run_baseline_scan(session: aiohttp.ClientSession, markets: list[str], cache: dict) -> None:
    """기준선(최초 알고리즘) 병행 기록과 모의 지정가 기록. 텔레그램은 config.ALERT_STRATEGY가 'original'일 때만 묶어서 보낸다. 실패해도 개선판에는 영향을 주지 않는다."""
    if not config.ORIGINAL_ENABLED:
        return
    try:
        days = await btc_days_above(session)
        if days < config.ORIGINAL_BTC_HOLD_DAYS:
            print(f"기준선: BTC 종가가 MA20 위 {days}일째 (2일 이상 필요) -> 새 추천 없음")
            return
        found = original_scanner.scan_original(cache, markets, set(price_tracker.recent_recommendation_markets("original")))
        if not found:
            print("기준선: 새 추천 없음")
            return
        prices = await upbit_client.fetch_ticker_prices(session, [m for m, _ in found])
        try:  # 직전에 추천한 종목 표시용 (새 진입을 기록하기 전에 읽는다). 실패해도 추천 기록·알림은 그대로 진행
            prior = original_alerts.prior_entries(price_tracker.load_recommendations(), [m for m, _ in found], datetime.now(timezone.utc))
        except Exception as exc:
            print(f"기준선: 재추천 표시 계산 실패(표시만 생략): {exc!r}")
            prior = {}
        try:  # 진입 시점의 호가 쏠림을 기록에 남긴다 (나중에 효과를 사전 등록 검증하려는 자료, 추천 판단에는 안 씀)
            books = await orderbook.fetch_summaries(session, {m: p for m, p in prices.items() if p})
        except Exception as exc:
            print(f"기준선: 진입 시점 호가 조회 실패(기록만 생략): {exc!r}")
            books = {}
        entered = []
        for market, result in found:
            price = prices.get(market)
            if price is None:
                continue
            vol_pct = original_scanner.volatility_pct(cache.get((market, "day")))
            ob = books.get(market)
            extra = {"ob": {k: ob[k] for k in ("imb", "bid_krw", "ask_krw", "range_pct", "level")}} if ob else None
            rec_at = price_tracker.record_entry(
                market, price, "-", round(result.score, 1), original_scanner.breakdown(result, vol_pct, extra), strategy="original")
            entered.append((market, price, round(result.score, 1), vol_pct, prior.get(market), ob["imb"] if ob else None))
            # 모의 지정가: 1시간봉 스윙 저점에 기록 (개선판과 동일)
            support_lows = []
            hour_candles = cache.get((market, "1h"))
            if hour_candles is None:
                # 개선판 스캔은 일봉 게이트에서 걸린 종목의 1시간봉을 안 받으므로, 대조군 진입 종목은 여기서 따로 받는다
                try:
                    hour_candles = await upbit_client.fetch_candles(session, market, "1h", count=config.PAPER_SUPPORT_WINDOW_HOURS + 30)
                except Exception as exc:
                    print(f"기준선: {market} 1시간봉 조회 실패(모의 지정가만 건너뜀): {exc!r}")
            if hour_candles is not None and not hour_candles.empty:
                support_lows = find_support_lows(hour_candles["close"].tail(config.PAPER_SUPPORT_WINDOW_HOURS))
            paper_limit.place(market, rec_at, price, support_lows)
        alert_on = config.ALERT_STRATEGY == "original"
        print(f"기준선: 새 추천 {len(entered)}건 기록" + (" (텔레그램 묶음 알림)" if alert_on else " (알림 없음)"))
        if alert_on and entered:
            try:  # 기록은 이미 끝났으니 전송 실패가 있어도 추천 기록에는 영향이 없다
                await original_alerts.send_all(
                    session, original_alerts.entry_header(len(entered), datetime.now(timezone.utc), sum(1 for e in entered if e[4]), any(e[5] is not None for e in entered)), original_alerts.entry_lines(entered))
            except Exception as exc:
                print(f"기준선: 알림 전송 실패(기록은 완료): {exc!r}")
    except Exception as exc:
        print(f"기준선 스캔 실패(개선판은 영향 없음): {exc!r}")


async def track_prices(session: aiohttp.ClientSession) -> dict[str, float]:
    """발굴 후 TRACK_DAYS 이내인 종목들의 현재가를 스냅샷으로 남기고, 그 가격표를 돌려준다.
    tracker_job(5분 간격 추적 전용 사이클)도 이 함수를 그대로 재사용한다."""
    markets = price_tracker.active_tracked_markets()
    if not markets:
        return {}
    prices = await upbit_client.fetch_ticker_prices(session, markets)
    price_tracker.record_snapshots(prices)
    print(f"가격 추적 중인 종목 {len(markets)}개 스냅샷 기록")
    return prices
