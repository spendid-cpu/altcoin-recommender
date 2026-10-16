"""한 번의 스캔 사이클: BTC 추세 확인 -> (유리하면) 전체 스캔 -> 상태 비교 -> 알림.
scripts/run_scan.py(1회 실행)와 scripts/run_loop.py(계속 실행)가 공유해서 쓴다."""

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

from src import btc_context, coinbase_premium, manual_tp, scoring, btc_wave_gate, config, cycle, exits, fear_greed, funnel, macro_job, market_cap, migrations, orderbook, paper_limit, price_tracker, report, scan_log, shadow, state_store, telegram_client, upbit_extra
from src.exchanges import upbit_client
from src.notifier import diff_alerts
from src.scanner import check_btc_trend, scan_all

KST = ZoneInfo("Asia/Seoul")


async def _send_heartbeat(session: aiohttp.ClientSession, text: str) -> None:
    if config.HEARTBEAT_ENABLED and telegram_client.is_configured():
        try:
            await telegram_client.send_message(session, text)
        except Exception as exc:
            print(f"[하트비트] 전송 실패(무시): {exc!r}")


BTC_MA20_STATE_KEY = "btc_ma20_state"  # 직전 스캔의 BTC 일봉 MA20 위/아래 ('above' | 'below') — 바뀔 때만 알린다


async def _notify_btc_ma20(session: aiohttp.ClientSession, favorable: bool) -> None:
    """BTC 일봉 종가가 MA20을 넘나들 때 한 번 알린다 (알트매크로 추천이 알림 주체일 때만, 첫 스캔은 알리지 않고 상태만 저장)."""
    cur = "above" if favorable else "below"
    prev = state_store.get_meta(BTC_MA20_STATE_KEY)
    state_store.set_meta(BTC_MA20_STATE_KEY, cur)
    if prev is None or prev == cur or not telegram_client.is_configured():
        return
    if favorable:
        text = "🟢 BTC 일봉 종가가 MA20 위로 올라왔어요 — MA20 아래 경고를 해제해요"
    elif config.BTC_FILTER_MODE == "warn":
        text = ("⚠️ BTC 일봉 종가가 MA20 아래로 내려갔어요 — 알트매크로 추천은 계속 나오지만 하락장 추천이라 위험이 더 커요. "
                "이때 나온 추천에는 BTC 이탈 정리를 적용하지 않아요")
    else:
        text = "🔴 BTC 일봉 종가가 MA20 아래로 내려갔어요 — 알트매크로 신규 추천을 멈춰요 (이미 추천한 종목은 BTC 이탈 규칙대로 정리)"
    try:
        await telegram_client.send_message(session, text)
    except Exception as exc:
        print(f"[BTC MA20] 상태 변경 알림 전송 실패(무시): {exc!r}")


CYCLE_IDLE_REFRESH_MIN = 55  # BTC 필터가 꺼져 스캔이 일찍 끝나는 동안에도 사이클·섹터 탭 데이터는 이 시간이 지나면 새로 만든다


async def _refresh_cycle_when_idle(session: aiohttp.ClientSession) -> None:
    """BTC 추세가 불리하면 스캔이 일찍 끝나 사이클 갱신까지 못 간다(2026-10-07 이후 4일간 사이클 탭이 멈춰 있었다).
    사이클·섹터 탭이 낡지 않게, 이럴 때는 일봉만 받아서 CYCLE_IDLE_REFRESH_MIN분에 한 번 갱신한다."""
    cached = cycle.load_cached()
    if cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached["generated_at"])).total_seconds() < CYCLE_IDLE_REFRESH_MIN * 60:
        return
    markets = await upbit_client.fetch_markets(session)
    cache: dict = {}
    sem = asyncio.Semaphore(6)

    async def one(m: str) -> None:
        async with sem:
            try:
                cache[(m, "day")] = await upbit_client.fetch_candles(session, m, "day", count=200)
            except Exception as exc:
                print(f"[사이클] {m} 일봉 조회 실패(건너뜀): {exc!r}")

    await asyncio.gather(*(one(m) for m in markets))
    await cycle.refresh(session, cache, markets)


