"use strict";

/* Vanilla front-end for the candidates dashboard. No framework, no build step.
   Filtering, sorting and pagination all happen server-side, so the browser only
   ever holds the 100 rows it is showing plus the ladders that are expanded. */

const PAGE_SIZE = 100;

/* Must match the candidates <thead> column count, or the expanded detail row
   shrinks and the ladder no longer lines up under the table. */
const EVENT_TABLE_COLS = 12;

/* Inner ladder: ticker, title, four prices, spread, four sizes, fee, vol, OI. */
const LADDER_COLS = 14;

const ROUTE_LABELS = {
  sell_yes: "Sell YES",
  buy_no: "Buy NO",
  buy_yes: "Buy YES",
  sell_no: "Sell NO",
};

const ROUTE_IDS = ["buy_no", "buy_yes", "sell_yes", "sell_no"];

/* A quoted edge is only as good as the snapshot it came from; past this the run
   is old enough that the order books behind it have probably moved on. */
const STALE_AFTER_MINUTES = 15;

const CYCLE_POLL_MS = 1500;
const UPDATED_JUST_NOW_MS = 5000;
const CYCLE_TICK_MS = 1000;
const TRADER_POLL_MS = 2000;

const state = {
  runId: null,
  page: 1,
  sort: "sell_yes_net",
  order: "desc",
  q: "",
  minNetEdge: "",
  totalPages: 1,
  cycleAvailable: false,
  cycleWasRunning: false,
  cycleJustUpdatedUntil: 0,
  lastCycleStatus: null,
  cycleIdleMinutes: 5,
  candidatesDir: null,
  environment: null,
  traderAvailable: false,
};

/** Cache of fetched event ladders, keyed by "runId\nticker". */
const detailCache = new Map();

const dom = {
  runSelect: document.getElementById("run-select"),
  errorBar: document.getElementById("error-bar"),
  demoBar: document.getElementById("demo-bar"),
  staleBar: document.getElementById("stale-bar"),
  emptyState: document.getElementById("empty-state"),
  emptyDir: document.getElementById("empty-dir"),
  cycleControls: document.getElementById("cycle-controls"),
  cycleStatus: document.getElementById("cycle-status"),
  runView: document.getElementById("run-view"),
  funnelBody: document.getElementById("funnel-body"),
  metaList: document.getElementById("meta-list"),
  gatesList: document.getElementById("gates-list"),
  feeList: document.getElementById("fee-list"),
  watchlistEmpty: document.getElementById("watchlist-empty"),
  watchlistTable: document.getElementById("watchlist-table"),
  watchlistBody: document.getElementById("watchlist-body"),
  search: document.getElementById("search"),
  sortMenu: document.getElementById("sort-menu"),
  sortButton: document.getElementById("sort-select"),
  sortList: document.getElementById("sort-list"),
  minEdgeSelect: document.getElementById("min-edge-select"),
  resultCount: document.getElementById("result-count"),
  eventsBody: document.getElementById("events-body"),
  pageLabel: document.getElementById("page-label"),
  pageFirst: document.getElementById("page-first"),
  pagePrev: document.getElementById("page-prev"),
  pageNext: document.getElementById("page-next"),
  pageLast: document.getElementById("page-last"),
  traderState: document.getElementById("trader-state"),
  traderEmpty: document.getElementById("trader-empty"),
  traderTable: document.getElementById("trader-table"),
  traderBody: document.getElementById("trader-body"),
  liveEdgesEmpty: document.getElementById("live-edges-empty"),
  liveEdgesTable: document.getElementById("live-edges-table"),
  liveEdgesBody: document.getElementById("live-edges-body"),
  shardCash: document.getElementById("shard-cash"),
  inventoryEmpty: document.getElementById("inventory-empty"),
  inventoryTable: document.getElementById("inventory-table"),
  inventoryBody: document.getElementById("inventory-body"),
};

/* -- small helpers -------------------------------------------------------- */

/** Build an element; text is always set via textContent so data cannot inject markup. */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children || []) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fmtInt(value) {
  return typeof value === "number" ? value.toLocaleString("en-US") : "-";
}

/** Bundle count from the log, or from equal leg counts on older rows that
    stored bundles as null. Accepts a JSON number or a decimal string. */
function tradeBundles(trade) {
  const logged = trade && trade.bundles;
  if (typeof logged === "number" || (typeof logged === "string" && logged !== "")) {
    return logged;
  }
  const legs = trade && Array.isArray(trade.legs) ? trade.legs : [];
  if (!legs.length) return null;
  const counts = legs.map((leg) => (leg && leg.count != null ? String(leg.count) : ""));
  if (counts.every((count) => count !== "" && count === counts[0])) return counts[0];
  return null;
}

function fmtBundles(raw) {
  if (typeof raw === "number" && Number.isFinite(raw)) return raw.toLocaleString("en-US");
  return fmtQty(raw);
}

/** Group thousands in a decimal string without ever parsing it as a float.
    The pipeline emits exact decimal strings; reformatting via Number() would
    silently change values like "706.15" at the edges. */
function fmtQty(raw) {
  if (raw === null || raw === undefined || raw === "") return "-";
  const text = String(raw);
  const negative = text.startsWith("-");
  const body = negative ? text.slice(1) : text;
  const [whole, fraction] = body.split(".");
  if (!/^\d+$/.test(whole)) return text;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (negative ? "-" : "") + grouped + (fraction ? "." + fraction : "");
}

/** Show a price exactly as the file gave it; no invented precision. */
function fmtPrice(raw) {
  return raw === null || raw === undefined || raw === "" ? "-" : String(raw);
}

/* Unit suffixes only. Every one of these values was computed as a Decimal on the
   server precisely so the browser never has to do sums in binary floats. */

function fmtCents(raw) {
  const text = fmtPrice(raw);
  return text === "-" ? text : `${text}c`;
}

function decimalPlaces(text) {
  const body = String(text).replace(/^[+-]/, "");
  const dot = body.indexOf(".");
  return dot === -1 ? 0 : body.length - dot - 1;
}

/** Exact integer scaled by 10^scale. Null when the text is not a decimal. */
function toScaledInt(raw, scale) {
  let text = String(raw).trim();
  let negative = false;
  if (text.startsWith("-")) {
    negative = true;
    text = text.slice(1);
  } else if (text.startsWith("+")) {
    text = text.slice(1);
  }
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  const digits = whole + fraction.padEnd(scale, "0");
  const scaled = BigInt(digits);
  return negative ? -scaled : scaled;
}

function fromScaledInt(value, scale) {
  const negative = value < 0n;
  const digits = (negative ? -value : value).toString().padStart(scale + 1, "0");
  let body = scale === 0 ? digits : digits.slice(0, digits.length - scale) + "." + digits.slice(digits.length - scale);
  if (body.includes(".")) body = body.replace(/0+$/, "").replace(/\.$/, "");
  if (body === "") body = "0";
  return (negative ? "-" : "") + body;
}

/** left − right as a decimal string. Display only; never a binary float. */
function subtractDecimalStrings(left, right) {
  if (left === null || left === undefined || left === "") return null;
  if (right === null || right === undefined || right === "") return null;
  const scale = Math.max(decimalPlaces(left), decimalPlaces(right));
  const a = toScaledInt(left, scale);
  const b = toScaledInt(right, scale);
  if (a === null || b === null) return null;
  return fromScaledInt(a - b, scale);
}

