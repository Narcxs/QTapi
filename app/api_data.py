# -*- coding: utf-8 -*-
"""
Public data API - model:  /api/{package}/{instrument}/{period}

Reads the JSON files the poller saved in server/data (so the output is exactly
"ces data"). Routes:

  GET /api                                     -> index: lists every available URL
  GET /api/{package}/{instrument}/{period}     -> one saved file
  GET /api/{package}/{instrument}              -> orderflow (period-less), or
                                                  classic/state at DEFAULT_PERIOD

  package    = classic | state | orderflow
  instrument = spx | spy | ndx | qqq | ...
  period     = zero (0DTE) | full | one        (ignored for orderflow)

Examples (default poller config):
  /api/classic/spx/zero      /api/state/ndx/full      /api/classic/qqq/one
  /api/orderflow/spx         /api/orderflow/qqq

Query params:
  ?raw=1     return only the raw GEXBot payload (drop the envelope metadata)
  ?token=... required only if DATA_API_TOKEN is set in .env (else open access)
"""
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from . import config, store

router = APIRouter(prefix="/api", tags=["data"])

_GREEKS = ("delta", "gamma", "vanna", "charm")
_PACKAGES = ("classic", "state", "orderflow") + _GREEKS
# let browsers / a reverse proxy / CDN absorb bursts (data changes every ~2s)
_CACHE_HEADERS = {"Cache-Control": f"public, max-age={max(1, int(config.POLL_INTERVAL))}"}


def _auth_ok(token: str) -> bool:
    required = config.DATA_API_TOKEN
    return (not required) or (token == required)


def _deny():
    return JSONResponse(
        {"error": "unauthorized", "hint": "pass ?token=<DATA_API_TOKEN>"}, 401)


def _serve(package: str, instrument: str, period, raw: bool, future: bool):
    if package not in _PACKAGES:
        return JSONResponse(
            {"error": "unknown package",
             "hint": "use classic, state or orderflow"}, 400)
    variant = "fut" if future else ""
    # HOT PATH: pre-serialized bytes from memory, no disk read, no re-serialization
    data = store.MEM.get_bytes(package, instrument, period, raw, variant)
    if data is None:
        hint = ("no futures pair for this instrument, or AUTO_CONVERT is off"
                if future else
                "not polled yet, or not in POLLED_TICKERS/POLL_PERIODS")
        return JSONResponse(
            {"error": "not found", "package": package,
             "instrument": instrument.upper(),
             "period": None if package == "orderflow" else period,
             "scale": "future" if future else "index", "hint": hint}, 404)
    return Response(content=data, media_type="application/json",
                    headers=_CACHE_HEADERS)


# --------------------------------------------------------------------------- #
@router.get("")
@router.get("/")
async def index(request: Request, token: str = Query("")):
    if not _auth_ok(token):
        return _deny()
    base = str(request.base_url).rstrip("/")
    endpoints = []

    def _add(url, ticker):
        endpoints.append(url)
        # advertise the futures-scaled variant for mapped instruments
        if config.AUTO_CONVERT and ticker.upper() in config.FUTURES_MAP:
            sep = "&" if "?" in url else "?"
            endpoints.append(f"{url}{sep}future=1")

    for pkg in ("classic", "state"):
        for t in config.POLLED_TICKERS:
            for p in config.POLL_PERIODS:
                _add(f"{base}/api/{pkg}/{t.lower()}/{p}", t)
    for t in config.POLLED_TICKERS:
        _add(f"{base}/api/orderflow/{t.lower()}", t)
    if config.POLL_GREEKS:
        for g in config.GREEKS:
            for t in config.POLLED_TICKERS:
                for p in config.GREEK_PERIODS:
                    _add(f"{base}/api/{g}/{t.lower()}/{p}", t)

    return {
        "model": "/api/{package}/{instrument}/{period}",
        "packages": list(_PACKAGES),
        "greeks": list(config.GREEKS) if config.POLL_GREEKS else [],
        "greek_periods": config.GREEK_PERIODS,
        "instruments": config.POLLED_TICKERS,
        "periods": config.POLL_PERIODS,
        "futures_map": config.FUTURES_MAP,
        "scales": ["index (default)", "future (append ?future=1)"],
        "auth_required": bool(config.DATA_API_TOKEN),
        "count": len(endpoints),
        "endpoints": endpoints,
    }


@router.get("/{package}/{instrument}/{period}")
async def get_with_period(package: str, instrument: str, period: str,
                          token: str = Query(""), raw: bool = Query(False),
                          future: bool = Query(False)):
    if not _auth_ok(token):
        return _deny()
    return _serve(package, instrument, period, raw, future)


@router.get("/{package}/{instrument}")
async def get_no_period(package: str, instrument: str,
                        token: str = Query(""), raw: bool = Query(False),
                        future: bool = Query(False)):
    """orderflow (no period) or classic/state at DEFAULT_PERIOD."""
    if not _auth_ok(token):
        return _deny()
    period = None if package == "orderflow" else config.DEFAULT_PERIOD
    return _serve(package, instrument, period, raw, future)
