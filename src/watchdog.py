"""스캔/추적이 멈췄는지 감시하는 별도 장치. scan.yml·track.yml은 같은 동시성 그룹(group: scan)을 쓰는데,
그 그룹 안에서 뭔가 막히면(예: 2026-09-28에 실제로 있었던 일 — GitHub Pages 배포 승인 대기가 승인자 없이
붕 떠서 43시간 동안 그 뒤 모든 실행이 시작도 못 하고 취소됨) 그 그룹 '안'에서 도는 스크립트로는 절대
알아챌 수 없다(애초에 실행이 안 되니까). 그래서 이 감시 장치는 own 동시성 그룹(watchdog)으로 따로 돌면서,
깃허브 API로 scan/track의 마지막 성공 시각을 직접 확인한다.

같은 이상 상태로 계속 알림이 쌓이지 않게, 한 번 알린 뒤에는 REMIND_MINUTES가 지나야 다시 알리고,
복구되면(새 성공이 생기면) '복구됐다'는 알림을 한 번 더 보낸다. 이 상태는 state.db(meta)에 저장한다."""

import json
import os
from datetime import datetime, timedelta, timezone

import aiohttp

from src import jsonutil, state_store, telegram_client

STALE_MINUTES = int(os.environ.get("WATCHDOG_STALE_MINUTES") or "30")  # 이보다 오래 성공이 없으면 이상으로 본다
REMIND_MINUTES = int(os.environ.get("WATCHDOG_REMIND_MINUTES") or "120")  # 막힌 동안 다시 알리는 간격
STUCK_MINUTES = int(os.environ.get("WATCHDOG_STUCK_MINUTES") or "20")  # 이보다 오래 queued/waiting이면 멈춘 실행으로 보고 취소
STUCK_STATUSES = ("queued", "waiting")
WATCH_WORKFLOWS = ("scan", "track")
STATE_KEY = "watchdog_state"
API_BASE = "https://api.github.com"


def _repo() -> str:
    """'소유자/저장소' — GitHub Actions가 자동으로 주는 GITHUB_REPOSITORY를 쓰고, 로컬 테스트용 기본값을 둔다."""
    return os.environ.get("GITHUB_REPOSITORY", "spendid-cpu/altcoin-recommender")


