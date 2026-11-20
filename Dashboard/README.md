# Kalshi candidates dashboard

A local web view of the research pipeline's filter output. It reads
`candidates_*.json` files from `..\Core\data\candidates` and
`watchlist_*.json` from `..\Core\data\watchlist`, and shows the filter
funnel (including the 24h-volume scoring gate), a top-N watchlist, a browsable table of
candidate event ladders, and an estimate of how mispriced each mutually
exclusive ladder is across four basket trades.

Unless started with `--no-cycle`, the dashboard immediately runs one Greater
Cycle (`collect.py`, then `filter_candidates.py`) and then ten Lesser Cycles
(`refresh_quotes.py`, then `score.py`). After each cycle's work finishes it
idles `CYCLE_IDLE_MINUTES` (default 5) before the next one. A failed cycle is
retried after that idle and does not advance the sequence. Collection still
uses `Core`'s own interpreter and `KALSHI_ENV`.

Unless started with `--no-trader`, it also supervises `Core/trade.py` as a
separate long-running process (restart on death, no step timeout). The
last-10 trade log and live-edge peek are **display only**. Dry vs live is
`TRADER_DRY_MODE` in `Core/.env`; there is no dashboard toggle. The 5%
annualized gate and $10 unnetted cap per event ticker are arbitrary
diversification choices.

There is no Refresh button and no order-placement button.

## The trades being modelled

On a mutually exclusive event, at most one leg can ever settle YES (pay 100¢).
Four top-of-book baskets follow from that. **Every visible edge number is net
after estimated taker fees.** Gross is hover-only, never a table cell.

| Route | Action | Gross (cents / 1 bundle) | Size bottleneck |
| --- | --- | --- | --- |
| Sell YES | sell YES @ `yes_bid` on every leg | Σ yes_bid − 100 | min yes_bid_size |
| Buy NO | buy NO @ `no_ask` on every leg | (N−1)×100 − Σ no_ask | min no_ask_size |
| Buy YES | buy YES @ `yes_ask` on every leg | 100 − Σ yes_ask | min yes_ask_size |
| Sell NO | sell NO @ `no_bid` on every leg | Σ no_bid − (N−1)×100 | min no_bid_size |

Net per bundle = gross − sum of 1-contract taker fees on each leg. Net at max
size uses `C = max bundles`, then divides those fees by C (floored to the price
quantum). The table shows the conservative figure (net per bundle). Max size,
bottleneck, capital and annualized live in the expanded detail, per route.

**NO is never inferred from YES.** The candidate file must carry actual
`no_bid_cents` / `no_ask_cents` (and sizes). A missing field is null, not
`100 − yes_*`. A route is priced only when every leg is hittable: price
strictly between 0 and 100 cents, and resting size `> 0`. `0` and `100` are
empty-book sentinels. Old candidate files without NO quotes or NO sizes still
load; a Greater Cycle file fills missing NO sizes from the YES complement, and
a Lesser Cycle replaces prices and sizes from the live market and orderbook.

Deltas (net per bundle; null, not zero, if either side is missing):

- **Bid-side Δ** = Sell YES net − Buy NO net. Positive ⇒ Sell YES is better.
- **Ask-side Δ** = Buy YES net − Sell NO net. Positive ⇒ Buy YES is better.

**Best edge** = max of whichever of the four nets exist, with `best_route` naming
the winner. Row highlight and the "edge" badge mean the **best** net is positive.

Capital: shorts (`sell_yes`, `sell_no`) show unnetted Σ(100 − sale price) and
netted `max(0, −gross)`, because it is not established whether Kalshi nets
margin across the legs. Longs (`buy_yes`, `buy_no`) use the cash outlay (the sum
of the asks) for both figures. Annualized return on the table uses the **best**
route's conservative net and unnetted capital, measured from snapshot time, not
wall clock.

## The fee model, and why it is an estimate

Kalshi's published taker fee, in dollars:

```
fee = ceil_to_next_cent(M * 0.07 * C * P * (1 - P))
```

`P` is the price in dollars **of the contract actually traded** (the bid you hit
or the ask you lift), `C` the contract count, `M` the series multiplier.
The assumptions live in named constants at the top of `data_store.py`
(`TAKER_FEE_RATE`, `ASSUMED_SERIES_MULTIPLIER`, `ASSUMED_SIDE`) and are returned
by the API so the UI can display them. The same model is applied to all four
routes.

The rounding happens **once per order, not per contract**, which is why two net
figures are shown:

| Figure | Fees computed with | Use it for |
| --- | --- | --- |
| `net edge per bundle` | `C = 1` on each leg | The conservative case; true even for one bundle. |
| `net edge at max size` | `C = max bundles`, then divided by max bundles | What the trade is worth if the whole bottleneck size fills. |

Worked example (Sell YES): bids of 60 and 45 sum to 105, so gross is 5 cents. Fees at
one contract are `ceil(7 * 0.60 * 0.40) = 2` and `ceil(7 * 0.45 * 0.55) = 2`, so
net edge per bundle is 1 cent. With best-bid sizes of 30 and 12, max bundles is
12 and the second leg is the bottleneck; fees there are `ceil(20.16) = 21` and
`ceil(20.79) = 21`, so 42 cents over 12 bundles is 3.5, and net edge at max size
is 1.5 cents.

