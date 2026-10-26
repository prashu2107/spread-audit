"""
spread_audit — what is this strategy's edge NET of trading costs?

Standalone by design: one file, standard library only, no install step.
Point it at a trade log and it reports gross EV, cost drag and net EV per
trade, in R.

    python spread_audit.py trades.csv --spread 1.5
    python spread_audit.py deals.csv --rr 2 --stop-pips 12 --commission-pips 0.2
    python spread_audit.py trades.csv --json > audit.json

WHY THIS EXISTS
===============
Most backtests compute expected value straight from the win rate:

    ev = win_rate * rr - (1 - win_rate)

That figure is gross of every cost, and the omission hides unusually well:
every reported row reproduces exactly from its win rate alone, so there is
no internal inconsistency to notice. A model showing +0.116R can be
-0.009R once the spread is charged, and nothing in the output says so.

The correction reduces to a single identity:

    cost in R  =  cost in price terms / stop distance

Risk per unit IS the stop distance, so any cost quoted in the same units
as the stop converts to R by dividing by it. No position size, account
currency or balance is needed. It applies to every trade, win or lose,
which is why it shifts EV one-for-one and cannot be averaged away by
taking more trades.

Two consequences that routinely surprise people:

  * TIGHTER STOPS COST MORE. The same cash risk spread over a shorter stop
    buys more units, and every unit crosses the same spread. At a 1.5 pip
    spread a 12 pip stop pays 0.125R; a 40 pip stop pays 0.037R. Stop
    width is a cost decision, not only a hit-rate decision.
  * THE BREAKEVEN WIN RATE MOVES. At RR=2 it is not 33.3% but
    (1 + cost_R) / (1 + RR) — 37.5% at 0.125R of drag.

WHAT THIS TOOL WILL NOT DO
==========================
It does not invent data. Every figure in the report is labelled MEASURED
(read from the log) or ASSUMED (supplied by a flag or a default), because
a cost audit whose inputs are guesses is just arithmetic with extra steps.
If the log carries no spread column, the honest output is a range across
plausible spreads, which is what the sensitivity grid is for.

It also does not model: partial fills, slippage beyond the spread,
borrow/financing other than the swap model below, currency conversion on
the cost side, or latency. Those push the real number further down, never
up, so treat net EV here as an upper bound.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

VERSION = "1.0"

# A "pip" is a convention, not a fact about the market, so it has to be
# either told to us or inferred from price magnitude. These cover the FX
# majors; anything else (metals, indices, crypto) should pass --pip.
PIP_FX = 0.0001
PIP_JPY = 0.01

MEASURED = "measured"
ASSUMED = "assumed"


# --------------------------------------------------------------------------
# Column mapping
#
# Every platform names its export columns differently, and a tool that only
# reads one layout is a tool nobody else can run. Aliases are matched after
# normalising case, spaces, slashes and underscores away.
# --------------------------------------------------------------------------

ALIASES: dict[str, tuple[str, ...]] = {
    "direction": ("direction", "side", "type", "action", "buysell", "longshort",
                  "ordertype", "dealtype", "position"),
    "entry": ("entry", "entryprice", "openprice", "priceopen", "open", "fillprice",
              "entry_px", "avgentryprice"),
    "stop": ("stop", "stopprice", "sl", "stoploss", "slprice", "stopl"),
    "target": ("target", "targetprice", "tp", "takeprofit", "tpprice", "takeprofitprice"),
    "exit": ("exit", "exitprice", "closeprice", "priceclose", "close", "exit_px"),
    "won": ("won", "win", "iswin", "result", "outcome", "status"),
    "pnl": ("pnl", "profit", "netprofit", "netpnl", "pl", "profitloss", "gain",
            "realisedpnl", "realizedpnl"),
    "r": ("r", "rmultiple", "rmult", "rrrealised", "rrrealized", "rvalue",
          "pnlr", "rresult"),
    "lots": ("lots", "lot", "volume", "size", "qty", "quantity", "contracts"),
    "units": ("units", "unitsize", "notionalunits"),
    "spread": ("spread", "spreadpips", "spreadpoints", "entryspread"),
    "stop_pips": ("stoppips", "stopdistancepips", "slpips", "stopdistance", "riskpips"),
    "entry_time": ("entrytime", "opentime", "timeopen", "time", "datetime", "date",
                   "openedat", "entrydate"),
    "exit_time": ("exittime", "closetime", "timeclose", "closedat", "exitdate"),
}

LONG_WORDS = {"buy", "long", "bullish", "bull", "b", "1", "+1", "up", "dealtypebuy"}
SHORT_WORDS = {"sell", "short", "bearish", "bear", "s", "-1", "down", "dealtypesell"}
WIN_WORDS = {"1", "true", "t", "yes", "y", "win", "won", "profit", "tp", "takeprofit", "target"}
LOSS_WORDS = {"0", "false", "f", "no", "n", "loss", "lost", "lose", "sl", "stoploss", "stopped"}

# A log row may legitimately say "this trade never resolved". Those are
# dropped rather than counted as losses, which is the mistake that makes a
# strategy look worse than it is.
OPEN_WORDS = {"open", "running", "active", "none", "", "pending", "unresolved", "na", "nan"}


def normalise(name: str) -> str:
    """Fold a header into a comparable key: lowercase, alphanumerics only."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


