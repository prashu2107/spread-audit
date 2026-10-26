"""Tests for the standalone spread-cost audit.

The arithmetic here is the whole product, so the cost identity, the
breakeven shift and the column mapping are all pinned down. Figures are
checked against hand calculations rather than against the tool's own
output, which is the only way a cost model can be verified at all.
"""
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

import spread_audit as sa

TOOL = Path(__file__).resolve().parent.parent / "spread_audit.py"


def write_log(tmp_path, rows, name="trades.csv"):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# The identity: cost in R = cost in pips / stop distance in pips
# --------------------------------------------------------------------------

def test_spread_cost_in_r_is_spread_over_stop_distance():
    # The headline claim: 1.5 pips over a 12-pip stop is 0.125R, on every
    # trade, win or lose.
    trade = sa.Trade(stop_pips=12.0, spread_pips=1.5, entry=1.34, direction="long")
    assert sa.cost_r(trade, sa.Options())["spread"] == pytest.approx(0.125)


def test_tighter_stops_cost_more_per_r():
    # Same cash risk over a shorter stop buys more units, all crossing the
    # same spread. This is the counter-intuitive one worth proving.
    opts = sa.Options()
    tight = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=1.5), opts)["spread"]
    wide = sa.cost_r(sa.Trade(stop_pips=40.0, spread_pips=1.5), opts)["spread"]
    assert tight == pytest.approx(0.125)
    assert wide == pytest.approx(0.0375)
    assert tight > wide


def test_cost_applies_to_winners_and_losers_alike():
    # Nothing in cost_r reads the outcome — a cost the winners dodge would
    # make the whole correction smaller than it is.
    opts = sa.Options()
    won = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=1.5, won=True), opts)
    lost = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=1.5, won=False), opts)
    assert won == lost


def test_crossings_scales_the_spread_cost():
    opts = sa.Options(crossings=2.0)
    assert sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=1.5), opts)["spread"] == pytest.approx(0.25)


def test_commission_in_pips_lands_on_the_same_denominator():
    opts = sa.Options(spread_pips=0.0, commission_pips=0.6)
    trade = sa.Trade(stop_pips=12.0, spread_pips=0.0)
    assert sa.cost_r(trade, opts)["commission"] == pytest.approx(0.05)


def test_commission_per_lot_needs_lots_and_risk_amount():
    opts = sa.Options(commission_per_lot=7.0, risk_amount=1000.0)
    with_lots = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=0.0, lots=2.0), opts)
    assert with_lots["commission"] == pytest.approx(14.0 / 1000.0)
    # No lots column: the per-lot term cannot be computed, and silently
    # treating it as zero must not also zero the rest of the cost.
    without = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=1.5), opts)
    assert without["commission"] == 0.0
    assert without["spread"] == pytest.approx(0.125)


def test_zero_stop_distance_yields_no_cost_rather_than_dividing_by_zero():
    assert sa.cost_r(sa.Trade(stop_pips=0.0, spread_pips=1.5), sa.Options())["total"] == 0.0


# --------------------------------------------------------------------------
# Swap
# --------------------------------------------------------------------------

def test_swap_pips_per_night_from_percentage_rate():
    # MT5 quotes swap as a percentage against notional at current price,
    # charged per night on a 360-day basis.
    pips = sa.swap_pips_per_night(-1.51, 1.34, 0.0001)
    assert pips == pytest.approx(1.34 * 0.0151 / 360 / 0.0001, rel=1e-9)
    assert pips == pytest.approx(0.562, abs=0.001)


def test_swap_uses_the_rate_for_the_trade_s_own_side():
    opts = sa.Options(swap_pct_long=-1.51, swap_pct_short=-0.54, pip=0.0001)
    long = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=0.0, entry=1.34,
                              direction="long", nights=1), opts)["swap"]
    short = sa.cost_r(sa.Trade(stop_pips=12.0, spread_pips=0.0, entry=1.34,
                               direction="short", nights=1), opts)["swap"]
    assert long > short > 0


def test_swap_is_zero_without_an_overnight_hold():
    opts = sa.Options(swap_pct_long=-1.51, pip=0.0001)
    trade = sa.Trade(stop_pips=12.0, spread_pips=0.0, entry=1.34, direction="long", nights=0)
    assert sa.cost_r(trade, opts)["swap"] == 0.0


