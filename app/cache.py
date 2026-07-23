# -*- coding: utf-8 -*-
"""A tiny thread-safe TTL (time-to-live) in-memory cache."""
import time
import threading


class TTLCache:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        """Return (value, is_fresh). value is None if the key was never set."""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None, False
            value, expiry = item
            return value, time.monotonic() < expiry

    def set(self, key, value, ttl):
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)

    def get_stale(self, key):
        with self._lock:
            item = self._data.get(key)
            return item[0] if item else None
