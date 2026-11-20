# Kalshi event-ladder screener

Research tooling for one question: **do mutually exclusive events with real
liquidity actually exist on Kalshi, and how do they behave over time?**

Kalshi groups markets into events, and an event can be flagged
`mutually_exclusive`, meaning at most one of its markets can settle YES. When that
holds, the event's YES prices have a known relationship to 100 - a tournament
winner event should have all the players' YES prices summing to roughly 100,
because exactly one wins and a winning contract pays $1.00. That relationship is an
anchor for fair value. This package establishes whether such events exist in
tradeable form, then ranks a short watchlist from each snapshot. The dashboard
half of the project shows the funnel, the watchlist, and four-route edge
estimates from the files written here.

The live loop is one Greater Cycle (collect, then filter) followed by ten
Lesser Cycles (refresh quotes, then score). `enrich_sizes.py` remains for a
manual size fill; the scheduler does not run it. A long-running watchlist
trader and an authentication smoke test sit beside that loop.

The collect → filter → refresh → score path still cannot place an order:
`KalshiReadOnlyClient` is GET-only, and `filter_candidates.py` never imports
`client`, `orders`, or the WebSocket helper. Writes happen only in `trade.py`.
`TRADER_DRY_MODE` defaults on. Pointing the **pipeline** at production is still
a read of public market data; disabling dry mode is what makes this package
write-capable.

## Setup

From this directory, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Then edit `.env`. It holds **paths to your credential files, never the keys
themselves**; the code opens those files at runtime. Keep the key files outside
this directory so they cannot be committed by accident. `.env` is gitignored.

The settings you need first:

| Setting | Meaning |
| --- | --- |
| `KALSHI_ENV` | `demo` or `production` |
| `KALSHI_DEMO_API_KEY_ID_PATH` | path to a file holding your demo key ID |
| `KALSHI_DEMO_PRIVATE_KEY_PATH` | path to your demo private key PEM |
| `KALSHI_PROD_API_KEY_ID_PATH` | same, for production; leave blank until you have keys |
| `KALSHI_PROD_PRIVATE_KEY_PATH` | same |

Every other setting has a working default and is documented inline in
`.env.example`.

Requires Python 3.11 or newer. Five dependencies: `httpx`, `cryptography`,
`truststore`, `python-dotenv`, `websockets`.

## Running it

### 1. Check authentication

```powershell
.\.venv\Scripts\python.exe check_auth.py
```

Makes exactly one signed request (`GET /portfolio/balance`) against whichever
environment `KALSHI_ENV` selects, and prints whether it worked plus your balance.
Run this first and get it passing before touching anything else - authentication is
the fiddly part of this API and much easier to debug on its own. This script is
intentionally standalone and duplicates the signing code, so when it breaks there
is one short self-contained file to read.

It needs credentials for the selected environment. Collect and enrich can run
unsigned (see below). The filter never calls the API.

### 2. Collect a snapshot

```powershell
.\.venv\Scripts\python.exe collect.py
```

Pages through every open event with its markets attached and writes the raw
responses to `data/raw/events_open_<env>_<UTC>.json.gz`. Prints a loud banner
saying which environment it is pointed at, then a summary: pages, events, markets,
elapsed time, and where it saved.

The dashboard runs this as the Greater Cycle, then idles. The payload is still
stored verbatim, but this is not an unbounded archive: after a successful
Greater or Lesser cycle, only the current raw snapshot is kept per environment.
Snapshots are gzipped
because a full one is roughly 300 MB of JSON; gzip cuts that by well over 10x
so the live file (and the extra copy that exists mid-cycle) stay manageable.
Set `COLLECTOR_COMPRESS=false` if you want to read the raw JSON in an editor.

`GET /events` is a public endpoint, so if the selected environment has no
credentials configured the collector reads unsigned rather than failing. This is
how you can collect real production data before generating production keys; the
banner says clearly when a run is unsigned.

### 3. Filter to candidates

```powershell
.\.venv\Scripts\python.exe filter_candidates.py
# or a specific snapshot:
.\.venv\Scripts\python.exe filter_candidates.py --snapshot data\raw\events_open_production_20260918T112647Z.json.gz
```

