"""한국 주식(코스피·코스닥) 주봉 RSI 과매도 신규 후보 추천 (실험 단계, 코인 추천과 별개).

규칙(2026-10-06 사전 등록 백테스트, 2017-01~2026-08, 신호 9,652건): 마감된 주봉 RSI(14, Wilder) <= 30이고 같은 종목을 최근 4주 안에
추천하지 않았으며 유동성(4주 평균 일 거래대금 5억 원 이상, 종가 1,000원 이상, 최근 4주 매주 거래 있음)을 통과하면 신규 후보.
다음 주 월요일 시가에 진입해 4주 뒤 종가로 평가했을 때 같은 주 전 종목 평균 대비 초과수익 +0.91%p [+0.41, +1.45]였다.
다만 현재 상장 종목만으로 만든 결과라 상장폐지 종목이 빠진 낙관 편향이 있고, 건당 변동이 크다(표준편차 약 19%). 그래서 알림에 '검증 중'을 밝히고
실제 성과를 기록해 쌓는다. RSI 20 이하, RSI 골든크로스, 스토캐스틱 RSI(K3 D5) 변형은 같은 방식으로 검증해 모두 기각됐다.

- 종목 목록: KIND 상장법인 다운로드(막히면 저장해 둔 목록, 마지막으로 data/kr_universe_seed.json). 가격: 야후 주봉(비공식).
- 신호는 마감된 주봉이 생길 때(금요일 장 마감 후)만 새로 나온다. 같은 마감 주봉은 한 번만 처리한다(meta stock_last_week).
- 알림은 코스피·코스닥을 나눠 한 통에 묶고, 맨 위에 두 지수의 상태를 보여 준다. 후보가 없으면 보내지 않는다."""

import asyncio
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
import pandas as pd

from src import jsonutil, original_alerts, state_store, telegram_client
from src.indicators.stoch_rsi import rsi, stoch_rsi

KST = ZoneInfo("Asia/Seoul")
KIND_URL = "http://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval={itv}"
SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "kr_universe_seed.json"
UNIVERSE_KEY = "kr_universe_json"
UNIVERSE_AT_KEY = "kr_universe_at"
LAST_WEEK_KEY = "stock_last_week"
OVERVIEW_KEY = "stock_overview_json"
UNIVERSE_REFRESH_DAYS = 7

RSI_PERIOD = 14
RSI_MAX = 30
COOLDOWN_DAYS = 28            # 같은 종목은 4주에 한 번만
MIN_TV_KRW = 5e8              # 4주 평균 일 거래대금(주간 거래대금/5)
MIN_PRICE = 1000
MAX_PER_MARKET = 10           # 알림에 보여 주는 시장별 상한 (거래대금 큰 순, 표시 순서일 뿐 검증된 기준 아님)
CONCURRENCY = 20
MARKET_NAME = {"유가": "코스피", "코스닥": "코스닥"}
INDEXES = (("코스피", "%5EKS11"), ("코스닥", "%5EKQ11"))


def _connect():
    conn = state_store.connect_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock_recs ("
        "code TEXT NOT NULL, signal_week TEXT NOT NULL, name TEXT, market TEXT, rsi REAL, k REAL, d REAL, signal_close REAL, tv_krw REAL, "
        "created_at TEXT, entry_open REAL, ret1w REAL, ret4w REAL, base4w REAL, shown INTEGER, chg4w REAL, last_price REAL, last_at TEXT, PRIMARY KEY (code, signal_week))"
    )
    return conn


# ----------------------------------------------------------------------------- 종목 목록
def parse_kind(raw: bytes) -> list[dict]:
    text = raw.decode("euc-kr", errors="replace")
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", text, re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 4 and re.fullmatch(r"\d{6}", cells[2] or ""):
            out.append({"name": cells[0], "market": cells[1], "code": cells[2], "sector": cells[3]})
    return out


