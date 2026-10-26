"""Puts the single-module tool on the path for the test run.

The tool is deliberately one file at the repo root rather than a package,
so that `python spread_audit.py trades.csv` works on a fresh clone with no
install step. That is the main thing people want from it, and it is worth
one line of conftest to keep.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
