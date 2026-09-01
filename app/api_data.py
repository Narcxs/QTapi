# -*- coding: utf-8 -*-
"""
Public data API - mirrors the official GEXBot URL structure:

  GET /api/tickers                              -> available instruments
  GET /api/{package}/categories                 -> classic|state|orderflow cats
  GET /api/{ticker}/classic/{period}            period = zero | full | one
  GET /api/{ticker}/state/{period}              period = zero | full | one
  GET /api/{ticker}/state/{greek}_{period}      greek = delta|gamma|vanna|charm
                                                (greek periods: zero | one)
  GET /api/{ticker}/orderflow/orderflow         (orderflow has no period)
  GET /api/{ticker}/orderflow                   (short alias)

Query params (examples always put the token LAST):
  ?future=1    futures price scale (SPX/SPY->ES, NDX/QQQ->NQ)
  ?raw=1       return only the raw payload (= the official response shape)
  &token=...   your personal token (when REQUIRE_TOKEN=1)

The PREVIOUS model keeps working so existing users don't break:
  /api/{package}/{instrument}/{period}      /api/{package}/{instrument}
  e.g. /api/classic/spx/zero   /api/gamma/spx/zero   /api/orderflow/spx
"""
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from . import config, store, tokens

router = APIRouter(prefix="/api", tags=["data"])

_PACKAGES = ("classic", "state", "orderflow")
_GREEKS = ("delta", "gamma", "vanna", "charm")
_PERIODS = ("zero", "full", "one")
_GREEK_PERIODS = ("zero", "one")
_OLD_PACKAGES = _PACKAGES + _GREEKS          # old model: greek was a package
# let browsers / a reverse proxy / CDN absorb bursts (data changes every ~2s)
_CACHE_HEADERS = {"Cache-Control": f"public, max-age={max(1, int(config.POLL_INTERVAL))}"}


def _auth_ok(token: str, ticker: str = "", future: bool = False) -> tuple:
    # Returns (is_ok: bool, reason: str)
    if config.DATA_API_TOKEN and token == config.DATA_API_TOKEN:
        return True, "OK"
    fut_str = ""
    if future and ticker:
        fut_str = config.FUTURES_MAP.get(ticker.upper(), "")
    v = tokens.validate(token, ticker=ticker, future=fut_str)

    # Premium tickers ALWAYS require a premium token (even if REQUIRE_TOKEN=0 for free ones)
    is_prem = (ticker and ticker.upper() in config.PREMIUM_TICKERS) or (fut_str in {"GC", "VX"})
    if is_prem:
        if not v.get("ok"):
            return False, v.get("reason", "PREMIUM_REQUIRED")
        return True, "OK"

    if not config.REQUIRE_TOKEN:
        return True, "OK"

    if not v.get("ok"):
        return False, v.get("reason", "UNAUTHORIZED")
    return True, "OK"


def _deny(reason: str = "UNAUTHORIZED"):
    if reason == "PREMIUM_REQUIRED":
        return JSONResponse(
            {"error": "premium_required",
             "hint": "GLD and VIX require a Premium token. Join group -1003822153102 to get one."},
            403)
    return JSONResponse(
        {"error": "unauthorized",
         "hint": "get your token from our Telegram bot, then add &token=..."},
        401)


def _err(status: int, **kw):
    return JSONResponse(kw, status)


def _serve(package: str, instrument: str, period, raw: bool, future: bool):
    if package not in _OLD_PACKAGES:
        return _err(400, error="unknown package",
                    hint="use classic, state or orderflow")
    variant = "fut" if future else ""
    # HOT PATH: pre-serialized bytes from memory, no disk read, no re-serialization
    data = store.MEM.get_bytes(package, instrument, period, raw, variant)
    if data is None:
        hint = ("no futures pair for this instrument, or AUTO_CONVERT is off"
                if future else
                "not polled yet, or not in POLLED_TICKERS/POLL_PERIODS")
        return _err(404, error="not found", package=package,
                    instrument=instrument.upper(),
                    period=None if package == "orderflow" else period,
                    scale="future" if future else "index", hint=hint)
    return Response(content=data, media_type="application/json",
                    headers=_CACHE_HEADERS)


def _serve_new(ticker: str, package: str, category: str, raw: bool, future: bool):
    """Official-style route: /api/{ticker}/{package}/{category}."""
    t = ticker.upper()
    if t in config.TICKER_ALIASES:
        t, _ = config.TICKER_ALIASES[t]
        future = True
    if t not in config.POLLED_TICKERS:
        return _err(404, error="unknown ticker", ticker=t,
                    hint="available: " + ",".join(config.POLLED_TICKERS))
    pkg, cat = package.lower(), category.lower()

    if pkg == "classic":
        if cat not in _PERIODS:
            return _err(400, error="unknown category", category=cat,
                        hint="classic categories: " + ",".join(_PERIODS))
        return _serve("classic", t, cat, raw, future)

    if pkg == "state":
        if cat in _PERIODS:
            return _serve("state", t, cat, raw, future)
        # Greek profiles live under state: {greek}_{period}, e.g. gamma_zero
        if "_" in cat:
            g, p = cat.rsplit("_", 1)
            if g in _GREEKS and p in _GREEK_PERIODS:
                return _serve(g, t, p, raw, future)
        return _err(400, error="unknown category", category=cat,
                    hint="state categories: " + ",".join(_PERIODS) +
                         " + " + ",".join(f"{g}_zero" for g in _GREEKS))

    if pkg == "orderflow":
        if cat != "orderflow":
            return _err(400, error="unknown category", category=cat,
                        hint="orderflow has a single category: orderflow")
        return _serve("orderflow", t, None, raw, future)

    return _err(400, error="unknown package", package=pkg,
                hint="use classic, state or orderflow")


