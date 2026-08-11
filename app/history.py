# -*- coding: utf-8 -*-
"""
Daily history of the major levels (Call Wall / Put Wall / Zero gamma).

Once per poll cycle the poller snapshots the current majors into

    data/history/{ticker}/{period}/{YYYY-MM-DD}.json   (New York date)

Today's file is overwritten every cycle (always the latest snapshot); past
days are immutable. /overlay serves the previous days as HIST lines so the
indicator can draw "history majors" like the reference GEXBot indicator.
"""
import json
import logging
import os

from . import config, market_hours

log = logging.getLogger("history")

HIST_DAYS = 3        # how many past days the /overlay feed exposes


def _dir(ticker: str, period: str) -> str:
    return os.path.join(config.DATA_DIR, "history", ticker.upper(), period)


def archive(ticker: str, period: str, raw: dict) -> None:
    """Snapshot today's majors (called by the poller; already off-loop)."""
    if not isinstance(raw, dict):
        return
    snap = {
        "date": market_hours.now_et().strftime("%Y-%m-%d"),
        "ticker": ticker.upper(),
        "period": period,
        "spot": raw.get("spot"),
        "zero_gamma": raw.get("zero_gamma"),
        "major_pos_vol": raw.get("major_pos_vol"),
        "major_pos_oi": raw.get("major_pos_oi"),
        "major_neg_vol": raw.get("major_neg_vol"),
        "major_neg_oi": raw.get("major_neg_oi"),
        "sum_gex_vol": raw.get("sum_gex_vol"),
        "sum_gex_oi": raw.get("sum_gex_oi"),
        "source_ts": raw.get("timestamp"),
    }
    try:
        d = _dir(ticker, period)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, snap["date"] + ".json")
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, p)
    except OSError as e:
        log.warning("history write error %s/%s: %s", ticker, period, e)


def previous_days(ticker: str, period: str, days: int = HIST_DAYS):
    """Return the snapshots of the most recent PAST days (today excluded),
    newest first: [ {"date": ..., "major_pos_vol": ...}, ... ]"""
    out = []
    today = market_hours.now_et().strftime("%Y-%m-%d")
    d = _dir(ticker, period)
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name[:-5] >= today:
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (ValueError, OSError):
            continue
        if len(out) >= days:
            break
    return out