function canonicalDecimal(raw) {
  if (raw === null || raw === undefined || raw === "") return "0";
  const text = String(raw).trim();
  const scaled = toScaledInt(text, decimalPlaces(text));
  if (scaled === null) return text;
  return fromScaledInt(scaled, decimalPlaces(text));
}

/** Signed net for deltas: keep the server's digits, only add a leading + when
    the string is not already negative. Never parses as Number. */
function fmtSignedCents(raw) {
  const text = fmtPrice(raw);
  if (text === "-") return text;
  const signed = text.startsWith("-") || text.startsWith("+") ? text : `+${text}`;
  return `${signed}c`;
}

function fmtDollars(raw) {
  const text = fmtPrice(raw);
  return text === "-" ? text : `$${text}`;
}

/** Cents string to a dollar string. Exact decimal arithmetic, never a float. */
function dollarsFromCents(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  const text = String(raw).trim();
  const scale = decimalPlaces(text);
  const scaled = toScaledInt(text, scale);
  if (scaled === null) return null;
  return fromScaledInt(scaled, scale + 2);
}

/** Round a decimal string to `places` digits. Display only: the characters are
    adjusted in place, never parsed as a binary float. */
function roundDecimalString(text, places) {
  let negative = false;
  let body = text;
  if (body.startsWith("-")) {
    negative = true;
    body = body.slice(1);
  } else if (body.startsWith("+")) {
    body = body.slice(1);
  }
  if (!/^\d+(\.\d+)?$/.test(body)) return null;
  const dot = body.indexOf(".");
  let whole = (dot === -1 ? body : body.slice(0, dot)).split("");
  const fraction = dot === -1 ? "" : body.slice(dot + 1);
  const padded = fraction + "0".repeat(places + 1);
  const keep = padded.slice(0, places).split("");
  if (padded[places] >= "5") {
    let i = places - 1;
    while (i >= 0 && keep[i] === "9") {
      keep[i] = "0";
      i--;
    }
    if (i >= 0) {
      keep[i] = String.fromCharCode(keep[i].charCodeAt(0) + 1);
    } else {
      let j = whole.length - 1;
      while (j >= 0 && whole[j] === "9") {
        whole[j] = "0";
        j--;
      }
      if (j >= 0) whole[j] = String.fromCharCode(whole[j].charCodeAt(0) + 1);
      else whole.unshift("1");
    }
  }
  return (negative ? "-" : "") + whole.join("") + "." + keep.join("");
}

function fmtPct(raw) {
  const text = fmtPrice(raw);
  if (text === "-") return text;
  const rounded = roundDecimalString(text, 2);
  return `${rounded === null ? text : rounded}%`;
}

function fmtUtc(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    date.getUTCFullYear() +
    "-" + pad(date.getUTCMonth() + 1) +
    "-" + pad(date.getUTCDate()) +
    " " + pad(date.getUTCHours()) +
    ":" + pad(date.getUTCMinutes()) + "Z"
  );
}

/** "in 3d 4h" / "in 45m" / "12h ago", or null when there is no close time. */
function untilText(iso) {
  if (!iso) return null;
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return null;
  let minutes = Math.round((target - Date.now()) / 60000);
  const past = minutes < 0;
  minutes = Math.abs(minutes);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  let text;
  if (days > 0) text = `${days}d ${hours}h`;
  else if (hours > 0) text = `${hours}h ${minutes % 60}m`;
  else text = `${minutes}m`;
  return past ? `${text} ago` : `in ${text}`;
}

/** Whole minutes since a timestamp, or null when unusable. Clock arithmetic:
    the ban on computing in JavaScript is about money, not about dates. */
