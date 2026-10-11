"""일봉 고점권 안내와 수동 익절 (2026-10-12 사용자 요청).

- 일봉 고점권 = 일봉 스토캐스틱 RSI 단기 %K가 고점권(100 - config.OVERSOLD_THRESHOLD = 80 이상)에 닿은 때. 마감된 일봉에 지금 가격을 오늘 봉으로 붙여 계산한다(진행 중인 봉이라 하루 안에도 닿을 수 있다).
  추적 중인 추천이 처음 닿았을 때 한 번만 텔레그램으로 안내하고(rec_signals 표에 기록해 반복하지 않는다), 대시보드 추천 카드에 '수동 익절 추천' 표시가 뜬다.
- 안내 메시지의 '수동 익절' 버튼(텔레그램 인라인 버튼)을 누르면 그 추천을 '수동 익절'로 완료 처리한다. 버튼 누름은 추적 사이클(5분)과 스캔(15분)이 getUpdates로 받아 처리하므로
  최대 몇 분 늦고, 완료가격은 누른 시각이 아니라 처리 시각의 현재가다(실제 매도가가 아니라 기록용 근사). 허용 채팅(TELEGRAM_CHAT_ID)의 버튼만 받는다.
- 자동 익절(+config.EXIT_TAKE_PROFIT_PCT)·손절·만료 규칙은 그대로이고, 수동 익절은 그 위에 얹힌 사용자 선택이다."""

import json
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd

from src import config, price_tracker, state_store, telegram_client
from src.exchanges import upbit_client
from src.formatting import fmt_price
from src.indicators.stoch_rsi import stoch_rsi_all_periods

HIGH_ZONE_K = 100 - config.OVERSOLD_THRESHOLD
OFFSET_KEY = "tg_update_offset"
CALLBACK_PREFIX = "mtp"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DDL = (
    "CREATE TABLE IF NOT EXISTS rec_signals ("
    "market TEXT NOT NULL, entered_at TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, value REAL, price REAL, ret REAL, "
    "PRIMARY KEY (market, entered_at, kind))"
)


def _connect():
    conn = state_store.connect_db()
    conn.execute(DDL)
    return conn


def entered_key(entered_at: datetime) -> int:
    """추천 시각을 마이크로초 정수로 (버튼 데이터에 넣는다 — 실수 오차 없이 같은 추천을 다시 찾으려고)."""
    return (entered_at.astimezone(timezone.utc) - _EPOCH) // timedelta(microseconds=1)


def callback_data(rec: dict) -> str:
    return f"{CALLBACK_PREFIX}|{rec['market']}|{entered_key(rec['entered_at'])}"  # 텔레그램 제한 64바이트 안


def signals() -> dict[tuple[str, str], dict]:
    """대시보드용: {(종목, 추천 시각 ISO): {at, k, ret, price}} (고점권 안내가 나간 추천). 표가 없으면 만들지 않고 빈 dict."""
    conn = state_store.connect_db()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rec_signals'").fetchone():
            return {}
        rows = conn.execute("SELECT market, entered_at, at, value, price, ret FROM rec_signals WHERE kind = 'high_zone'").fetchall()
    finally:
        conn.close()
    return {(m, e): {"at": at, "k": None if v is None else round(v, 1), "price": p, "ret": None if r is None else round(r, 2)} for m, e, at, v, p, r in rows}


def _already(conn) -> set[tuple[str, str]]:
    return {(m, e) for m, e in conn.execute("SELECT market, entered_at FROM rec_signals WHERE kind = 'high_zone'")}


def _alert_text(rec: dict, k: float, price: float, ret: float) -> str:
    tp = config.EXIT_TAKE_PROFIT_PCT
    return (
        f"🔔 [알트매크로] {rec['market']} 일봉 고점권 진입 — 수동 익절 추천\n"
        f"일봉 스토RSI %K {k:.0f} (고점권 {HIGH_ZONE_K:.0f} 이상) · 추천가 대비 {ret:+.1f}%"
        f"{f' (자동 익절 목표 +{tp:g}%)' if tp is not None else ''}\n"
        f"{fmt_price(rec['entry_price'])} → {fmt_price(price)}\n"
        f"아래 버튼을 누르면 이 추천을 '수동 익절'로 완료 처리해요. 누르지 않으면 정해 둔 익절·손절·만료 규칙대로 계속 따라가요."
    )