**Verify the fee schedule for a series before acting on any of this.** Kalshi
sets series-specific multipliers and charges maker fees on some series (sports,
CPI, Fed and similar). This model ignores both and assumes a taker on every leg.

## What this does NOT model

Everything below is unpriced here. Any one of them can turn a displayed edge into
a loss:

- **Depth beyond the top of book.** Only the best bid/ask and its size are used.
  Size beyond the top level is invisible, and so is the price impact of taking it.
- **Maker fees and series multipliers.** Assumed away; see above.
- **Margin netting.** Whether the exchange collateralises the basket as one
  position or as N independent shorts is not established, hence two capital
  figures and no single "return on capital".
- **Partial fill risk.** The edge only exists if *every* leg fills. Filling some
  legs and not others leaves an outright directional position, not an arbitrage.
- **Quote staleness.** These are snapshots. The dashboard shows the snapshot's
  age and warns past 15 minutes, because a quoted arbitrage from twenty minutes
  ago is probably already gone.
- **Early close.** Markets carry `can_close_early`; an event can settle before its
  stated close time, which invalidates the annualized figure.
- **Settlement edge cases.** "At most one YES" is not "exactly one YES". If no leg
  settles YES the short basket pays nothing out, which is *better* than modelled;
  void, amended or disputed settlements are not modelled at all.

**Demo data is worthless for this.** Demo order books are synthetic, so any edge
found there is an artefact.

## Run it

```powershell
cd Dashboard
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python serve.py
```

Then open the URL it prints, <http://127.0.0.1:8787/>.

If `py` reports `No installed Python found!` (the launcher does not see
Anaconda-managed interpreters), use `python -m venv .venv` for the second step.

## Flags

| Flag | Default | Notes |
| --- | --- | --- |
| `--candidates-dir` | `..\Core\data\candidates` | Directory to read run files from. Also settable with the `CANDIDATES_DIR` environment variable; the flag wins. |
| `--watchlist-dir` | `..\Core\data\watchlist` | Directory to read `watchlist_*.json` from. Also settable with `WATCHLIST_DIR`; the flag wins. |
| `--core-dir` | `..\Core` | Pipeline directory for cycle scripts. Also settable with `CORE_DIR`; the flag wins. |
| `--no-cycle` | off | Serve as a file viewer only: do not start the Greater/Lesser loop. |
| `--no-trader` | off | Do not start or supervise `Core/trade.py`. The trader panel still reads `status.json` if a trader is running elsewhere. |
| `--port` | `8787` | |
| `--host` | `127.0.0.1` | Loopback only by default. |

```powershell
python serve.py --candidates-dir D:\kalshi-runs --port 9000
python serve.py --no-cycle
python serve.py --no-trader
```

The startup banner says whether the cycle and trader are available, which
interpreter they will use, and whether the trader is DRY or LIVE, so a `Core`
venv that cannot be found is obvious at boot.

## Using it

- **Run picker** (top bar) lists every readable run file, newest first, labelled
  with its `filtered_at_utc`, environment and candidate count. After a live
  cycle that is the pinned Greater universe plus the two newest quote snapshots.
  The newest run loads on open. After a cycle finishes the picker reloads onto
  the new run.
- **Cycle status** (top bar) names the phase (Greater, or Lesser n/10) and the
  current step. While idle it counts down to the next phase from the moment the
  previous cycle finished. There is no button: the loop starts with the server.
- **Funnel** shows each gate the pipeline applied, how many events survived, and
  the change from the previous stage. When a matching watchlist exists, the
  `computable volume_24h` gate is appended after the snapshot gates.
- **Watchlist** is the top N events from the latest Lesser Cycle: rank, ticker,
  title, 24h volume, best edge, and best route. The volume is the score.
  A Greater Cycle does not publish one. If the selected run has no watchlist,
  the panel keeps showing the newest watchlist, which stays in force.
- **Live watchlist trader** (under the watchlist) reads `Core/data/trader/status.json`,
  polled about every 2 seconds. It shows a live-edge peek (best route and net
  per current watchlist event, sampled every 5 seconds by the trader), open
  inventory and unused room on the per-event cap from that same file, cash on
  each exchange shard (subaccount 0, read by the trader about every 60 seconds),
  plus the rolling last 10 attempts. Live annualized is from now and can differ from
  snapshot watchlist and candidates edges. It is a log viewer: it cannot arm,
  disarm, or switch dry/live. Expected PnL and capital locked are the trader's
  pre-trade estimates, not Kalshi fill accounting. Partial fills are not
  unwound; capital stays committed until that event's close even if the name
  leaves the watchlist.
- **Run / Gates** panel shows the snapshot the run came from, how old that
  snapshot is, the exact thresholds that produced the view, and the fee
  assumptions behind every edge figure.
