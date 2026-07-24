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
def validate(token: str) -> dict:
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
    return {"ok": True, "reason": "OK", "expires_at": exp,
            "telegram_id": rec.get("telegram_id")}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fmt_exp(rec: dict) -> str:
    exp = rec.get("expires_at")
    if not exp:
        return "never"
    return datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def create_or_get(telegram_id: int, username: str, days: int):
    """Return the user's existing active token, or create a new one."""
    with _lock:
        data = _load_raw()
        toks = data["tokens"]
        for t, rec in toks.items():
            if rec.get("telegram_id") == telegram_id and _active(rec):
                return t, rec
        return _issue(data, telegram_id, username, days)


def renew(telegram_id: int, username: str, days: int):
    """Revoke any existing tokens for the user and issue a fresh one."""
    with _lock:
        data = _load_raw()
        for t, rec in data["tokens"].items():
            if rec.get("telegram_id") == telegram_id:
                rec["revoked"] = True
        return _issue(data, telegram_id, username, days)


def _issue(data: dict, telegram_id: int, username: str, days: int):
    token = "qt_" + secrets.token_urlsafe(24)
    rec = {
        "telegram_id": telegram_id,
        "username": username,
        "created_at": _now(),
        "expires_at": _now() + days * 86400,
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


def list_all():
    return list(_load_cached().get("tokens", {}).items())


def stats() -> dict:
    total = active = expired = revoked = 0
    for rec in _load_cached().get("tokens", {}).values():
        total += 1
        if rec.get("revoked"):
            revoked += 1
        elif rec.get("expires_at") and _now() > rec["expires_at"]:
            expired += 1
        else:
            active += 1
    return {"total": total, "active": active, "expired": expired, "revoked": revoked}
