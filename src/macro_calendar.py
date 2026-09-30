"""미국 거시경제 일정(FOMC 금리 결정·CPI·PPI·고용·GDP/PCE). 브리핑과 대시보드에 보여주는 참고 정보일 뿐이고
추천 여부에는 쓰지 않는다(백테스트로 검증한 규칙이 아님).

날짜는 연준·BLS·BEA 공식 발표 일정에서 2026-09-30에 확인한 것만 넣었다. 일정은 바뀔 수 있고 사람이 갱신해야 하므로,
마지막 일정이 지나면 브리핑/대시보드가 '등록된 일정이 없다'고 알려준다. 시각은 한국시간이다
(미국 동부 8:30 = 서머타임 기간 21:30, 11월 초 이후 22:30 / 연준 결정 14:00 = 서머타임 기간 다음 날 03:00, 겨울 04:00).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAY = "월화수목금토일"
UPCOMING_DAYS = 14  # 브리핑에 보여줄 기간
BRIEFING_LIMIT = 5
EXPORT_LIMIT = 8
MAJOR_KINDS = {"fomc", "cpi", "jobs"}  # 금리 결정·물가·고용은 '주요'로 표시

# (한국시간 "YYYY-MM-DD HH:MM", 종류, 이름)
_RAW = [
    ("2026-10-02 21:30", "jobs", "미국 9월 고용보고서"),
    ("2026-10-14 21:30", "cpi", "미국 9월 CPI(소비자물가)"),
    ("2026-10-15 21:30", "ppi", "미국 9월 PPI(생산자물가)"),
    ("2026-10-29 03:00", "fomc", "FOMC 금리 결정 (10/27~28 회의)"),
    ("2026-10-29 21:30", "gdp", "미국 3분기 GDP 속보치·9월 개인소득·지출(PCE)"),
    ("2026-11-06 22:30", "jobs", "미국 10월 고용보고서"),
    ("2026-11-10 22:30", "cpi", "미국 10월 CPI(소비자물가)"),
    ("2026-11-13 22:30", "ppi", "미국 10월 PPI(생산자물가)"),
    ("2026-12-10 04:00", "fomc", "FOMC 금리 결정 (12/8~9 회의)"),
]
EVENTS = sorted(
    ({"at": datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=KST), "kind": kind, "title": title,
      "major": kind in MAJOR_KINDS} for at, kind, title in _RAW),
    key=lambda e: e["at"],
)


def day_gap(at: datetime, now: datetime) -> int:
    """한국 날짜 기준으로 며칠 뒤인지 (오늘이면 0)."""
    return (at.astimezone(KST).date() - now.astimezone(KST).date()).days


def d_label(at: datetime, now: datetime) -> str:
    if at < now:
        return "발표됨"
    gap = day_gap(at, now)
    return "오늘" if gap == 0 else "내일" if gap == 1 else f"D-{gap}"


def upcoming(now: datetime, days: int | None = UPCOMING_DAYS, limit: int | None = None) -> list[dict]:
    """지금 이후(오늘 이미 지난 발표는 제외) 일정. days는 한국 날짜 기준이고 None이면 기간 제한 없음."""
    out = [e for e in EVENTS if e["at"] >= now and (days is None or day_gap(e["at"], now) <= days)]
    return out[:limit] if limit else out


def _line(e: dict, now: datetime) -> str:
    at = e["at"].astimezone(KST)
    mark = "🔔" if e["major"] else "▫️"
    return f"{mark} {at.strftime('%m-%d')}({WEEKDAY[at.weekday()]}) {at.strftime('%H:%M')} · {e['title']} · {d_label(e['at'], now)}"


def briefing_lines(now: datetime) -> list[str]:
    """텔레그램 브리핑용 블록. 14일 안에 일정이 없으면 다음 일정 하나를, 등록된 일정이 아예 없으면 갱신 안내를 보여준다."""
    head = "🗓 거시 일정 (미국 · 한국시간 · 🔔=금리·물가·고용)"
    near = upcoming(now, limit=BRIEFING_LIMIT)
    if near:
        return [head] + [_line(e, now) for e in near]
    later = upcoming(now, days=None, limit=1)
    if later:
        return [head, f"   {UPCOMING_DAYS}일 안에는 없어요. 다음 일정:", _line(later[0], now)]
    return [head, "   등록된 일정이 없어요 (일정표를 갱신해야 해요)"]


def export(now: datetime) -> dict:
    """대시보드용. 방금 발표된 것도 몇 시간은 '발표됨'으로 보이도록 6시간 전부터 담는다."""
    since = now - timedelta(hours=6)
    events = [e for e in EVENTS if e["at"] >= since][:EXPORT_LIMIT]
    return {
        "events": [{"at": e["at"].isoformat(), "kind": e["kind"], "title": e["title"], "major": e["major"]} for e in events],
        "registered_until": EVENTS[-1]["at"].isoformat(),
    }
