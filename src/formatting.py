"""텔레그램 메시지용 표시 헬퍼 (이모지, 등급 배지, 가격/수익률 포맷)."""

FRAME_LABEL = {"day": "일봉", "4h": "4시간", "1h": "1시간", "15m": "15분", "5m": "5분", "extra": "보조"}

GRADE_BADGE = {"S": "🏆 S등급", "A": "🟢 A등급", "B": "🔵 B등급"}
NO_GRADE_BADGE = "⚪ 등급 없음"


# 내부 코드값(DB의 strategy 컬럼)은 옛 이름 'legacy'를 그대로 두고, 화면·알림에는 '알트매크로 추천'이라고 부른다.
# (2026-10-12 대조군 전략을 삭제해서 전략은 하나뿐이다.)
STRATEGY_LABEL = {"legacy": "알트매크로"}


def strategy_tag(rec: dict) -> str:
    """추천 항목 앞에 붙이는 전략 표시."""
    return f"[알트매크로] {grade_badge(rec.get('grade'))}"


def grade_badge(grade: str | None) -> str:
    return GRADE_BADGE.get(grade or "", NO_GRADE_BADGE)


def frames_text(frames: list[str]) -> str:
    return " › ".join(FRAME_LABEL.get(f, f) for f in frames)


def fmt_price(price: float | None) -> str:
    if price is None:
        return "—"
    if price >= 100:
        return f"{price:,.0f}원"
    if price >= 1:
        return f"{price:,.2f}원"
    return f"{price:.4f}원"


def fmt_pct(value: float) -> str:
    """부호와 방향 이모지를 함께 붙여 색을 못 봐도 방향이 보이게 한다."""
    icon = "🔺" if value > 0 else "🔻" if value < 0 else "➖"
    return f"{icon} {value:+.2f}%"
