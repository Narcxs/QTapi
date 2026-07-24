# -*- coding: utf-8 -*-
"""
US stock-market session window (New York time - DST handled automatically).

When MARKET_HOURS_ONLY=1, the poller only fetches GEXBot inside the window:

    [MARKET_OPEN_ET - PRE_OPEN_MINUTES  ->  MARKET_CLOSE_ET]   Monday-Friday
    e.g. 09:28 -> 16:00 America/New_York with the defaults.

Outside the window the HTTP API stays up and keeps serving the last cached
data (that costs no GEXBot calls); only the upstream fetching sleeps.
"""
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger("market")

try:
    ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - tzdata missing: pip install tzdata
    log.error("No timezone data found - run: pip install tzdata  "
              "(falling back to fixed UTC-4)")
    ET = timezone(timedelta(hours=-4))  # crude EDT fallback


def _parse_hhmm(s: str, default: time) -> time:
    try:
        h, m = s.strip().split(":")[:2]
        return time(int(h), int(m))
    except Exception:  # noqa: BLE001
        log.warning("bad HH:MM value %r - using %s", s, default)
        return default


def _window(day: datetime):
    """(start, end) of the fetch window, as ET datetimes, for the given ET day."""
    o = _parse_hhmm(config.MARKET_OPEN_ET, time(9, 30))
    c = _parse_hhmm(config.MARKET_CLOSE_ET, time(16, 0))
    start = (datetime.combine(day.date(), o, ET)
             - timedelta(minutes=config.PRE_OPEN_MINUTES))
    end = datetime.combine(day.date(), c, ET)
    return start, end


def now_et() -> datetime:
    return datetime.now(ET)


def is_open(now: datetime = None) -> bool:
    """True if GEXBot fetching should be running right now."""
    if not config.MARKET_HOURS_ONLY:
        return True
    now = now or now_et()
    if now.weekday() >= 5:                       # Saturday / Sunday
        return False
    start, end = _window(now)
    return start <= now <= end


def next_open(now: datetime = None) -> datetime:
    """Next ET datetime at which the fetch window opens."""
    now = now or now_et()
    for d in range(0, 8):
        day = now + timedelta(days=d)
        start, _ = _window(day)
        if day.weekday() < 5 and start > now:
            return start
    return now + timedelta(hours=1)              # unreachable; never crash


def seconds_until_open(now: datetime = None) -> float:
    now = now or now_et()
    return max(1.0, (next_open(now) - now).total_seconds())
