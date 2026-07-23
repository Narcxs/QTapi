# -*- coding: utf-8 -*-
"""
In-memory, pre-serialized data store = the HOT read path for the /api endpoints.

Why: with 1000+ concurrent clients we must NOT open+parse a JSON file per
request. Instead the poller serializes each payload to bytes ONCE per cycle and
keeps it here; endpoints return those bytes with near-zero CPU.

Two modes, both handled transparently by get_bytes():
  * Single process (poller in the same process): reads hit memory only.
  * Extra web workers (POLL_ENABLED=0): memory is refreshed from the files the
    poller writes, using a cheap os.stat() check (re-parse only when changed).
"""
import json
import os
import threading
import time

from . import config, storage


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class MemoryStore:
    def __init__(self):
        self._mem = {}
        self._lock = threading.RLock()
        # In single-process mode the poller refreshes every POLL_INTERVAL, so
        # trust an authoritative entry for a few cycles without touching disk.
        self.hot_ttl = max(2.0, config.POLL_INTERVAL * 3)

    @staticmethod
    def _key(package: str, instrument: str, period, variant: str = "") -> str:
        period = None if package == "orderflow" else period
        return f"{package}:{instrument}:{period}:{variant}".upper()

    def put(self, env: dict) -> None:
        """Authoritative in-process update from the poller."""
        key = self._key(env["package"], env["ticker"],
                        env.get("period"), env.get("variant", ""))
        item = {
            "env": _dumps(env),
            "raw": _dumps(env.get("data")),
            "at": time.monotonic(),
            "authoritative": True,
            "mtime": None,
            "size": None,
        }
        with self._lock:
            self._mem[key] = item

    def _store_from_file(self, key, env, mtime, size):
        item = {
            "env": _dumps(env),
            "raw": _dumps(env.get("data")),
            "at": time.monotonic(),
            "authoritative": False,
            "mtime": mtime,
            "size": size,
        }
        with self._lock:
            self._mem[key] = item
        return item

    def get_bytes(self, package: str, instrument: str, period,
                  raw: bool = False, variant: str = ""):
        """Return pre-serialized JSON bytes, or None if unavailable.
        variant "" = index scale, "fut" = futures-converted scale."""
        field = "raw" if raw else "env"
        key = self._key(package, instrument, period, variant)
        now = time.monotonic()

        with self._lock:
            item = self._mem.get(key)

        # fast path: fresh authoritative (poller) entry -> no disk access at all
        if item is not None and item["authoritative"] and (now - item["at"] < self.hot_ttl):
            return item[field]

        # file-backed path (cold entry / stale / other worker): cheap stat check
        p = storage.path(instrument, package,
                         None if package == "orderflow" else period, variant)
        try:
            st = os.stat(p)
        except (FileNotFoundError, OSError):
            return item[field] if item is not None else None

        if (item is not None
                and item.get("mtime") == st.st_mtime
                and item.get("size") == st.st_size):
            return item[field]

        env = storage.read_path(p)
        if env is None:
            return item[field] if item is not None else None
        item = self._store_from_file(key, env, st.st_mtime, st.st_size)
        return item[field]


# process-wide singleton
MEM = MemoryStore()
