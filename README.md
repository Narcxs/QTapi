# GexBot server

Its job: **fetch the GEXBot API and save every result to a JSON file, every 2 seconds.**

## Folder layout
```
server/
├── .env                 <-- YOU create this (copy of .env.example). API key goes here.
├── .env.example
├── requirements.txt
├── run.bat / run.sh     start the server (poller + API)
├── schema.sql           MySQL `users` table
├── data/                <-- generated JSON files (one per fetch)
│   ├── SPX/  SPY/  NDX/  QQQ/
│   │     classic_zero.json  classic_full.json  classic_one.json
│   │     state_zero.json    state_full.json    state_one.json
│   │     orderflow.json
└── app/
    ├── config.py        settings from .env (incl. DATA_DIR, poller options)
    ├── poller.py        the 2-second fetch+save loop  ← core job
    ├── gexbot_client.py GEXBot calls (Bearer) + cache + futures conversion
    ├── storage.py       atomic JSON file read/write
    ├── db.py            MySQL subscription check
    ├── cache.py         tiny in-memory TTL cache
    └── main.py          FastAPI endpoints
```

## Where do I put my API key?
In **`server/.env`** (create it by copying `.env.example`):
```
GEXBOT_API_KEY=your_real_key_here
```
It is sent to GEXBot as `Authorization: Bearer <key>` and never leaves the server.

## What gets saved
By default the poller fetches **SPX, SPY, NDX, QQQ**, for **all periods**
(`zero`, `full`, `one`), packages `classic` + `state`, plus `orderflow`:

> 4 tickers × (2 packages × 3 periods + 1 orderflow) = **28 JSON files**,
> rewritten every `POLL_INTERVAL` (2 s). Each file is one fetch, saved separately.

Each file is an envelope around the raw GEXBot response:
```json
{
  "ticker": "SPX",
  "package": "classic",
  "period": "zero",
  "fetched_at": 1753283590,
  "source_ts": 1753283590,
  "data": { ...exact GEXBot JSON... }
}
```
Writes are atomic (`.tmp` + rename), so a reader never sees a half-written file.

## Run
```
# 1. put your key + DB creds in .env
copy .env.example .env      (Windows)   |   cp .env.example .env  (Linux)
# 2. start
run.bat                     (Windows)   |   ./run.sh              (Linux)
```
On startup you'll see: `Poller started: tickers=[...] periods=[...] every 2.0s -> 28 files`.

## Check it's working
- `http://127.0.0.1:8787/health`  → poll config
- `http://127.0.0.1:8787/status`  → every file + its age in seconds (should stay < 4)
- Look in `server/data/SPX/` → the `.json` files appear and update.

## Data API — model `/api/{package}/{instrument}/{period}`
Clean REST routes that return the saved JSON files. **This is the main data API.**

| Endpoint | Returns |
|----------|---------|
| `GET /api` | index: lists every available URL |
| `GET /api/{package}/{instrument}/{period}` | one saved file |
| `GET /api/{package}/{instrument}` | orderflow (no period) or classic/state at DEFAULT_PERIOD |

- `package` = `classic` \| `state` \| `orderflow`
- `instrument` = `spx` \| `spy` \| `ndx` \| `qqq` \| …
- `period` = `zero` (0DTE) \| `full` (all expiries) \| `one` (next expiry) — ignored for orderflow

Examples (default config):
```
/api/classic/spx/zero     /api/state/ndx/full     /api/classic/qqq/one
/api/orderflow/spx        /api/orderflow/qqq
```
Query params:
- `?future=1` — **futures-scaled** data (SPX→ES, NDX→NQ, …), pre-computed by the
  poller so it costs nothing per request. Default (no param) = raw index scale.
- `?raw=1` — return only the raw GEXBot payload (drop the envelope metadata).
- `?token=...` — required only if `DATA_API_TOKEN` is set in `.env` (else open).

### Index scale vs futures scale
By default every ticker is returned on its **native index/ETF scale** (SPX ≈ 6326).
Add `?future=1` to get the **futures scale** (ES ≈ 6339) — spot, all levels and
every strike are shifted with GEXBot's `/v2/futures/conversion` (refreshed every
~10 min). Pairs: SPX/SPY→ES, NDX/QQQ→NQ, RUT/IWM→RTY, DIA→YM, GLD→GC, USO→CL.
Futures files are saved alongside the index ones, e.g. `classic_zero_fut.json`.

```
/api/classic/spx/zero            -> SPX index scale
/api/classic/spx/zero?future=1   -> same data on ES scale (contract e.g. ESU6)
```
Set `AUTO_CONVERT=0` in `.env` to disable pre-computing the futures variant.

## Other endpoints (NinjaTrader indicator + admin)
| Endpoint | For | Returns |
|----------|-----|---------|
| `/data?ticker=SPX&package=classic&period=zero&key=...` | raw file (subscription-gated) | the saved JSON envelope |
| `/overlay?ticker=SPX&key=...&period=zero&future=ES` | overlay indicator | levels + profiles (text, ES-converted) |
| `/orderflow?ticker=SPX&key=...&future=ES` | panel indicator | flow metrics + gamma levels (text) |
| `/verify?key=...` | subscription | `OK\|<expiry>` / `DENIED\|<reason>` |
| `/status` | admin | every saved file + its age (s) |

The `/overlay` and `/orderflow` feeds are built from the **same freshly-saved
data** (served from the warm cache), so the indicator always gets < 4-second-old
values without ever calling GEXBot directly.

## Performance & scaling (1000+ concurrent clients)
The `/api` read path is built to be crash-proof under load:
- **No disk I/O per request.** The poller serializes each payload to JSON bytes
  once per cycle and keeps them in memory (`app/store.py`); endpoints return
  those bytes directly (no file open, no re-serialization).
- **Shared HTTP client** with a connection pool (no socket leak under load).
- **Supervised poller** that restarts itself if it ever throws.
- **`Cache-Control: public, max-age=2`** on `/api` responses, so a reverse
  proxy / CDN / browser absorbs repeated hits — 1000 clients can collapse to a
  handful of origin reads.
- File writes are **offloaded to a thread**, so they never block the readers.

A single process already serves thousands of concurrent async connections.
To use more CPU cores, scale horizontally (all instances share the same
`data/` folder):

```
# 1 instance does the polling (writes the files):
POLL_ENABLED=1  python -m app.main          # port 8787

# N extra API-only instances (read the shared files, no GEXBot calls):
POLL_ENABLED=0 SERVER_PORT=8788  python -m app.main
POLL_ENABLED=0 SERVER_PORT=8789  python -m app.main
```
Put nginx/Caddy (or a cloud load balancer) in front of them and enable its
response cache for `/api/*` (1–2 s) — that, not the app, is what carries a huge
audience.

> ⚠️ Don't run `uvicorn --workers N` with `POLL_ENABLED=1`: every worker would
> start its own poller = N× the GEXBot calls. Use the layout above instead
> (one poller instance, the rest `POLL_ENABLED=0`).

## Poller settings (`.env`)
```
POLL_ENABLED=1
POLLED_TICKERS=SPX,SPY,NDX,QQQ
POLL_PERIODS=zero,full,one
POLL_INTERVAL=2
DATA_DIR=            # optional; defaults to server/data
```
⚠️ 28 files / 2 s ≈ **14 GEXBot calls/second**. If you get HTTP 429 in the logs,
raise `POLL_INTERVAL`, drop a period, or shorten `POLLED_TICKERS`.
