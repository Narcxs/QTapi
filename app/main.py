# -*- coding: utf-8 -*-
"""
GexBot cache server (FastAPI).

The poller (poller.py) fetches GEXBot and saves each result to a JSON file in
server/data every POLL_INTERVAL seconds. This module:
  * runs the poller,
  * verifies subscriptions against Hostinger MySQL (cached => 5-min re-check),
  * serves the data (text for the indicator, JSON files on demand).

Endpoints
  GET /health
  GET /status                                  -> which files exist + their age
  GET /verify?key=...
  GET /data?ticker=SPX&package=classic&period=zero&key=...   -> the saved JSON file
  GET /overlay?ticker=SPX&key=...&period=zero&future=ES      -> lines + profiles (overlay indicator)
  GET /orderflow?ticker=SPX&key=...&future=ES                -> flow metrics + gamma levels (panel indicator)
  GET /tickers?key=...
"""
import asyncio
import logging

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import api_data, config, db, market_hours, poller, storage, store
from . import gexbot_client as gx
from .cache import TTLCache

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO))
log = logging.getLogger("main")

app = FastAPI(title="GexBot Cache Server", version="3.0")
app.include_router(api_data.router)   # /api/{package}/{instrument}/{period}
_sub_cache = TTLCache()


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _startup():
    if not config.GEXBOT_API_KEY:
        log.error("GEXBOT_API_KEY is EMPTY. Put your key in server/.env "
                  "(GEXBOT_API_KEY=...) then restart. Poller will fail until then.")
    else:
        log.info("GEXBot key loaded (%d chars).", len(config.GEXBOT_API_KEY))
    poller.start()


@app.on_event("shutdown")
async def _shutdown():
    await poller.stop()
    await gx.aclose()


# --------------------------------------------------------------------------- #
# subscription
# --------------------------------------------------------------------------- #
async def verify_cached(key: str) -> dict:
    # Free / open API: no subscription check, no MySQL touched.
    if not config.REQUIRE_SUBSCRIPTION:
        return {"ok": True, "reason": "OPEN", "expires_at": None}
    res, fresh = _sub_cache.get(key)
    if fresh:
        return res
    res = await asyncio.to_thread(db.verify_key, key)
    _sub_cache.set(key, res, config.SUB_CACHE_TTL if res.get("ok") else 60)
    return res


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# basic endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    return {"status": "ok", "poll_enabled": config.POLL_ENABLED,
            "tickers": config.POLLED_TICKERS, "periods": config.POLL_PERIODS,
            "interval_s": config.POLL_INTERVAL,
            "market_hours_only": config.MARKET_HOURS_ONLY,
            "market_open": market_hours.is_open(),
            "next_open_et": (market_hours.next_open().isoformat()
                             if config.MARKET_HOURS_ONLY
                             and not market_hours.is_open() else None)}


@app.get("/status")
async def status():
    return {"data_dir": config.DATA_DIR, "files": storage.status()}


@app.get("/verify", response_class=PlainTextResponse)
async def verify(key: str = Query("")):
    res = await verify_cached(key)
    if res.get("ok"):
        return f"OK|{res.get('expires_at') or ''}"
    return f"DENIED|{res.get('reason')}"


@app.get("/tickers")
async def tickers(key: str = Query("")):
    sub = await verify_cached(key)
    if not sub.get("ok"):
        return JSONResponse({"status": "DENIED", "reason": sub.get("reason")}, 403)
    data, stale = await gx.get_tickers()
    return {"stale": stale, "data": data}


@app.get("/data")
async def data_file(ticker: str = Query(...),
                    package: str = Query(...),
                    key: str = Query(""),
                    period: str = Query("zero"),
                    future: bool = Query(False)):
    """Return the exact saved JSON file (envelope + raw GEXBot data).
    future=1 returns the futures-converted variant for mapped instruments."""
    sub = await verify_cached(key)
    if not sub.get("ok"):
        return JSONResponse({"status": "DENIED", "reason": sub.get("reason")}, 403)
    if package not in ("classic", "state", "orderflow",
                       "delta", "gamma", "vanna", "charm"):
        return JSONResponse({"error": "unknown package"}, 400)
    variant = "fut" if future else ""
    data = store.MEM.get_bytes(package, ticker, period, raw=False, variant=variant)
    if data is None:
        return JSONResponse({"error": "not found (not polled yet?)"}, 404)
    return Response(content=data, media_type="application/json")


