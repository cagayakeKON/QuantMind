"""Japanese market data and rule inputs to the common simulation paths.

Live simulation and replay use LocalMarketData, market_rules, the shared matcher
and the original base-currency ledger. JP configuration adds trading units,
ticks, daily price limits, T+0 and Tokyo cash-equity sessions. Replay retains the
ordinary model-signal, code, manual-confirmation and stop-loss modes.

Native-JPY cash checkpoints, funding provenance, delivery/settlement accounting
and the separate JP corporate-action accounting protocol have been withdrawn.
Those capabilities require a separate design review before being introduced.
Existing native caches/metadata are recognized read-only; no migration, reset
or conversion is performed. Research snapshot provenance remains available.
"""