async def check_high_zone(session: aiohttp.ClientSession, prices: dict[str, float]) -> int:
    """추적 중인 추천 중 일봉이 처음 고점권에 닿은 것을 안내한다. 이번에 안내한 건수를 돌려준다. 실패해도 호출하는 쪽이 감싼다."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=price_tracker.TRACK_DAYS)
    recs = [r for r in price_tracker.load_recommendations() if r["exit"] is None and r["entered_at"] >= cutoff and r["market"] in prices]
    if not recs:
        return 0
    conn = _connect()
    sent = 0
    try:
        done = _already(conn)
        for rec in recs:
            key = (rec["market"], rec["entered_at"].isoformat())
            if key in done:
                continue
            price = prices[rec["market"]]
            try:
                candles = await upbit_client.fetch_candles(session, rec["market"], "day", count=200)
            except Exception as exc:
                print(f"  [일봉 고점권] {rec['market']} 일봉 조회 실패(건너뜀): {exc!r}")
                continue
            if candles is None or candles.empty:
                continue
            close = pd.concat([candles["close"].astype(float), pd.Series([float(price)])], ignore_index=True)  # 마감 일봉 + 지금 가격(오늘 봉)
            k = float(stoch_rsi_all_periods(close)["short"]["k"].iloc[-1])
            if pd.isna(k) or k < HIGH_ZONE_K:
                continue
            ret = (price / rec["entry_price"] - 1) * 100
            conn.execute("INSERT OR IGNORE INTO rec_signals (market, entered_at, kind, at, value, price, ret) VALUES (?, ?, 'high_zone', ?, ?, ?, ?)",
                         (rec["market"], key[1], datetime.now(timezone.utc).isoformat(), k, price, ret))
            conn.commit()
            print(f"  [일봉 고점권] {rec['market']} %K {k:.0f} · {ret:+.1f}% -> 수동 익절 안내")
            sent += 1
            if telegram_client.is_configured():
                try:
                    keyboard = {"inline_keyboard": [[{"text": "✅ 수동 익절 (완료 처리)", "callback_data": callback_data(rec)}]]}
                    await telegram_client.send_message(session, _alert_text(rec, k, price, ret), reply_markup=keyboard)
                except Exception as exc:
                    print(f"  [일봉 고점권] {rec['market']} 안내 전송 실패(기록은 남김): {exc!r}")
    finally:
        conn.close()
    return sent


async def _complete(session: aiohttp.ClientSession, data: str) -> str:
    """버튼 데이터로 추천을 찾아 수동 익절로 완료하고, 사용자에게 보여 줄 결과 문구를 돌려준다."""
    try:
        _, market, key = data.split("|", 2)
        key = int(key)
    except ValueError:
        return "버튼 데이터를 읽지 못했어요"
    rec = next((r for r in price_tracker.load_recommendations() if r["market"] == market and entered_key(r["entered_at"]) == key), None)
    if rec is None:
        return f"{market} 추천을 찾지 못했어요"
    if rec["exit"] is not None:
        labels = {"take_profit": "익절", "stop_loss": "손절", "expired": "만료", "manual_tp": "수동 익절", "btc_exit": "BTC 이탈", "trailing": "되돌림"}
        return f"{market}은 이미 {labels.get(rec['exit']['reason'], rec['exit']['reason'])}로 종료된 추천이에요 ({rec['exit']['return_pct']:+.2f}%)"
    prices = await upbit_client.fetch_ticker_prices(session, [market])
    price = prices.get(market)
    if price is None:
        return f"{market} 현재가를 받지 못했어요 — 잠시 뒤 다시 눌러 주세요"
    ret = (price / rec["entry_price"] - 1) * 100
    price_tracker.record_exit(market, rec["entered_at"], price, ret, "manual_tp")
    return (f"✅ [알트매크로] {market} 수동 익절 완료 {ret:+.2f}% ({fmt_price(rec['entry_price'])} → {fmt_price(price)})\n"
            f"완료 가격은 버튼을 누른 시각이 아니라 처리한 시각의 현재가예요(기록용 근사).")


async def poll_actions(session: aiohttp.ClientSession) -> int:
    """텔레그램에서 눌린 '수동 익절' 버튼을 받아 처리한다. 처리한 건수를 돌려준다. 실패해도 호출하는 쪽이 감싼다."""
    if not telegram_client.is_configured():
        return 0
    offset = int(state_store.get_meta(OFFSET_KEY) or 0)
    updates = await telegram_client.get_updates(session, offset)
    handled = 0
    for u in updates:
        offset = max(offset, int(u["update_id"]) + 1)
        cb = u.get("callback_query")
        data = (cb or {}).get("data") or ""
        if not cb or not data.startswith(CALLBACK_PREFIX + "|"):
            continue
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != str(config.TELEGRAM_CHAT_ID):
            await telegram_client.answer_callback(session, cb["id"], "허용되지 않은 채팅이에요")
            continue
        try:
            text = await _complete(session, data)
        except Exception as exc:
            print(f"  [수동 익절] 처리 실패: {exc!r}")
            await telegram_client.answer_callback(session, cb["id"], "처리하지 못했어요. 잠시 뒤 다시 눌러 주세요")
            continue
        await telegram_client.answer_callback(session, cb["id"], text.split("\n")[0][:190])
        msg = cb.get("message") or {}
        if msg.get("message_id"):
            try:
                await telegram_client.clear_buttons(session, chat, int(msg["message_id"]))
            except Exception as exc:
                print(f"  [수동 익절] 버튼 정리 실패(무시): {exc!r}")
        await telegram_client.send_message(session, text)
        handled += 1
        print(f"  [수동 익절] {text.splitlines()[0]}")
    state_store.set_meta(OFFSET_KEY, str(offset))
    return handled