# --------------------------------------------------------------------------- #
# overlay indicator feed (classic + state profiles + key levels)
# --------------------------------------------------------------------------- #
@app.get("/overlay", response_class=PlainTextResponse)
async def overlay(ticker: str = Query("SPX"), key: str = Query(""),
                  period: str = Query(None), future: str = Query("")):
    sub = await verify_cached(key)
    if not sub.get("ok"):
        return f"STATUS=DENIED|{sub.get('reason')}"

    period = period or config.DEFAULT_PERIOD
    conv = await gx.get_conversion(ticker, future) if future else None
    classic, c_stale = await gx.get_classic(ticker, period)
    state, s_stale = await gx.get_state(ticker, period)
    of, o_stale = await gx.get_orderflow(ticker)

    def cv(v):
        return gx.convert(_num(v), conv)

    spot = cv((classic or {}).get("spot")) if classic else cv((of or {}).get("spot"))
    lines = [
        f"STATUS=OK|{sub.get('expires_at') or ''}",
        f"TS={(classic or of or {}).get('timestamp') or ''}",
        f"STALE={'1' if (c_stale or s_stale) else '0'}",
        f"SPOT={_fmt(spot)}",
        f"NETVOL={_fmt(_num((classic or {}).get('sum_gex_vol')))}",
        f"NETOI={_fmt(_num((classic or {}).get('sum_gex_oi')))}",
    ]

    if classic:
        z = cv(classic.get("zero_gamma"))
        if z:
            lines.append(f"CLVL|Zero γ|{_fmt(z)}|FFD000|dot|1")
        cw = cv(classic.get("major_pos_vol")); pw = cv(classic.get("major_neg_vol"))
        if cw:
            lines.append(f"CLVL|Call Wall|{_fmt(cw)}|00C800|solid|2")
        if pw:
            lines.append(f"CLVL|Put Wall|{_fmt(pw)}|FF3232|solid|2")
        cwo = cv(classic.get("major_pos_oi")); pwo = cv(classic.get("major_neg_oi"))
        if cwo:
            lines.append(f"CLVL|Call Wall OI|{_fmt(cwo)}|009600|dash|1")
        if pwo:
            lines.append(f"CLVL|Put Wall OI|{_fmt(pwo)}|C83232|dash|1")

    if of:
        lg = cv(of.get("z_mlgamma")); sg = cv(of.get("z_msgamma"))
        if lg:
            lines.append(f"OLVL|Long γ|{_fmt(lg)}|1E90FF|solid|2")
        if sg:
            lines.append(f"OLVL|Short γ|{_fmt(sg)}|FF7F00|solid|2")

    if state:
        scw = cv(state.get("major_pos_vol")); spw = cv(state.get("major_neg_vol"))
        if scw:
            lines.append(f"SLVL|Call GEX|{_fmt(scw)}|00E0A0|dash|1")
        if spw:
            lines.append(f"SLVL|Put GEX|{_fmt(spw)}|E060FF|dash|1")

    if classic:
        for s in classic.get("strikes") or []:
            if isinstance(s, (list, tuple)) and len(s) >= 3:
                st = cv(s[0]); gv = _num(s[1]); go = _num(s[2])
                if st is not None and (gv or go):
                    lines.append(f"CPRO|{_fmt(st)}|{_fmt(gv)}|{_fmt(go)}")

    if state:
        for s in state.get("strikes") or []:
            if isinstance(s, (list, tuple)) and len(s) >= 2:
                st = cv(s[0]); im = _num(s[1])
                if st is not None and im:
                    lines.append(f"SPRO|{_fmt(st)}|{_fmt(im)}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# orderflow indicator feed (flow metrics + gamma levels)
# --------------------------------------------------------------------------- #
_OF_METRICS = [
    "dexoflow", "gexoflow", "cvroflow",
    "net_dex", "net_call_dex", "net_put_dex",
    "agg_dex", "agg_call_dex", "agg_put_dex",
    "one_dexoflow", "one_gexoflow", "one_cvroflow",
    "one_net_dex", "one_agg_dex",
    "zgr", "zcvr", "zvanna", "zcharm",
    "ogr", "ocvr", "ovanna", "ocharm",
]


@app.get("/orderflow", response_class=PlainTextResponse)
async def orderflow(ticker: str = Query("SPX"), key: str = Query(""),
                    future: str = Query("")):
    sub = await verify_cached(key)
    if not sub.get("ok"):
        return f"STATUS=DENIED|{sub.get('reason')}"

    conv = await gx.get_conversion(ticker, future) if future else None
    of, stale = await gx.get_orderflow(ticker)

    def cv(v):
        return gx.convert(_num(v), conv)

    of = of or {}
    lines = [
        f"STATUS=OK|{sub.get('expires_at') or ''}",
        f"TS={of.get('timestamp') or ''}",
        f"STALE={'1' if stale else '0'}",
        f"SPOT={_fmt(cv(of.get('spot')))}",
    ]
    lg = cv(of.get("z_mlgamma")); sg = cv(of.get("z_msgamma"))
    mc = cv(of.get("zero_mcall")); mp = cv(of.get("zero_mput"))
    if lg:
        lines.append(f"OLVL|Long γ|{_fmt(lg)}|1E90FF|solid|2")
    if sg:
        lines.append(f"OLVL|Short γ|{_fmt(sg)}|FF7F00|solid|2")
    if mc:
        lines.append(f"OLVL|Call Wall|{_fmt(mc)}|00C800|solid|2")
    if mp:
        lines.append(f"OLVL|Put Wall|{_fmt(mp)}|FF3232|solid|2")

    for m in _OF_METRICS:
        val = _num(of.get(m))
        if val is not None:
            lines.append(f"OF|{m}|{_fmt(val)}")

    return "\n".join(lines)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=config.SERVER_HOST,
                port=config.SERVER_PORT, reload=False)
