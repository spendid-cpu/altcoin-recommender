"""텔레그램 메시지용 표시 헬퍼 (이모지, 등급 배지, 가격/수익률 포맷)."""

FRAME_LABEL = {"day": "일봉", "4h": "4시간", "1h": "1시간"}

GRADE_BADGE = {"A": "🟢 A등급", "B": "🔵 B등급"}
NO_GRADE_BADGE = "⚪ 등급 없음"


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
