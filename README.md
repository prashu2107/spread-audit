# spread-audit

[![tests](https://github.com/<you>/spread-audit/actions/workflows/tests.yml/badge.svg)](https://github.com/<you>/spread-audit/actions/workflows/tests.yml)

**Does your strategy's edge survive its own costs?**

One file. Standard library only. No install, no dependencies, no account
credentials. Point it at a trade log:

```bash
python spread_audit.py trades.csv --spread 1.5
```

```
------------------------------------------------------------------------
  THE EDGE
------------------------------------------------------------------------
  gross EV / trade      +0.1151R    [ASSUMED]
  cost drag / trade     -0.0999R
  net EV / trade        +0.0152R
  95% CI (bootstrap)    -0.1090R to +0.1411R

------------------------------------------------------------------------
  BREAKEVEN
------------------------------------------------------------------------
  breakeven win rate, gross   33.3%
  breakeven win rate, net     36.7%   <-- the one that pays the bills
  your win rate               37.2%  (+0.5 points vs net breakeven)
  edge survives spread up to  1.74 pips (at a 15.1 pip stop)
```

---

## Why this exists

I found this bug in my own trading system.

My backtest reported **+0.116R per trade**. The line that produced it was:

```python
ev = win_rate * rr - (1 - win_rate)
```

No spread. No commission. No swap. Charged properly, that became roughly
breakeven — and with the fill a market order actually gets, rather than the
idealised one the backtest assumed, about **−0.19R per trade**.

It went unnoticed for months, and the reason is worth understanding: **every
row in the report reproduced exactly from its win rate alone.** Nothing
contradicted anything. There was no error, no warning, no inconsistency to
notice. The report was internally consistent and economically meaningless.

If that line looks like your backtest, this tool tells you what it's hiding.

---

## The arithmetic

Cost in R reduces to one identity:

```
cost in R  =  cost in price terms / stop distance
```

Risk per unit **is** the stop distance, so any cost quoted in the same units
as the stop converts to R by dividing by it. No position size, balance,
leverage or account currency required — which is why this works on a log
that contains almost nothing.

It applies to **every trade, win or lose**. That is what makes it serious:
it shifts expectancy one-for-one and cannot be averaged away by taking more
trades.

### Tighter stops cost more

The same cash risk over a shorter stop buys more units, and every unit
crosses the same spread.

| Stop | Cost at a 1.5 pip spread |
|---|---|
| 12 pips | **0.125R** |
| 20 pips | 0.075R |
| 40 pips | 0.037R |

Stop width is a cost decision, not only a hit-rate decision.

### Your breakeven win rate moves

At RR=2 it is not 33.3% but `(1 + cost_R) / (1 + RR)` — **37.5%** at 0.125R
of drag. Four points of win rate is the difference between a system that
compounds and one that bleeds.

---

## Install

There is nothing to install. Clone it, or download the single file:

```bash
curl -O https://raw.githubusercontent.com/<you>/spread-audit/main/spread_audit.py
python spread_audit.py trades.csv
```

If you'd rather have it on your `PATH`:

```bash
pip install .
spread-audit trades.csv --spread 1.5
```

Requires Python 3.9 or newer. Tested down to 3.9.6.

---

## Usage

```bash
# A log with entry/stop prices and win/loss
python spread_audit.py trades.csv --spread 1.5

# Commission and overnight swap (MT5 quotes swap as a percentage)
python spread_audit.py trades.csv --spread 1.4 --commission-pips 0.2 \
    --swap-pct-long -1.51 --swap-pct-short -0.54

# Read a P&L column as R by saying what was risked per trade
python spread_audit.py trades.csv --risk-amount 1000

# Machine-readable, and per-trade detail
python spread_audit.py trades.csv --json > audit.json
python spread_audit.py trades.csv --out per_trade.csv

# Fail a CI job when the edge isn't positive
python spread_audit.py trades.csv --fail-on-negative
```

Try it on the bundled sample — a log that is exactly breakeven gross and
loses 0.13R a trade net:

```bash
python spread_audit.py examples/trades.csv --swap-pct-long -1.51 --swap-pct-short -0.54
```

Full flag list: `python spread_audit.py --help`

---

## What it needs from your log

Any CSV. Column names are matched across common platform exports (MT4/MT5,
MyFXBook, TradingView, cTrader, hand-rolled), case and punctuation
insensitive.

**Required** — an outcome and a stop distance:

| Needs | Accepted columns | Fallback |
|---|---|---|
| Outcome | `won`, `win`, `result`, `outcome`, `profit`, `pnl`, `r`, `r_multiple` | — |
| Stop distance | `stop_pips`, `sl_pips`, or `entry` + `stop` prices | `--stop-pips` |

**Optional** — each one replaces an assumption with a measurement:

| Improves | Accepted columns | Fallback |
|---|---|---|
| Spread | `spread`, `spread_pips`, `spread_points` | `--spread` |
| Reward:risk | `target`, `tp`, `take_profit` | `--rr` |
| Swap | `open_time` + `close_time` (counts nights held) | `--nights` |
| Commission | `lots`, `volume`, `size` (with `--commission-per-lot`) | `--commission-pips` |
| Swap side | `direction`, `side`, `type` | treated as long |

Rows whose outcome reads as unresolved (`open`, `pending`, blank) are
**dropped and counted**, never scored as losses — that mistake makes a
strategy look worse than it is. Rows with no usable stop distance are
dropped the same way. The report says how many, and why.

MT5 writes the spread column in **points** (ten to the pip on a 5-digit
pair). The unit is detected once for the whole column — a median above 10 is
read as points, since no liquid FX pair sustains a 10-pip spread — and the
report states which way it went. Override with `--spread-units pips|points`.

---

## Reading the output

Every figure is tagged **MEASURED** (read from your log), **ASSUMED**
(supplied by a flag or default) or **MIXED**. That's deliberate: a cost audit
whose inputs are guesses is arithmetic with extra steps, and you should be
able to see which is which at a glance.

The **95% confidence interval** comes from bootstrapping per-trade net R. A
positive net EV whose CI straddles zero is not distinguishable from
breakeven, however good the point estimate looks — the verdict section says
so in plain language rather than letting you read past it.

The **sensitivity grid** holds your measured gross edge fixed and varies
spread against stop width. That's the question to ask when your log has no
spread column: *at what cost does this edge stop existing?* The
`edge survives spread up to N pips` line is the same answer as one number.

---

## What it does not model

All of these push the real figure **down**, never up, so treat net EV from
this tool as an **upper bound**:

- slippage beyond the spread
- partial fills and requotes
- latency between signal and fill
- currency conversion on the cost side
- financing other than the per-night swap model

It assumes the spread is crossed once per round turn, on entry, which matches
a backtest priced off a single bid or mid series. If your fills are priced
off both sides, pass `--crossings 2`.

Night counting uses date boundaries in whatever timezone your log is written
in. If that isn't the broker's server day, a trade near midnight can be off
by one night. Swap is usually a small term — but check it rather than assume
it.

**Most importantly: this measures cost, not edge.** Cost drag is arithmetic
and reliable. Whether a *gross* edge is real needs out-of-sample data, and a
positive result from one window with a hundred comparisons behind it is a
hypothesis, not an edge. The confidence interval and the small-sample
caution exist to keep that distinction visible instead of flattering you.

---

## Validation

Run against a 254-day GBP/USD dataset of 1,436 signal candidates — the
system this tool was written for — it independently reproduces figures that
had been derived separately, by a different method. The slice below is the
12–20 pip stop band at the live confluence threshold, 530 resolved trades:

| | Derived independently | spread-audit |
|---|---|---|
| Win rate | 37.2% | **37.2%** |
| Gross EV | +0.116R | **+0.1151R** |
| Spread drag | 0.097R | **0.0999R** |
| Net EV | −0.009 to +0.056R | **+0.0152R** |

The test suite checks the cost arithmetic against **hand calculations**
rather than against the tool's own output, which is the only way a cost
model can be verified at all:

```bash
pip install -e ".[dev]"
pytest
```

90 tests. Three of them are regressions for bugs found while building it,
each worth naming because they're the class of thing that corrupts a cost
figure silently:

1. A column matcher that matched aliases in both directions, so the header
   `sl` satisfied the alias `slpips` — a stop *price* of 1.3388 was read as a
   1.34-pip stop *distance*, inflating cost drag roughly tenfold.
2. Punctuation stripped before parsing, so `-1` became `1` and every short
   was read as a long, applying the wrong swap rate to half the book.
3. Points-to-pips conversion decided per row, so in one file a `14` stayed
   "14 pips" while a `21` became "2.1 pips" — two units inside the same
   average.

---

## Contributing

Issues and pull requests welcome. Two things to know:

- **The cost arithmetic is the product.** Any change to it needs a test
  checked against a hand calculation, not against current behaviour.
- **New column aliases are very welcome.** If your platform's export doesn't
  map cleanly, open an issue with the header row (no trade data needed) and
  it'll get added.

---

## Not financial advice

This is a measurement tool. It does not recommend strategies, instruments or
position sizes, and nothing in its output is investment advice. It may well
tell you that a strategy you like loses money. That's the tool working.

MIT licensed — see [LICENSE](LICENSE).
