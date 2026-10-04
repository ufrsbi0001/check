"""
signals/ — Signal generation package.

Houses:
  - base.py           — shared FamilySpec + strategy implementations
  - router.py         — routes coins to their family modules
  - decision_engine.py — 6-filter trade approval layer
  - momentum.py, volatility.py, trend.py, range.py
                      — family-specific engines
"""