def test_nights_held_counts_date_boundaries_not_elapsed_hours():
    # Opened 23:00, closed 01:00 is billed as one night despite lasting
    # two hours — that is how the broker charges it.
    short_hold = sa.nights_held(sa.parse_time("2026-08-26 23:00:00"),
                                sa.parse_time("2026-08-27 01:00:00"))
    long_hold = sa.nights_held(sa.parse_time("2026-08-26 01:00:00"),
                               sa.parse_time("2026-08-26 23:00:00"))
    assert short_hold == 1
    assert long_hold == 0


def test_nights_held_is_zero_for_missing_or_reversed_timestamps():
    assert sa.nights_held(None, sa.parse_time("2026-08-27 01:00:00")) == 0
    assert sa.nights_held(sa.parse_time("2026-08-27 01:00:00"),
                          sa.parse_time("2026-08-26 01:00:00")) == 0


# --------------------------------------------------------------------------
# Breakeven arithmetic
# --------------------------------------------------------------------------

def test_breakeven_win_rate_shifts_by_the_cost_drag():
    # At RR=2 the gross breakeven is 33.3%. With 0.125R of drag it is
    # (1 + 0.125) / 3 = 37.5%. This is the number clients actually need.
    trades = [sa.Trade(stop_pips=12.0, spread_pips=1.5, rr=2.0, won=i < 4,
                       gross_r=2.0 if i < 4 else -1.0) for i in range(10)]
    result = sa.audit(trades, sa.Options())
    assert result["breakeven_win_rate_gross"] == pytest.approx(1 / 3)
    assert result["breakeven_win_rate_net"] == pytest.approx(0.375)


def test_net_ev_is_gross_ev_minus_drag():
    # 40% win rate at RR=2 is +0.200R gross; 0.125R of spread leaves
    # +0.075R net.
    trades = [sa.Trade(stop_pips=12.0, spread_pips=1.5, rr=2.0, won=i < 4,
                       gross_r=2.0 if i < 4 else -1.0) for i in range(10)]
    result = sa.audit(trades, sa.Options())
    assert result["gross_ev_r"] == pytest.approx(0.2)
    assert result["cost_drag_r"] == pytest.approx(0.125)
    assert result["net_ev_r"] == pytest.approx(0.075)


def test_breakeven_spread_is_the_spread_that_exactly_eats_the_edge():
    # +0.200R over a 12-pip stop survives up to 0.2 * 12 = 2.4 pips.
    trades = [sa.Trade(stop_pips=12.0, spread_pips=1.5, rr=2.0, won=i < 4,
                       gross_r=2.0 if i < 4 else -1.0) for i in range(10)]
    result = sa.audit(trades, sa.Options())
    assert result["breakeven_spread_pips"] == pytest.approx(2.4)


def test_no_breakeven_spread_when_the_gross_edge_is_already_negative():
    # A losing strategy is not rescued by a tighter spread, so reporting a
    # threshold would be misleading.
    trades = [sa.Trade(stop_pips=12.0, spread_pips=1.5, rr=2.0, won=i < 3,
                       gross_r=2.0 if i < 3 else -1.0) for i in range(10)]
    result = sa.audit(trades, sa.Options())
    assert result["gross_ev_r"] < 0
    assert result["breakeven_spread_pips"] is None


def test_the_documented_repo_case_reproduces():
    # From the project's own notes: 31.2% win rate at RR=2 is -0.064R
    # gross, and roughly -0.189R net at 1.5 pips over a 12-pip stop.
    wins, total = 312, 1000
    trades = [sa.Trade(stop_pips=12.0, spread_pips=1.5, rr=2.0, won=i < wins,
                       gross_r=2.0 if i < wins else -1.0) for i in range(total)]
    result = sa.audit(trades, sa.Options(bootstrap=0))
    assert result["gross_ev_r"] == pytest.approx(-0.064, abs=0.0005)
    assert result["net_ev_r"] == pytest.approx(-0.189, abs=0.0005)


def test_no_breakeven_spread_when_the_gross_edge_rounds_to_zero():
    # Exactly breakeven gross: "survives up to 0.00 pips" would read as a
    # measurement instead of the absence of an edge.
    trades = [sa.Trade(stop_pips=13.0, spread_pips=1.5, rr=2.0, won=i < 4,
                       gross_r=2.0 if i < 4 else -1.0) for i in range(12)]
    result = sa.audit(trades, sa.Options(bootstrap=0))
    assert result["gross_ev_r"] == pytest.approx(0.0)
    assert result["breakeven_spread_pips"] is None