Defaults to the newest snapshot in `data/raw`. Never calls the API. Prints the
survival funnel - the actual research output - and writes the survivors to
`data/candidates/candidates_<env>_<snapshot UTC>.json`, keeping only event ticker
and title, the mutually exclusive flag, close time, and per market the ticker,
title, that market's own close time, YES and NO top-of-book prices, YES and NO
sizes, 24h volume and open interest. Every output file records which raw snapshot it came from.

Prices are the actual book fields (`yes_bid_dollars`, `yes_ask_dollars`,
`no_bid_dollars`, `no_ask_dollars`). NO is never inferred from YES: if Kalshi
omits a field the candidate file writes null.

Sizes are top of book only - nothing here walks the order book.
`yes_bid_size_contracts` / `yes_ask_size_contracts` (from `yes_bid_size_fp` /
`yes_ask_size_fp`) come from nested markets in the snapshot.
`no_bid_size_contracts` / `no_ask_size_contracts` are filled from the YES
complement when the nested payload omits `no_*_size_fp` (NO ask size from the
YES bid, NO bid size from the YES ask). NO prices are never inferred. The file
is tagged `cycle_kind: greater`. It is not scored, and it does not replace the
watchlist.

Gates, all adjustable in `.env`, applied in this order:

1. flagged mutually exclusive
2. at least `FILTER_MIN_MARKETS` markets (default 2)
3. at most `FILTER_MAX_MARKETS` markets (default 20) - a 30-leg event is
   impractical to trade even if it looks attractive
4. every market tradeable
5. at least one of the four full-bundle routes is executable: every leg's
   price strictly between 0 and 100 cents, and positive resting size (`0` and
   `100` are empty-book sentinels). Nested snapshots omit NO sizes; this gate
   uses the YES complement size for those two routes (YES bid size for buy NO,
   YES ask size for sell NO). Prices are never inferred.
6. every market above a liquidity floor: 24h volume ≥ 0 or open interest ≥ 0
   (both floors are 0 on purpose)
7. closing in 15 minutes to 365 days

### 4. Refresh quotes (Lesser Cycle)

```powershell
.\.venv\Scripts\python.exe refresh_quotes.py
```

Loads the newest Greater Cycle file. Markets already on those events are
refreshed in groups of 100. `GET /markets?tickers=A,B,C` returns prices, YES
sizes, status, volume, open interest and close time. When requests are signed,
NO sizes come from `GET /markets/orderbooks` for the same group. When they are
unsigned, each ticker still gets `GET /markets/{ticker}/orderbook?depth=1`.
Prices are never inferred from the book. New listings are not added. There is
no pause between markets on the batched path; the client backs off on HTTP 429.

The same gates run again, starting from "candidate universe". Events that fail
are omitted from this file only; the next Lesser reloads the Greater file, so
a name can pass later. Writes `candidates_<env>_<UTC>.json` with
`cycle_kind: lesser`.

### 5. Fill NO sizes (manual only)

The scheduler does not run this. Use it to fill NO sizes on an existing file
without refreshing prices.

```powershell
.\.venv\Scripts\python.exe enrich_sizes.py
# or a specific candidates file:
.\.venv\Scripts\python.exe enrich_sizes.py --candidates data\candidates\candidates_production_20260918T112647Z.json
```

Defaults to the newest file in `data/candidates`. Fetches
`GET /markets/{ticker}/orderbook?depth=1` for each surviving market and writes
the NO top-of-book sizes:

- `no_bid_size_contracts` from the best NO bid
- `no_ask_size_contracts` from the best YES bid (Kalshi's book is bids only: a
  YES bid at `P` is the NO ask at `1-P` with the same resting size)

YES sizes and all prices stay as the snapshot wrote them. Empty or missing books
stay null; one 404 does not abort the run. The file is replaced in place and
records `sizes_source`, `sizes_enriched_at_utc`, and fetched/failed counts.

This endpoint is public, so unsigned reads work the same way as collect.

### 6. Score a watchlist

```powershell
.\.venv\Scripts\python.exe score.py
# or a specific candidates file:
.\.venv\Scripts\python.exe score.py --candidates data\candidates\candidates_production_20260918T112647Z.json
.\.venv\Scripts\python.exe score.py --top 10
```

Defaults to the newest file in `data/candidates`. Never calls the API. Refuses
a Greater Cycle file, so that run cannot publish an empty watchlist. For each
Lesser Cycle survivor it takes `max(volume_24h_contracts)` across legs, drops
the event when any leg is missing a volume or has a negative one, and writes
`data/watchlist/watchlist_<env>_<snapshot UTC>.json`.

