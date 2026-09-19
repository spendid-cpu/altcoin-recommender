"""비트코인 매크로 분석의 조회/캐시/텔레그램 발송.

- 분석(btc_macro.analyse)은 시간당 한 번만 새로 계산해 state.db(meta)에 JSON으로 저장한다. 대시보드는 이 저장본을 읽는다.
- 매일 config.MACRO_BRIEFING_HOUR(기본 오전 8시, 한국시간) 이후 첫 스캔에서 브리핑을 텔레그램으로 한 번 보낸다.
  발송 직전에는 캔들이 오래되지 않게 분석을 새로 계산한다.
- 여기서 나는 오류는 알트코인 스캔을 막으면 안 되므로 run()이 삼킨다.
"""

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd

from src import btc_macro, config, state_store, telegram_client
from src.exchanges import binance_client

KST = ZoneInfo("Asia/Seoul")
MACRO_KEY = "macro_json"
LAST_SENT_KEY = "macro_last_sent_date"
BRIEFING_REFRESH_MINUTES = 10  # 발송 직전에는 이보다 오래된 분석을 쓰지 않는다
WEEKDAY = "월화수목금토일"
STAR = {1: "★", 2: "★★", 3: "★★★", 4: "★★★★"}
STATE_ICON = {"golden": "🟢", "dead": "🔴", "up": "📈", "down": "📉"}
WAVE_ICON = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}


# ----------------------------------------------------------------------------- 조회/캐시
async def build_macro(session: aiohttp.ClientSession) -> dict:
    day, h4, h1, price = await asyncio.gather(
        binance_client.fetch_ohlcv(session, config.BINANCE_SYMBOL, "1d", 200),
        binance_client.fetch_ohlcv(session, config.BINANCE_SYMBOL, "4h", btc_macro.CANDLES_4H),
        binance_client.fetch_ohlcv(session, config.BINANCE_SYMBOL, "1h", 200),
        binance_client.fetch_price(session, config.BINANCE_SYMBOL),
    )
    macro = btc_macro.analyse(day, h4, h1, price)
    macro["generated_at"] = datetime.now(timezone.utc).isoformat()
    return macro


def load_cached() -> dict | None:
    raw = state_store.get_meta(MACRO_KEY)
    return json.loads(raw) if raw else None


def _age_minutes(macro: dict, now: datetime) -> float:
    return (now - datetime.fromisoformat(macro["generated_at"])).total_seconds() / 60


async def refresh(session: aiohttp.ClientSession, max_age_minutes: float) -> dict:
    """저장본이 max_age_minutes보다 오래됐거나 없으면 새로 계산해 저장하고, 아니면 저장본을 그대로 돌려준다."""
    now = datetime.now(timezone.utc)
    cached = load_cached()
    if cached and _age_minutes(cached, now) < max_age_minutes:
        return cached
    macro = await build_macro(session)
    state_store.set_meta(MACRO_KEY, json.dumps(macro, ensure_ascii=False, separators=(",", ":")))
    print(f"[매크로] 분석 갱신: BTC {macro['price']:,.0f} · 지지 {len(macro['fib']['supports'])}구간 · "
          f"저항 {len(macro['fib']['resistances'])}구간")
    return macro


# ----------------------------------------------------------------------------- 텔레그램 메시지
def _fmt(price: float) -> str:
    return f"{price:,.0f}"


def _pivot_day(iso: str) -> str:
    """4시간봉 마감 시각(UTC) -> 그 캔들이 시작된 한국시간 '월-일'."""
    start = pd.Timestamp(iso) - pd.Timedelta(hours=4) + pd.Timedelta(hours=9)
    return start.strftime("%m-%d")


def _members_text(zone: dict) -> str:
    parts = []
    for m in zone["members"]:
        tag = ("큰" if m["wave"] == "large" else "작") + (f"{m['ratio']}" if m["kind"] == "ret" else f"확장{m['ratio']}")
        if tag not in parts:
            parts.append(tag)
    return "·".join(parts)


def pick_zones(zones: list[dict], limit: int = 4) -> list[dict]:
    """현재가에서 가까운 순으로 정렬된 구간 중 보여줄 것: 겹침 2개 이상 우선, 최소 2개는 채운다."""
    strong = [z for z in zones if z["count"] >= 2][:limit]
    if len(strong) >= 2:
        return sorted(strong, key=lambda z: abs(z["distance_pct"]))
    return zones[:max(2, len(strong))]


def _zone_line(z: dict, icon: str) -> str:
    stars = STAR[z["strength"]]
    return f"{icon} {_fmt(z['price'])} ({z['distance_pct']:+.1f}%) {stars} {z['strength_label']} · {_members_text(z)}"