# --------------------------------------------------------------------------
# Spread units
# --------------------------------------------------------------------------

def test_spread_units_are_decided_for_the_whole_column_not_per_row():
    # Regression: a per-row ">20 is points" rule left 14 as "14 pips" while
    # turning 21 into "2.1 pips" — two units inside one average.
    divisor, note = sa.resolve_spread_units([14, 17, 12, 15, 21, 13, 19, 16, 14, 22])
    assert divisor == 10.0
    assert "points" in note


def test_a_column_already_in_pips_is_left_alone():
    divisor, note = sa.resolve_spread_units([1.4, 1.7, 1.2, 2.1, 1.5])
    assert divisor == 1.0
    assert "pips" in note


def test_spread_units_can_be_forced_either_way():
    assert sa.resolve_spread_units([14, 17], "pips")[0] == 1.0
    assert sa.resolve_spread_units([1.4, 1.7], "points")[0] == 10.0


def test_spread_units_resolver_handles_an_empty_column():
    assert sa.resolve_spread_units([])[0] == 1.0


def test_mt5_points_log_yields_a_sane_pip_spread_end_to_end(tmp_path):
    rows = [{"side": "buy", "entry_price": "1.3394", "sl": "1.3382",
             "result": "win" if i < 4 else "loss", "spread": str(14 + i)}
            for i in range(12)]
    path = write_log(tmp_path, rows)
    trades, _, provenance = sa.load_trades(path, sa.Options())
    assert "points" in provenance["spread_units"]
    assert statistics.median([t.spread_pips for t in trades]) == pytest.approx(1.95)
    assert all(t.spread_source == sa.MEASURED for t in trades)


# --------------------------------------------------------------------------
# Parsing and column mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("buy", "long"), ("BUY", "long"), ("Long", "long"), ("bullish", "long"),
    ("1", "long"), ("DEAL_TYPE_BUY", "long"), ("Buy limit", "long"),
    ("sell", "short"), ("Short", "short"), ("bearish", "short"), ("-1", "short"),
    ("POSITION_TYPE_SELL", "short"),
    ("", None), ("balance", None),
])
def test_parse_direction_handles_platform_spellings(raw, expected):
    assert sa.parse_direction(raw) == expected


def test_numeric_direction_signs_survive_normalisation():
    # Regression: normalise() strips punctuation, so "-1" became "1" and
    # every short was read as a long — which silently applied the wrong
    # swap rate to half the book.
    assert sa.parse_direction("-1") == "short"
    assert sa.parse_direction("+1") == "long"
    assert sa.parse_direction("0") is None


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("WIN", True), ("tp", True), ("12.40", True),
    ("0", False), ("false", False), ("loss", False), ("sl", False), ("-8.10", False),
    ("open", None), ("", None), ("pending", None),
])
def test_parse_outcome_distinguishes_unresolved_from_losing(raw, expected):
    # An open trade read as a loss would understate the strategy, which is
    # as wrong as overstating it.
    assert sa.parse_outcome(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1234.50", 1234.5), ("1,234.50", 1234.5), ("(12.30)", -12.3),
    ("£45.00", 45.0), ("-0.5", -0.5), ("", None), ("n/a", None), ("abc", None),
])
def test_parse_float_is_tolerant_of_export_formatting(raw, expected):
    assert sa.parse_float(raw) == expected


@pytest.mark.parametrize("raw", [
    "2026-08-26 13:45:00", "2026-08-26T13:45:00", "2026.08.26 13:45:00",
    "26/08/2026 13:45:00", "2026-08-26T13:45:00+00:00", "2026-08-26T13:45:00Z",
])
def test_parse_time_accepts_common_export_formats(raw):
    parsed = sa.parse_time(raw)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 8, 26)


def test_parse_time_returns_none_rather_than_raising_on_junk():
    assert sa.parse_time("not a date") is None
    assert sa.parse_time("") is None


def test_find_column_prefers_an_exact_alias_over_a_prefix_match():
    headers = ["profit_pips", "profit"]
    assert sa.find_column(headers, "pnl") == "profit"


def test_a_short_header_cannot_satisfy_a_longer_alias():
    # Regression: "sl" is a stop PRICE, but it satisfied the stop_pips
    # alias "slpips" under two-way prefix matching, so a 1.3388 stop price
    # was read as a 1.34-pip stop distance and inflated the cost drag
    # roughly tenfold.
    headers = ["side", "entry", "sl", "tp", "won"]
    cols = sa.map_columns(headers)
    assert cols["stop"] == "sl"
    assert cols["stop_pips"] is None


