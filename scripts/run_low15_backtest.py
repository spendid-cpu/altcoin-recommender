"""'15분 저점에서만 추천' 규칙 백테스트.

같은 종목·같은 기간에서 추천 시점을 정의하는 방식만 바꿔가며 추천 후 3일 성과를 비교한다.
  - 옛 방식: 일봉 게이트만 통과하면 추천
  - 일봉/4시간/1시간 게이트를 모두 통과하면 추천 (15분 조건 없음)
  - 위 + 15분 골든크로스 (예전 '15분 타점')
  - 위 + 15분 저점 (지금 적용한 규칙: 단기 %K 저점권이거나 최근 3캔들 안에 저점권 최초 도달/골든크로스)
  - 15분 저점의 세부 변형, 일봉+15분 저점(4시간/1시간 무시)
모든 신호는 마감된 캔들 기준이고, 유효기간(24시간)과 유동성 필터, 3일 쿨다운은 실제 서비스와 같다.
'시장 평균 대비'는 같은 시각에 아무 종목이나 골랐을 때의 3일 수익률을 뺀 초과 수익률이라, 장세(전체가 오르는 효과)를 걷어낸다.

사용법: python scripts/run_low15_backtest.py [--limit N] [--days 65]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import numpy as np
import pandas as pd
from aiolimiter import AsyncLimiter

from src import config
from src.backtest import _asof_series, btc_filter_asof, build_market_signals, load_raw_frames
from src.backtest_exits import BARS_PER_DAY, HORIZON_BARS, Entry, Rule, bootstrap_ci, evaluate
from src.exchanges import binance_client, upbit_client
from src.indicators.stoch_rsi import stoch_rsi_all_periods
from src.scoring import EPS, first_touch_series, golden_cross_series

TP5 = Rule("익절+5%", take_profit=5)
HOLD3 = Rule("3일 보유")


def frame_counts(days: int) -> dict[str, int]:
    return {
        "day_count": 200,
        "h4_count": int(days * 6) + 60,
        "h1_count": int(days * 24) + 120,
        "m15_count": int(days * BARS_PER_DAY) + 300,
    }


COOLDOWN = pd.Timedelta(days=3)


def to_grid(signals) -> tuple[np.ndarray, pd.Series]:
    """거래가 없어 빠진 15분봉은 직전 종가를 이어받아 15분 간격 시간축을 채운다. '몇 개 뒤'가 아니라 '몇 시간 뒤'로
    수익률을 재기 위해서다 (거래가 뜸한 종목은 캔들이 자주 비어 288개 뒤가 3일 뒤가 아닌 경우가 많다)."""
    t = pd.to_datetime(signals.times15).reset_index(drop=True)
    s = pd.Series(signals.close15.to_numpy(dtype=float), index=t)
    s = s[~s.index.duplicated(keep="last")]
    grid = pd.date_range(s.index[0], s.index[-1], freq="15min")
    filled = s.reindex(grid).ffill()
    return filled.to_numpy(), pd.Series(np.arange(len(grid)), index=grid)


def find_entries_grid(signals, ok: np.ndarray) -> list[Entry]:
    """조건이 False에서 True로 바뀌는 첫 캔들을 추천으로 보고, 추천 후 3일 경로를 '실제 시간' 기준으로 잰다.
    한 종목은 3일 동안 다시 추천하지 않고, 3일치 가격이 다 있는 진입만 쓴다."""
    g, pos = to_grid(signals)
    times = pd.to_datetime(signals.times15).to_numpy()
    fresh = ok & ~np.concatenate([[False], ok[:-1]])
    entries: list[Entry] = []
    last = None
    for i in np.flatnonzero(fresh):
        t0 = pd.Timestamp(times[i])
        if last is not None and t0 - last < COOLDOWN:
            continue
        p = int(pos[t0])
        if p + HORIZON_BARS >= len(g):
            continue
        last = t0
        price = g[p]
        entries.append(Entry(
            market=signals.market, time=t0, price=price, score=0.0,
            path_ret=(g[p + 1:p + 1 + HORIZON_BARS] / price - 1) * 100, path_score=np.zeros(HORIZON_BARS),
        ))
    return entries


def masks(signals) -> dict[str, np.ndarray]:
    """15분 캔들마다 각 조건이 그 시점에 참이었는지 (그 시각까지 마감된 캔들만 사용)."""
    t = signals.times15
    passed = lambda ff: _asof_series(t, ff.times, ff.passed_window, default=False).astype(bool).to_numpy()  # noqa: E731
    day, h4, h1 = passed(signals.day), passed(signals.h4), passed(signals.h1)
    liquid = (
        _asof_series(t, signals.day.times, signals.day_trade_value, default=0.0) >= config.MIN_DAILY_TRADE_VALUE_KRW
    ).to_numpy()

    short = stoch_rsi_all_periods(signals.close15)["short"]
    k, d = short["k"], short["d"]
    lookback = max(1, round(config.LOW_LIKE_FRAMES["15m"] / config.FRAME_HOURS["15m"]))
    at_low = (k <= config.OVERSOLD_THRESHOLD + EPS).fillna(False).to_numpy()
    touch = first_touch_series(k).astype(float).rolling(lookback, min_periods=1).max().astype(bool).to_numpy()
    golden_low = golden_cross_series(k, d).astype(float).rolling(lookback, min_periods=1).max().astype(bool).to_numpy()
    low15 = at_low | touch | golden_low
    cross15 = signals.m15_entry.to_numpy(dtype=bool)  # 예전 15분 타점 (순수 골든크로스)

    funnel = day & h4 & h1
    return {
        "liquid": liquid,
        "일봉만 통과 (옛 추천 방식)": day & liquid,
        "일봉·4시간·1시간 통과 (15분 조건 없음)": funnel & liquid,
        "위 + 15분 골든크로스 (옛 15분 타점)": funnel & cross15 & liquid,
        "위 + 15분 저점 (지금 적용한 규칙)": funnel & low15 & liquid,
        "  └ 15분 %K 저점권(20 이하)만": funnel & at_low & liquid,
        "  └ 15분 저점 반등(저점권 골든크로스)만": funnel & golden_low & liquid,
        "일봉 + 15분 저점 (4시간·1시간 무시)": day & low15 & liquid,
    }


def describe(name, entries, bench, extra_rows):
    if not entries:
        print(f"  {name:42s} 추천 없음")
        return
    r3 = np.array([e.path_ret[-1] for e in entries])
    r1 = np.array([e.path_ret[BARS_PER_DAY - 1] for e in entries])
    ex = np.array([e.path_ret[-1] - bench.get(e.time, np.nan) for e in entries])
    ex = ex[~np.isnan(ex)]
    lo, hi = bootstrap_ci(r3)
    elo, ehi = bootstrap_ci(ex)
    tp = evaluate(entries, TP5, HOLD3)
    span = (max(e.time for e in entries) - min(e.time for e in entries)).days + 1
    print(f"  {name:42s} n={len(r3):4d} ({len({e.market for e in entries}):3d}종목) | 3일 평균 {r3.mean():+5.2f}% [{lo:+5.2f},{hi:+5.2f}] "
          f"승률 {(r3 > 0).mean() * 100:4.1f}% 중앙 {np.median(r3):+5.2f}% | 시장 평균 대비 {ex.mean():+5.2f}% [{elo:+5.2f},{ehi:+5.2f}] "
          f"| 익절+5% 평균 {tp['mean']:+5.2f}% 승률 {tp['win']:4.1f}%")
    extra_rows.append((name, entries, r3, ex))


async def main(limit: int | None, days: int) -> None:
    counts = frame_counts(days)
    upbit_client.set_throttle(6)  # 종목당 요청이 수십 건이라 전체 초당 요청 수를 여기서 묶는다
    async with aiohttp.ClientSession() as session:
        markets = await upbit_client.fetch_markets(session)
        if limit:
            markets = markets[:limit]
        btc_df = await binance_client.fetch_klines(session, "BTCUSDT", "1d", 200)
        limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
        semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)
        done = 0

        async def load(market: str):
            nonlocal done
            async with semaphore, limiter:
                try:
                    frames = await load_raw_frames(session, market, **counts)
                except Exception as exc:
                    print(f"  {market} 스킵: {exc}", flush=True)
                    frames = None
            done += 1
            if done % 25 == 0:
                print(f"  ... {done}/{len(markets)} 종목 로드", flush=True)
            return market, frames

        loaded = await asyncio.gather(*(load(m) for m in markets))
    raw = {m: f for m, f in loaded if f}
    signals = [build_market_signals(m, f) for m, f in raw.items()]
    first = min(s.times15.iloc[0] for s in signals)
    last = max(s.times15.iloc[-1] for s in signals)
    print(f"\n마켓 {len(signals)}개, 15분봉 기간 {first} ~ {last} (유효기간 {config.VALIDITY_WINDOW_HOURS}시간, 유동성 필터, 3일 쿨다운)\n", flush=True)

    all_masks = {s.market: masks(s) for s in signals}
    btc_ok = {s.market: btc_filter_asof(s.times15, btc_df).to_numpy() for s in signals}

    # 시장 평균: 같은 시각에 (유동성 필터를 통과한) 모든 종목의 3일 수익률 평균
    rows = []
    for s in signals:
        g, pos = to_grid(s)
        times = pd.to_datetime(s.times15)
        p = pos.reindex(times).to_numpy()
        valid = all_masks[s.market]["liquid"] & (p + HORIZON_BARS < len(g))
        p = p[valid].astype(int)
        rows.append(pd.DataFrame({"time": times.to_numpy()[valid], "r": (g[p + HORIZON_BARS] / g[p] - 1) * 100}))
    bench_all = pd.concat(rows)
    bench = bench_all.groupby("time")["r"].mean().to_dict()
    print(f"[참고] 아무 종목이나 아무 때 샀을 때 3일 평균 {bench_all['r'].mean():+.2f}% 승률 {(bench_all['r'] > 0).mean() * 100:.1f}% "
          f"(표본 {len(bench_all):,}개 종목·시각, 서로 강하게 겹침)\n", flush=True)

    names = [k for k in next(iter(all_masks.values())) if k != "liquid"]
    for label, use_btc in (("BTC 필터 없음", False), ("BTC 필터 적용 (일봉 종가 MA20 위)", True)):
        print(f"=== {label} ===", flush=True)
        keep = []
        for name in names:
            entries = []
            for s in signals:
                ok = all_masks[s.market][name]
                if use_btc:
                    ok = ok & btc_ok[s.market]
                entries.extend(find_entries_grid(s, ok))
            describe(name, entries, bench, keep)
        print(flush=True)

        # 새 규칙 vs 비교군: 시기별 일관성 (추천 시각 기준 앞/뒤 절반) 과 하루 평균 추천 수
        print(f"  [시기별 일관성 / 하루 평균 추천 수] ({label})")
        for name, entries, r3, ex in keep:
            if len(entries) < 8:
                continue
            times = sorted(e.time for e in entries)
            mid = times[len(times) // 2]
            early = np.array([e.path_ret[-1] for e in entries if e.time < mid])
            late = np.array([e.path_ret[-1] for e in entries if e.time >= mid])
            span = max(1, (times[-1] - times[0]).days + 1)
            print(f"    {name:42s} 앞 절반 {early.mean():+5.2f}% (n={len(early)}) / 뒤 절반 {late.mean():+5.2f}% (n={len(late)}) | 하루 {len(entries) / span:4.1f}건")
        print(flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--days", type=int, default=65)
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.days))