def _wave_block(name: str, w: dict) -> str:
    arrow = "↗" if w["direction"] == "up" else "↘"
    end_tag = "(진행 중)" if w["end"]["provisional"] else ""
    lo_hi = (f"{_fmt(w['start']['price'])}({_pivot_day(w['start']['time'])}) → "
             f"{_fmt(w['end']['price'])}({_pivot_day(w['end']['time'])}){end_tag}")
    lines = w["lines"]
    return (
        f"{WAVE_ICON[w['bias']]} {name} {arrow} {w['label']}\n"
        f"   {lo_hi}\n"
        f"   0.618 라인 {_fmt(lines['0.618'])} · 0.382 라인 {_fmt(lines['0.382'])}"
    )


def format_briefing(macro: dict, now: datetime) -> str:
    kst = now.astimezone(KST)
    trend, fib, stoch = macro["trend"], macro["fib"], macro["stoch"]
    change = macro.get("change_24h_pct")
    change_text = f" · 24시간 {change:+.2f}%" if change is not None else ""
    filter_text = (
        f"🟢 켜짐 (일봉 종가가 MA20 위 {trend['days_above']}일째)" if trend["favorable"]
        else (f"🔴 꺼짐 (일봉 종가가 MA20 아래)" if trend["days_above"] == 0
              else f"🔴 꺼짐 (MA20 위 {trend['days_above']}일째, {config.BTC_HOLD_DAYS}일 유지 필요)")
    )

    lines = [
        f"🌅 비트코인 매크로 브리핑 · {kst.strftime('%m-%d')}({WEEKDAY[kst.weekday()]}) {kst.strftime('%H:%M')} KST",
        "",
        f"₿ ${_fmt(macro['price'])}{change_text}",
        f"🎯 알트코인 추천 필터: {filter_text}",
        f"📏 일봉 MA20 {_fmt(trend['ma20'])} · 최근 일봉 종가 {_fmt(trend['close'])}",
        "",
        "📐 피보나치 (4시간 종가 기준)",
    ]
    for key, name in (("large", "큰 파동"), ("small", "작은 파동")):
        if key in fib["waves"]:
            lines.append(_wave_block(name, fib["waves"][key]))
    lines.append(f"🧭 {fib['read']}")
    lines.append("")

    lines.append("🟢 지지 (현재가 아래, 겹칠수록 강함)")
    picked = pick_zones(fib["supports"])
    lines += [_zone_line(z, "▫️") for z in picked] or ["   (범위 안에 라인이 없어요)"]
    lines.append("🔴 저항 (현재가 위)")
    picked = pick_zones(fib["resistances"])
    lines += [_zone_line(z, "▫️") for z in picked] or ["   (범위 안에 라인이 없어요)"]
    lines.append("")

    lines.append("📊 스토캐스틱 RSI (마감 캔들 기준)")
    for frame, icon in (("day", "📅"), ("4h", "🕓"), ("1h", "🕐")):
        fr = stoch["frames"][frame]
        lines.append(f"{icon} {btc_macro.FRAME_LABEL[frame]}")
        for name in btc_macro.STOCH_PERIODS:
            cell = fr["cells"].get(name)
            if not cell:
                continue
            lines.append(f"   {btc_macro.PERIOD_LABEL[name]} {STATE_ICON[cell['state']]} {cell['text']} "
                         f"(K{cell['k']:.0f}/D{cell['d']:.0f})")
        lines.append(f"   💬 {fr['read']}")
    lines.append(f"📌 {stoch['overall']}")
    lines.append("")
    lines.append("※ 참고용 분석이며 투자 권유가 아니에요. 자세한 차트는 대시보드의 '비트코인 분석' 탭에서 볼 수 있어요.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- 스캔에서 호출
def briefing_due(now: datetime) -> bool:
    hour = config.MACRO_BRIEFING_HOUR
    if hour is None:
        return False
    kst = now.astimezone(KST)
    return kst.hour >= hour and state_store.get_meta(LAST_SENT_KEY) != kst.date().isoformat()


async def run(session: aiohttp.ClientSession) -> None:
    """스캔 사이클 앞머리에서 호출: 분석을 갱신하고, 브리핑 시각이 지났으면 텔레그램으로 보낸다."""
    try:
        now = datetime.now(timezone.utc)
        due = briefing_due(now)
        macro = await refresh(session, BRIEFING_REFRESH_MINUTES if due else config.MACRO_REFRESH_MINUTES)
        if not due:
            return
        text = format_briefing(macro, now)
        print(text)
        if telegram_client.is_configured():
            await telegram_client.send_message(session, text)
            state_store.set_meta(LAST_SENT_KEY, now.astimezone(KST).date().isoformat())
            print("[매크로] 브리핑 발송 완료")
        else:
            print("(텔레그램 미설정 -> 콘솔에만 출력)")
    except Exception as exc:  # 매크로 쪽 문제로 알트코인 스캔이 멈추면 안 된다
        print(f"[매크로] 분석/발송 실패(이번 사이클은 건너뜀): {type(exc).__name__}: {exc}")
