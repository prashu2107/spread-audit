# Changelog

## 1.0.0

First release.

- Gross EV, cost drag and net EV per trade, in R
- Breakeven win rate net of costs
- Cost decomposition across spread, commission and swap
- Bootstrap confidence interval on net EV
- Sensitivity grid across spread and stop width, plus the breakeven spread
- Every figure labelled `MEASURED` or `ASSUMED`
- Column mapping across MT4/MT5, MyFXBook, TradingView and custom exports
- JSON output, per-trade CSV export, and `--fail-on-negative` for CI use
- 90 tests, checked against hand calculations