function minutesSince(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.round((Date.now() - then) / 60000);
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = `${detail}: ${body.detail}`;
    } catch (_) {
      /* non-JSON error body: the status alone is enough */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function getJson(url) {
  return fetchJson(url);
}

function showError(message) {
  dom.errorBar.textContent = message;
  dom.errorBar.hidden = false;
}

function clearError() {
  dom.errorBar.hidden = true;
}

/* -- rendering ------------------------------------------------------------ */

function renderFunnel(funnel) {
  clear(dom.funnelBody);
  for (const stage of funnel) {
    // The first stage has no predecessor, so its delta cell stays blank.
    let delta = "";
    if (stage.delta !== null && stage.delta !== undefined) {
      const sign = stage.delta > 0 ? "+" : stage.delta < 0 ? "-" : "";
      delta = `(${sign}${Math.abs(stage.delta).toLocaleString("en-US")})`;
    }
    dom.funnelBody.appendChild(
      el("tr", {}, [
        el("td", { text: stage.gate === null ? "-" : String(stage.gate) }),
        el("td", { class: "num", text: fmtInt(stage.surviving) }),
        el("td", { class: "num delta", text: delta }),
        el("td", { class: "bar-col" }, [
          el("div", { class: "funnel-bar", style: `width: ${stage.bar_pct || 0}%` }),
        ]),
      ])
    );
  }
}

function kvRows(target, entries) {
  clear(target);
  for (const [key, value] of entries) {
    let text;
    let cls = null;
    if (value === null || value === undefined) {
      text = "-";
    } else if (typeof value === "boolean") {
      text = value ? "true" : "false";
      cls = value ? "bool-true" : "bool-false";
    } else if (typeof value === "number") {
      text = value.toLocaleString("en-US");
    } else {
      text = String(value);
    }
    target.appendChild(el("dt", { text: key }));
    target.appendChild(el("dd", { class: cls, text }));
  }
}

function renderRunMeta(run) {
  const meta = run.meta || {};
  const snapshotAge = untilText(meta.source_snapshot_captured_at_utc);
  kvRows(dom.metaList, [
    ["environment", meta.environment],
    ["candidate_count", meta.candidate_count],
    ["filtered_at_utc", meta.filtered_at_utc],
    ["snapshot_captured_at_utc", meta.source_snapshot_captured_at_utc],
    ["snapshot_age", snapshotAge],
    ["source_snapshot", meta.source_snapshot],
    ["price_unit", meta.price_unit],
    ["price_encoding", meta.price_encoding],
    ["quantity_unit", meta.quantity_unit],
    ["file", run.filename],
  ]);
  kvRows(dom.gatesList, Object.entries(run.gates || {}));
  // Lists (the "ignores" entry) would stringify with bare commas; join them.
  kvRows(
    dom.feeList,
    Object.entries(run.fee_model || {}).map(([key, value]) =>
      Array.isArray(value) ? [key, value.join("; ")] : [key, value]
    )
  );
  state.environment = meta.environment || null;
  dom.demoBar.hidden = meta.environment !== "demo";

  const age = minutesSince(meta.source_snapshot_captured_at_utc);
  const stale = age !== null && age >= STALE_AFTER_MINUTES;
  if (stale) {
    dom.staleBar.textContent =
      `This snapshot was captured ${snapshotAge}. Order books move; an edge quoted from ` +
      `a stale snapshot has very likely gone. A Greater Cycle rebuilds the universe, then ten Lesser Cycles refresh quotes, idling ${state.cycleIdleMinutes} minutes after each.`;
  }
  dom.staleBar.hidden = !stale;
}

function routeOf(item, routeId) {
  const routes = item.routes || {};
  return routes[routeId] || {};
}

/** Visible text is net. Gross lives only in the title attribute. */
function netCell(route, routeId) {
  const net = route.net_cents_per_bundle;
  const attrs = { class: "num", text: fmtCents(net) };
  if (net !== null && net !== undefined && net !== "") {
    attrs.title = `${ROUTE_LABELS[routeId] || routeId} · gross: ${fmtCents(route.gross_cents)}`;
  }
  return el("td", attrs);
}

function routeTag(routeId) {
  const label = ROUTE_LABELS[routeId];
  if (!label) return null;
  return el("span", { class: "badge route", text: label.toLowerCase() });
}

/** Four net route cards. Gross is hover-only; a skipped route says why. */
function mispricingSummary(detail) {
  const edge = detail.mispricing || {};
  const routes = edge.routes || {};
  const cards = ROUTE_IDS.map((routeId) => {
    const route = routes[routeId] || {};
    if (!route.applicable) {
      return el("div", { class: "route-card skipped" }, [
        el("span", { class: "route-name", text: ROUTE_LABELS[routeId] }),
        el("span", { class: "muted", text: route.reason || "not computed" }),
      ]);
    }
    const netAttrs = { text: fmtCents(route.net_cents_per_bundle) };
    if (route.net_cents_per_bundle !== null && route.net_cents_per_bundle !== undefined) {
      netAttrs.title = `${ROUTE_LABELS[routeId]} · gross: ${fmtCents(route.gross_cents)}`;
    }
    return el("div", { class: route.net_edge_positive ? "route-card has-edge" : "route-card" }, [
      el("span", { class: "route-name" }, [
        ROUTE_LABELS[routeId],
        route.net_edge_positive ? el("span", { class: "badge edge", text: "edge" }) : null,
        edge.best_route === routeId ? el("span", { class: "badge route", text: "best" }) : null,
      ]),
      el("span", {}, ["net/bundle: ", el("b", netAttrs)]),
      el("span", {}, ["net at max size: ", el("b", { text: fmtCents(route.net_cents_at_max_size) })]),
      el("span", {}, [
        "max bundles: ",
        el("b", { text: fmtInt(route.max_bundles) }),
        route.bottleneck_ticker ? ` capped by ${route.bottleneck_ticker}` : null,
      ]),
      el("span", {}, ["max profit: ", el("b", { text: fmtDollars(route.max_profit_dollars) })]),
      el("span", {}, [
        "capital/bundle: ",
        el("b", { text: fmtCents(route.capital_per_bundle_unnetted_cents) }),
        " unnetted / ",
        el("b", { text: fmtCents(route.capital_per_bundle_netted_cents) }),
        " if netted",
      ]),
      el("span", {}, ["annualized: ", el("b", { text: fmtPct(route.annualized_return_pct) })]),
    ]);
  });

  const deltas = el("div", { class: "detail-head" }, [
    el("span", {
      title: edge.bid_delta_legs
        ? `Sell YES net ${fmtCents(edge.bid_delta_legs.sell_yes_net_cents)} (gross ${fmtCents(edge.bid_delta_legs.sell_yes_gross_cents)}) − Buy NO net ${fmtCents(edge.bid_delta_legs.buy_no_net_cents)} (gross ${fmtCents(edge.bid_delta_legs.buy_no_gross_cents)})`
        : null,
    }, ["bid-side Δ: ", el("b", { text: fmtSignedCents(edge.bid_delta_cents) })]),
    el("span", {
      title: edge.ask_delta_legs
        ? `Buy YES net ${fmtCents(edge.ask_delta_legs.buy_yes_net_cents)} (gross ${fmtCents(edge.ask_delta_legs.buy_yes_gross_cents)}) − Sell NO net ${fmtCents(edge.ask_delta_legs.sell_no_net_cents)} (gross ${fmtCents(edge.ask_delta_legs.sell_no_gross_cents)})`
        : null,
    }, ["ask-side Δ: ", el("b", { text: fmtSignedCents(edge.ask_delta_cents) })]),
    el("span", {
      title: edge.best_net_cents_per_bundle != null
        ? `${ROUTE_LABELS[edge.best_route] || "best"} · gross: ${fmtCents(edge.best_gross_cents)}`
        : null,
    }, [
      "best: ",
      el("b", { text: fmtCents(edge.best_net_cents_per_bundle) }),
      edge.best_route ? routeTag(edge.best_route) : null,
      edge.net_edge_positive ? el("span", { class: "badge edge", text: "edge" }) : null,
    ]),
    el("span", {}, ["days to close: ", el("b", { text: fmtPrice(edge.days_to_close) })]),
    el("span", {
      class: "muted",
      text: "estimates: assumes taker fees on every leg, series multiplier 1, top of book only. Visible figures are net; hover for gross.",
    }),
  ]);

  return el("div", { class: "edge-head" }, [
    el("div", { class: "route-grid" }, cards),
    deltas,
  ]);
}

function complementTitle(sum, left, right) {
  if (sum === null || sum === undefined || sum === "") return null;
  return `${left} + ${right} = ${sum} (vs 100)`;
}

function sizeCell(market, field) {
  const children = [fmtQty(market[field])];
  if (market.is_bottleneck && market.bottleneck_size_field === field) {
    children.push(el("span", { class: "badge bottleneck", text: "bottleneck" }));
  }
  return el("td", { class: "num" }, children);
}

function ladderTable(detail) {
  const markets = detail.markets || [];
  const head = el("tr", {}, [
    el("th", { text: "ticker" }),
    el("th", { text: "title" }),
    el("th", { class: "num", text: "yes bid" }),
    el("th", { class: "num", text: "yes ask" }),
    el("th", { class: "num", text: "no bid" }),
    el("th", { class: "num", text: "no ask" }),
    el("th", { class: "num", text: "spread" }),
    el("th", { class: "num", text: "yes bid size" }),
    el("th", { class: "num", text: "yes ask size" }),
    el("th", { class: "num", text: "no bid size" }),
    el("th", { class: "num", text: "no ask size" }),
    el("th", { class: "num", text: "est fee /contract" }),
    el("th", { class: "num", text: "vol 24h" }),
    el("th", { class: "num", text: "open interest" }),
  ]);

  const body = el("tbody", {}, markets.map((market) => {
    const rowClass = market.is_bottleneck ? "is-bottleneck" : null;

    return el("tr", { class: rowClass }, [
      el("td", { class: "mono" }, [
        String(market.ticker === null || market.ticker === undefined ? "-" : market.ticker),
      ]),
      el("td", { text: market.title === null || market.title === undefined ? "-" : String(market.title) }),
      el("td", {
        class: "num",
        text: fmtPrice(market.yes_bid_cents),
        title: complementTitle(market.yes_bid_plus_no_ask_cents, "yes bid", "no ask"),
      }),
      el("td", {
        class: "num",
        text: fmtPrice(market.yes_ask_cents),
        title: complementTitle(market.yes_ask_plus_no_bid_cents, "yes ask", "no bid"),
      }),
      el("td", {
        class: "num",
        text: fmtPrice(market.no_bid_cents),
        title: complementTitle(market.yes_ask_plus_no_bid_cents, "yes ask", "no bid"),
      }),
      el("td", {
        class: "num",
        text: fmtPrice(market.no_ask_cents),
        title: complementTitle(market.yes_bid_plus_no_ask_cents, "yes bid", "no ask"),
      }),
      el("td", { class: "num", text: fmtPrice(market.spread_cents) }),
      sizeCell(market, "yes_bid_size_contracts"),
      sizeCell(market, "yes_ask_size_contracts"),
      sizeCell(market, "no_bid_size_contracts"),
      sizeCell(market, "no_ask_size_contracts"),
      el("td", { class: "num", text: fmtCents(market.est_fee_cents_per_contract) }),
      el("td", { class: "num", text: fmtQty(market.volume_24h_contracts) }),
      el("td", { class: "num", text: fmtQty(market.open_interest_contracts) }),
    ]);
  }));

  if (markets.length === 0) {
    body.appendChild(
      el("tr", {}, [el("td", { colspan: String(LADDER_COLS), class: "muted", text: "this event has no markets" })])
    );
  }

  const totals = detail.ladder_totals || {};
  const summary = el("div", { class: "detail-head" }, [
    detail.sub_title ? el("span", {}, ["sub_title: ", el("b", { text: String(detail.sub_title) })]) : null,
    el("span", {}, ["yes bid sum: ", el("b", { text: fmtPrice(totals.yes_bid_sum_cents) })]),
    el("span", {}, ["yes ask sum: ", el("b", { text: fmtPrice(totals.yes_ask_sum_cents) })]),
    el("span", {}, ["no bid sum: ", el("b", { text: fmtPrice(totals.no_bid_sum_cents) })]),
    el("span", {}, ["no ask sum: ", el("b", { text: fmtPrice(totals.no_ask_sum_cents) })]),
    el("span", {}, ["mutually_exclusive: ", el("b", { text: String(Boolean(detail.mutually_exclusive)) })]),
    detail.close_time_basis ? el("span", { text: `close basis: ${detail.close_time_basis}` }) : null,
  ]);

  return el("div", {}, [
    summary,
    mispricingSummary(detail),
    el("table", { class: "ladder" }, [el("thead", {}, [head]), body]),
  ]);
}

function copyButton(text) {
  const button = el("button", { class: "copy-btn", type: "button", title: `Copy ${text}`, text: "copy" });
  button.addEventListener("click", async (event) => {
    // Stop the click from toggling the row it lives in.
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "copied";
    } catch (_) {
      button.textContent = "select it";
    }
    setTimeout(() => { button.textContent = "copy"; }, 1200);
  });
  return button;
}

