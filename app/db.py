# -*- coding: utf-8 -*-
"""
MySQL subscription verification (Hostinger).

The `users` table needs at least:
    license_key VARCHAR   status VARCHAR('active'/...)   expires_at DATETIME
See schema.sql for a ready-to-run definition.
"""
import logging
from datetime import datetime

import mysql.connector
from mysql.connector import pooling

from . import config

log = logging.getLogger("db")
_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="gexpool",
            pool_size=3,
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            database=config.DB_NAME,
            connection_timeout=8,
        )
    return _pool


def verify_key(license_key: str) -> dict:
    """Blocking DB check. Returns {"ok","reason","expires_at"}.
    reason: OK | NO_KEY | INVALID | INACTIVE | EXPIRED | DB_ERROR
    Called from FastAPI via asyncio.to_thread so it never blocks the loop."""
    if not license_key:
        return {"ok": False, "reason": "NO_KEY", "expires_at": None}

    conn = None
    try:
        conn = _get_pool().get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT status, expires_at FROM users WHERE license_key=%s LIMIT 1",
            (license_key,),
        )
        row = cur.fetchone()
        cur.close()

        if not row:
            return {"ok": False, "reason": "INVALID", "expires_at": None}

        status = (row.get("status") or "").lower()
        exp = row.get("expires_at")

        if status != "active":
            return {"ok": False, "reason": "INACTIVE",
                    "expires_at": str(exp) if exp else None}
        if exp is not None and exp < datetime.now():
            return {"ok": False, "reason": "EXPIRED", "expires_at": str(exp)}
        return {"ok": True, "reason": "OK", "expires_at": str(exp) if exp else None}

    except Exception as e:  # noqa: BLE001
        log.error("DB error verifying key: %s", e)
        return {"ok": False, "reason": "DB_ERROR", "expires_at": None}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