# A prefix match shorter than this is not evidence of anything: with
# 2-character aliases allowed, the header "sl" would satisfy the alias
# "slpips" and a stop PRICE would be read as a stop DISTANCE.
MIN_PREFIX = 3


def find_column(headers: list[str], field_name: str,
                taken: set[str] | None = None) -> str | None:
    """Best header match for a logical field, exact alias before prefix.

    Exact matches are tried across ALL aliases before any prefix match, so
    a log with both "profit" and "profit_pips" binds `pnl` to "profit"
    whatever the column order. Prefix matching only ever grows a header
    ("stoppips" matching "stoppipsavg"), never shrinks one — matching an
    alias against a shorter header is what let "sl" pose as "slpips".
    """
    taken = taken or set()
    norm = {normalise(h): h for h in headers if h not in taken}
    for alias in ALIASES[field_name]:
        if alias in norm:
            return norm[alias]
    for alias in ALIASES[field_name]:
        if len(alias) < MIN_PREFIX:
            continue
        for key, original in norm.items():
            if key.startswith(alias):
                return original
    return None


def map_columns(headers: list[str]) -> dict[str, str | None]:
    """Bind every logical field to at most one header, exclusively.

    Exclusivity is the point. "entry_time" exactly matches the entry_time
    field, and without consuming it the looser `entry` prefix would also
    claim it and then try to parse a date as a price. Exact matches are
    resolved for all fields first, so a precise header is never eaten by
    another field's prefix.
    """
    cols: dict[str, str | None] = {name: None for name in ALIASES}
    taken: set[str] = set()
    for exact_only in (True, False):
        for name, aliases in ALIASES.items():
            if cols[name] is not None:
                continue
            norm = {normalise(h): h for h in headers if h not in taken}
            match = None
            for alias in aliases:
                if alias in norm:
                    match = norm[alias]
                    break
            if match is None and not exact_only:
                match = find_column(headers, name, taken)
            if match is not None:
                cols[name] = match
                taken.add(match)
    return cols


def parse_direction(raw: str) -> str | None:
    """-> 'long' | 'short' | None. Swap differs by side, so this matters."""
    text = str(raw).strip()
    # Signed numeric encodings have to be read before normalise() strips
    # the minus sign and turns -1 into 1.
    numeric = parse_float(text)
    if numeric is not None and text.lstrip("+-").replace(".", "", 1).isdigit():
        if numeric > 0:
            return "long"
        if numeric < 0:
            return "short"
        return None
    key = normalise(text)
    if key in LONG_WORDS:
        return "long"
    if key in SHORT_WORDS:
        return "short"
    # MT5 exports write "DEAL_TYPE_BUY" / "Buy limit" / "position_short".
    if "buy" in key or "long" in key or "bull" in key:
        return "long"
    if "sell" in key or "short" in key or "bear" in key:
        return "short"
    return None


def parse_outcome(raw: str) -> bool | None:
    """-> True (win) | False (loss) | None (unresolved / unreadable)."""
    key = normalise(str(raw))
    if key in OPEN_WORDS:
        return None
    if key in WIN_WORDS:
        return True
    if key in LOSS_WORDS:
        return False
    value = parse_float(raw)
    if value is not None:
        # A zero-P&L row is a scratch, not a win; callers treat it via R.
        return value > 0
    return None