async def run_once(session: aiohttp.ClientSession) -> None:
    now = datetime.now(KST).strftime("%H:%M KST")

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
    btc_warn = (not favorable) and config.BTC_FILTER_MODE == "warn"  # MA20 아래여도 경고만 달고 추천은 계속한다 (config.BTC_FILTER_MODE)
    if btc_warn:
        print("⚠️ BTC 일봉이 MA20 아래 — 경고와 함께 추천은 계속합니다 (BTC_FILTER_MODE=warn)")
    try:
        await _notify_btc_ma20(session, favorable)
    except Exception as exc:
        print(f"[BTC MA20] 상태 기록 실패(무시): {exc!r}")
    try:  # 작은 파동 게이트: 막혀 있으면 신규 추천만 보류 (추적·종료는 그대로). 계산이 실패하면 막지 않는다
        gate, _ = btc_wave_gate.evaluate()  # 상태 변경 알림은 아래 pending_notice가 '마지막으로 알린 상태' 기준으로 따로 판단한다
    except Exception as exc:
        print(f"[BTC 작은 파동 게이트] 상태 계산 실패(보류하지 않음): {exc!r}")
        gate = {"blocked": False, "known": False, "small": None, "note": "계산 실패"}
    small = gate.get("small") or {}
    print(f"[BTC 작은 파동 게이트] {config.BTC_WAVE_GATE}: " + ("보류 중" if gate["blocked"] else "통과") + f" — 작은 파동 {small.get('label', gate.get('note') or '-')}")
    if not favorable and not btc_warn:
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
        try:  # 추천이 멈춘 동안에도 사이클·섹터 화면은 계속 갱신한다 (실패해도 스캔은 정상 종료)
            await _refresh_cycle_when_idle(session)
        except Exception as exc:
            print(f"[사이클] 유휴 갱신 실패(이번 사이클은 건너뜀): {exc!r}")
        await _send_heartbeat(session, f"💓 {now} 스캔 완료 — BTC 추세 불리, 스캔 건너뜀")
        return

    print("업비트 KRW 마켓 스캔 중...")
    markets = await upbit_client.fetch_markets(session)
    candle_cache: dict = {}  # 스캔에서 받은 일봉 캔들을 사이클·섹터 탭이 재사용한다
    scan_stats: dict = {}  # 추천 퍼널용: 일봉에서 빠진 이유별 종목 수
    candidates = await scan_all(session, markets, candle_cache, scan_stats)

    try:  # 프레임 밖 보조 조건(MA 합류·급등 이력·거래량·BTC 환경)을 점수에 더하고 점수순으로 다시 줄 세운다 (추천 대상 판정에는 쓰지 않는다)
        ctx_now = btc_context.snapshot()
        scoring.apply_extras(candidates, btc_context.score_env(ctx_now))
        candidates.sort(key=lambda c: c.total_score, reverse=True)
    except Exception as exc:
        print(f"[점수] 보조 조건 계산 실패(프레임 점수만 사용): {exc!r}")
        ctx_now = None

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

    tracked_now = set(price_tracker.recent_recommendation_markets())
    try:  # 규칙(작은 파동 게이트·일봉 저점 확인) 때문에 막힌 새 추천은 그림자 진입으로 따로 남겨 이후 성과를 비교한다 (추천 기록·알림에는 섞지 않는다)
        held = shadow.record(candidates, tracked_now, gate["blocked"])
        if held:
            print(f"[그림자 기록] 규칙에 막힌 새 추천 {held}건 기록")
    except Exception as exc:
        print(f"[그림자 기록] 실패(무시): {exc!r}")

    # candidates가 비어 있어도 diff_alerts/save_all은 실행한다 — 이번에 후보에서 빠진 종목의 상태를
    # 정리해야 하기 때문이다 (추적이 끝난 종목은 초기화, 추적 중인 종목은 알린 단계 유지).
    alerts, new_states = diff_alerts(candidates, gate_blocked=gate["blocked"], btc_warning=btc_warn)
    state_store.save_all(new_states)

    try:  # 상태 변경 알림: 마지막으로 알린 상태와 다를 때만 (4시간 안에 두 번은 안 보내고, 그 사이 바뀐 상태는 간격이 지난 뒤 알린다)
        notice = btc_wave_gate.pending_notice(gate) if telegram_client.is_configured() else None
        if notice:
            await telegram_client.send_message(session, notice)
            btc_wave_gate.mark_announced(gate)
    except Exception as exc:
        print(f"[BTC 작은 파동 게이트] 상태 변경 알림 전송 실패(다음 스캔에 다시 시도): {exc!r}")
    try:  # 추천 퍼널: 이번 스캔에서 단계마다 몇 개가 남았고 5분 저점까지 온 종목이 왜 추천이 안 됐는지 (표시 전용)
        funnel.record(funnel.build(len(markets), scan_stats, candidates, tracked_now, {a.market for a in alerts if a.kind == "new_candidate"}, gate["blocked"]))
    except Exception as exc:
        print(f"[퍼널] 기록 실패(무시): {exc!r}")
    print(f"\n알림 대상 {len(alerts)}건 (상태가 바뀐 종목만)")
    if not telegram_client.is_configured():
        print("(텔레그램 미설정 -> 콘솔에만 출력, .env에 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID를 넣으면 전송됨)")
    candidate_by_market = {c.market: c for c in candidates}
    try:
        btc_ctx = ctx_now if any(a.kind == "new_candidate" for a in alerts) else None
    except Exception as exc:
        print(f"[BTC 컨텍스트] 기록값 준비 실패(기록만 생략): {exc!r}")
        btc_ctx = None
    for a in alerts:
        print(f"  [{a.kind}] {a.message}")
        # 알림 하나(전송 실패 등)에서 난 오류로 나머지 알림·진입 기록이 막히면 안 된다
        try:
            if telegram_client.is_configured():
                await telegram_client.send_message(session, a.message)
        except Exception as exc:
            print(f"  알림 전송 실패({a.market}, 기록은 계속 진행): {exc!r}")
        if a.kind == "new_candidate":
            try:
                # 발굴 시점의 가격/등급/점수를 '진입 기록'으로 남긴다 — 이후 계속 추적해 실제 수익률을 검증하는 원본 데이터.
                c = candidate_by_market[a.market]
                detail = c.breakdown()
                if btc_warn:  # MA20 아래에서 경고를 달고 나간 추천 표식 — 나중에 따로 성과를 비교하고, BTC 이탈 정리 대상에서도 뺀다
                    detail = {**detail, "btc_below_ma20": True}
                fng = fear_greed.latest_value()  # 추천 순간의 공포·탐욕 지수 (참고용 기록, 추천 판단에는 안 씀 — 탐욕 구간 손절 라이브 확인용)
                if fng is not None:
                    detail = {**detail, "fng": fng}
                if btc_ctx is not None:  # 추천 순간의 BTC 지표 상태(일목·MACD·볼린저·파동·MVRV) — 나중에 검증하려는 기록, 추천 판단에는 안 씀
                    detail = {**detail, "btc_ctx": btc_ctx}
                cbp = coinbase_premium.entry_value()  # 추천 순간의 코인베이스 프리미엄(24시간 평균) — 백테스트 근거 없음, 라이브 확인용 기록만
                if cbp is not None:
                    detail = {**detail, "cbp": cbp}
                detail = {**detail, **await _entry_microstructure(session, a.market, c.current_price)}
                rec_at = price_tracker.record_entry(a.market, c.current_price, c.grade, round(c.total_score, 1), detail)
                paper_limit.place(a.market, rec_at, c.current_price, c.support_lows)
            except Exception as exc:
                print(f"  {a.market} 진입 기록 실패(이번 사이클은 건너뜀): {exc!r}")

    prices = await track_prices(session)
    # warn 모드에서 MA20 아래여도 이번 스캔에서 후보를 받았으니 리포트는 일반 상태로 쓴다. 종료 판단에는 실제 BTC 상태를 넘겨서
    # BTC가 MA20 위일 때 들어간 추천은 BTC 이탈 규칙대로 정리하고, 경고를 달고 나간 추천은 exits.check_exit가 뺀다.
    await exits.process_exits(session, prices, candidates, btc_filter_on=favorable)
    await _update_paper(session, True)
    await _manual_tp_cycle(session, prices)
    try:  # 그림자 진입(막힌 추천)의 가격 확인·종료
        await shadow.update(session)
    except Exception as exc:
        print(f"[그림자 기록] 가격 확인 실패(사이클은 계속): {exc!r}")
    await report.maybe_send_report(session, prices, candidates, btc_filter_on=True)
    scan_log.record_scan(btc_favorable=favorable, candidates=len(candidates), alerts=len(alerts))

    await _send_heartbeat(
        session, f"💓 {now} 스캔 완료 — 후보 {len(candidates)}개, 알림 {len(alerts)}건" + (" (BTC MA20 아래 경고)" if btc_warn else "")
    )


