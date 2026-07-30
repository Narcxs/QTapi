# -*- coding: utf-8 -*-
"""
HWID license store for the ConvexValue desktop app (shared by the API, which
reads it for /cv-auth, and the Telegram bot, which manages it - admin only).

Same design as tokens.py: JSON file, atomic writes, mtime-cached reads.

File format (data/hwids.json):
{
  "devices": {
    "HWID-STRING": {"active": true, "added_at": 1753283590}
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
    return os.path.join(config.DATA_DIR, "hwids.json")


def _load_raw() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("devices", {})
            return data
    except (FileNotFoundError, ValueError, OSError):
        return {"devices": {}}


def _load_cached() -> dict:
    p = _path()
    try:
        m = os.stat(p).st_mtime
    except (FileNotFoundError, OSError):
        return {"devices": {}}
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
# read (used by the API - hot path, cached)
# --------------------------------------------------------------------------- #
def is_active(hwid: str) -> bool:
    rec = _load_cached().get("devices", {}).get(hwid)
    return bool(rec and rec.get("active"))


# --------------------------------------------------------------------------- #
# write (used by the bot - admin only)
# --------------------------------------------------------------------------- #
def add(hwid: str) -> None:
    """Add a device (or re-activate an existing one)."""
    with _lock:
        data = _load_raw()
        data["devices"][hwid] = {"active": True, "added_at": int(time.time())}
        _save(data)


def remove(hwid: str) -> bool:
    """Delete a device entirely. Returns True if it existed."""
    with _lock:
        data = _load_raw()
        if hwid in data["devices"]:
            del data["devices"][hwid]
            _save(data)
            return True
        return False


def list_all():
    """[(hwid, rec), ...] newest first."""
    items = list(_load_cached().get("devices", {}).items())
    return sorted(items, key=lambda kv: kv[1].get("added_at", 0), reverse=True)
