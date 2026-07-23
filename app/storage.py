# -*- coding: utf-8 -*-
"""
JSON file storage (persistence + crash/restart recovery + multi-worker sharing).

Every fetch is written to its OWN file:

    server/data/
      SPX/  classic_zero.json  state_full.json  orderflow.json  ...
      SPY/  NDX/  QQQ/  ...

Files are the durable copy. The HOT read path is store.py (in-memory bytes);
files are only touched on write, on cold start, or by extra web workers.
Writes are ATOMIC (.tmp + os.replace) so a reader never sees a partial file.
"""
import json
import os
import time

from . import config


def _dir(ticker: str) -> str:
    return os.path.join(config.DATA_DIR, ticker.upper())


def filename(package: str, period: str = None, variant: str = "") -> str:
    # variant "" = index scale, "fut" = futures-converted scale
    suffix = f"_{variant}" if variant else ""
    if package == "orderflow":
        return f"orderflow{suffix}.json"
    return f"{package}_{period}{suffix}.json"


def path(ticker: str, package: str, period: str = None, variant: str = "") -> str:
    return os.path.join(_dir(ticker), filename(package, period, variant))


def build_envelope(ticker: str, package: str, period, raw,
                   variant: str = "", meta: dict = None) -> dict:
    env = {
        "ticker": ticker.upper(),
        "package": package,
        "period": None if package == "orderflow" else period,
        "variant": variant,                              # "" or "fut"
        "scale": "future" if variant == "fut" else "index",
        "fetched_at": int(time.time()),
        "source_ts": raw.get("timestamp") if isinstance(raw, dict) else None,
        "data": raw,
    }
    if meta:
        env.update(meta)
    return env


def write_envelope(env: dict) -> str:
    p = path(env["ticker"], env["package"], env.get("period"), env.get("variant", ""))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"          # pid-unique tmp: safe across workers
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)                       # atomic on Windows + POSIX
    return p


def write(ticker: str, package: str, period, raw, variant: str = "") -> str:
    return write_envelope(build_envelope(ticker, package, period, raw, variant))


def read_path(p: str):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def read(ticker: str, package: str, period: str = None, variant: str = ""):
    return read_path(path(ticker, package, period, variant))


def status() -> dict:
    """Summary of every stored file: age in seconds and source timestamp."""
    out = {}
    now = int(time.time())
    if not os.path.isdir(config.DATA_DIR):
        return out
    for ticker in sorted(os.listdir(config.DATA_DIR)):
        tdir = os.path.join(config.DATA_DIR, ticker)
        if not os.path.isdir(tdir):
            continue
        files = {}
        for name in sorted(os.listdir(tdir)):
            if not name.endswith(".json"):
                continue
            env = read_path(os.path.join(tdir, name))
            if env is None:
                files[name] = {"age_s": None, "source_ts": None}
            else:
                files[name] = {"age_s": now - env.get("fetched_at", now),
                               "source_ts": env.get("source_ts")}
        out[ticker] = files
    return out