def test_column_binding_is_exclusive_so_a_timestamp_is_not_read_as_a_price():
    # Regression: "entry_time" exactly matches the entry_time field, but
    # the looser `entry` prefix also claimed it, and a date parsed as a
    # price silently became None.
    cols = sa.map_columns(["entry_time", "open_price", "sl", "result"])
    assert cols["entry_time"] == "entry_time"
    assert cols["entry"] == "open_price"


def test_each_header_binds_to_at_most_one_field():
    cols = sa.map_columns(["type", "price_open", "sl", "tp", "profit", "volume",
                           "open_time", "close_time", "spread"])
    bound = [column for column in cols.values() if column]
    assert len(bound) == len(set(bound))


def test_short_aliases_still_match_exactly():
    # Trimming prefix matching must not cost us the two-letter headers
    # every platform actually uses.
    cols = sa.map_columns(["sl", "tp", "r"])
    assert (cols["stop"], cols["target"], cols["r"]) == ("sl", "tp", "r")


def test_infer_pip_size_separates_jpy_scale_prices_from_the_rest():
    assert sa.infer_pip_size([1.3401, 1.3388]) == 0.0001
    assert sa.infer_pip_size([156.21, 157.04]) == 0.01
    assert sa.infer_pip_size([]) == 0.0001


# --------------------------------------------------------------------------
# Loading real-shaped logs
# --------------------------------------------------------------------------

def test_loads_a_generic_log_and_derives_stop_pips_from_prices(tmp_path):
    path = write_log(tmp_path, [
        {"direction": "buy", "entry_price": "1.3400", "stop_price": "1.3388", "result": "win"},
        {"direction": "sell", "entry_price": "1.3400", "stop_price": "1.3412", "result": "loss"},
    ])
    trades, _, provenance = sa.load_trades(path, sa.Options())
    assert len(trades) == 2
    assert trades[0].stop_pips == pytest.approx(12.0)
    assert trades[0].stop_pips_source == sa.MEASURED
    assert provenance["pip_size"] == 0.0001