def _filter_universe(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        if r["market"] not in MARKET_NAME or "스팩" in r["name"] or r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    return out


async def load_universe(session: aiohttp.ClientSession, now: datetime) -> list[dict]:
    """KIND에서 주 1회 새로 받아 저장하고, 못 받으면 저장본 -> 시드 파일 순으로 쓴다."""
    cached = state_store.get_meta(UNIVERSE_KEY)
    at = state_store.get_meta(UNIVERSE_AT_KEY)
    fresh = bool(cached and at and now - datetime.fromisoformat(at) < timedelta(days=UNIVERSE_REFRESH_DAYS))
    if fresh:
        return json.loads(cached)
    try:
        async with session.get(KIND_URL, timeout=aiohttp.ClientTimeout(total=40)) as resp:
            resp.raise_for_status()
            rows = _filter_universe(parse_kind(await resp.read()))
        if len(rows) > 1500:
            state_store.set_meta(UNIVERSE_KEY, jsonutil.dumps(rows, ensure_ascii=False, separators=(",", ":")))
            state_store.set_meta(UNIVERSE_AT_KEY, now.isoformat())
            print(f"[주식] KIND 상장법인 {len(rows)}종목 갱신")
            return rows
        print(f"[주식] KIND 목록이 너무 적어 무시({len(rows)}종목)")
    except Exception as exc:
        print(f"[주식] KIND 목록 조회 실패({exc!r}) - 저장본/시드로 대체")
    if cached:
        return json.loads(cached)
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------- 가격
def last_closed_week_start(now_kst: datetime) -> pd.Timestamp:
    """마지막으로 마감된 주봉의 시작(월요일). 금요일 17시(KST) 이후와 주말에는 이번 주가 마감된 것으로 본다."""
    monday = pd.Timestamp((now_kst - timedelta(days=now_kst.weekday())).date())
    closed_now = now_kst.weekday() > 4 or (now_kst.weekday() == 4 and now_kst.hour >= 17)
    return monday if closed_now else monday - pd.Timedelta(days=7)


async def _chart(session: aiohttp.ClientSession, sem: asyncio.Semaphore, symbol: str, rng: str, itv: str) -> dict | None:
    url = YAHOO.format(symbol=symbol, rng=rng, itv=itv)
    for attempt in range(4):
        async with sem:
            try:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if resp.status != 200:
                        return None
                    j = await resp.json(content_type=None)
            except Exception:
                await asyncio.sleep(1)
                continue
        res = (j.get("chart") or {}).get("result")
        if not res or "timestamp" not in res[0] or not res[0].get("indicators", {}).get("quote"):
            return None
        return res[0]
    return None


def _weekly_frame(res: dict) -> pd.DataFrame | None:
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"t": pd.to_datetime(res["timestamp"], unit="s") + pd.Timedelta(hours=9), "o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"], "v": q["volume"]})
    df = df.dropna(subset=["c"])
    df["t"] = df.t.dt.normalize()
    df = df[(df.c > 0) & (df.o > 0)].reset_index(drop=True)
    return df if len(df) >= 30 else None


async def fetch_weekly_all(session: aiohttp.ClientSession, universe: list[dict]) -> dict[str, pd.DataFrame]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict):
        suffix = "KS" if row["market"] == "유가" else "KQ"
        res = await _chart(session, sem, f"{row['code']}.{suffix}", "2y", "1wk")
        return row["code"], (_weekly_frame(res) if res else None)

    results = await asyncio.gather(*(one(r) for r in universe))
    return {c: d for c, d in results if d is not None}


async def fetch_index(session: aiohttp.ClientSession, label: str, symbol: str, week_cut: pd.Timestamp) -> dict | None:
    sem = asyncio.Semaphore(2)
    try:
        daily = await _chart(session, sem, symbol, "1mo", "1d")
        weekly = await _chart(session, sem, symbol, "2y", "1wk")
        if not daily or not weekly:
            return None
        closes = [c for c in daily["indicators"]["quote"][0]["close"] if c is not None]
        # 야후 일봉은 오늘(또는 직전 거래일) 종가가 확정 전이면 비어 있어서, 현재값(meta)을 기준으로 직전 확정 종가와 비교한다
        price = (daily.get("meta") or {}).get("regularMarketPrice") or closes[-1]
        prev = closes[-2] if len(closes) > 1 and abs(price / closes[-1] - 1) < 1e-6 else closes[-1]
        out = {"label": label, "level": price, "chg_pct": (price / prev - 1) * 100}
        wk = _weekly_frame(weekly)
        if wk is not None:
            wk = wk[wk.t < week_cut + pd.Timedelta(days=7)]
            r = rsi(wk.c, RSI_PERIOD)
            out["rsi_w"] = float(r.iloc[-1]) if len(r) and not np.isnan(r.iloc[-1]) else None
            out["chg4w_pct"] = (wk.c.iloc[-1] / wk.c.iloc[-5] - 1) * 100 if len(wk) > 5 else None
        return out
    except Exception as exc:
        print(f"[주식] {label} 지수 조회 실패(건너뜀): {exc!r}")
        return None