That maximum is the score. Zero is a valid score and ranks last. Ties break
on `event_ticker`. Best edge and best route are stored beside the rank as
labels; a missing quote does not drop the event. The watchlist is the top
`WATCHLIST_SIZE` (default 10).

After a **live** Greater filter or a **live** score, older pipeline files are
removed. Per environment that keeps the newest Greater universe file, the two
newest candidate snapshots, one raw snapshot, and watchlists whose candidates
file is still retained. The newest watchlist is always kept, so a Greater
Cycle leaves the last list in force. `python score.py --candidates` on an
older file writes that watchlist and does not prune.

The dashboard runs this after each Lesser quote refresh, and does not run it
after a Greater Cycle.

### 7. Live watchlist trader

```powershell
.\.venv\Scripts\python.exe trade.py
```

Long-running. The dashboard starts this the same way it starts the cycle (Core's
interpreter, a subprocess, not an import) unless you pass `--no-trader`. It is
**not** a cycle step. Any cycle step can run up to 60 minutes; the
trader has no such timeout.

It joins the newest same-environment watchlist to that file's `source_candidates`
for market tickers and close times, subscribes to Kalshi's ticker WebSocket, and
prices all four basket routes with the same taker / M=1 model as
`kalshi/mispricing.py`. Subscribe and later `add_markets` both request
`send_initial_snapshot`, so a name that joins the watchlist is quoted from the
current book, not only after the next trade. The best 1-bundle net wins, with
tie-break order `buy_no`, `buy_yes`, `sell_yes`, `sell_no`.

A ticker update checks that game immediately. Every 2 seconds the trader also
checks every game on the current watchlist from the quotes it already holds, and
it does that again as soon as a new watchlist file is loaded, including games
that were already on the previous list. An unchanged watchlist file does not
reload. Candidate-file prices are still cleared and never fire a trade.

A fresh clip can fire again on a later check while the quote stays eligible.
After a live fill, the shown size is reduced by what filled and the price is
left as it is. The 2-second pass and the post-load pass take a smaller remainder
at that same price when the gates still pass, one shot at a time, and stop when
the shown size is gone. They do not invent the next price. A later ticker is
the only way that price appears. A same-price ticker that restores a larger size
than the size left after those fills is ignored. A smaller size or a different
price replaces the top and is checked immediately. A failed or zero-fill attempt
leaves the event free. Dry mode never POSTs, so it does not shrink the book; the
same shown top can be attempted again on the next 2-second pass. Eligibility for
another fresh clip is:

- nothing is in flight
- every leg of the event's position has the same filled count (a flat event counts)
- live annualized return, from **now** to the event's earliest `close_time`,
  using **unnetted** capital per bundle, at least `TRADER_MIN_ANNUALIZED_PCT`
  (default 5). The open position does not change that one-bundle edge
- at least one whole bundle: `min(top-of-book bottleneck, floor(shot cap /
  unnetted cents per bundle), floor(unused event capital / unnetted cents per
  bundle))`

`TRADER_MAX_CAPITAL_PER_SHOT_DOLLARS` (default 2) is the shot cap. It must be
above 0 and no higher than `TRADER_MAX_CAPITAL_PER_EVENT_DOLLARS` (default 10).
Asks of 40, 30, and 20 cents are 90 cents a bundle, so a $2 shot is 2 bundles,
not a fixed contract count. `5` and `$10` are arbitrary. 5% exists so capital is
not locked in low-yielding deals; $10 per **event ticker** exists for
diversification. There is **no account-wide portfolio cap**. Only filled
capital counts against the event cap, and it stays committed until Kalshi shows
every leg flat, including after the name leaves the watchlist. If that read
fails, the row stays until its stored latest leg close; a missing close stays.
The unfilled remainder is released. A new bundle is still refused once the
event's earliest close has passed.

The first non-zero fill pins the route. The other three routes on that event
are ignored after that. If the leg counts do not match, the trader does not
send another balanced clip. The only order is a completion on the short legs,
sized up to the current max fill, and only when the sunk fill prices plus the
current price on the missing contracts still leave a non-negative after-fee net.
A completion that partials or fails stays in completion until every leg count
matches again.

