"""Shared pieces for the Kalshi event-ladder screener.

Deliberately small. `client` is the GET-only network module used by collect.py,
refresh_quotes.py and enrich_sizes.py: it exposes `get_events_page`,
`get_event_markets`, `get_market`, `get_markets`, `get_market_orderbook`,
`get_markets_orderbooks`, `get_balance`, `get_subaccount_balances` and
`get_positions` and cannot place or cancel an order. Writes live in the sibling
`orders` module.
`filter_candidates.py` never imports `client` or `orders`, which
is what makes the filter structurally incapable of calling the API. This package
does not re-export trading helpers.
"""