# ----------------------------------------------------------------------------- 신호·추적
def _week_pos(d: pd.DataFrame, week: pd.Timestamp) -> int | None:
    """그 주(월요일 week)에 속한 주봉의 위치. 공휴일로 시작한 주는 봉 날짜가 화요일 이후라서 월~금 사이로 찾는다."""
    m = ((d.t >= week) & (d.t < week + pd.Timedelta(days=5))).to_numpy()
    pos = np.flatnonzero(m)
    return int(pos[-1]) if len(pos) else None


def _prepare(d: pd.DataFrame, week_cut: pd.Timestamp) -> pd.DataFrame:
    d = d[d.t < week_cut + pd.Timedelta(days=7)].reset_index(drop=True).copy()  # 마감된 주(week_cut이 속한 주)까지만, 이후 진행 중인 주는 뺀다
    d["rsi"] = rsi(d.c, RSI_PERIOD)
    d["tv"] = d.c * d.v.fillna(0) / 5                         # 일평균 거래대금 근사(주간 거래대금/5)
    d["liq"] = (d.tv.rolling(4).mean() >= MIN_TV_KRW) & (d.c >= MIN_PRICE) & (d.v.fillna(0).rolling(4).min() > 0)
    return d


def find_signals(panel: dict[str, pd.DataFrame], universe: dict[str, dict], week: pd.Timestamp, recent: set[str]) -> list[dict]:
    out = []
    for code, d in panel.items():
        if d.empty or _week_pos(d, week) != len(d) - 1 or len(d) < RSI_PERIOD + 5:
            continue
        i = len(d) - 1
        if not d.liq.iat[i] or not (d.rsi.iat[i] <= RSI_MAX) or code in recent:
            continue
        sr = stoch_rsi(d.c, RSI_PERIOD, RSI_PERIOD, 3, 5)  # 나중에 변형 검증용으로 값만 남긴다(추천 판단에는 안 씀)
        u = universe[code]
        out.append({
            "code": code, "name": u["name"], "market": u["market"], "rsi": float(d.rsi.iat[i]),
            "k": None if np.isnan(sr.k.iat[i]) else float(sr.k.iat[i]), "d": None if np.isnan(sr.d.iat[i]) else float(sr.d.iat[i]),
            "close": float(d.c.iat[i]), "tv": float(d.tv.rolling(4).mean().iat[i]),
            "chg4w": float(d.c.iat[i] / d.c.iat[i - 4] - 1) * 100 if i >= 4 else None,
        })
    return out


def _baseline_4w(panel: dict[str, pd.DataFrame], week: pd.Timestamp) -> float | None:
    """그 신호 주에 유동성을 통과한 전 종목의 (다음 주 시가 -> 4주 뒤 종가) 평균 수익률."""
    vals = []
    for d in panel.values():
        i = _week_pos(d, week)
        if i is not None and d.liq.iat[i] and i + 4 < len(d):
            vals.append(d.c.iat[i + 4] / d.o.iat[i + 1] - 1)
    return float(np.mean(vals)) if len(vals) >= 50 else None


