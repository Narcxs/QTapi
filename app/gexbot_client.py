# -*- coding: utf-8 -*-
"""
GEXBot API client (official endpoints, Bearer auth) + in-memory cache
+ futures conversion. The poller calls refresh() which fetches, caches AND
writes a JSON file (see storage.py).

Endpoints used:
  classic    /{TICKER}/classic/{period}
  state      /{TICKER}/state/{period}
  orderflow  /{TICKER}/orderflow/orderflow
  conversion /v2/futures/conversion   (on api.gex.bot)
"""
import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

import httpx

from . import config, storage, store
from .cache import TTLCache

log = logging.getLogger("gexbot")

_cache = TTLCache()        # raw payloads (used to build the indicator text feeds)
_conv_cache = TTLCache()   # futures conversion params

# One shared client (connection pool reused across all fetches). Creating a new
# AsyncClient per request is expensive and leaks sockets under load.
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {config.GEXBOT_API_KEY}",
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    }


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    headers=_headers(),
                    limits=httpx.Limits(max_connections=100,
                                        max_keepalive_connections=50),
                )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _num(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


async def _get(base: str, path: str, params: dict = None) -> Any:
    if not config.GEXBOT_API_KEY:
        raise RuntimeError("GEXBOT_API_KEY is empty - set it in server/.env")
    client = await get_client()
    r = await client.get(f"{base}{path}", params=params or {})
    r.raise_for_status()
    return r.json()


def _write_safe(env: dict) -> None:
    try:
        storage.write_envelope(env)
    except Exception as e:  # noqa: BLE001
        log.warning("file write error %s/%s: %s",
                    env.get("package"), env.get("ticker"), e)


def _publish(kind: str, ticker: str, period, raw,
             variant: str = "", meta: dict = None) -> None:
    """Update the in-memory hot store + persist the JSON file (off the loop)."""
    env = storage.build_envelope(
        ticker, kind, None if kind == "orderflow" else period, raw, variant, meta)
    store.MEM.put(env)                       # instant, in-memory (hot read path)
    # file write is offloaded to a thread so it never blocks request handling
    try:
        asyncio.get_running_loop().run_in_executor(None, _write_safe, env)
    except RuntimeError:
        _write_safe(env)                     # no running loop (e.g. tests)


# --------------------------------------------------------------------------- #
# futures conversion (SPX->ES, NDX->NQ, ...)
# --------------------------------------------------------------------------- #
async def get_conversion(ticker: str, future: str) -> Dict[str, Any]:
    key = f"{ticker}:{future}".upper()
    cached, fresh = _conv_cache.get(key)
    if fresh:
        return cached
    try:
        data = await _get(config.GEXBOT_ALT_BASE_URL, "/v2/futures/conversion",
                          {"ticker": ticker, "future": future, "model": "additive"})
        conv = {
            "multiplier": _num(data.get("multiplier")) or 1.0,
            "additive": _num(data.get("additive")) or 0.0,
            "contract": data.get("future_contract"),
        }
        _conv_cache.set(key, conv, config.CONV_CACHE_TTL)
        return conv
    except Exception as e:  # noqa: BLE001
        log.warning("conversion %s/%s error: %s", ticker, future, e)
        return _conv_cache.get_stale(key) or {"multiplier": 1.0, "additive": 0.0, "contract": None}


def convert(v: Optional[float], conv: Optional[dict]) -> Optional[float]:
    if v is None or conv is None:
        return v
    return v * conv["multiplier"] + conv["additive"]


# available Greek profiles (upstream: /{ticker}/state/{greek}_{period})
_GREEKS = {"delta", "gamma", "vanna", "charm"}

# price fields to convert per package (gex/flow values are NOT prices)
_PRICE_FIELDS = {
    "classic": ["spot", "zero_gamma", "major_pos_vol", "major_pos_oi",
                "major_neg_vol", "major_neg_oi"],
    "state": ["spot", "zero_gamma", "major_pos_vol", "major_pos_oi",
              "major_neg_vol", "major_neg_oi"],
    "orderflow": ["spot", "z_mlgamma", "z_msgamma", "o_mlgamma", "o_msgamma",
                  "zero_mcall", "zero_mput", "one_mcall", "one_mput"],
}
# Greek profiles share these price fields
_GREEK_PRICE_FIELDS = ["spot", "major_positive", "major_negative",
                       "major_long_gamma", "major_short_gamma"]


def convert_payload(raw: dict, kind: str, conv: dict) -> dict:
    """Return a NEW payload with every price/strike moved onto the futures scale.
    Zero / missing values are left untouched (they mean 'n/a', not a real price)."""
    if not isinstance(raw, dict) or conv is None:
        return raw
    out = dict(raw)

    fields = _GREEK_PRICE_FIELDS if kind in _GREEKS else _PRICE_FIELDS.get(kind, [])
    for f in fields:
        v = _num(out.get(f))
        if v:                                     # skip None and 0 (=> n/a)
            out[f] = convert(v, conv)

    # per-strike prices: classic/state use "strikes", greeks use "mini_contracts"
    list_key = "mini_contracts" if kind in _GREEKS else "strikes"
    if isinstance(out.get(list_key), list):
        new = []
        for s in out[list_key]:
            if isinstance(s, (list, tuple)) and s:
                s2 = list(s)
                c = convert(_num(s2[0]), conv)
                if c is not None:
                    s2[0] = c
                new.append(s2)
            else:
                new.append(s)
        out[list_key] = new
    return out


# --------------------------------------------------------------------------- #
# fetch / refresh / cached read
# --------------------------------------------------------------------------- #
async def _do_fetch(kind: str, ticker: str, period: str) -> Any:
    if kind == "classic":
        return await _get(config.GEXBOT_BASE_URL, f"/{ticker}/classic/{period}")
    if kind == "state":
        return await _get(config.GEXBOT_BASE_URL, f"/{ticker}/state/{period}")
    if kind == "orderflow":
        return await _get(config.GEXBOT_BASE_URL, f"/{ticker}/orderflow/orderflow")
    if kind in _GREEKS:
        # e.g. /SPX/state/delta_zero
        return await _get(config.GEXBOT_BASE_URL, f"/{ticker}/state/{kind}_{period}")
    raise ValueError(f"unknown kind {kind}")


def _key(kind: str, ticker: str, period: str) -> str:
    return f"{kind}:{ticker}:{period}".upper()


async def refresh(kind: str, ticker: str, period: str, ttl: int) -> bool:
    """Force a fetch from GEXBot: update cache + memory store + JSON file.
    Used by the poller every POLL_INTERVAL seconds."""
    try:
        raw = await _do_fetch(kind, ticker, period)
        _cache.set(_key(kind, ticker, period), raw, ttl)
        _publish(kind, ticker, period, raw)                 # index scale
        # futures-converted variant (pre-computed so /api?future=1 is instant)
        fut = config.FUTURES_MAP.get(ticker.upper()) if config.AUTO_CONVERT else None
        if fut:
            conv = await get_conversion(ticker, fut)
            craw = convert_payload(raw, kind, conv)
            _publish(kind, ticker, period, craw, variant="fut", meta={
                "future": fut,
                "future_contract": conv.get("contract"),
                "multiplier": conv.get("multiplier"),
                "additive": conv.get("additive"),
            })
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("refresh %s %s/%s error: %s", kind, ticker, period, e)
        return False


async def _cached(kind: str, ticker: str, period: str, ttl: int
                  ) -> Tuple[Optional[dict], bool]:
    """Return (payload, stale). Serves the poller-warmed cache; only fetches
    itself if the key is missing/expired (e.g. a non-polled ticker). Falls back
    to the JSON file, then to a stale cache value."""
    key = _key(kind, ticker, period)
    cached, fresh = _cache.get(key)
    if fresh:
        return cached, False
    try:
        raw = await _do_fetch(kind, ticker, period)
        _cache.set(key, raw, ttl)
        _publish(kind, ticker, period, raw)
        return raw, False
    except Exception as e:  # noqa: BLE001
        log.warning("fetch %s %s/%s error: %s", kind, ticker, period, e)
        stale = _cache.get_stale(key)
        if stale is not None:
            return stale, True
        env = storage.read(ticker, kind, None if kind == "orderflow" else period)
        return (env.get("data") if env else None), True


async def get_classic(ticker: str, period: str):
    return await _cached("classic", ticker, period, config.GEX_CACHE_TTL)


async def get_state(ticker: str, period: str):
    return await _cached("state", ticker, period, config.GEX_CACHE_TTL)


async def get_orderflow(ticker: str):
    return await _cached("orderflow", ticker, "-", config.ORDERFLOW_CACHE_TTL)


async def get_greek(kind: str, ticker: str, period: str):
    """Greek profile (delta/gamma/vanna/charm). Only 'zero' and 'one' exist
    upstream - anything else falls back to 'zero'."""
    if period not in ("zero", "one"):
        period = "zero"
    return await _cached(kind, ticker, period, config.GEX_CACHE_TTL)


async def get_tickers():
    try:
        return await _get(config.GEXBOT_BASE_URL, "/tickers"), False
    except Exception as e:  # noqa: BLE001
        log.warning("tickers error: %s", e)
        return None, True