function eventRow(item) {
  const closeText = fmtUtc(item.close_time);
  const until = untilText(item.close_time);
  const isPast = until !== null && until.endsWith("ago");

  const row = el("tr", {
    class: item.net_edge_positive ? "event-row has-edge" : "event-row",
    "data-ticker": item.event_ticker,
    "data-market-count": String(item.market_count ?? ""),
  }, [
    el("td", { class: "ticker-cell" }, [
      el("span", { class: "twisty", text: "\u25b8" }),
      item.event_ticker,
      copyButton(item.event_ticker),
    ]),
    el("td", { class: "title-cell", title: item.title || "" }, [
      item.title || "",
    ]),
    el("td", { class: "num", text: fmtInt(item.market_count) }),
    el("td", { class: "close-cell" }, [
      closeText || "-",
      until ? el("span", { class: isPast ? "until past" : "until", text: ` ${until}` }) : null,
    ]),
    el("td", { class: "num", text: fmtQty(item.max_volume_24h_contracts) }),
    el("td", { class: "num", text: fmtQty(item.max_open_interest_contracts) }),
    netCell(routeOf(item, "sell_yes"), "sell_yes"),
    netCell(routeOf(item, "buy_no"), "buy_no"),
    netCell(routeOf(item, "buy_yes"), "buy_yes"),
    netCell(routeOf(item, "sell_no"), "sell_no"),
    el("td", {
      class: "num",
      title: item.best_net_cents_per_bundle != null
        ? `${ROUTE_LABELS[item.best_route] || "best"} · gross: ${fmtCents(item.best_gross_cents)}`
        : null,
    }, [
      fmtCents(item.best_net_cents_per_bundle),
      item.best_route ? routeTag(item.best_route) : null,
      item.net_edge_positive ? el("span", { class: "badge edge", text: "edge" }) : null,
    ]),
    el("td", { class: "num", text: fmtInt(item.max_bundles) }),
  ]);

  row.addEventListener("click", () => toggleDetail(row, item.event_ticker));
  return row;
}

async function toggleDetail(row, ticker) {
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("detail-row")) {
    existing.remove();
    row.classList.remove("open");
    row.querySelector(".twisty").textContent = "\u25b8";
    return;
  }

  row.classList.add("open");
  row.querySelector(".twisty").textContent = "\u25be";
  // Must match the candidates table's column count, or the detail row shrinks.
  const cell = el("td", { colspan: String(EVENT_TABLE_COLS) }, [el("div", { class: "detail-loading", text: "loading ladder..." })]);
  const detailRow = el("tr", { class: "detail-row" }, [cell]);
  row.after(detailRow);

  const cacheKey = `${state.runId}\n${ticker}`;
  try {
    let detail = detailCache.get(cacheKey);
    if (!detail) {
      detail = await getJson(
        `/api/runs/${encodeURIComponent(state.runId)}/events/${encodeURIComponent(ticker)}`
      );
      detailCache.set(cacheKey, detail);
    }
    clear(cell);
    cell.appendChild(ladderTable(detail));
  } catch (error) {
    clear(cell);
    cell.appendChild(el("div", { class: "detail-loading", text: `could not load ladder: ${error.message}` }));
  }
}

function renderPager(payload) {
  const first = payload.total === 0 ? 0 : (payload.page - 1) * payload.page_size + 1;
  const last = Math.min(payload.page * payload.page_size, payload.total);
  dom.pageLabel.textContent =
    payload.total === 0
      ? "no matching events"
      : `${fmtInt(first)}-${fmtInt(last)} of ${fmtInt(payload.total)} | page ${fmtInt(payload.page)} of ${fmtInt(payload.total_pages)}`;
  dom.resultCount.textContent = `${fmtInt(payload.total)} events match`;

  const atFirst = payload.page <= 1;
  const atLast = payload.page >= payload.total_pages;
  dom.pageFirst.disabled = atFirst;
  dom.pagePrev.disabled = atFirst;
  dom.pageNext.disabled = atLast;
  dom.pageLast.disabled = atLast;
}

/* -- data loading -------------------------------------------------------- */

function eventListParams() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(PAGE_SIZE),
    sort: state.sort,
    order: state.order,
  });
  if (state.q) params.set("q", state.q);
  // Compared against "" rather than tested for truthiness: "0" is a real
  // threshold (break-even or better) but is falsy in JavaScript.
  if (state.minNetEdge !== "") params.set("min_net_edge_cents", state.minNetEdge);
  return params;
}

async function loadEvents() {
  const params = eventListParams();

  try {
    const payload = await getJson(
      `/api/runs/${encodeURIComponent(state.runId)}/events?${params.toString()}`
    );
    // A shrinking result set can leave us past the end; step back once.
    if (payload.total > 0 && payload.items.length === 0 && payload.page > payload.total_pages) {
      state.page = payload.total_pages;
      return loadEvents();
    }
    state.totalPages = payload.total_pages;
    clear(dom.eventsBody);
    for (const item of payload.items) dom.eventsBody.appendChild(eventRow(item));
    renderPager(payload);
    clearError();
  } catch (error) {
    showError(`Could not load events: ${error.message}`);
  }
}

