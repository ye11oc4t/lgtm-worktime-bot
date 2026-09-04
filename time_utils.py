import calendar
from datetime import date


def format_duration(total_seconds: int) -> str:
    total_seconds = max(0, total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}시간 {minutes}분 {seconds}초"


def parse_local_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("날짜는 `YYYY-MM-DD` 형식으로 입력해 주세요.") from exc


def parse_month(value: str | None, default: date) -> tuple[int, int]:
    if not value:
        return default.year, default.month
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError("월은 `YYYY-MM` 형식으로 입력해 주세요.") from exc
    return parsed.year, parsed.month


def grass_level(work_seconds: int) -> str:
    hours = max(0, work_seconds) / 3600
    if hours == 0:
        return "⬛"
    if hours < 6:
        return "🟦"
    if hours < 12:
        return "🟨"
    if hours < 18:
        return "🟧"
    return "🟥"


def render_grass(year: int, month: int, seconds_by_day: dict[date, int]) -> str:
    """월~일 행, 주차 열인 GitHub 스타일 월간 활동표를 만든다."""
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    labels = ("월", "화", "수", "목", "금", "토", "일")
    lines = []
    for weekday, label in enumerate(labels):
        cells = []
        for week in weeks:
            day = week[weekday]
            cells.append(grass_level(seconds_by_day.get(day, 0)) if day.month == month else "· ")
        lines.append(f"{label}  {''.join(cells)}")
    return "\n".join(lines)
