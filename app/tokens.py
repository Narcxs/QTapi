# -*- coding: utf-8 -*-
"""
Per-user API token store (shared by the API and the Telegram bot).

- One active token per Telegram user.
- Tokens expire (default 7 days) and can be revoked.
- Writes are atomic (.tmp + os.replace). The bot is the writer; the API only
  reads via validate(), which caches the file and reloads on mtime change so it
  stays fast under heavy load.

File format (data/tokens.json):
{
  "tokens": {
    "qt_xxx": {"telegram_id":123,"username":"joe","created_at":..,"expires_at":..,"revoked":false}
  }
}
"""
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()            # serialize writes within a process
_cache = {"data": None, "mtime": None}


def _now() -> int:
    return int(time.time())


def _path() -> str:
    return config.TOKENS_FILE


def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("tokens", {})
            return data
    except (FileNotFoundError, ValueError, OSError):
        return {"tokens": {}}


def _load_cached() -> dict:
    """Fast read for validate(): reload only when the file changed."""
    p = _path()
    try:
        m = os.stat(p).st_mtime
    except (FileNotFoundError, OSError):
        return {"tokens": {}}
    if _cache["data"] is None or _cache["mtime"] != m:
        _cache["data"] = _load_raw()
        _cache["mtime"] = m
    return _cache["data"]


def _save(data: dict) -> None:
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    _cache["data"] = None            # invalidate our own cache


# --------------------------------------------------------------------------- #
# read (used by the API)
# --------------------------------------------------------------------------- #
def validate(token: str, ticker: str = None, future: str = None, client_ip: str = None) -> dict:
    if not token:
        return {"ok": False, "reason": "NO_TOKEN"}
    rec = _load_cached().get("tokens", {}).get(token)
    if not rec:
        return {"ok": False, "reason": "INVALID"}
    if rec.get("revoked"):
        return {"ok": False, "reason": "REVOKED"}
    exp = rec.get("expires_at")
    if exp and _now() > exp:
        return {"ok": False, "reason": "EXPIRED"}
    
    tier = rec.get("tier", "free")
    
    # 1-IP Lock for free tokens
    if tier == "free" and client_ip:
        bound_ip = rec.get("bound_ip")
        if not bound_ip:
            # Bind to first calling IP
            with _lock:
                data = _load_raw()
                if token in data.get("tokens", {}):
                    if not data["tokens"][token].get("bound_ip"):
                        data["tokens"][token]["bound_ip"] = client_ip
                        _save(data)
                        rec["bound_ip"] = client_ip
        elif bound_ip != client_ip:
            return {"ok": False, "reason": "IP_MISMATCH", "tier": tier,
                    "expires_at": exp, "telegram_id": rec.get("telegram_id")}

    # Resolve aliases (e.g. ES_SPX -> SPX, GC_GLD -> GLD)
    if ticker:
        t_up = ticker.strip().upper()
        if t_up in config.TICKER_ALIASES:
            t_base, auto_fut = config.TICKER_ALIASES[t_up]
            ticker = t_base
            if not future:
                future = auto_fut

    # Check premium-only tickers and futures
    if ticker:
        t = ticker.strip().upper()
        if t in config.PREMIUM_TICKERS and tier != "premium":
            return {"ok": False, "reason": "PREMIUM_REQUIRED", "tier": tier,
                    "expires_at": exp, "telegram_id": rec.get("telegram_id")}
    if future:
        f = future.strip().upper()
        if f in {"GC", "VX"} and tier != "premium":
            return {"ok": False, "reason": "PREMIUM_REQUIRED", "tier": tier,
                    "expires_at": exp, "telegram_id": rec.get("telegram_id")}

    return {"ok": True, "reason": "OK", "tier": tier, "expires_at": exp,
            "telegram_id": rec.get("telegram_id")}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fmt_exp(rec: dict) -> str:
    exp = rec.get("expires_at")
    if not exp:
        return "Permanent (no expiry)"
    return datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fmt_tier(rec: dict) -> str:
    return "⭐ Premium" if rec.get("tier") == "premium" else "🆓 Free"


def _active(rec: dict) -> bool:
    if rec.get("revoked"):
        return False
    exp = rec.get("expires_at")
    return not exp or _now() <= exp


# --------------------------------------------------------------------------- #
# write (used by the bot)
# --------------------------------------------------------------------------- #
def get_user(telegram_id: int):
    """Return the user's active token record (with 'token' key) or None."""
    best = None
    for t, rec in _load_cached().get("tokens", {}).items():
        if rec.get("telegram_id") == telegram_id and _active(rec):
            r = dict(rec)
            r["token"] = t
            if best is None or r.get("created_at", 0) > best.get("created_at", 0):
                best = r
    return best


def get_user_any(telegram_id: int):
    """Return the user's most recent token record (ANY status: active, expired
    or revoked, with 'token' key) or None if they never had one."""
    best = None
    for t, rec in _load_cached().get("tokens", {}).items():
        if rec.get("telegram_id") == telegram_id:
            r = dict(rec)
            r["token"] = t
            if best is None or r.get("created_at", 0) > best.get("created_at", 0):
                best = r
    return best