- **Candidates table** is one row per snapshot survivor, 100 per page. Search
  filters on event ticker and title; the sort selector re-queries the server.
  Alongside the liquidity columns
  each row shows the four route nets, the two deltas, best edge and max bundles
  (bottleneck of the best route). Visible cells are **net**; hover a net cell for
  that route's gross. A row whose **best** net is positive is badged and railed
  in green, with a small route tag on Best edge. Click a row to expand all four
  routes (net visible, gross on hover, skip reasons when a route is "-") and the
  full market ladder, including YES/NO bids and asks, all four top-of-book sizes,
  estimated per-contract fee at the YES bid, complement hints (yes_bid+no_ask and
  yes_ask+no_bid vs 100), and the ladder's YES and NO bid/ask sums. The best
  route's bottleneck size is marked.
- Events that are not flagged mutually exclusive get no route numbers at all.
  A route that is missing a required price shows "-" and the expanded panel says
  why. Old candidates files predate NO quotes and some size fields, so for those
  only the routes the file supports are filled; missing size leaves max bundles /
  profit unknown while per-bundle nets still work.
- Event tickers have a **copy** button. There are no links out to Kalshi: the
  data contains no market URL and guessing one would produce dead links.
- A **demo** run shows a warning banner, because demo order books are synthetic,
  so the liquidity gates and every edge figure mean nothing there. A snapshot
  older than 15 minutes shows a staleness banner for the same reason.

Re-running the pipeline from a terminal is also picked up automatically: parsed
runs are cached by file mtime and size, and the directory is re-scanned on every
request, so refreshing the browser is enough. No restart needed.

If the candidates directory is missing or empty, the dashboard still starts, and
the first Greater Cycle (or collect, then filter, from Core) will fill it.

## Cycle

One Greater Cycle, then ten Lesser Cycles, then Greater again. Greater is
`collect.py` then `filter_candidates.py`: it writes the candidate universe and
does not score. Lesser is `refresh_quotes.py` then `score.py`: it re-quotes
that universe, drops gate failures, and publishes the watchlist. They are
separate scripts but one operation from here.

A successful Greater filter or live score then prunes. Per environment that
keeps the pinned Greater file, the two newest candidate snapshots, one raw
snapshot, and the in-force watchlist. A failed cycle keeps the extra files and
is retried after the idle; the phase does not advance.

The first cycle starts as soon as `serve.py` is up. The idle clock
(`CYCLE_IDLE_MINUTES`, default 5) starts when that cycle's work finishes, not
when it starts. `CYCLE_INTERVAL_MINUTES` is not read. Phase and the universe
filename are stored in `Core/data/cycle_state.json`, so a restart resumes; a
missing universe file starts at Greater. On success the run picker reloads
onto the new run. The watchlist panel stays on the last published list through
a Greater Cycle.

Details worth knowing:

- **One at a time.** The next cycle does not start until the current one has
  finished and the idle has elapsed.
- **Which environment, and whose credentials, are `Core`'s business.** The cycle
  passes no configuration in; `KALSHI_ENV` and the credential paths in
  `Core\.env` decide everything, exactly as they do from a terminal. The Lesser
  refresh is pointed at the universe file the Greater Cycle just wrote.
- **The interpreter matters.** `Core` and `Dashboard` have separate virtualenvs.
  The cycle still runs `Core\.venv\Scripts\python.exe` when it exists so
  `collect.py` sees Core's packages, and only falls back to the current
  interpreter (the right answer when one venv serves both).
- **Timeout.** Collect, filter, quote refresh, and score are each killed after
  60 minutes. Each step has its own clock, started when that process is spawned.
  Reaching the timeout means something is genuinely stuck.
- **No authentication.** The dashboard binds to loopback and has no login, so
  anyone who can reach it can spend API quota. That is fine for a local research
  tool and is not fine if you ever change `--host`.

## Layout

| File | Role |
| --- | --- |
| `serve.py` | CLI flags, configuration, cycle scheduler, trader supervisor, startup banner, uvicorn entry point. |
| `data_store.py` | Run and watchlist discovery, caching, the fee model, and `Decimal`-exact per-event aggregates and edge figures. |
| `refresh_job.py` | Cycle job runner: interpreter resolution, subprocesses, status. |
| `trader_job.py` | Long-running `trade.py` supervisor with restart backoff. The dashboard still has no Kalshi client. |
| `api.py` | FastAPI routes. |
| `static\` | `index.html`, `app.js`, `styles.css` (vanilla, no build step). Cascadia Code is loaded from the jsDelivr CDN. |

Prices and sizes in the source files are decimal strings in cents/contracts.
They are parsed with `Decimal` and rendered as written; nothing is passed through
a float on the way to the screen. Every derived number - edges, fees, capital,
annualized return - is computed in `data_store.py` and crosses the API as a
string, because JavaScript numbers are binary floats and would quietly undo the
precision the string encoding exists to preserve. The browser only ever adds a
unit suffix.

Time to close is measured from the snapshot's capture time, not from now, so the
numbers are reproducible from the run file alone. Parsed runs are cached until
the file changes, so a wall-clock reading taken at parse time would go stale
without anything invalidating it.