# --------------------------------------------------------------------------- #
@router.get("")
@router.get("/")
async def index(request: Request, token: str = Query("")):
    ok, reason = _auth_ok(token)
    if not ok:
        return _deny(reason)
    base = str(request.base_url).rstrip("/")

    def url(path, *params):
        """Build a listed URL; the caller's token is always appended LAST."""
        qs = "&".join(p for p in params if p)
        if token:
            qs = (qs + "&" if qs else "") + f"token={token}"
        return f"{base}/api/{path}" + (f"?{qs}" if qs else "")

    endpoints = [url("tickers")]
    for pkg in _PACKAGES:
        endpoints.append(url(f"{pkg}/categories"))
    for t in config.POLLED_TICKERS:
        tl = t.lower()
        for p in config.POLL_PERIODS:
            for pkg in ("classic", "state"):
                endpoints.append(url(f"{tl}/{pkg}/{p}"))
                if config.AUTO_CONVERT and t in config.FUTURES_MAP:
                    endpoints.append(url(f"{tl}/{pkg}/{p}", "future=1"))
        endpoints.append(url(f"{tl}/orderflow/orderflow"))
        if config.AUTO_CONVERT and t in config.FUTURES_MAP:
            endpoints.append(url(f"{tl}/orderflow/orderflow", "future=1"))
        if config.POLL_GREEKS:
            for g in config.GREEKS:
                for p in config.GREEK_PERIODS:
                    endpoints.append(url(f"{tl}/state/{g}_{p}"))

    return {
        "model": "/api/{ticker}/{package}/{category}?future=1&token=YOUR_TOKEN",
        "packages": list(_PACKAGES),
        "greeks_under_state": list(config.GREEKS) if config.POLL_GREEKS else [],
        "instruments": config.POLLED_TICKERS,
        "periods": config.POLL_PERIODS,
        "greek_periods": config.GREEK_PERIODS,
        "futures_map": config.FUTURES_MAP,
        "scales": ["index (default)", "future (?future=1)"],
        "auth_required": bool(config.REQUIRE_TOKEN),
        "legacy_model_still_works": "/api/{package}/{instrument}/{period}",
        "count": len(endpoints),
        "endpoints": endpoints,
    }


@router.get("/tickers")
async def tickers(token: str = Query("")):
    """Mirror of the official GET /tickers (limited to what we poll)."""
    ok, reason = _auth_ok(token)
    if not ok:
        return _deny(reason)
    polled = set(config.POLLED_TICKERS)
    out = {"stocks": [], "indexes": [], "futures": []}
    for t in ("SPY", "QQQ", "IWM", "DIA", "GLD", "USO"):
        if t in polled:
            out["stocks"].append(t)
    for t in ("SPX", "NDX", "RUT", "VIX"):
        if t in polled:
            out["indexes"].append(t)
    if config.AUTO_CONVERT:
        fut = {"ES": "ES_SPX", "NQ": "NQ_NDX", "RTY": "RTY_RUT", "YM": "YM_DIA",
               "GC": "GC_GLD", "VX": "VX_VIX"}
        for t in config.POLLED_TICKERS:
            f = config.FUTURES_MAP.get(t)
            if f and fut.get(f) and fut[f] not in out["futures"]:
                out["futures"].append(fut[f])
    return out


@router.get("/{a}/{b}")
async def two_segments(a: str, b: str,
                       token: str = Query(""), raw: bool = Query(False),
                       future: bool = Query(False)):
    al, bl = a.lower(), b.lower()

    # official GET /{package}/categories (category names carry the gex_ prefix)
    if al in _PACKAGES and bl == "categories":
        ok, reason = _auth_ok(token)
        if not ok:
            return _deny(reason)
        if al == "orderflow":
            return ["orderflow"]
        cats = [f"gex_{p}" for p in config.POLL_PERIODS]
        if al == "state" and config.POLL_GREEKS:
            cats += [f"{g}_{p}" for g in config.GREEKS
                     for p in config.GREEK_PERIODS]
        return cats

    # old model: /api/{package}/{instrument} (orderflow or DEFAULT_PERIOD)
    if al in _OLD_PACKAGES:
        ok, reason = _auth_ok(token, ticker=b, future=future)
        if not ok:
            return _deny(reason)
        period = None if al == "orderflow" else config.DEFAULT_PERIOD
        return _serve(al, b, period, raw, future)

    # new model alias: /api/{ticker}/orderflow
    if bl == "orderflow":
        ok, reason = _auth_ok(token, ticker=a, future=future)
        if not ok:
            return _deny(reason)
        return _serve("orderflow", a, None, raw, future)

    return _err(400, error="bad request",
                hint="use /api/{ticker}/{package}/{category}")


@router.get("/{a}/{b}/{c}")
async def three_segments(a: str, b: str, c: str,
                         token: str = Query(""), raw: bool = Query(False),
                         future: bool = Query(False)):
    # old model: /api/{package}/{instrument}/{period}
    if a.lower() in _OLD_PACKAGES:
        ok, reason = _auth_ok(token, ticker=b, future=future)
        if not ok:
            return _deny(reason)
        return _serve(a, b, c, raw, future)
    # new (official) model: /api/{ticker}/{package}/{category}
    ok, reason = _auth_ok(token, ticker=a, future=future)
    if not ok:
        return _deny(reason)
    return _serve_new(a, b, c, raw, future)