function renderWatchlist(watchlist) {
  const rows = (watchlist && watchlist.watchlist) || [];
  clear(dom.watchlistBody);
  if (!rows.length) {
    dom.watchlistEmpty.hidden = false;
    dom.watchlistTable.hidden = true;
    if (watchlist && typeof watchlist.scoreable_count === "number") {
      dom.watchlistEmpty.textContent =
        `No events passed the four scoring gates (${watchlist.scoreable_count} scoreable). ` +
        `Route-net deltas need this snapshot and the previous one.`;
    } else {
      dom.watchlistEmpty.textContent =
        "No watchlist yet. A Lesser Cycle publishes one; a Greater Cycle leaves the last one in force.";
    }
    return;
  }
  dom.watchlistEmpty.hidden = true;
  dom.watchlistTable.hidden = false;
  for (const row of rows) {
    const routeId = row.best_route;
    dom.watchlistBody.appendChild(
      el("tr", {}, [
        el("td", { class: "num", text: row.rank != null ? String(row.rank) : "-" }),
        el("td", { class: "ticker-cell", text: row.event_ticker || "-" }),
        el("td", { class: "title-cell", text: row.title || "" }),
        el("td", { class: "num", text: fmtPrice(row.score) }),
        el("td", { class: "num", text: fmtCents(row.best_edge) }),
        el("td", {}, [routeId ? routeTag(routeId) : el("span", { text: "-" })]),
        el("td", { class: "num", text: fmtPrice(row.percentile_best_edge) }),
        el("td", { class: "num", text: fmtPrice(row.percentile_best_edge_route_delta) }),
        el("td", { class: "num", text: fmtPrice(row.percentile_best_route_delta) }),
        el("td", { class: "num", text: fmtPrice(row.percentile_turnover_velocity) }),
      ])
    );
  }
}

async function loadRun(runId) {
  state.runId = runId;
  state.page = 1;
  detailCache.clear();
  try {
    const run = await getJson(`/api/runs/${encodeURIComponent(runId)}`);
    renderFunnel(run.funnel || []);
    renderRunMeta(run);
    let watchlist = run.watchlist;
    if (!watchlist) {
      try {
        const inForce = await getJson("/api/watchlist");
        const sameEnv = !inForce.environment || inForce.environment === (run.meta || {}).environment;
        watchlist = sameEnv ? inForce : null;
      } catch (_) {
        watchlist = null;
      }
    }
    renderWatchlist(watchlist);
    dom.runView.hidden = false;
    dom.emptyState.hidden = true;
    await loadEvents();
  } catch (error) {
    showError(`Could not load run ${runId}: ${error.message}`);
  }
}

function runLabel(run) {
  const when = fmtUtc(run.filtered_at_utc) || run.filename;
  const count = typeof run.candidate_count === "number" ? `${run.candidate_count} candidates` : "?";
  return `${when}  ${run.environment || "?"}  (${count})`;
}

function showEmptyState() {
  state.environment = null;
  renderWatchlist(null);
  dom.runView.hidden = true;
  dom.emptyState.hidden = false;
  dom.emptyDir.textContent = state.candidatesDir || "the configured candidates directory";
}

/** Rebuild the run picker and show the newest run.

    Called on load and again after a cycle finishes, which is why it has to cope
    with both directions: an empty directory that now has a run, and a run list
    that is simply longer than before. loadRun() handles unhiding the table. */
async function loadRuns() {
  let runs = [];
  try {
    runs = await getJson("/api/runs");
  } catch (error) {
    showError(`Could not list runs: ${error.message}`);
    return;
  }

  clear(dom.runSelect);
  if (runs.length === 0) {
    showEmptyState();
    return;
  }

  for (const run of runs) {
    dom.runSelect.appendChild(el("option", { value: run.id, text: runLabel(run) }));
  }
  // The API returns newest first, so the first option is the freshest run.
  dom.runSelect.value = runs[0].id;
  await loadRun(runs[0].id);
}

/* -- cycle status -------------------------------------------------------- */

const CYCLE_STEP_LABELS = {
  collect: "collecting snapshot...",
  filter: "filtering candidates...",
  refresh: "refreshing quotes...",
  score: "scoring watchlist...",
};

function setCycleStatus(text, kind) {
  if (!dom.cycleStatus) return;
  dom.cycleStatus.textContent = text || "";
  dom.cycleStatus.className = kind ? `cycle-${kind}` : "";
}

function phaseLabel(status) {
  return (status && status.next_label) || "next cycle";
}

function formatCountdown(ms, label) {
  if (ms <= 0) return `${label} soon`;
  const totalSec = Math.ceil(ms / 1000);
  const minutes = Math.floor(totalSec / 60);
  const seconds = totalSec % 60;
  return `${label} in ${minutes}:${String(seconds).padStart(2, "0")}`;
}

function idleRemainingMs(status) {
  const until = Date.parse(status && status.idle_until_utc);
  return Number.isFinite(until) ? until - Date.now() : NaN;
}

function renderIdleStatus(status) {
  const label = phaseLabel(status);
  const remaining = idleRemainingMs(status);
  if (status && status.status === "failed") {
    const when = Number.isFinite(remaining) ? formatCountdown(remaining, "retry") : "retrying after the idle";
    setCycleStatus(`${label} failed, ${when}`, "failed");
    return;
  }
  const kind = status && status.status === "succeeded" ? "done" : "";
  if (Date.now() < state.cycleJustUpdatedUntil) {
    setCycleStatus("updated just now", "done");
    return;
  }
  if (!Number.isFinite(remaining)) {
    setCycleStatus(`idle until ${label}`, kind);
    return;
  }
  setCycleStatus(formatCountdown(remaining, label), kind);
}

function tickIdleCycleStatus() {
  const status = state.lastCycleStatus;
  if (!status || status.status === "running") return;
  renderIdleStatus(status);
}

async function pollCycle() {
  if (!state.cycleAvailable) return;
  let status;
  try {
    status = await getJson("/api/cycle/status");
  } catch (_) {
    setTimeout(pollCycle, CYCLE_POLL_MS);
    return;
  }

  state.lastCycleStatus = status;

  if (status.status === "running") {
    state.cycleWasRunning = true;
    const step = CYCLE_STEP_LABELS[status.step] || "working...";
    setCycleStatus(`${phaseLabel(status)}: ${step}`, "running");
  } else if (status.status === "failed") {
    state.cycleWasRunning = false;
    state.cycleJustUpdatedUntil = 0;
    renderIdleStatus(status);
  } else if (status.status === "succeeded" && state.cycleWasRunning) {
    state.cycleWasRunning = false;
    state.cycleJustUpdatedUntil = Date.now() + UPDATED_JUST_NOW_MS;
    setCycleStatus("updated just now", "done");
    await loadRuns();
  }

  setTimeout(pollCycle, CYCLE_POLL_MS);
}

/* -- trader log (view-only) ---------------------------------------------- */

async function getTrader() {
  let response;
  try {
    response = await fetch("/api/trader");
  } catch (_) {
    return {};
  }
  try {
    const body = await response.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body : {};
  } catch (_) {
    return {};
  }
}

