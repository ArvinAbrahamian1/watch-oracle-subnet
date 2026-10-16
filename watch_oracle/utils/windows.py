"""
Scoring window arithmetic shared by miners and validators.

A window is `window_days` long (1 to 7) and starts at 00:00 UTC. Every window length
is anchored on Monday 1970-01-05, so 7-day windows start on Mondays and 1-day windows
start at every UTC midnight:

    window_start = ANCHOR + floor((day - ANCHOR) / n) * n days

A window is closed once now >= window_start + window_days.
All datetimes passed in must be timezone-aware UTC.
"""

from datetime import date, datetime, timedelta, timezone

ANCHOR = date(1970, 1, 5)
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 7


def is_valid_window_days(window_days: object) -> bool:
    return (
        isinstance(window_days, int)
        and not isinstance(window_days, bool)
        and MIN_WINDOW_DAYS <= window_days <= MAX_WINDOW_DAYS
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def window_start(d: date, window_days: int) -> date:
    """Start of the `window_days` window containing `d`."""
    offset = (d - ANCHOR).days
    return ANCHOR + timedelta(days=(offset // window_days) * window_days)


def target_window_start(now: datetime, horizon_days: int, window_days: int) -> date:
    """Start of the window containing now + `horizon_days`."""
    return window_start((now + timedelta(days=horizon_days)).date(), window_days)


def latest_closed_window_start(now: datetime, window_days: int) -> date:
    """Start of the most recent window that has fully closed as of `now`."""
    return window_start(now.date(), window_days) - timedelta(days=window_days)


def rolling_window_start(now: datetime, window_days: int, rolling_days: int) -> date:
    """Earliest window start whose window ended inside the trailing `rolling_days`."""
    return window_start((now - timedelta(days=rolling_days)).date(), window_days)