def parse_float(raw) -> float | None:
    """Tolerant numeric parse: handles 1,234.50, (12.30), £, %, blanks."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or normalise(text) in {"na", "nan", "none", "null"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    for junk in "()$£€¥%,  ":
        text = text.replace(junk, "")
    text = text.replace(" ", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def parse_time(raw) -> datetime | None:
    """Parse a timestamp, discarding any timezone.

    Swap is charged on date boundaries in the BROKER's day, and a log
    rarely says what that day is. Normalising everything to naive local
    values keeps the night count self-consistent; it can be off by one
    night per trade near midnight, which the report states outright.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Trailing 'Z' and +00:00 offsets are common in exports; fromisoformat
    # handles most, so try it before the format list.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            # Timezone is dropped deliberately; see the docstring above.
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def infer_pip_size(prices: list[float]) -> float:
    """Guess pip size from price magnitude. JPY-quoted pairs run 20-1000."""
    usable = [p for p in prices if p and p > 0]
    if not usable:
        return PIP_FX
    median = statistics.median(usable)
    return PIP_JPY if 20.0 <= median < 1000.0 else PIP_FX


# A 5-digit FX pair has ten points to the pip, and MT5 writes the spread
# column in POINTS. Judging that per row is wrong: on one log a 14 stays
# "14 pips" while a 21 becomes "2.1 pips", mixing two units inside the same
# average. The unit is a property of the COLUMN, so decide it once.
POINTS_PER_PIP = 10.0
# Above this, a spread quoted in pips is not credible for any liquid FX
# pair — even a news-time blowout rarely clears 10 pips, while the same
# figure in points (100) is unremarkable.
MAX_CREDIBLE_PIP_SPREAD = 10.0


def resolve_spread_units(values: list[float], mode: str = "auto") -> tuple[float, str]:
    """-> (divisor, description). Applies to the whole spread column."""
    if mode == "pips":
        return 1.0, "pips (forced by --spread-units)"
    if mode == "points":
        return POINTS_PER_PIP, "points (forced by --spread-units), /10 to pips"
    usable = [v for v in values if v > 0]
    if not usable:
        return 1.0, "pips (no values to judge)"
    if statistics.median(usable) > MAX_CREDIBLE_PIP_SPREAD:
        return POINTS_PER_PIP, f"points (median {statistics.median(usable):.0f}), /10 to pips"
    return 1.0, f"pips (median {statistics.median(usable):.2f})"


def nights_held(entry: datetime | None, exit_: datetime | None) -> int:
    """Daily rollovers crossed — what swap is actually billed on.

    Counts date boundaries, not 24-hour blocks: opened 23:00 and closed
    01:00 is one night despite lasting two hours, which is how a broker
    bills it.
    """
    if entry is None or exit_ is None or exit_ <= entry:
        return 0
    return max(0, (exit_.date() - entry.date()).days)


def swap_pips_per_night(annual_pct: float, price: float, pip: float) -> float:
    """Convert a percentage swap rate into pips charged per night.

    MT5 quotes swap "in percentage terms, using current price". A night
    costs `price * pct/100 / 360` in price terms; dividing by pip size puts
    it on the same scale as the spread. Sign is dropped — the caller knows
    whether it is a cost or a credit.
    """
    if not annual_pct or not price or not pip:
        return 0.0
    return abs(price * (annual_pct / 100.0) / 360.0) / pip


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------

@dataclass
class Trade:
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    stop_pips: float | None = None
    stop_pips_source: str = ASSUMED
    rr: float | None = None
    rr_source: str = ASSUMED
    gross_r: float | None = None
    gross_r_source: str = ASSUMED
    won: bool | None = None
    spread_pips: float | None = None
    spread_source: str = ASSUMED
    nights: int = 0
    nights_known: bool = False
    lots: float | None = None
    row: int = 0