def update_tracking(conn, raw: dict[str, pd.DataFrame], panel: dict[str, pd.DataFrame]) -> list[dict]:
    """진입 시가(신호 다음 주 월요일 시가)는 그 주 봉이 시작되자마자 채우고, 1주·4주 성과는 마감된 봉으로만 채운다.
    이번에 4주가 끝난 추천 목록을 돌려준다."""
    matured = []
    rows = conn.execute("SELECT code, signal_week, name FROM stock_recs WHERE ret4w IS NULL").fetchall()
    for code, wk, name in rows:
        sig = pd.Timestamp(wk)
        rd, d = raw.get(code), panel.get(code)
        if rd is None:
            continue
        pos = _week_pos(rd, sig)
        if pos is None or pos + 1 >= len(rd):
            continue
        entry = float(rd.o.iat[pos + 1])
        r1 = r4 = base = None
        if d is not None:
            i = _week_pos(d, sig)
            if i is not None:
                if i + 1 < len(d):
                    r1 = float(d.c.iat[i + 1] / entry - 1)
                if i + 4 < len(d):
                    r4 = float(d.c.iat[i + 4] / entry - 1)
                    base = _baseline_4w(panel, sig)
        conn.execute("UPDATE stock_recs SET entry_open=?, ret1w=?, ret4w=?, base4w=? WHERE code=? AND signal_week=?", (entry, r1, r4, base, code, wk))
        if r4 is not None:
            matured.append({"code": code, "name": name, "ret4w": r4, "base4w": base})
    conn.commit()
    return matured


async def refresh_open_prices(session: aiohttp.ClientSession, conn) -> int:
    """아직 4주가 안 끝난 추천의 현재가(야후 최신가)를 받아 둔다 - 대시보드의 '지금 수익'용."""
    rows = conn.execute("SELECT DISTINCT code, market FROM stock_recs WHERE ret4w IS NULL").fetchall()
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(code: str, market: str):
        res = await _chart(session, sem, f"{code}.{'KS' if market == '유가' else 'KQ'}", "5d", "1d")
        price = (res.get("meta") or {}).get("regularMarketPrice") if res else None
        return code, price

    got = await asyncio.gather(*(one(c, m) for c, m in rows))
    n = 0
    stamp = datetime.now(KST).isoformat()
    for code, price in got:
        if price:
            conn.execute("UPDATE stock_recs SET last_price=?, last_at=? WHERE code=? AND ret4w IS NULL", (float(price), stamp, code))
            n += 1
    conn.commit()
    return n


# ----------------------------------------------------------------------------- 메시지
def _idx_text(ix: dict | None) -> str:
    if not ix:
        return "지수 조회 실패"
    chg = f"{ix['chg_pct']:+.1f}%" if ix.get("chg_pct") is not None else "—"
    rs = f"{ix['rsi_w']:.0f}" if ix.get("rsi_w") is not None else "—"
    c4 = f"{ix['chg4w_pct']:+.1f}%" if ix.get("chg4w_pct") is not None else "—"
    return f"{ix['level']:,.2f} (전일 {chg} · 4주 {c4} · 주봉 RSI {rs})"