def create_or_get(telegram_id: int, username: str, days: int = 7, tier: str = "free"):
    """Return the user's existing active token, or create a new one."""
    with _lock:
        data = _load_raw()
        toks = data["tokens"]
        for t, rec in toks.items():
            if rec.get("telegram_id") == telegram_id and _active(rec):
                # If requesting premium but currently only free, upgrade
                if tier == "premium" and rec.get("tier") != "premium":
                    rec["tier"] = "premium"
                    rec["expires_at"] = None
                    _save(data)
                    return t, rec
                return t, rec
        return _issue(data, telegram_id, username, days, tier)


def can_renew_premium(telegram_id: int) -> tuple:
    """Check if a premium user can regenerate their token (limit: 3 per 7 days).
    Returns (allowed: bool, remaining_renewals: int, resets_in_sec: int).
    """
    with _lock:
        data = _load_raw()
        now = _now()
        history = [
            t for t, r in data.get("tokens", {}).items()
            if r.get("telegram_id") == telegram_id and r.get("tier") == "premium"
        ]
        # Filter timestamps from the last 7 days (604800 seconds)
        recent = [
            data["tokens"][t]["created_at"] for t in history
            if now - data["tokens"][t].get("created_at", 0) < 7 * 86400
        ]
        # Max 3 renewals per rolling 7-day window (initial creation doesn't count against limit, but we track all recent creations)
        # To be user friendly: max 3 NEW tokens created in a 7-day rolling window
        count = len(recent)
        max_allowed = 3
        if count >= max_allowed:
            oldest = min(recent)
            resets_in = (oldest + 7 * 86400) - now
            return False, 0, max(1, resets_in)
        return True, max_allowed - count, 0


def renew_premium(telegram_id: int, username: str) -> tuple:
    """Revoke existing active tokens and issue a fresh Premium token with qt_prem_ prefix."""
    with _lock:
        data = _load_raw()
        for t, rec in data["tokens"].items():
            if rec.get("telegram_id") == telegram_id:
                rec["revoked"] = True
        token, rec = _issue(data, telegram_id, username, days=0, tier="premium")
        return token, rec


def grant_premium(telegram_id: int, username: str):
    """Revoke previous and grant an indefinite Premium token."""
    with _lock:
        data = _load_raw()
        for t, rec in data["tokens"].items():
            if rec.get("telegram_id") == telegram_id:
                rec["revoked"] = True
        return _issue(data, telegram_id, username, days=0, tier="premium")


def grant_free(telegram_id: int, username: str, days: int = 7):
    """Revoke previous and grant a Free token with specified days."""
    with _lock:
        data = _load_raw()
        for t, rec in data["tokens"].items():
            if rec.get("telegram_id") == telegram_id:
                rec["revoked"] = True
        return _issue(data, telegram_id, username, days=days, tier="free")


def renew(telegram_id: int, username: str, days: int = 7, tier: str = "free"):
    """Revoke any existing tokens for the user and issue a fresh one."""
    with _lock:
        data = _load_raw()
        for t, rec in data["tokens"].items():
            if rec.get("telegram_id") == telegram_id:
                rec["revoked"] = True
        return _issue(data, telegram_id, username, days, tier)


def _issue(data: dict, telegram_id: int, username: str, days: int = 7, tier: str = "free"):
    prefix = "qt_prem_" if tier == "premium" else "qt_"
    token = prefix + secrets.token_urlsafe(24)
    exp = None if tier == "premium" else (_now() + days * 86400)
    rec = {
        "telegram_id": telegram_id,
        "username": username,
        "tier": tier,
        "created_at": _now(),
        "expires_at": exp,
        "revoked": False,
    }
    data["tokens"][token] = rec
    _save(data)
    return token, rec


def revoke_token(token: str) -> bool:
    with _lock:
        data = _load_raw()
        if token in data["tokens"]:
            data["tokens"][token]["revoked"] = True
            _save(data)
            return True
        return False


def revoke_user(telegram_id: int) -> int:
    with _lock:
        data = _load_raw()
        n = 0
        for rec in data["tokens"].values():
            if rec.get("telegram_id") == telegram_id and not rec.get("revoked"):
                rec["revoked"] = True
                n += 1
        if n:
            _save(data)
        return n


def list_all(tier_filter: str = None, only_active: bool = False):
    items = list(_load_cached().get("tokens", {}).items())
    # Sort by created_at descending (most recent first)
    items.sort(key=lambda x: x[1].get("created_at", 0), reverse=True)
    if tier_filter:
        items = [x for x in items if x[1].get("tier", "free") == tier_filter]
    if only_active:
        items = [x for x in items if _active(x[1])]
    return items


def stats() -> dict:
    total = active = expired = revoked = premium = free = 0
    for rec in _load_cached().get("tokens", {}).values():
        total += 1
        is_prem = rec.get("tier") == "premium"
        if is_prem:
            premium += 1
        else:
            free += 1
        if rec.get("revoked"):
            revoked += 1
        elif rec.get("expires_at") and _now() > rec["expires_at"]:
            expired += 1
        else:
            active += 1
    return {
        "total": total, "active": active, "expired": expired,
        "revoked": revoked, "premium": premium, "free": free
    }