@dataclass
class Options:
    rr: float = 2.0
    spread_pips: float = 1.5
    stop_pips: float | None = None
    pip: float | None = None
    crossings: float = 1.0
    spread_units: str = "auto"
    commission_pips: float = 0.0
    commission_per_lot: float = 0.0
    risk_amount: float | None = None
    swap_pct_long: float = 0.0
    swap_pct_short: float = 0.0
    nights_default: float = 0.0
    bootstrap: int = 2000
    seed: int = 7
    grid_spreads: list[float] = field(default_factory=lambda: [1.0, 1.2, 1.5, 2.0, 2.5])
    grid_stops: list[float] = field(default_factory=lambda: [12, 16, 20, 30, 40])


def load_trades(path: Path, opts: Options) -> tuple[list[Trade], list[str], dict]:
    """Read a trade log into Trades, reporting what was found and inferred.

    Returns (trades, warnings, provenance). Rows that cannot yield a gross
    R are dropped and counted — never silently treated as losses.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"{path}: no data rows")

    headers = [h for h in (rows[0].keys() or []) if h]
    cols = map_columns(headers)

    prices = [parse_float(r.get(cols["entry"])) for r in rows] if cols["entry"] else []
    pip = opts.pip or infer_pip_size([p for p in prices if p is not None])

    logged_spreads = ([parse_float(r.get(cols["spread"])) for r in rows]
                      if cols["spread"] else [])
    spread_divisor, spread_unit = resolve_spread_units(
        [v for v in logged_spreads if v is not None and v >= 0], opts.spread_units)

    trades: list[Trade] = []
    warnings: list[str] = []
    dropped = {"no_outcome": 0, "no_stop": 0}

    for index, row in enumerate(rows, start=2):  # header is line 1
        trade = Trade(row=index)
        trade.direction = parse_direction(row.get(cols["direction"], "")) if cols["direction"] else None
        trade.entry = parse_float(row.get(cols["entry"])) if cols["entry"] else None
        trade.stop = parse_float(row.get(cols["stop"])) if cols["stop"] else None
        trade.target = parse_float(row.get(cols["target"])) if cols["target"] else None
        trade.lots = parse_float(row.get(cols["lots"])) if cols["lots"] else None

        # --- stop distance, the denominator of every cost figure ---
        logged_stop_pips = parse_float(row.get(cols["stop_pips"])) if cols["stop_pips"] else None
        if logged_stop_pips is not None and logged_stop_pips > 0:
            trade.stop_pips, trade.stop_pips_source = abs(logged_stop_pips), MEASURED
        elif trade.entry is not None and trade.stop is not None and trade.entry != trade.stop:
            trade.stop_pips, trade.stop_pips_source = abs(trade.entry - trade.stop) / pip, MEASURED
        elif opts.stop_pips:
            trade.stop_pips, trade.stop_pips_source = opts.stop_pips, ASSUMED
        else:
            dropped["no_stop"] += 1
            continue

        # --- reward:risk, per trade where the log allows it ---
        if trade.target is not None and trade.entry is not None and trade.stop_pips:
            trade.rr = abs(trade.target - trade.entry) / pip / trade.stop_pips
            trade.rr_source = MEASURED
        else:
            trade.rr, trade.rr_source = opts.rr, ASSUMED

        # --- outcome and gross R ---
        logged_r = parse_float(row.get(cols["r"])) if cols["r"] else None
        pnl = parse_float(row.get(cols["pnl"])) if cols["pnl"] else None
        won = parse_outcome(row.get(cols["won"], "")) if cols["won"] else None

        if logged_r is not None:
            trade.gross_r, trade.gross_r_source = logged_r, MEASURED
            trade.won = logged_r > 0
        elif pnl is not None and opts.risk_amount:
            trade.gross_r, trade.gross_r_source = pnl / opts.risk_amount, MEASURED
            trade.won = pnl > 0
        elif won is not None:
            trade.won = won
            trade.gross_r = trade.rr if won else -1.0
            # Measured only in the sense that the WIN/LOSS is real; the
            # magnitude comes from the assumed RR unless the log gave one.
            trade.gross_r_source = MEASURED if trade.rr_source == MEASURED else ASSUMED
        elif pnl is not None:
            trade.won = pnl > 0
            trade.gross_r = trade.rr if trade.won else -1.0
            trade.gross_r_source = ASSUMED
        else:
            dropped["no_outcome"] += 1
            continue

        # --- spread ---
        logged_spread = parse_float(row.get(cols["spread"])) if cols["spread"] else None
        if logged_spread is not None and logged_spread >= 0:
            trade.spread_pips = logged_spread / spread_divisor
            trade.spread_source = MEASURED
        else:
            trade.spread_pips, trade.spread_source = opts.spread_pips, ASSUMED

        # --- nights held, for swap ---
        entry_time = parse_time(row.get(cols["entry_time"])) if cols["entry_time"] else None
        exit_time = parse_time(row.get(cols["exit_time"])) if cols["exit_time"] else None
        if entry_time and exit_time:
            trade.nights, trade.nights_known = nights_held(entry_time, exit_time), True
        else:
            trade.nights, trade.nights_known = opts.nights_default, False

        trades.append(trade)

    if not trades:
        raise SystemExit(
            f"{path}: no usable trades.\n"
            f"  Needed: an outcome (won/result/profit/R) and a stop distance\n"
            f"  (stop_pips, or entry+stop prices, or --stop-pips).\n"
            f"  Headers seen: {', '.join(headers)}"
        )

    for reason, count in dropped.items():
        if count:
            warnings.append({"no_outcome": f"{count} row(s) dropped: no readable outcome",
                             "no_stop": f"{count} row(s) dropped: no stop distance"}[reason])

    provenance = {
        "pip_size": pip,
        "spread_units": spread_unit,
        "pip_source": MEASURED if opts.pip else "inferred from price magnitude",
        "columns": {k: v for k, v in cols.items() if v},
        "rows_read": len(rows),
        "trades_used": len(trades),
        "dropped": dropped,
    }
    return trades, warnings, provenance


# --------------------------------------------------------------------------
# The audit
# --------------------------------------------------------------------------

def cost_r(trade: Trade, opts: Options) -> dict[str, float]:
    """Per-trade cost decomposition, in R.

    Everything lands on the same denominator — the stop distance — because
    that is what one R is worth in price terms.
    """
    stop = trade.stop_pips or 0.0
    if stop <= 0:
        return {"spread": 0.0, "commission": 0.0, "swap": 0.0, "total": 0.0}

    spread = (trade.spread_pips or 0.0) * opts.crossings / stop

    commission = opts.commission_pips / stop
    if opts.commission_per_lot and trade.lots and opts.risk_amount:
        commission += (opts.commission_per_lot * abs(trade.lots)) / opts.risk_amount

    swap = 0.0
    pct = opts.swap_pct_long if trade.direction != "short" else opts.swap_pct_short
    if pct and trade.entry and trade.nights:
        pip = opts.pip or (PIP_JPY if trade.entry >= 20 else PIP_FX)
        swap = swap_pips_per_night(pct, trade.entry, pip) * trade.nights / stop

    return {"spread": spread, "commission": commission, "swap": swap,
            "total": spread + commission + swap}


def bootstrap_ci(values: list[float], iterations: int, seed: int,
                 alpha: float = 0.05) -> tuple[float, float] | None:
    """Percentile CI for the MEAN of per-trade R, by resampling trades.

    Reported because a mean EV from 15 trades and one from 1,500 are not
    the same claim, and a cost audit that hides that is selling confidence
    it does not have.
    """
    if iterations <= 0 or len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    lo = means[max(0, int(alpha / 2 * iterations) - 1)]
    hi = means[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return lo, hi


def audit(trades: list[Trade], opts: Options) -> dict:
    """Compute the whole report as plain data, so --json and text agree."""
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    gross = [t.gross_r or 0.0 for t in trades]
    costs = [cost_r(t, opts) for t in trades]
    net = [g - c["total"] for g, c in zip(gross, costs)]

    gross_ev = sum(gross) / n
    net_ev = sum(net) / n
    drag = gross_ev - net_ev

    mean_rr = statistics.mean([t.rr or opts.rr for t in trades])
    stops = [t.stop_pips for t in trades if t.stop_pips]
    spreads = [t.spread_pips for t in trades if t.spread_pips is not None]
    median_stop = statistics.median(stops) if stops else 0.0
    median_spread = statistics.median(spreads) if spreads else 0.0

    # Breakeven win rate shifts by exactly the cost drag.
    be_gross = 1.0 / (1.0 + mean_rr)
    be_net = (1.0 + drag) / (1.0 + mean_rr)

    # The spread at which a measured gross edge is exactly consumed. Only
    # meaningful when the gross edge is positive — a negative edge has no
    # spread that saves it.
    be_spread = None
    if gross_ev > 0 and opts.crossings:
        implied = gross_ev * median_stop / opts.crossings
        # A threshold under a tenth of a pip is not a finding — it means
        # the gross edge rounds to zero, and printing "survives up to 0.00
        # pips" reads like a measurement rather than the absence of one.
        be_spread = implied if implied >= 0.05 else None

    measured = {
        "spread": sum(1 for t in trades if t.spread_source == MEASURED),
        "stop": sum(1 for t in trades if t.stop_pips_source == MEASURED),
        "rr": sum(1 for t in trades if t.rr_source == MEASURED),
        "gross_r": sum(1 for t in trades if t.gross_r_source == MEASURED),
        "nights": sum(1 for t in trades if t.nights_known),
    }

    grid = []
    for spread in opts.grid_spreads:
        row = []
        for stop in opts.grid_stops:
            row.append(gross_ev - (spread * opts.crossings + opts.commission_pips) / stop)
        grid.append({"spread": spread, "net_ev": row})

    return {
        "version": VERSION,
        "trades": n,
        "wins": wins,
        "win_rate": wins / n,
        "mean_rr": mean_rr,
        "median_stop_pips": median_stop,
        "median_spread_pips": median_spread,
        "gross_ev_r": gross_ev,
        "net_ev_r": net_ev,
        "cost_drag_r": drag,
        "cost_components_r": {
            "spread": statistics.mean([c["spread"] for c in costs]),
            "commission": statistics.mean([c["commission"] for c in costs]),
            "swap": statistics.mean([c["swap"] for c in costs]),
        },
        "breakeven_win_rate_gross": be_gross,
        "breakeven_win_rate_net": be_net,
        "breakeven_spread_pips": be_spread,
        "total_net_r": sum(net),
        "net_ev_ci95": bootstrap_ci(net, opts.bootstrap, opts.seed),
        "measured_counts": measured,
        "grid": {"spreads": opts.grid_spreads, "stops": opts.grid_stops, "rows": grid},
        "per_trade": [
            {"row": t.row, "direction": t.direction, "stop_pips": t.stop_pips,
             "spread_pips": t.spread_pips, "nights": t.nights, "won": t.won,
             "gross_r": g, "cost_r": c["total"], "net_r": nr}
            for t, g, c, nr in zip(trades, gross, costs, net)
        ],
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def tag(measured_count: int, total: int) -> str:
    if measured_count == total:
        return "MEASURED"
    if measured_count == 0:
        return "ASSUMED"
    return f"MIXED ({measured_count}/{total} measured)"


def format_report(result: dict, provenance: dict, warnings: list[str], opts: Options,
                  source: str) -> str:
    n = result["trades"]
    out: list[str] = []
    add = out.append

    add("=" * 72)
    add(f"  SPREAD-COST AUDIT v{VERSION}".ljust(48) + Path(source).name[:23].rjust(23))
    add("=" * 72)
    add("")
    add(f"  trades used            {n} of {provenance['rows_read']} rows")
    add(f"  win rate               {result['win_rate'] * 100:.1f}%  ({result['wins']} wins)")
    add(f"  mean reward:risk       {result['mean_rr']:.2f}   [{tag(result['measured_counts']['rr'], n)}]")
    add(f"  median stop            {result['median_stop_pips']:.1f} pips   "
        f"[{tag(result['measured_counts']['stop'], n)}]")
    add(f"  median spread          {result['median_spread_pips']:.2f} pips   "
        f"[{tag(result['measured_counts']['spread'], n)}]")
    add("")
    add("-" * 72)
    add("  THE EDGE")
    add("-" * 72)
    add(f"  gross EV / trade      {result['gross_ev_r']:+.4f}R    "
        f"[{tag(result['measured_counts']['gross_r'], n)}]")
    add(f"  cost drag / trade     {-result['cost_drag_r']:+.4f}R")
    add(f"  net EV / trade        {result['net_ev_r']:+.4f}R")
    ci = result["net_ev_ci95"]
    if ci:
        add(f"  95% CI (bootstrap)    {ci[0]:+.4f}R to {ci[1]:+.4f}R")
    add(f"  total over {n} trades    {result['total_net_r']:+.2f}R")
    add("")
    components = result["cost_components_r"]
    add("  where the drag goes:")
    add(f"    spread              {components['spread']:.4f}R   "
        f"= {result['median_spread_pips']:.2f} pips x {opts.crossings:g} "
        f"/ {result['median_stop_pips']:.1f} pip stop")
    if components["commission"]:
        add(f"    commission          {components['commission']:.4f}R")
    if components["swap"]:
        add(f"    swap                {components['swap']:.4f}R   "
            f"[{tag(result['measured_counts']['nights'], n)} nights held]")
    elif opts.swap_pct_long or opts.swap_pct_short:
        add("    swap                0.0000R   (no overnight holds found)")
    else:
        add("    swap                not modelled — pass --swap-pct-long/--swap-pct-short")
    add("")
    add("-" * 72)
    add("  BREAKEVEN")
    add("-" * 72)
    add(f"  breakeven win rate, gross   {result['breakeven_win_rate_gross'] * 100:.1f}%")
    add(f"  breakeven win rate, net     {result['breakeven_win_rate_net'] * 100:.1f}%"
        f"   <-- the one that pays the bills")
    gap = result["win_rate"] - result["breakeven_win_rate_net"]
    add(f"  your win rate               {result['win_rate'] * 100:.1f}%  "
        f"({gap * 100:+.1f} points vs net breakeven)")
    if result["breakeven_spread_pips"] is not None:
        add(f"  edge survives spread up to  {result['breakeven_spread_pips']:.2f} pips "
            f"(at a {result['median_stop_pips']:.1f} pip stop)")
    add("")
    add("-" * 72)
    add("  NET EV IF THE SPREAD / STOP WERE DIFFERENT")
    add("  (same measured gross edge; rows = spread pips, cols = stop pips)")
    add("-" * 72)
    header = "  spread |" + "".join(f"{s:>9.0f}" for s in result["grid"]["stops"])
    add(header)
    add("  " + "-" * (len(header) - 2))
    for row in result["grid"]["rows"]:
        cells = "".join(f"{v:>+9.3f}" for v in row["net_ev"])
        add(f"  {row['spread']:>6.2f} |{cells}")
    add("")
    add("-" * 72)
    add("  VERDICT")
    add("-" * 72)
    net = result["net_ev_r"]
    if ci and ci[0] > 0:
        add("  POSITIVE net of modelled costs, and the 95% CI excludes zero.")
        add("  Still a single sample: confirm out-of-sample before sizing up.")
    elif net > 0:
        add("  Positive net of modelled costs, but the 95% CI includes zero —")
        add("  this is not yet distinguishable from breakeven. More trades needed.")
    elif net > -0.02:
        add("  Breakeven at best. Costs have consumed the edge; any unmodelled")
        add("  slippage or commission puts it underwater.")
    else:
        add(f"  NEGATIVE: {net:+.4f}R per trade. This strategy pays the broker")
        add("  more than it takes from the market. Trading it as-is loses money")
        add(f"  at a rate of roughly {abs(net) * 100:.1f}% of risked capital per trade.")
    if n < 30:
        add("")
        add(f"  CAUTION: {n} trades is too few to conclude anything. Treat every")
        add("  figure above as indicative only.")
    add("")
    add("-" * 72)
    add("  PROVENANCE")
    add("-" * 72)
    add(f"  pip size               {provenance['pip_size']} ({provenance['pip_source']})")
    add(f"  spread column          {provenance['spread_units']}")
    add(f"  spread crossings       {opts.crossings:g} (entry only by default)")
    bound = "columns mapped: " + ", ".join(f"{k}->{v}" for k, v in provenance["columns"].items())
    for line in wrap(bound, 68):
        add(f"  {line}")
    for warning in warnings:
        add(f"  ! {warning}")
    add("")
    add("  NOT MODELLED (all push net EV down, never up): slippage beyond the")
    add("  spread, partial fills, latency, currency conversion on costs.")
    add("  Net EV above is therefore an UPPER bound.")
    add("=" * 72)
    return "\n".join(out)


def wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def floats(raw: str) -> list[float]:
    return [float(part) for part in raw.replace(" ", "").split(",") if part]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spread_audit",
        description="Audit a strategy's expected value net of spread, commission and swap.",
        epilog="Cost in R = cost in pips / stop distance in pips. That is the whole idea.",
    )
    parser.add_argument("trade_log", help="CSV of trades (any common export layout)")
    parser.add_argument("--rr", type=float, default=2.0,
                        help="Reward:risk, used when the log has no target column (default 2.0)")
    parser.add_argument("--spread", type=float, default=1.5, dest="spread_pips",
                        help="Spread in pips, used when the log has no spread column (default 1.5)")
    parser.add_argument("--stop-pips", type=float, default=None,
                        help="Stop distance in pips, if the log has neither stop pips nor prices")
    parser.add_argument("--pip", type=float, default=None,
                        help="Pip size (default: 0.0001, or 0.01 for JPY-scale prices)")
    parser.add_argument("--spread-units", choices=("auto", "pips", "points"), default="auto",
                        help="How to read a spread column (default auto: >10 is taken as points)")
    parser.add_argument("--crossings", type=float, default=1.0,
                        help="Times the spread is crossed per round turn (default 1: entry only)")
    parser.add_argument("--commission-pips", type=float, default=0.0,
                        help="Round-turn commission expressed in pips")
    parser.add_argument("--commission-per-lot", type=float, default=0.0,
                        help="Round-turn commission per lot (needs --risk-amount and a lots column)")
    parser.add_argument("--risk-amount", type=float, default=None,
                        help="Cash risked per trade — lets P&L columns be read as R")
    parser.add_argument("--swap-pct-long", type=float, default=0.0,
                        help="Swap rate for longs, MT5 percentage form (e.g. -1.51)")
    parser.add_argument("--swap-pct-short", type=float, default=0.0,
                        help="Swap rate for shorts, MT5 percentage form (e.g. -0.54)")
    parser.add_argument("--nights", type=float, default=0.0, dest="nights_default",
                        help="Assumed nights held when the log has no exit time (default 0)")
    parser.add_argument("--bootstrap", type=int, default=2000,
                        help="Bootstrap iterations for the CI, 0 to skip (default 2000)")
    parser.add_argument("--seed", type=int, default=7, help="Bootstrap seed (default 7)")
    parser.add_argument("--grid-spreads", type=floats, default=None,
                        help="Sensitivity rows, e.g. 1.0,1.5,2.0")
    parser.add_argument("--grid-stops", type=floats, default=None,
                        help="Sensitivity columns, e.g. 12,20,40")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the report")
    parser.add_argument("--out", default=None, help="Write per-trade detail to this CSV")
    parser.add_argument("--fail-on-negative", action="store_true",
                        help="Exit 1 when net EV is not positive (for CI use)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = Options(
        rr=args.rr, spread_pips=args.spread_pips, stop_pips=args.stop_pips, pip=args.pip,
        crossings=args.crossings, spread_units=args.spread_units,
        commission_pips=args.commission_pips,
        commission_per_lot=args.commission_per_lot, risk_amount=args.risk_amount,
        swap_pct_long=args.swap_pct_long, swap_pct_short=args.swap_pct_short,
        nights_default=args.nights_default, bootstrap=args.bootstrap, seed=args.seed,
    )
    if args.grid_spreads:
        opts.grid_spreads = args.grid_spreads
    if args.grid_stops:
        opts.grid_stops = args.grid_stops

    path = Path(args.trade_log)
    if not path.exists():
        raise SystemExit(f"{path}: no such file")

    trades, warnings, provenance = load_trades(path, opts)
    result = audit(trades, opts)

    if args.json:
        print(json.dumps({"result": result, "provenance": provenance,
                          "warnings": warnings}, indent=2, default=str))
    else:
        print(format_report(result, provenance, warnings, opts, str(path)))

    if args.out:
        fields = list(result["per_trade"][0].keys())
        with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["per_trade"])
        if not args.json:
            print(f"\nwrote per-trade detail to {args.out}")

    if args.fail_on_negative and result["net_ev_r"] <= 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