async def _entry_microstructure(session: aiohttp.ClientSession, market: str, price: float) -> dict:
    """추천 순간의 호가 매수 비중(detail.ob)과 최근 1시간 체결 매수 비중(detail.flow)을 기록용으로 받아온다 (2026-10-13 사용자 요청: 점수에 넣을지 판단하려고).
    대조군에만 있던 기록이라 알트매크로 추천에는 없었다. 점수·추천 판단에는 안 쓰고, 실패해도 추천 기록은 계속 진행한다."""
    out: dict = {}
    try:
        s = (await orderbook.fetch_summaries(session, {market: price})).get(market)
        if s:
            out["ob"] = {k: s[k] for k in ("imb", "bid_krw", "ask_krw", "range_pct", "level")}
    except Exception as exc:
        print(f"  {market} 호가 기록 실패(생략): {exc!r}")
    try:
        f = (await upbit_extra.fetch_flow(session, [market])).get(market)
        if f:
            out["flow"] = f
    except Exception as exc:
        print(f"  {market} 체결강도 기록 실패(생략): {exc!r}")
    return out


async def _manual_tp_cycle(session: aiohttp.ClientSession, prices: dict[str, float]) -> None:
    """텔레그램 '수동 익절' 버튼 처리 + 일봉 고점권에 처음 닿은 추천 안내. 실패해도 스캔/추적 사이클은 계속 진행한다."""
    try:
        await manual_tp.poll_actions(session)
    except Exception as exc:
        print(f"[수동 익절] 버튼 확인 실패(사이클은 계속): {exc!r}")
    try:
        await manual_tp.check_high_zone(session, prices)
    except Exception as exc:
        print(f"[일봉 고점권] 확인 실패(사이클은 계속): {exc!r}")


async def _update_paper(session: aiohttp.ClientSession, btc_favorable: bool) -> None:
    """지지선 지정가 모의 실험 갱신. 실패해도 스캔/추적 사이클은 계속 진행한다."""
    try:
        await paper_limit.update(session, btc_favorable)
    except Exception as exc:
        print(f"모의 지정가 갱신 실패(사이클은 계속): {exc!r}")


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