def test_derives_per_trade_rr_from_a_target_column(tmp_path):
    path = write_log(tmp_path, [
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "tp": "1.3424", "won": "1"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options(rr=99.0))
    assert trades[0].rr == pytest.approx(2.0)
    assert trades[0].rr_source == sa.MEASURED
    assert trades[0].gross_r == pytest.approx(2.0)


def test_reads_mt5_style_spread_in_points_as_pips(tmp_path):
    # MT5 writes spread in points; a 5-digit pair has ten to the pip, so a
    # raw 15 is 1.5 pips. Treating points as pips would overstate the drag
    # tenfold.
    path = write_log(tmp_path, [
        {"type": "buy", "price_open": "1.3400", "sl": "1.3388", "result": "win", "spread": "150"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options())
    assert trades[0].spread_pips == pytest.approx(15.0)
    assert trades[0].spread_source == sa.MEASURED


def test_small_spread_values_are_taken_as_pips_already(tmp_path):
    path = write_log(tmp_path, [
        {"type": "buy", "price_open": "1.3400", "sl": "1.3388", "result": "win", "spread": "1.4"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options())
    assert trades[0].spread_pips == pytest.approx(1.4)


def test_pnl_becomes_r_when_risk_amount_is_supplied(tmp_path):
    path = write_log(tmp_path, [
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "profit": "200.00"},
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "profit": "-100.00"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options(risk_amount=100.0))
    assert [t.gross_r for t in trades] == pytest.approx([2.0, -1.0])
    assert all(t.gross_r_source == sa.MEASURED for t in trades)


def test_an_explicit_r_column_wins_over_everything_else(tmp_path):
    path = write_log(tmp_path, [
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "r_multiple": "1.4", "profit": "-50"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options(risk_amount=100.0))
    assert trades[0].gross_r == pytest.approx(1.4)
    assert trades[0].won is True


def test_unresolved_rows_are_dropped_not_counted_as_losses(tmp_path):
    path = write_log(tmp_path, [
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "result": "win"},
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "result": "open"},
    ])
    trades, warnings, provenance = sa.load_trades(path, sa.Options())
    assert len(trades) == 1
    assert provenance["dropped"]["no_outcome"] == 1
    assert any("no readable outcome" in w for w in warnings)


def test_rows_without_a_stop_distance_are_dropped_unless_one_is_supplied(tmp_path):
    rows = [{"side": "buy", "entry": "1.3400", "result": "win"}]
    path = write_log(tmp_path, rows)
    with pytest.raises(SystemExit):
        sa.load_trades(path, sa.Options())
    trades, _, _ = sa.load_trades(path, sa.Options(stop_pips=12.0))
    assert trades[0].stop_pips == 12.0
    assert trades[0].stop_pips_source == sa.ASSUMED


def test_nights_are_counted_from_timestamps_when_both_are_present(tmp_path):
    path = write_log(tmp_path, [
        {"side": "buy", "entry": "1.3400", "sl": "1.3388", "result": "win",
         "open_time": "2026-08-26 22:00:00", "close_time": "2026-08-27 09:00:00"},
    ])
    trades, _, _ = sa.load_trades(path, sa.Options())
    assert trades[0].nights == 1
    assert trades[0].nights_known is True


def test_empty_file_is_rejected_with_a_clear_message(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("direction,entry\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        sa.load_trades(path, sa.Options())


def test_a_log_with_no_usable_columns_names_the_headers_it_saw(tmp_path):
    path = write_log(tmp_path, [{"comment": "hello", "ticket": "1"}])
    with pytest.raises(SystemExit) as excinfo:
        sa.load_trades(path, sa.Options())
    assert "comment" in str(excinfo.value)


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

def test_bootstrap_ci_brackets_the_mean_and_is_deterministic():
    values = [2.0] * 4 + [-1.0] * 6
    first = sa.bootstrap_ci(values, 500, seed=7)
    second = sa.bootstrap_ci(values, 500, seed=7)
    assert first == second                      # same seed, same answer
    assert first[0] < statistics.mean(values) < first[1]


def test_bootstrap_ci_is_skipped_for_tiny_or_disabled_samples():
    assert sa.bootstrap_ci([1.0], 500, seed=7) is None
    assert sa.bootstrap_ci([1.0, 2.0], 0, seed=7) is None


# --------------------------------------------------------------------------
# End to end through the CLI
# --------------------------------------------------------------------------

def test_cli_reports_and_exits_zero_on_a_positive_edge(tmp_path):
    rows = [{"side": "buy", "entry": "1.3400", "sl": "1.3388",
             "result": "win" if i < 5 else "loss"} for i in range(10)]
    path = write_log(tmp_path, rows)
    done = subprocess.run([sys.executable, str(TOOL), str(path), "--spread", "1.0",
                           "--bootstrap", "0", "--fail-on-negative"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "net EV / trade" in done.stdout
    assert "BREAKEVEN" in done.stdout


def test_cli_exits_one_on_a_negative_edge_when_asked(tmp_path):
    rows = [{"side": "buy", "entry": "1.3400", "sl": "1.3388",
             "result": "win" if i < 2 else "loss"} for i in range(10)]
    path = write_log(tmp_path, rows)
    done = subprocess.run([sys.executable, str(TOOL), str(path),
                           "--bootstrap", "0", "--fail-on-negative"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 1
    assert "NEGATIVE" in done.stdout


def test_cli_json_output_is_machine_readable(tmp_path):
    rows = [{"side": "buy", "entry": "1.3400", "sl": "1.3388",
             "result": "win" if i < 4 else "loss"} for i in range(10)]
    path = write_log(tmp_path, rows)
    done = subprocess.run([sys.executable, str(TOOL), str(path), "--json", "--bootstrap", "0"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["result"]["trades"] == 10
    assert payload["result"]["net_ev_r"] == pytest.approx(0.075)


def test_cli_writes_per_trade_detail(tmp_path):
    rows = [{"side": "buy", "entry": "1.3400", "sl": "1.3388", "result": "win"}]
    path = write_log(tmp_path, rows)
    out = tmp_path / "detail.csv"
    done = subprocess.run([sys.executable, str(TOOL), str(path), "--out", str(out),
                           "--bootstrap", "0"], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    written = list(csv.DictReader(out.open(encoding="utf-8")))
    assert float(written[0]["stop_pips"]) == pytest.approx(12.0)
    assert float(written[0]["cost_r"]) == pytest.approx(0.125)


def test_missing_file_fails_fast():
    done = subprocess.run([sys.executable, str(TOOL), "nope.csv"],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "no such file" in (done.stdout + done.stderr)