Before any POST the trader writes one in-flight reservation to
`data/trader/status.json` (event, route, per-leg counts, prices, and
`client_order_id`s). `evaluate` is synchronous, so one process does not overlap
two shots; the reservation is for a crash. On restart those ids are resolved
before anything new is sent, and a POST is never retried with new ids. A missing
order is a zero fill. If the read fails, the reservation stays and nothing new
is sent.

Positions are **held to settlement**. Partial fills are **not unwound**. Live
mode sends the clip as one IOC batch (`POST /portfolio/events/orders/batched`);
time in force stays `immediate_or_cancel`. Dry mode (`TRADER_DRY_MODE=true`,
the default) follows the same reservation path and never POSTs, so a dry attempt
is a zero fill. Dry vs live lives only in `Core/.env` — the dashboard has no toggle.

When the trader starts, and about once a minute after that, it reads
`GET /portfolio/positions`. An event Kalshi shows flat is dropped. An event
Kalshi still holds is kept, and one missing from the file is imported. Fills
already on disk are not replaced. The in-flight event is left alone. A failed
read does not treat the book as flat: a row is dropped only when its stored
latest close has passed, and a missing close stays. Positive contracts are
stored as `buy_yes` and negative contracts as `buy_no`: Kalshi's sign is YES
versus NO, so a sell on that side is the same position and is stored as the
buy route. Mixed YES and NO on one event is kept with no route, counts against
the cap, and is not completed or added to. Filled capital is Kalshi's
`market_exposure_dollars`. The ledger close is the latest leg close. An event
missing a close, a count, or a positive exposure is skipped and logged. A close
already in the past is not a reason to skip.

Writes `data/trader/status.json` (gitignored) with a rolling last-10 trade log,
the open positions, any in-flight reservation, and a `live_edges` peek: every 2
seconds (not per ticker) the trader quotes each current watchlist event from the
in-memory WebSocket books and keeps the last 30 samples per event. Money fields
are decimal strings. The peek does not change the fire gate. The 2-second trade
scan is a separate pass.

## Data layout

```
data/raw/          current raw snapshot per environment, verbatim, gzipped
data/candidates/   pinned Greater universe plus the two newest quote snapshots
data/watchlist/    in-force watchlist (and any whose candidates file is retained)
data/trader/       live trader status.json (last-10 trades, positions, in-flight, live_edges)
data/cycle_state.json   which phase the dashboard resumes after a restart
```

All of `data/` is gitignored. Note that if this folder lives inside OneDrive, `data/` will
be synced; move the project or exclude the folder if that is not what you want.

Each raw file is still stored exactly as Kalshi sent it, wrapped in a metadata
envelope recording when, where and how it was fetched. Nothing is reshaped or
pruned on the way in. After a live Greater filter or a live score, older raw
files are deleted so disk stays bounded; re-filter an older dump only if you
still have that file.

---

# Technical notes

Learned the hard way. Don't re-derive these.

## Authentication

Three headers on every request:

- `KALSHI-ACCESS-KEY` - the key ID
- `KALSHI-ACCESS-TIMESTAMP` - milliseconds since epoch, as a string
- `KALSHI-ACCESS-SIGNATURE` - RSA-PSS, SHA-256, salt length equal to the digest
  length, base64

The signature covers exactly `timestamp + HTTP_METHOD + path`. Nearly all signature
failures come from two things:

- **the signed path must include the `/trade-api/v2` prefix**, and
- **query parameters must be stripped before signing**.

The timestamp is part of the signed message, so headers are rebuilt on every retry
rather than reused. Salt length must be the digest length; `PSS.MAX_LENGTH` is
rejected. A system clock off by more than a few seconds invalidates signatures.

The ticker WebSocket uses the same three headers on the handshake, signed as
`GET` over `/trade-api/ws/v2` (not the `wss://...` URL).

## TLS certificates

This machine has a TLS-intercepting root certificate installed (antivirus or a
proxy). It lives in the Windows certificate store, so browsers and curl work, but
Python's default certifi bundle does not have it and every request fails with
`CERTIFICATE_VERIFY_FAILED`. The `truststore` package makes Python verify against
the OS trust store instead.

Do **not** work around this by disabling certificate verification.

## Prices are fixed-point strings, not integer cents

