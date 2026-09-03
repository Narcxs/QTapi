# -*- coding: utf-8 -*-
"""
MenthorQ access tokens (mq_...) - separate from the QTapi qt_ tokens.

These are created ONLY by the admin via the Telegram bot ("Create MenthorQ
Token" button, 1 week / 2 weeks / 1 month) and are used by the NinjaTrader
MenthorQ indicator to authenticate against /menthorq/levels.

Same design as tokens.py: JSON file, atomic writes, mtime-cached reads.

File format (data/mq_tokens.json):
{
  "tokens": {
    "mq_xxx": {"created_at":..,"expires_at":..,"revoked":false,"label":"..."}
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

_lock = threading.Lock()
_cache = {"data": None, "mtime": None}


def _now() -> int:
    return int(time.time())


def _path() -> str:
    return os.path.join(config.DATA_DIR, "mq_tokens.json")


def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("tokens", {})
            return data
    except (FileNotFoundError, ValueError, OSError):
        return {"tokens": {}}


def _load_cached() -> dict:
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
    _cache["data"] = None


def _active(rec: dict) -> bool:
    if rec.get("revoked"):
        return False
    exp = rec.get("expires_at")
    return not exp or _now() <= exp


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
    return {"ok": True, "reason": "OK", "expires_at": exp}


# --------------------------------------------------------------------------- #
# write (used by the bot)
# --------------------------------------------------------------------------- #
def get_user_trial(telegram_id: int):
    """Return the user's existing trial token record (with 'token' key) or None."""
    for t, rec in _load_cached().get("tokens", {}).items():
        if rec.get("telegram_id") == telegram_id:
            r = dict(rec)
            r["token"] = t
            return r
    return None


def create_trial(telegram_id: int, username: str, days: int = 14):
    """Issue a 1-time trial token for a telegram user. If they already had one, return None."""
    with _lock:
        data = _load_raw()
        toks = data["tokens"]
        for t, rec in toks.items():
            if rec.get("telegram_id") == telegram_id:
                return None, rec
        token = "mq_" + secrets.token_urlsafe(24)
        rec = {
            "telegram_id": telegram_id,
            "username": username,
            "created_at": _now(),
            "expires_at": _now() + days * 86400,
            "revoked": False,
            "label": f"Trial (@{username})",
        }
        toks[token] = rec
        _save(data)
        return token, rec


def create(days: int, label: str = "", telegram_id: int = None, username: str = None):
    """Issue a fresh mq_ token valid `days` days."""
    with _lock:
        data = _load_raw()
        token = "mq_" + secrets.token_urlsafe(24)
        rec = {
            "created_at": _now(),
            "expires_at": _now() + days * 86400,
            "revoked": False,
            "label": label,
        }
        if telegram_id:
            rec["telegram_id"] = telegram_id
        if username:
            rec["username"] = username
        data["tokens"][token] = rec
        _save(data)
        return token, rec


def revoke(token: str) -> bool:
    with _lock:
        data = _load_raw()
        if token in data["tokens"]:
            data["tokens"][token]["revoked"] = True
            _save(data)
            return True
        return False


def list_all():
    return list(_load_cached().get("tokens", {}).items())


def fmt_exp(rec: dict) -> str:
    exp = rec.get("expires_at")
    if not exp:
        return "never"
    return datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