def build_messages(week: pd.Timestamp, now: datetime, signals: list[dict], idx: dict, matured: list[dict], stats: dict, first_run: bool) -> tuple[str, list[str]]:
    end = (week + pd.Timedelta(days=4)).strftime("%m-%d")
    head = [f"🇰🇷 주식 주봉 RSI 과매도 신규 후보 · {now.astimezone(KST):%m-%d %H:%M} KST", f"{end} 마감 주봉 기준 · RSI(14) ≤ {RSI_MAX} · 같은 종목은 4주에 한 번만" + (" · 첫 실행이라 지난 마감 기준 목록이에요" if first_run else "")]
    head.append(f"📈 코스피 {_idx_text(idx.get('코스피'))}")
    head.append(f"📈 코스닥 {_idx_text(idx.get('코스닥'))}")
    head.append("⚠️ 검증 중인 규칙이에요(매수 권유 아님). 백테스트 4주 초과수익 +0.9%p였지만 상장폐지 종목이 빠진 낙관적 결과이고 건당 변동이 ±19%로 커요. 진입은 다음 주 월요일 시가 기준으로 기록해요.")
    lines = []
    for mk in ("유가", "코스닥"):
        group = sorted([s for s in signals if s["market"] == mk], key=lambda s: -s["tv"])
        if not group:
            lines.append(f"{'🔷' if mk == '유가' else '🔶'} {MARKET_NAME[mk]}: 신규 후보 없음")
            continue
        shown = group[:MAX_PER_MARKET]
        lines.append(f"{'🔷' if mk == '유가' else '🔶'} {MARKET_NAME[mk]} 신규 후보 {len(group)}종목 (거래대금 큰 순{f' 상위 {len(shown)}개' if len(group) > len(shown) else ''})")
        for s in shown:
            c4 = f" · 4주 {s['chg4w']:+.0f}%" if s.get("chg4w") is not None else ""
            lines.append(f"{s['code']} {s['name']}  {s['close']:,.0f}원 · RSI {s['rsi']:.1f}{c4} · 일 거래대금 {s['tv'] / 1e8:,.0f}억")
        if len(group) > len(shown):
            lines.append(f"… 외 {len(group) - len(shown)}종목은 기록만 해요")
    if matured:
        r4 = [m["ret4w"] for m in matured]
        ex = [m["ret4w"] - m["base4w"] for m in matured if m["base4w"] is not None]
        lines.append(f"📊 이번에 4주가 지난 추천 {len(matured)}종목: 평균 {np.mean(r4) * 100:+.1f}% · 플러스 {sum(1 for v in r4 if v > 0)}종목" + (f" · 같은 주 전 종목 평균 대비 {np.mean(ex) * 100:+.1f}%p" if ex else ""))
    if stats.get("n"):
        lines.append(f"📚 누적(4주 경과 {stats['n']}종목): 평균 {stats['mean'] * 100:+.1f}% · 플러스 비율 {stats['win'] * 100:.0f}%" + (f" · 같은 주 전 종목 평균 대비 {stats['excess'] * 100:+.1f}%p" if stats.get("excess") is not None else ""))
    return "\n".join(head), lines


def _stats(conn) -> dict:
    """4주가 끝난 추천의 성과(전체와 시장별)."""
    out = {}
    for key, cond in (("all", ""), ("유가", " AND market='유가'"), ("코스닥", " AND market='코스닥'")):
        rows = conn.execute(f"SELECT ret4w, base4w FROM stock_recs WHERE ret4w IS NOT NULL{cond}").fetchall()
        if not rows:
            out[key] = {"n": 0}
            continue
        r4 = np.array([r[0] for r in rows])
        ex = [r[0] - r[1] for r in rows if r[1] is not None]
        out[key] = {"n": len(rows), "mean": float(r4.mean()), "median": float(np.median(r4)), "win": float((r4 > 0).mean()),
                    "excess": float(np.mean(ex)) if ex else None}
    return out


def export() -> dict | None:
    """대시보드용: 개요(지수·이번 주 후보 수), 추천 기록, 성과 통계."""
    raw = state_store.get_meta(OVERVIEW_KEY)
    if not raw:
        return None
    conn = _connect()
    try:
        cols = ["code", "signal_week", "name", "market", "rsi", "k", "d", "signal_close", "tv_krw", "entry_open", "ret1w", "ret4w", "base4w", "shown", "chg4w", "last_price", "last_at"]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM stock_recs ORDER BY signal_week DESC, tv_krw DESC LIMIT 600").fetchall()
        recs = [dict(zip(cols, r)) for r in rows]
        stats = _stats(conn)
    finally:
        conn.close()
    return {**json.loads(raw), "recs": recs, "stats": stats}


