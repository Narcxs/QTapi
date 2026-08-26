# -*- coding: utf-8 -*-
"""
Warning system for the Telegram group.

The admin replies to a user's message with /warn -> the user is warned and
notified. At WARN_THRESHOLD warnings the bot bans the user from the group for
WARN_BAN_DAYS day(s) (timed ban via until_date, Telegram lifts it alone) and
the counter resets.

Persistent JSON store (data/warns.json), same design as tokens.py:
{
  "users": {
    "123456": {"warns": [{"at":..,"by":..,"reason":".."}], "bans": 1}
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
    return os.path.join(config.DATA_DIR, "warns.json")


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


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def count(user_id: int) -> int:
    u = _load_cached().get("users", {}).get(str(user_id), {})
    return len(u.get("warns", []))


def get(user_id: int) -> dict:
    return _load_cached().get("users", {}).get(str(user_id),
                                               {"warns": [], "bans": 0})


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def add(user_id: int, by: int, reason: str = "") -> int:
    """Add a warning; returns the new count."""
    with _lock:
        data = _load_raw()
        u = data["users"].setdefault(str(user_id), {"warns": [], "bans": 0})
        u["warns"].append({"at": int(time.time()), "by": by, "reason": reason})
        _save(data)
        return len(u["warns"])


def reset(user_id: int, banned: bool = False) -> None:
    """Clear the warnings (after a ban, or manually). Optionally count the ban."""
    with _lock:
        data = _load_raw()
        u = data["users"].setdefault(str(user_id), {"warns": [], "bans": 0})
        u["warns"] = []
        if banned:
            u["bans"] = u.get("bans", 0) + 1
        _save(data)


def remove_last(user_id: int) -> int:
    """Remove the most recent warning; returns the remaining count."""
    with _lock:
        data = _load_raw()
        u = data["users"].setdefault(str(user_id), {"warns": [], "bans": 0})
        if u["warns"]:
            u["warns"].pop()
        _save(data)
        return len(u["warns"])
