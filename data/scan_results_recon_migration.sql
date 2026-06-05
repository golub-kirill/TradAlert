-- Capture each fired signal's geometry so it can be scored to a forward R and
-- matched to backtest_trades expectancy by strategy. Additive & non-destructive.
-- Run ONCE before deploying the updated main.py / src/persistence/db.py.
--
--   mysql -u <user> -p <db> < data/scan_results_recon_migration.sql

ALTER TABLE scan_results
    ADD COLUMN stop_price   DOUBLE       NULL AFTER `close`,
    ADD COLUMN target_price DOUBLE       NULL AFTER stop_price,
    ADD COLUMN signal_type  VARCHAR(24)  NULL AFTER target_price;

-- Existing 62 entry_long rows keep NULL geometry; the reconciliation harness
-- reconstructs stop/target from close+atr+config for those (interim mode A).