# ----------------------------------------------------------------------------- 실행
async def run(session: aiohttp.ClientSession, now: datetime | None = None, force: bool = False, notify: bool = False, dry: bool = False) -> dict:
    """notify=True일 때만 텔레그램으로 보낸다(기본은 대시보드용 기록만). dry=True면 DB·처리 표시 없이 계산·출력만 한다."""
    now = now or datetime.now(KST)
    week = last_closed_week_start(now.astimezone(KST))
    new_week = force or state_store.get_meta(LAST_WEEK_KEY) != week.date().isoformat()

    idx = {label: await fetch_index(session, label, sym, week) for label, sym in INDEXES}
    overview = {"at": now.isoformat(), "week": week.date().isoformat(), "indexes": idx,
                "rule": {"rsi_max": RSI_MAX, "period": RSI_PERIOD, "cooldown_days": COOLDOWN_DAYS, "min_tv_krw": MIN_TV_KRW, "min_price": MIN_PRICE}}
    result = {"week": week.date().isoformat(), "new_week": bool(new_week)}
    conn = None if dry else _connect()
    try:
        if new_week:
            universe_rows = await load_universe(session, now)
            universe = {r["code"]: r for r in universe_rows}
            raw = await fetch_weekly_all(session, universe_rows)
            print(f"[주식] 주봉 수집 {len(raw)}/{len(universe_rows)}종목")
            if len(raw) < len(universe_rows) * 0.7:
                print("[주식] 수집 성공률이 낮아 이번엔 중단(다음 실행에서 다시 시도)")
                return {**result, "error": "low_coverage"}
            panel = {c: _prepare(d, week) for c, d in raw.items()}
            overview["universe"] = {"listed": len(universe_rows), "fetched": len(raw)}
            recent = set()
            if conn is not None:
                cutoff = (week - pd.Timedelta(days=COOLDOWN_DAYS - 1)).date().isoformat()
                recent = {r[0] for r in conn.execute("SELECT code FROM stock_recs WHERE signal_week >= ?", (cutoff,)).fetchall()}
            signals = find_signals(panel, universe, week, recent)
            shown = set()
            for mk in ("유가", "코스닥"):
                for sg in sorted([x for x in signals if x["market"] == mk], key=lambda x: -x["tv"])[:MAX_PER_MARKET]:
                    shown.add(sg["code"])
            matured = []
            if conn is not None:
                for sg in signals:
                    conn.execute(
                        "INSERT OR IGNORE INTO stock_recs (code, signal_week, name, market, rsi, k, d, signal_close, tv_krw, created_at, shown, chg4w) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sg["code"], week.date().isoformat(), sg["name"], sg["market"], sg["rsi"], sg["k"], sg["d"], sg["close"], sg["tv"], now.isoformat(), 1 if sg["code"] in shown else 0, sg["chg4w"]))
                conn.commit()
                matured = update_tracking(conn, raw, panel)
            result.update(signals=len(signals), matured=len(matured))
            print(f"[주식] {week.date()} 마감 주봉 신규 후보 {len(signals)}종목 (코스피 {sum(1 for x in signals if x['market'] == '유가')} · 코스닥 {sum(1 for x in signals if x['market'] == '코스닥')}), 4주 경과 {len(matured)}종목")
            if conn is None:
                for mk in ("유가", "코스닥"):
                    print(MARKET_NAME[mk], [(x["code"], x["name"], round(x["rsi"], 1)) for x in sorted([y for y in signals if y["market"] == mk], key=lambda y: -y["tv"])[:5]])
            else:
                if notify and telegram_client.is_configured():
                    rows = conn.execute("SELECT code, name, market, rsi, signal_close, tv_krw, chg4w FROM stock_recs WHERE signal_week = ?", (week.date().isoformat(),)).fetchall()
                    sigs = [{"code": r[0], "name": r[1], "market": r[2], "rsi": r[3], "close": r[4], "tv": r[5], "chg4w": r[6]} for r in rows]
                    if sigs:
                        first_run = conn.execute("SELECT COUNT(DISTINCT signal_week) FROM stock_recs").fetchone()[0] <= 1
                        header, lines = build_messages(week, now, sigs, idx, matured, _stats(conn)["all"], first_run)
                        await original_alerts.send_all(session, header, lines)  # 실패하면 예외 -> 처리 표시를 안 남겨 다음 실행에서 다시 보낸다
                state_store.set_meta(LAST_WEEK_KEY, week.date().isoformat())
        if conn is not None:
            result["open_prices"] = await refresh_open_prices(session, conn)
            prev = json.loads(state_store.get_meta(OVERVIEW_KEY) or "{}")
            if not new_week and "universe" in prev:
                overview["universe"] = prev["universe"]
            state_store.set_meta(OVERVIEW_KEY, jsonutil.dumps(overview, ensure_ascii=False, separators=(",", ":")))
    finally:
        if conn is not None:
            conn.close()
    return result
