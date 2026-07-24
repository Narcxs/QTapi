# -*- coding: utf-8 -*-
"""
Background poller.

Its ONLY job: fetch the GEXBot API for the configured tickers and SAVE every
result to a JSON file, all at once, every POLL_INTERVAL seconds.

Per ticker it fetches:
  classic_<period>   for each period in POLL_PERIODS
  state_<period>     for each period in POLL_PERIODS
  orderflow
=> 4 tickers x (2 x 3 periods + 1) = 28 files by default, refreshed every 2 s.
"""
import asyncio
import logging
import time

from . import config
from . import market_hours as mh
from . import gexbot_client as gx

log = logging.getLogger("poller")

# Store with a TTL comfortably above the poll interval so a single missed cycle
# never turns a served value stale.
_CLASSIC_TTL = max(config.GEX_CACHE_TTL, int(config.POLL_INTERVAL * 4) + 1)
_STATE_TTL = _CLASSIC_TTL
_OF_TTL = max(config.ORDERFLOW_CACHE_TTL, int(config.POLL_INTERVAL * 4) + 1)

_task = None


def _jobs():
    jobs = []
    for t in config.POLLED_TICKERS:
        for p in config.POLL_PERIODS:
            jobs.append(gx.refresh("classic", t, p, _CLASSIC_TTL))
            jobs.append(gx.refresh("state", t, p, _STATE_TTL))
        jobs.append(gx.refresh("orderflow", t, "-", _OF_TTL))  # no period
        if config.POLL_GREEKS:
            for g in config.GREEKS:
                for p in config.GREEK_PERIODS:
                    jobs.append(gx.refresh(g, t, p, _STATE_TTL))
    return jobs


async def _loop():
    greeks_per_ticker = (len(config.GREEKS) * len(config.GREEK_PERIODS)
                         if config.POLL_GREEKS else 0)
    per_ticker = 2 * len(config.POLL_PERIODS) + 1 + greeks_per_ticker
    log.info("Poller started: tickers=%s periods=%s greeks=%s every %.1fs -> %d files",
             config.POLLED_TICKERS, config.POLL_PERIODS,
             config.GREEKS if config.POLL_GREEKS else [],
             config.POLL_INTERVAL, len(config.POLLED_TICKERS) * per_ticker)
    if config.MARKET_HOURS_ONLY:
        log.info("Market hours only: fetching %s-%s ET (starts %s min early), Mon-Fri",
                 config.MARKET_OPEN_ET, config.MARKET_CLOSE_ET,
                 config.PRE_OPEN_MINUTES)
    was_open = None
    while True:
        # --- market-hours gate: sleep outside the session window ---
        if not mh.is_open():
            if was_open is not False:
                log.info("Market closed - next fetch window opens %s ET",
                         mh.next_open().strftime("%a %Y-%m-%d %H:%M"))
            was_open = False
            # sleep in <=60s chunks so shutdown stays responsive
            await asyncio.sleep(min(mh.seconds_until_open() + 1, 60))
            continue
        if was_open is not True:
            log.info("Market window OPEN - fetching every %.1fs", config.POLL_INTERVAL)
        was_open = True

        start = time.monotonic()
        try:
            results = await asyncio.gather(*_jobs(), return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            if ok < len(results):
                log.warning("poll cycle: %d/%d ok", ok, len(results))
        except Exception as e:  # noqa: BLE001
            log.warning("poll cycle error: %s", e)
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(0.05, config.POLL_INTERVAL - elapsed))


async def _supervised():
    """Never let the poller die silently: restart it if it ever throws."""
    while True:
        try:
            await _loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("poller crashed, restarting in 2s: %s", e)
            await asyncio.sleep(2)


def start():
    global _task
    if config.POLL_ENABLED and config.POLLED_TICKERS and _task is None:
        _task = asyncio.create_task(_supervised())


async def stop():
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
