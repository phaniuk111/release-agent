"""Small pure helpers the Investigate modules share: the business date's
window, and text and time formatting for evidence lines."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

_NAME_EXTRA = "-_."
_CHANGE_DAYS = 7
_M_GROUPS = 8             # log/denial groups the model and the timeline keep
_WINDOW_HOURS = 36
_TEXT_MAX = 160


def cob_window(business_date: date, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The window a business date's pipelines run in: that date's 00:00 UTC for
    36 hours (overnight batches land after the date they are for). Today's date
    is still running, so the window ends NOW — an instant query at a time that
    has not happened yet would see nothing."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    start = datetime(business_date.year, business_date.month, business_date.day, tzinfo=timezone.utc)
    return start, min(start + timedelta(hours=_WINDOW_HOURS), now)


def _line(text: Any, limit: int = _TEXT_MAX) -> str:
    """The first line, whitespace folded, cut with an ellipsis — a stack trace
    never reaches a timeline or a lead."""
    raw = "" if text is None else str(text).strip()
    one = " ".join((raw.splitlines() or [""])[0].split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%H:%M")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _name_ok(name: str) -> bool:
    return bool(name) and all(("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c in _NAME_EXTRA
                              for c in name)


def _ok(step: Any) -> dict[str, Any] | None:
    return step if isinstance(step, dict) and step.get("ok") else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
