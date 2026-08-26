# -*- coding: utf-8 -*-
"""
Central configuration. All secrets come from server/.env (never hard-coded).
Copy server/.env.example to server/.env and fill in your real values.

  >>> Put your GEXBot API key in server/.env  ->  GEXBOT_API_KEY=...
"""
import os
from dotenv import load_dotenv

# .../server  (one level above this app/ package)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Load .env.example as defaults first, then let a real .env override it. This
# way the server works whether you filled in .env OR .env.example.
load_dotenv(os.path.join(BASE_DIR, ".env.example"))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

# ---------------------------------------------------------------------------
# GEXBot API  (Bearer auth; stays on the server only)
# ---------------------------------------------------------------------------
# .strip() removes stray spaces / newlines that would make an illegal header.
GEXBOT_API_KEY = os.getenv("GEXBOT_API_KEY", "").strip()
GEXBOT_BASE_URL = os.getenv("GEXBOT_BASE_URL", "https://api.gexbot.com")
GEXBOT_ALT_BASE_URL = os.getenv("GEXBOT_ALT_BASE_URL", "https://api.gex.bot")
USER_AGENT = os.getenv("USER_AGENT", "GexBotNinjaTrader/1.0")
DEFAULT_PERIOD = os.getenv("DEFAULT_PERIOD", "zero")

# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------
POLL_ENABLED = os.getenv("POLL_ENABLED", "1") == "1"
POLLED_TICKERS = [t.strip().upper()
                  for t in os.getenv("POLLED_TICKERS", "SPX,SPY,NDX,QQQ").split(",")
                  if t.strip()]
POLL_PERIODS = [p.strip()
                for p in os.getenv("POLL_PERIODS", "zero,full,one").split(",")
                if p.strip()]
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))

# Greek profiles (delta/gamma/vanna/charm). Only 'zero' and 'one' exist upstream.
POLL_GREEKS = os.getenv("POLL_GREEKS", "1") == "1"
GREEKS = [g.strip() for g in os.getenv("GREEKS", "delta,gamma,vanna,charm").split(",")
          if g.strip()]
GREEK_PERIODS = [p.strip() for p in os.getenv("GREEK_PERIODS", "zero,one").split(",")
                 if p.strip()]

# ---------------------------------------------------------------------------
# US market hours (New York time, DST handled automatically)
# ---------------------------------------------------------------------------
# 1 = the poller only fetches GEXBot inside the session window:
#     [MARKET_OPEN_ET - PRE_OPEN_MINUTES -> MARKET_CLOSE_ET], Monday-Friday.
# The HTTP API stays up outside the window and serves the last cached data.
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "1") == "1"
MARKET_OPEN_ET = os.getenv("MARKET_OPEN_ET", "09:30")
MARKET_CLOSE_ET = os.getenv("MARKET_CLOSE_ET", "16:00")
PRE_OPEN_MINUTES = int(os.getenv("PRE_OPEN_MINUTES", "2"))

# ---------------------------------------------------------------------------
# Futures conversion
# ---------------------------------------------------------------------------
# When on, the poller ALSO pre-computes a futures-scaled copy of every mapped
# ticker (SPX->ES, etc.) so /api/...?future=1 is instant (zero per-request cost).
AUTO_CONVERT = os.getenv("AUTO_CONVERT", "1") == "1"
# Official source-ticker -> futures product mapping (from GEXBot docs).
FUTURES_MAP = {
    "SPX": "ES", "SPY": "ES",
    "NDX": "NQ", "QQQ": "NQ",
    "RUT": "RTY", "IWM": "RTY",
    "DIA": "YM",
    "GLD": "GC",
    "USO": "CL",
}

# ---------------------------------------------------------------------------
# JSON file storage  (each fetch is written here separately)
# ---------------------------------------------------------------------------
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))

# ---------------------------------------------------------------------------
# Public data API (/api/...)  -  optional shared token
# ---------------------------------------------------------------------------
# If empty, the /api endpoints are open. If set, callers must pass ?token=...
DATA_API_TOKEN = os.getenv("DATA_API_TOKEN", "").strip()

# ---------------------------------------------------------------------------
# Subscriptions (OPTIONAL) - off by default => free / open API, no MySQL needed
# ---------------------------------------------------------------------------
REQUIRE_SUBSCRIPTION = os.getenv("REQUIRE_SUBSCRIPTION", "0") == "1"

# Hostinger MySQL (only used when REQUIRE_SUBSCRIPTION=1)
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "")

# ---------------------------------------------------------------------------
# Caching / rate limiting (seconds)
# ---------------------------------------------------------------------------
GEX_CACHE_TTL = int(os.getenv("GEX_CACHE_TTL", "15"))
ORDERFLOW_CACHE_TTL = int(os.getenv("ORDERFLOW_CACHE_TTL", "3"))
CONV_CACHE_TTL = int(os.getenv("CONV_CACHE_TTL", "600"))
SUB_CACHE_TTL = int(os.getenv("SUB_CACHE_TTL", "300"))

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8787"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Per-user API tokens
# ---------------------------------------------------------------------------
# When on, every /api request must carry a valid ?token=... (one token = one
# user, issued by the Telegram bot). Turn it on AFTER your bot is running.
REQUIRE_TOKEN = os.getenv("REQUIRE_TOKEN", "0") == "1"
# File where issued tokens are stored (shared between the API and the bot).
TOKENS_FILE = os.getenv("TOKENS_FILE", os.path.join(DATA_DIR, "tokens.json"))
# Public URL shown to users by the bot.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", f"http://127.0.0.1:{SERVER_PORT}")
# How long an issued token is valid (days).
TOKEN_VALID_DAYS = int(os.getenv("TOKEN_VALID_DAYS", "7"))

# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
TELEGRAM_GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID", "0") or 0)
# Public invite link shown on the "Join" button. If empty, the bot tries to
# fetch/create one automatically (needs the bot to be group admin).
TELEGRAM_GROUP_LINK = os.getenv("TELEGRAM_GROUP_LINK", "").strip()
# Public channel shown as a "Main Channel" button in the bot menus.
TELEGRAM_CHANNEL_LINK = os.getenv("TELEGRAM_CHANNEL_LINK", "").strip()

# ---------------------------------------------------------------------------
# ConvexValue shared account (returned by POST /cv-auth to licensed HWIDs)
# ---------------------------------------------------------------------------
CV_SHARED_EMAIL = os.getenv("CV_SHARED_EMAIL", "").strip()
CV_SHARED_PASSWORD = os.getenv("CV_SHARED_PASSWORD", "").strip()

# ---------------------------------------------------------------------------
# MenthorQ levels (api.menthorq.io) - fetched every MENTHORQ_POLL_INTERVAL
# seconds and shared as free text in the Telegram bot
# ---------------------------------------------------------------------------
MENTHORQ_API_KEY = os.getenv("MENTHORQ_API_KEY", "").strip()
MENTHORQ_BASE_URL = os.getenv("MENTHORQ_BASE_URL", "https://api.menthorq.io")
MENTHORQ_PLATFORM = os.getenv("MENTHORQ_PLATFORM", "atas")
MENTHORQ_TICKERS = [t.strip().upper()
                    for t in os.getenv("MENTHORQ_TICKERS", "ES,NQ,VIX,GC").split(",")
                    if t.strip()]
MENTHORQ_POLL_INTERVAL = int(os.getenv("MENTHORQ_POLL_INTERVAL", "600"))