async def _last_success_at(session: aiohttp.ClientSession) -> datetime | None:
    """scan/track 워크플로 중 가장 최근에 성공한 실행의 생성 시각(UTC)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    best: datetime | None = None
    for name in WATCH_WORKFLOWS:
        url = f"{API_BASE}/repos/{_repo()}/actions/workflows/{name}.yml/runs"
        async with session.get(url, params={"status": "success", "per_page": 1}, headers=headers) as resp:
            if resp.status != 200:
                continue
            data = await resp.json()
        runs = data.get("workflow_runs") or []
        if not runs:
            continue
        at = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
        if best is None or at > best:
            best = at
    return best


def _api_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def cancel_stuck_runs(session: aiohttp.ClientSession) -> list[dict]:
    """scan/track 실행 중 STUCK_MINUTES 넘게 시작도 못 한 채 queued(러너 배정 대기)/waiting(승인 대기)인 것을 취소한다.
    그런 실행 하나가 동시성 그룹(scan)을 붙잡으면 뒤의 모든 실행이 시작도 못 하고 취소된다(2026-09-28, 10-03 실제 사례).
    진행 중(in_progress)인 실행은 건드리지 않는다. 취소한 실행의 정보 목록을 돌려준다."""
    now = datetime.now(timezone.utc)
    headers = _api_headers()
    cancelled: list[dict] = []
    for name in WATCH_WORKFLOWS:
        for status in STUCK_STATUSES:
            url = f"{API_BASE}/repos/{_repo()}/actions/workflows/{name}.yml/runs"
            async with session.get(url, params={"status": status, "per_page": 20}, headers=headers) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            for run in data.get("workflow_runs") or []:
                age = (now - datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
                if age < STUCK_MINUTES:
                    continue
                async with session.post(f"{API_BASE}/repos/{_repo()}/actions/runs/{run['id']}/cancel", headers=headers) as resp:
                    ok = resp.status in (200, 202)
                print(f"[감시장치] {name} 실행 {run['id']}이(가) {status} 상태로 {age:.0f}분째 멈춰 있어 취소 {'요청' if ok else '실패'} (HTTP {resp.status})")
                if ok:
                    cancelled.append({"id": run["id"], "workflow": name, "status": status, "age_minutes": age})
    return cancelled


def _load_state() -> dict:
    raw = state_store.get_meta(STATE_KEY)
    return json.loads(raw) if raw else {}


def _save_state(state: dict) -> None:
    state_store.set_meta(STATE_KEY, jsonutil.dumps(state, ensure_ascii=False))


async def check(session: aiohttp.ClientSession) -> None:
    """한 번 확인하고 필요하면 텔레그램으로 알린다. watchdog.yml이 30분마다 부른다."""
    now = datetime.now(timezone.utc)
    try:
        cancelled = await cancel_stuck_runs(session)
    except Exception as exc:  # 취소 시도가 실패해도 아래의 정지 감지·알림은 계속 돌아야 한다
        print(f"[감시장치] 멈춘 실행 취소 중 오류(무시): {exc!r}")
        cancelled = []
    if cancelled:
        lines = "\n".join(f"· {c['workflow']} 실행 {c['id']} ({'러너 배정' if c['status'] == 'queued' else '승인'} 대기 {c['age_minutes'] / 60:.1f}시간)" for c in cancelled)
        text = f"🛠 멈춰 있던 실행 {len(cancelled)}건을 자동으로 취소했어요\n{lines}\n대기 중이던 다음 스캔·추적이 이어서 돌아요."
        if telegram_client.is_configured():
            try:
                await telegram_client.send_message(session, text)
            except Exception as exc:
                print(f"[감시장치] 취소 알림 전송 실패: {exc!r}")
        else:
            print(text)

    last_success = await _last_success_at(session)
    if last_success is None:
        print("[감시장치] 최근 성공 실행을 찾지 못함(API 문제일 수 있음) — 이번엔 건너뜀")
        return

    age_minutes = (now - last_success).total_seconds() / 60
    state = _load_state()
    stale = age_minutes > STALE_MINUTES

    if stale:
        last_alert_at = state.get("last_alert_at")
        should_alert = not state.get("ongoing") or (
            last_alert_at and (now - datetime.fromisoformat(last_alert_at)).total_seconds() / 60 >= REMIND_MINUTES
        )
        print(f"[감시장치] 마지막 성공 {age_minutes:.0f}분 전({last_success.isoformat()}) — 이상 상태"
              f"{' (알림 보냄)' if should_alert else ' (이미 알렸음, 대기)'}")
        if should_alert:
            text = (
                f"🚨 알트코인 추천기 스캔이 멈춘 것 같아요\n"
                f"마지막 성공: {last_success.strftime('%m-%d %H:%M')} UTC ({age_minutes/60:.1f}시간 전)\n"
                f"scan/track 워크플로가 그만큼 동안 한 번도 성공하지 못했어요. GitHub Actions에서 막힌 실행이\n"
                f"없는지 확인해 주세요(예: Pages 배포 승인 대기로 붙잡혀 있는 경우)."
            )
            if telegram_client.is_configured():
                try:
                    await telegram_client.send_message(session, text)
                except Exception as exc:
                    print(f"[감시장치] 알림 전송 실패: {exc!r}")
            else:
                print(text)
            _save_state({"ongoing": True, "last_alert_at": now.isoformat(), "last_success_at": last_success.isoformat()})
    else:
        if state.get("ongoing"):
            down_for = None
            prev_success = state.get("last_success_at")
            if prev_success:
                down_for = (last_success - datetime.fromisoformat(prev_success)).total_seconds() / 3600
            text = (
                f"✅ 알트코인 추천기 스캔이 복구됐어요\n"
                f"마지막 성공: {last_success.strftime('%m-%d %H:%M')} UTC"
                + (f" (약 {down_for:.1f}시간 멈춰 있었어요)" if down_for and down_for > 0 else "")
            )
            if telegram_client.is_configured():
                try:
                    await telegram_client.send_message(session, text)
                except Exception as exc:
                    print(f"[감시장치] 복구 알림 전송 실패: {exc!r}")
            else:
                print(text)
        else:
            print(f"[감시장치] 정상 (마지막 성공 {age_minutes:.0f}분 전)")
        _save_state({"ongoing": False, "last_success_at": last_success.isoformat()})