function traderFills(trade) {
  if (!trade || trade.mode === "dry") return "not sent";
  if (trade.error) return String(trade.error);
  const legs = trade.legs || [];
  if (!legs.length) {
    if (trade.execution_ok === false) return "failed";
    return "-";
  }
  return legs.map((leg) => {
    const ticker = leg && leg.ticker ? String(leg.ticker) : "?";
    if (leg && leg.error) return `${ticker}: ${leg.error}`;
    const fill = leg && leg.fill_count != null && leg.fill_count !== "" ? String(leg.fill_count) : "-";
    return `${ticker} fill ${fill}`;
  }).join("; ");
}

/* Last-30 annualized samples, oldest at the left. That oldest value is the
   axis: later samples rise when the edge improves and fall when it weakens.
   Number() only places the curve; the hover title keeps the server's digits. */
const SVG_NS = "http://www.w3.org/2000/svg";
const SPARK_W = 96;
const SPARK_H = 26;

function svgEl(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of children || []) {
    if (child) node.appendChild(child);
  }
  return node;
}

function sparkNum(value) {
  return String(Math.round(value * 100) / 100);
}

function sampleAnnualized(sample) {
  const raw = sample && sample.annualized_pct;
  if (raw === null || raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function sparkHit(index, count) {
  if (count <= 1) return { x: 0, width: SPARK_W };
  const xAt = (i) => (i / (count - 1)) * SPARK_W;
  const left = index === 0 ? 0 : (xAt(index - 1) + xAt(index)) / 2;
  const right = index === count - 1 ? SPARK_W : (xAt(index) + xAt(index + 1)) / 2;
  return { x: left, width: right - left };
}

/* Insert a point on the axis wherever a segment changes sign, then keep the
   pieces above and below separate so each can be filled on its own side. */
function sparkSides(points, xAt, yAt, mid) {
  const up = [];
  const down = [];
  let run = [];

  const flush = () => {
    if (run.length >= 2) {
      const curve = [];
      for (const point of run) {
        const here = { x: xAt(point.index), y: yAt(point.delta), delta: point.delta };
        const prev = curve[curve.length - 1];
        if (prev && ((prev.delta > 0 && here.delta < 0) || (prev.delta < 0 && here.delta > 0))) {
          const t = prev.delta / (prev.delta - here.delta);
          curve.push({ x: prev.x + t * (here.x - prev.x), y: mid, delta: 0 });
        }
        curve.push(here);
      }
      for (let i = 1; i < curve.length; i++) {
        const a = curve[i - 1];
        const b = curve[i];
        if (a.delta === 0 && b.delta === 0) continue;
        (a.delta >= 0 && b.delta >= 0 ? up : down).push({ a, b });
      }
    }
    run = [];
  };

  for (const point of points) {
    if (point.delta === null) flush();
    else run.push(point);
  }
  flush();
  return { up, down };
}

function sparkArea(segments, mid) {
  const y0 = sparkNum(mid);
  return segments.map(({ a, b }) =>
    `M ${sparkNum(a.x)} ${sparkNum(a.y)} L ${sparkNum(b.x)} ${sparkNum(b.y)} ` +
    `L ${sparkNum(b.x)} ${y0} L ${sparkNum(a.x)} ${y0} Z`
  ).join(" ");
}

function sparkStroke(segments) {
  const cmds = [];
  let prev = null;
  for (const seg of segments) {
    if (prev && prev.b === seg.a) {
      cmds.push(`L ${sparkNum(seg.b.x)} ${sparkNum(seg.b.y)}`);
    } else {
      cmds.push(`M ${sparkNum(seg.a.x)} ${sparkNum(seg.a.y)}`);
      cmds.push(`L ${sparkNum(seg.b.x)} ${sparkNum(seg.b.y)}`);
    }
    prev = seg;
  }
  return cmds.join(" ");
}

function edgeSparkSvg(samples) {
  if (!samples.length) return null;
  const baseline = sampleAnnualized(samples[0]);
  if (baseline === null) return null;

  const points = samples.map((sample, index) => {
    const value = sampleAnnualized(sample);
    return {
      index,
      delta: value === null ? null : value - baseline,
      title: `${fmtUtc(sample && sample.time_utc) || "-"} · ${fmtPct(sample && sample.annualized_pct)}`,
    };
  });

  const mid = SPARK_H / 2;
  const half = mid - 1.5;
  let maxAbs = 0;
  for (const point of points) {
    if (point.delta !== null) maxAbs = Math.max(maxAbs, Math.abs(point.delta));
  }
  const count = points.length;
  const xAt = (index) => (count <= 1 ? 0 : (index / (count - 1)) * SPARK_W);
  const yAt = (delta) => (maxAbs === 0 ? mid : mid - (delta / maxAbs) * half);
  const sides = maxAbs === 0 ? { up: [], down: [] } : sparkSides(points, xAt, yAt, mid);

  const svg = svgEl("svg", {
    class: "edge-spark",
    viewBox: `0 0 ${SPARK_W} ${SPARK_H}`,
    width: SPARK_W,
    height: SPARK_H,
  });
  const upArea = sparkArea(sides.up, mid);
  const downArea = sparkArea(sides.down, mid);
  if (upArea) svg.appendChild(svgEl("path", { class: "fill-up", d: upArea }));
  if (downArea) svg.appendChild(svgEl("path", { class: "fill-down", d: downArea }));
  svg.appendChild(svgEl("line", { class: "axis", x1: 0, y1: mid, x2: SPARK_W, y2: mid }));
  const upStroke = sparkStroke(sides.up);
  const downStroke = sparkStroke(sides.down);
  if (upStroke) svg.appendChild(svgEl("path", { class: "stroke-up", d: upStroke }));
  if (downStroke) svg.appendChild(svgEl("path", { class: "stroke-down", d: downStroke }));

  for (const point of points) {
    const band = sparkHit(point.index, count);
    const title = svgEl("title");
    title.textContent = point.title;
    svg.appendChild(svgEl("rect", {
      class: "edge-spark-hit",
      x: sparkNum(band.x),
      y: 0,
      width: sparkNum(band.width),
      height: SPARK_H,
    }, [title]));
  }
  return svg;
}

function liveEdgeTicks(samples) {
  const chronological = Array.isArray(samples) ? samples.slice().reverse() : [];
  const spark = edgeSparkSvg(chronological);
  return el("td", { class: "edge-ticks" }, spark ? [spark] : []);
}

function renderLiveEdges(liveEdges) {
  if (!dom.liveEdgesBody || !dom.liveEdgesTable || !dom.liveEdgesEmpty) return;
  clear(dom.liveEdgesBody);
  const tickers = liveEdges && typeof liveEdges === "object" && !Array.isArray(liveEdges)
    ? Object.keys(liveEdges)
    : [];
  if (!tickers.length) {
    dom.liveEdgesEmpty.hidden = false;
    dom.liveEdgesTable.hidden = true;
    return;
  }
  dom.liveEdgesEmpty.hidden = true;
  dom.liveEdgesTable.hidden = false;
  for (const eventTicker of tickers) {
    const samples = Array.isArray(liveEdges[eventTicker]) ? liveEdges[eventTicker] : [];
    const latest = samples[0] || {};
    const routeId = latest.route;
    dom.liveEdgesBody.appendChild(
      el("tr", {}, [
        el("td", { class: "ticker-cell", text: eventTicker || "-" }),
        el("td", {}, [routeId ? routeTag(routeId) : el("span", { text: "-" })]),
        el("td", { class: "num", text: fmtCents(latest.net_per_bundle_cents) }),
        el("td", { class: "num", text: fmtPct(latest.annualized_pct) }),
        el("td", {}, [
          el("span", {
            class: latest.eligible ? "badge live-eligible" : "badge",
            text: latest.eligible ? "yes" : "no",
          }),
        ]),
        liveEdgeTicks(samples),
      ])
    );
  }
}

function inventoryRoute(position) {
  const routeId = position.route || position.pending_route;
  if (!routeId) return el("span", { text: "-" });
  return routeTag(routeId) || el("span", { text: routeId });
}

function inventoryLegsCell(legs) {
  const items = Array.isArray(legs) ? legs.filter((leg) => leg && typeof leg === "object") : [];
  if (!items.length) return el("td", { text: "-" });
  const counts = items.map((leg) => canonicalDecimal(leg.filled));
  const unbalanced = counts.some((count) => count !== counts[0]);
  const children = [];
  items.forEach((leg, index) => {
    if (index > 0) children.push(el("span", { class: "muted", text: " · " }));
    const attrs = { text: fmtQty(leg.filled == null || leg.filled === "" ? "0" : leg.filled) };
    if (leg.ticker) attrs.title = String(leg.ticker);
    children.push(el("span", attrs));
  });
  if (unbalanced) {
    children.push(el("span", { class: "badge unbalanced", text: "unbalanced" }));
  }
  return el("td", {}, children);
}

function renderShardCash(shardCash) {
  if (!dom.shardCash) return;
  const shards = shardCash && typeof shardCash === "object" && !Array.isArray(shardCash)
    ? shardCash.shards
    : null;
  if (!Array.isArray(shards)) {
    dom.shardCash.textContent = "Shard cash: not read yet";
    return;
  }
  const parts = [];
  for (const row of shards) {
    if (!row || typeof row !== "object") continue;
    const dollars = dollarsFromCents(row.cash_cents);
    const amount = dollars === null ? "-" : fmtDollars(dollars);
    parts.push(`shard ${row.exchange_index}: ${amount}`);
  }
  const body = parts.length ? parts.join(" · ") : "none";
  const updated = fmtUtc(shardCash.updated_at_utc);
  dom.shardCash.textContent = updated
    ? `${body} · updated ${updated}`
    : body;
}

function renderInventory(positions, capCents) {
  if (!dom.inventoryBody || !dom.inventoryTable || !dom.inventoryEmpty) return;
  clear(dom.inventoryBody);
  const entries = positions && typeof positions === "object" && !Array.isArray(positions)
    ? Object.entries(positions).filter(([, position]) => position && typeof position === "object")
    : [];
  if (!entries.length) {
    dom.inventoryEmpty.hidden = false;
    dom.inventoryTable.hidden = true;
    return;
  }
  entries.sort((left, right) => {
    const leftTime = Date.parse(left[1].close_time);
    const rightTime = Date.parse(right[1].close_time);
    const leftKey = Number.isNaN(leftTime) ? Infinity : leftTime;
    const rightKey = Number.isNaN(rightTime) ? Infinity : rightTime;
    if (leftKey !== rightKey) return leftKey - rightKey;
    return left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0;
  });
  dom.inventoryEmpty.hidden = true;
  dom.inventoryTable.hidden = false;
  const filledFallback = "0";
  for (const [eventTicker, position] of entries) {
    const filled = position.filled_capital_cents == null || position.filled_capital_cents === ""
      ? filledFallback
      : position.filled_capital_cents;
    const remaining = capCents == null || capCents === ""
      ? null
      : subtractDecimalStrings(capCents, filled);
    dom.inventoryBody.appendChild(
      el("tr", {}, [
        el("td", { class: "ticker-cell", text: eventTicker || "-" }),
        el("td", {}, [inventoryRoute(position)]),
        el("td", { class: "num", text: fmtCents(filled) }),
        el("td", { class: "num", text: fmtCents(remaining) }),
        el("td", { text: fmtUtc(position.close_time) || "-" }),
        inventoryLegsCell(position.legs),
      ])
    );
  }
}

function renderTradeLog(trades) {
  if (!dom.traderBody || !dom.traderTable || !dom.traderEmpty) return;
  clear(dom.traderBody);
  if (!trades.length) {
    dom.traderEmpty.hidden = false;
    dom.traderTable.hidden = true;
    return;
  }
  dom.traderEmpty.hidden = true;
  dom.traderTable.hidden = false;
  for (const trade of trades.slice(0, 10)) {
    const routeId = trade.route;
    dom.traderBody.appendChild(
      el("tr", {}, [
        el("td", { class: "mono", text: fmtUtc(trade.time_utc) || "-" }),
        el("td", {}, [
          el("span", {
            class: trade.mode === "live" ? "badge trader-live" : "badge",
            text: trade.mode === "live" ? "live" : "dry",
          }),
        ]),
        el("td", { class: "ticker-cell", text: trade.event_ticker || "-" }),
        el("td", {}, [routeId ? routeTag(routeId) : el("span", { text: "-" })]),
        el("td", { class: "num", text: fmtBundles(tradeBundles(trade)) }),
        el("td", { class: "num", text: fmtCents(trade.expected_pnl_cents) }),
        el("td", { class: "num", text: fmtCents(trade.expected_capital_locked_cents) }),
        el("td", { class: "num", text: fmtPct(trade.annualized_pct) }),
        el("td", { text: trade.lockup_text || "-" }),
        el("td", { class: "fills-cell", text: traderFills(trade) }),
      ])
    );
  }
}

function renderTrader(payload) {
  if (!dom.traderState) return;
  const supervisor = (payload && payload.supervisor) || {};
  const dry = payload && Object.prototype.hasOwnProperty.call(payload, "dry_mode")
    ? Boolean(payload.dry_mode)
    : supervisor.dry_mode !== false;
  const mode = dry ? "DRY" : "LIVE";
  const parts = [mode];
  if (supervisor.enabled === false) {
    parts.push("not supervised");
  } else if (supervisor.process_up === true) {
    parts.push("process up");
  } else if (supervisor.process_up === false) {
    parts.push("process down");
  }
  if (payload && payload.ws_connected === true) parts.push("ws connected");
  else if (payload && payload.ws_connected === false) parts.push("ws down");
  if (payload && payload.watchlist_id) parts.push(String(payload.watchlist_id));
  if (parts.length === 1 && !(payload && payload.last_trades) && supervisor.process_up == null) {
    dom.traderState.textContent = state.traderAvailable ? "waiting for trader status" : "trader unavailable";
  } else {
    dom.traderState.textContent = parts.join(" · ");
  }
  dom.traderState.className = dry ? "muted" : "trader-live";

  renderLiveEdges((payload && payload.live_edges) || {});
  renderShardCash(payload && payload.shard_cash);
  renderInventory(
    (payload && payload.positions) || {},
    payload && payload.max_capital_per_event_cents
  );
  renderTradeLog((payload && payload.last_trades) || []);
}

async function pollTrader() {
  try {
    renderTrader(await getTrader());
  } catch (_) {
    if (dom.traderState) dom.traderState.textContent = "could not read trader status";
  }
  setTimeout(pollTrader, TRADER_POLL_MS);
}

/* -- startup ------------------------------------------------------------- */

async function loadConfig() {
  try {
    const config = await getJson("/api/config");
    state.candidatesDir = config.candidates_dir;
    state.cycleAvailable = Boolean(config.cycle_available);
    state.traderAvailable = Boolean(config.trader_available);
    const idle = Number(config.cycle_idle_minutes);
    if (Number.isFinite(idle) && idle >= 1) {
      state.cycleIdleMinutes = idle;
    }
  } catch (_) {
    state.cycleAvailable = false;
    state.traderAvailable = false;
  }
  if (dom.cycleControls) dom.cycleControls.hidden = !state.cycleAvailable;
}

async function init() {
  await loadConfig();
  await loadRuns();
  pollTrader();
  if (state.cycleAvailable) {
    try {
      const status = await getJson("/api/cycle/status");
      state.lastCycleStatus = status;
      if (status.status === "running") {
        state.cycleWasRunning = true;
        const step = CYCLE_STEP_LABELS[status.step] || "working...";
        setCycleStatus(`${phaseLabel(status)}: ${step}`, "running");
      } else if (status.status === "failed") {
        renderIdleStatus(status);
      } else {
        renderIdleStatus(status);
      }
    } catch (_) {
      /* dashboard can still show files */
    }
    pollCycle();
    setInterval(tickIdleCycleStatus, CYCLE_TICK_MS);
  }
}

/* -- events -------------------------------------------------------------- */

dom.runSelect.addEventListener("change", () => loadRun(dom.runSelect.value));

let searchTimer = null;
dom.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = dom.search.value;
    state.page = 1;
    loadEvents();
  }, 250);
});

