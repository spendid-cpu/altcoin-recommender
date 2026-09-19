"""종료 규칙 백테스트 실행.
1) 추천 기준(점수 임계값)을 바꿔가며 3일 보유 성과를 비교하고
2) 정한 기준에서 여러 종료 규칙(고정 익절/손절, 되돌림, 점수 하락, 조건 이탈)을 같은 진입들로 비교한다.

사용법: python scripts/run_exit_backtest.py [--threshold 13] [--btc] [--limit N]
  --btc: BTC 추세 필터(MA20 위 2일 연속)가 켜진 시점의 진입만 사용 (실제 서비스와 같은 조건, 표본은 줄어든다)
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import numpy as np
from aiolimiter import AsyncLimiter

from src import config
from src.backtest import build_market_signals, load_raw_frames
from src.backtest_exits import Rule, bootstrap_ci, evaluate, find_entries, simulate
from src.exchanges import binance_client, upbit_client

ENTRY_THRESHOLDS = [3, 8, 11, 13, 16]
BASELINE = Rule("3일 보유")


def build_rules() -> list[Rule]:
    rules = [Rule("1일 보유", time_bars=96), BASELINE]
    for tp in (3, 5, 8, 12):
        for sl in (3, 5, 8, None):
            rules.append(Rule(f"익절+{tp}% / 손절{'-' + str(sl) + '%' if sl else '없음'}", take_profit=tp, stop_loss=sl))
    for arm in (2, 4, 6):
        for dd in (2, 3, 5):
            rules.append(Rule(f"되돌림: +{arm}% 달성 후 고점대비 -{dd}%", trail_arm=arm, trail_dd=dd))
    for drop in (3, 5, 8):
        rules.append(Rule(f"점수 {drop}점 이상 하락", score_drop=drop))
    rules.append(Rule("조건 이탈(점수 0)", gate_lost=True))
    rules.append(Rule("되돌림(+4%/-3%) + 조건 이탈", trail_arm=4, trail_dd=3, gate_lost=True))
    rules.append(Rule("되돌림(+4%/-3%) + 손절-5%", trail_arm=4, trail_dd=3, stop_loss=5))
    rules.append(Rule("되돌림(+4%/-3%) + 점수 5점 하락", trail_arm=4, trail_dd=3, score_drop=5))
    return rules


async def main(threshold: float, use_btc: bool, limit: int | None) -> None:
    async with aiohttp.ClientSession() as session:
        markets = await upbit_client.fetch_markets(session)
        if limit:
            markets = markets[:limit]
        btc_df = await binance_client.fetch_klines(session, "BTCUSDT", "1d", 200)
        limiter = AsyncLimiter(config.UPBIT_RATE_LIMIT_PER_SEC, 1)
        semaphore = asyncio.Semaphore(config.UPBIT_CONCURRENCY)

        async def load(market: str):
            async with semaphore, limiter:
                try:
                    return market, await load_raw_frames(session, market)
                except Exception as exc:
                    print(f"  {market} 스킵: {exc}")
                    return market, None

        loaded = await asyncio.gather(*(load(m) for m in markets))
    raw = {m: f for m, f in loaded if f}
    signals = [build_market_signals(m, f) for m, f in raw.items()]
    print(f"마켓 {len(signals)}개 (캔들 마감 시각 기준, 유효기간 {config.VALIDITY_WINDOW_HOURS}시간, 유동성 필터 적용)\n")

    print("=== 1) 추천 기준(점수)별 성과: 추천 후 3일 보유 ===")
    for btc_label, btc in (("BTC 필터 없음", None), ("BTC 필터 적용", btc_df)):
        print(f"[{btc_label}]")
        for thr in ENTRY_THRESHOLDS:
            entries = [e for s in signals for e in find_entries(s, thr, btc)]
            if not entries:
                print(f"  점수 {thr:>2}+  진입 없음")
                continue
            r3 = np.array([e.path_ret[-1] for e in entries])
            r1 = np.array([e.path_ret[95] for e in entries])
            lo, hi = bootstrap_ci(r3)
            print(f"  점수 {thr:>2}+  n={len(entries):4d} | +1일 평균 {r1.mean():+6.2f}% 승률 {(r1 > 0).mean() * 100:5.1f}%"
                  f" | +3일 평균 {r3.mean():+6.2f}% [{lo:+.2f}, {hi:+.2f}] 승률 {(r3 > 0).mean() * 100:5.1f}% 중앙값 {np.median(r3):+.2f}%")
    print()

    btc = btc_df if use_btc else None
    entries = [e for s in signals for e in find_entries(s, threshold, btc)]
    print(f"=== 2) 종료 규칙 비교: 점수 {threshold}+ 추천 {len(entries)}건 ({'BTC 필터 적용' if use_btc else 'BTC 필터 없음'}) ===")
    if len(entries) < 30:
        print("  표본이 30건 미만이라 결과를 믿기 어렵습니다.")
    if not entries:
        return
    days = sorted({e.time.date() for e in entries})
    print(f"  추천 시점 분포: {days[0]} ~ {days[-1]} ({len(days)}일에 걸침), 종목 {len({e.market for e in entries})}개\n")

    rows = [evaluate(entries, rule, BASELINE) for rule in build_rules()]
    rows.sort(key=lambda r: r["mean"], reverse=True)
    header = f"{'규칙':34s} {'평균':>7s} {'95% 구간':>16s} {'승률':>6s} {'중앙값':>7s} {'보유':>6s} {'하위10%':>8s} {'최악':>8s} {'3일보유 대비':>20s}  종료사유"
    print(header)
    for r in rows:
        vs = f"{r['vs_base']:+.2f}% [{r['vs_lo']:+.2f},{r['vs_hi']:+.2f}]"
        reasons = " ".join(f"{k}{v}" for k, v in r["reasons"].items())
        print(f"{r['rule']:34s} {r['mean']:+6.2f}% [{r['ci_lo']:+6.2f},{r['ci_hi']:+6.2f}] {r['win']:5.1f}% {r['median']:+6.2f}% "
              f"{r['hold_h']:5.1f}h {r['p10']:+7.2f}% {r['worst']:+7.2f}% {vs:>20s}  {reasons}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=13)
    parser.add_argument("--btc", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.threshold, args.btc, args.limit))