Kalshi migrated away from integer cents, and the legacy integer fields were removed
from all REST and WebSocket payloads on 12 March 2026. Confirmed against live
production data: the old fields are simply absent now.

- prices arrive in fields suffixed `_dollars`, as strings like `"0.4200"`
- contract counts arrive in fields suffixed `_fp`, as strings like `"13.00"`

They are strings because tick sizes can be smaller than a cent and quantities can
be fractional down to 0.01 contracts. Responses emit up to 6 decimal places. Real
production data does contain fractional open interest, e.g. `"5264.02"`.

Everything is parsed through `Decimal`, never `float`. Getting this wrong does not
crash - it silently produces slightly wrong prices, which is much worse than a
crash. Most sample code online still reads the old integer-cent fields and is out
of date.

**Unit convention:** this project reports prices in **cents (0-100)** everywhere
downstream of `kalshi/fixedpoint.py`, encoded in JSON as **strings** (`"42.5"` is
42.5 cents). Strings, not numbers, because a JSON float would reintroduce exactly
the floating-point drift the `Decimal` parsing exists to avoid. Anything reading
these files back can hand the string straight to `Decimal()`. Quantities are
likewise decimal strings, in contracts.

## The endpoint

`GET /events` with `status=open` and `with_nested_markets=true`. Each event comes
back with its markets already attached in `event.markets`, so it is one call per
page rather than one call per market. Paginate with `limit` and `cursor`, following
the cursor until it comes back empty.

`limit` is capped at 200, and `with_nested_markets=true` now honours 200 rather
than being silently reduced to 100. This endpoint already excludes multivariate
(combo) events, so no extra filtering is needed on the way in.

The event object's boolean `mutually_exclusive` field - true means only one market
in the event can resolve YES - is the entire basis of this project.

Nested markets include YES sizes but not NO sizes. A Greater Cycle file stores
the YES complement for those missing sizes. A Lesser Cycle fetches real prices
from `GET /markets?tickers=` in groups of 100, and real NO sizes from
`GET /markets/orderbooks` when signed or from
`GET /markets/{ticker}/orderbook?depth=1` when unsigned. `enrich_sizes.py` can
still fill NO sizes by hand.
Kalshi's orderbook is bids only:
a YES bid at `P` is the NO ask at `1-P` with the same resting size. Arrays are
price-ascending; the last level is the best bid.

## Gotcha: the tradeable market status is `active`, not `open`

The market `status` enum is `initialized`, `inactive`, `active`, `closed`,
`determined`, `disputed`, `amended`, `finalized`. **There is no `open` value.**
`status=open` is valid as an event *query parameter* but never appears as a market
*status*, so a `status == "open"` gate silently yields zero survivors and looks
like a research finding rather than a bug.

## Gotcha: events have no close time

Only markets carry `close_time`. The event object has none. This project derives an
event's close time as the **earliest** `close_time` among its markets, because one
leg closing in three minutes makes the whole ladder untradeable regardless of the
others. Each output event records `close_time_basis` saying so.

## Base URLs

| Environment | Base URL |
| --- | --- |
| Production | `https://external-api.kalshi.com/trade-api/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` |

The older shared hosts `api.elections.kalshi.com` and `demo-api.kalshi.co` still
work. Despite the `elections` subdomain, the production API serves all markets.
These hostnames have changed over time, so they are overridable from `.env`;
confirm against the current docs rather than assuming.

Credentials are not shared between environments: demo keys work only on demo hosts
and production keys only on production hosts. `KALSHI_ENV` switches the base URL
and the credential paths together so the two cannot be mismatched.

**Demo order books are synthetic and thin, so liquidity results from demo are
meaningless.** The difference is stark in practice - on the same day, 5,274 demo
events passed the tradeable gate but only 422 had an executable basket, against
4,890 and 3,850 respectively on production. Use production for research.

## Rate limits

There are thousands of open markets. Be polite: a short sleep between pages
(`COLLECTOR_PAGE_SLEEP_SECONDS`, default 0.3s), exponential backoff with jitter on
429 and 5xx honouring `Retry-After`, and a retry cap (`HTTP_MAX_RETRIES`, default
4). A full production walk is about 74 pages and takes roughly a minute. Size
enrichment reuses the same sleep between per-market orderbook calls, but only
for the markets that survived the filter.