/* sell and NO share the dashboard red; buy and YES share its cyan. The other
   words stay the normal amber. A row can carry both colors. */
const SORT_TONE = { sell: "tone-bad", NO: "tone-bad", buy: "tone-good", YES: "tone-good" };

function sortLabelNodes(text) {
  return text.split(/(\b(?:sell|buy|YES|NO)\b)/).filter((part) => part !== "").map((part) => {
    const tone = SORT_TONE[part];
    return tone ? el("span", { class: tone }, [part]) : part;
  });
}

function paintSortLabel(node) {
  const text = node.textContent;
  clear(node);
  for (const child of sortLabelNodes(text)) {
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
}

function sortOptions() {
  return [...dom.sortList.querySelectorAll("[role='option']")];
}

function selectedSortOption() {
  return dom.sortList.querySelector("[aria-selected='true']") || sortOptions()[0];
}

function syncSortButton() {
  clear(dom.sortButton);
  for (const child of selectedSortOption().childNodes) {
    dom.sortButton.appendChild(child.cloneNode(true));
  }
}

let sortOpen = false;

function placeSortList() {
  const rect = dom.sortButton.getBoundingClientRect();
  dom.sortList.style.left = `${rect.left}px`;
  dom.sortList.style.minWidth = `${rect.width}px`;
  const listHeight = dom.sortList.offsetHeight;
  const spaceBelow = window.innerHeight - rect.bottom;
  const top = spaceBelow >= listHeight || spaceBelow >= rect.top
    ? rect.bottom
    : Math.max(8, rect.top - listHeight);
  dom.sortList.style.top = `${top}px`;
}

function setSortActive(option) {
  for (const row of sortOptions()) row.classList.toggle("is-active", row === option);
  if (option) dom.sortButton.setAttribute("aria-activedescendant", option.id);
}

function setSortOpen(open) {
  sortOpen = open;
  dom.sortButton.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) {
    document.body.appendChild(dom.sortList);
    dom.sortList.hidden = false;
    placeSortList();
    setSortActive(selectedSortOption());
    return;
  }
  dom.sortList.hidden = true;
  dom.sortMenu.appendChild(dom.sortList);
  dom.sortButton.removeAttribute("aria-activedescendant");
  for (const row of sortOptions()) row.classList.remove("is-active");
}

