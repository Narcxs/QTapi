# -*- coding: utf-8 -*-
"""
Subscriber registry for bot broadcasts.

Every user who writes to the bot in private is recorded here (upsert on any
message). The admin can then /broadcast a message to all of them; users who
blocked the bot are flagged and skipped next time.

File format (data/subscribers.json):
{
  "users": {
    "123456": {"username": "joe", "first_seen": .., "last_seen": .., "blocked": false}
  }
}
"""
import json
import os
import threading
import time

from . import config

_lock = threading.Lock()
_cache = {"data": None, "mtime": None}


def _path() -> str:
    return os.path.join(config.DATA_DIR, "subscribers.json")


def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("users", {})
            return data
    except (FileNotFoundError, ValueError, OSError):
        return {"users": {}}


def _load_cached() -> dict:
    p = _path()
    try:
        m = os.stat(p).st_mtime
    except (FileNotFoundError, OSError):
        return {"users": {}}
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


def touch(user_id: int, username: str = "") -> None:
    """Record/refresh a subscriber (called on any private message to the bot)."""
    with _lock:
        data = _load_raw()
        u = data["users"].setdefault(
            str(user_id), {"username": "", "first_seen": int(time.time()),
                           "last_seen": 0, "blocked": False})
        if username:
            u["username"] = username
        u["last_seen"] = int(time.time())
        u["blocked"] = False               # they wrote us -> not blocked anymore
        _save(data)


def mark_blocked(user_id: int) -> None:
    with _lock:
        data = _load_raw()
        u = data["users"].get(str(user_id))
        if u is not None:
            u["blocked"] = True
            _save(data)


def list_active():
    """[(user_id, username), ...] of reachable subscribers."""
    out = []
    for uid, u in _load_cached().get("users", {}).items():
        if not u.get("blocked"):
            try:
                out.append((int(uid), u.get("username", "")))
            except (TypeError, ValueError):
                continue
    return out


def count() -> int:
    return len(list_active())
