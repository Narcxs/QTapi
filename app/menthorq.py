# -*- coding: utf-8 -*-
"""
MenthorQ levels fetcher (api.menthorq.io).

Polls /getDailyLevels every MENTHORQ_POLL_INTERVAL seconds for the configured
tickers (ES, NQ, VIX, GC) and stores the latest levels in

    data/menthorq_levels.json

The Telegram bot reads that file to share the levels as free text
(no QTapi token required). Supervised loop, same design as the poller.
"""
import asyncio
import json
import logging
import os
import time

import httpx

from . import config

log = logging.getLogger("menthorq")

_LEVEL_TYPES = "blindspots,swing_levels,gamma_levels,gamma_levels_intraday"
_task = None


def _path() -> str:
    return os.path.join(config.DATA_DIR, "menthorq_levels.json")


def _save(payload: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)


def get_levels():
    """Latest levels for every ticker, read by the bot:
    {ticker: {"date":..., "fetched_at":..., "types": {level_type: [{name, value}]}}}"""
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


async def _fetch_all() -> None:
    if not config.MENTHORQ_API_KEY:
        log.error("MENTHORQ_API_KEY is empty - set it in server/.env")
        return
    out = {}
    async with httpx.AsyncClient(
            base_url=config.MENTHORQ_BASE_URL.rstrip("/"),
            headers={"X-API-Key": config.MENTHORQ_API_KEY},
            timeout=30.0) as client:
        for ticker in config.MENTHORQ_TICKERS:
            try:
                r = await client.get("/getDailyLevels", params={
                    "platform": config.MENTHORQ_PLATFORM,
                    "ticker": ticker,
                    "level_type": _LEVEL_TYPES,
                    "user_id": "userId",
                })
                r.raise_for_status()
                data = r.json()
                types = {}
                date = ""
                for group in data.get("levels", []):
                    lt = group.get("level_type", "unknown")
                    date = date or group.get("date", "")
                    types[lt] = [{"name": lv.get("name"), "value": lv.get("value")}
                                 for lv in group.get("level_values", [])]
                out[ticker] = {"date": date, "fetched_at": int(time.time()),
                               "types": types}
                log.info("MenthorQ %s: %d level groups", ticker, len(types))
            except Exception as e:  # noqa: BLE001
                log.warning("MenthorQ %s fetch error: %s", ticker, e)
            await asyncio.sleep(1.5)      # be gentle with their rate limits
    if out:
        await asyncio.get_running_loop().run_in_executor(None, _save, out)


async def _loop() -> None:
    log.info("MenthorQ fetcher started: tickers=%s every %ss",
             config.MENTHORQ_TICKERS, config.MENTHORQ_POLL_INTERVAL)
    while True:
        start = time.monotonic()
        try:
            await _fetch_all()
        except Exception as e:  # noqa: BLE001
            log.warning("menthorq cycle error: %s", e)
        elapsed = time.monotonic() - start
        await asyncio.sleep(max(30, config.MENTHORQ_POLL_INTERVAL - elapsed))


async def _supervised() -> None:
    while True:
        try:
            await _loop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("menthorq loop crashed, restarting in 10s: %s", e)
            await asyncio.sleep(10)


def start() -> None:
    global _task
    if config.MENTHORQ_API_KEY and config.MENTHORQ_TICKERS and _task is None:
        _task = asyncio.create_task(_supervised())
    elif not config.MENTHORQ_API_KEY:
        log.warning("MenthorQ fetcher disabled (MENTHORQ_API_KEY empty)")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