function chooseSort(option) {
  const [sort, order] = option.dataset.value.split(":");
  const changed = state.sort !== sort || state.order !== order;
  for (const row of sortOptions()) {
    row.setAttribute("aria-selected", row === option ? "true" : "false");
  }
  syncSortButton();
  setSortOpen(false);
  dom.sortButton.focus();
  if (!changed) return;
  state.sort = sort;
  state.order = order;
  state.page = 1;
  loadEvents();
}

for (const option of sortOptions()) paintSortLabel(option);
syncSortButton();

dom.sortButton.addEventListener("click", () => {
  setSortOpen(!sortOpen);
});

/* The options are not focusable, so pressing one would blur the button and let
   the blur handler close the list before the click lands. */
dom.sortList.addEventListener("mousedown", (event) => {
  event.preventDefault();
});

dom.sortList.addEventListener("click", (event) => {
  const option = event.target.closest("[role='option']");
  if (!option || !dom.sortList.contains(option)) return;
  chooseSort(option);
});

dom.sortButton.addEventListener("keydown", (event) => {
  const options = sortOptions();
  const active = dom.sortList.querySelector(".is-active") || selectedSortOption();
  const index = options.indexOf(active);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!sortOpen) {
      setSortOpen(true);
      return;
    }
    const next = event.key === "ArrowDown"
      ? Math.min(options.length - 1, index + 1)
      : Math.max(0, index - 1);
    setSortActive(options[next]);
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (!sortOpen) setSortOpen(true);
    else chooseSort(active);
    return;
  }
  if (event.key === "Escape" && sortOpen) {
    event.preventDefault();
    setSortOpen(false);
  }
});

document.addEventListener("click", (event) => {
  if (!sortOpen) return;
  if (dom.sortMenu.contains(event.target) || dom.sortList.contains(event.target)) return;
  setSortOpen(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sortOpen) setSortOpen(false);
});

window.addEventListener("resize", () => {
  if (sortOpen) setSortOpen(false);
});

window.addEventListener("scroll", (event) => {
  if (!sortOpen || event.target === dom.sortList) return;
  setSortOpen(false);
}, true);

dom.sortButton.addEventListener("blur", () => {
  setTimeout(() => {
    if (!sortOpen) return;
    if (document.activeElement === dom.sortButton) return;
    setSortOpen(false);
  }, 0);
});

dom.minEdgeSelect.addEventListener("change", () => {
  state.minNetEdge = dom.minEdgeSelect.value;
  state.page = 1;
  loadEvents();
});

function goToPage(page) {
  const target = Math.min(Math.max(1, page), state.totalPages);
  if (target === state.page) return;
  state.page = target;
  loadEvents();
}

dom.pageFirst.addEventListener("click", () => goToPage(1));
dom.pagePrev.addEventListener("click", () => goToPage(state.page - 1));
dom.pageNext.addEventListener("click", () => goToPage(state.page + 1));
dom.pageLast.addEventListener("click", () => goToPage(state.totalPages));

/* Two fainter copies of the lotus, shifted into the empty space on either
   side and drawn behind the original. Same node, so they share its size. */
function placeAsciiGhosts() {
  const art = document.querySelector(".ascii-stage > .ascii-art");
  if (!art) return;
  for (const side of ["left", "right"]) {
    const ghost = art.cloneNode(true);
    ghost.classList.add("ascii-ghost", `ascii-ghost-${side}`);
    art.before(ghost);
  }
}

placeAsciiGhosts();
init();
