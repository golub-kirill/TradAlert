-- D1 migration (audit): immutable risk denominators + size-weighted journal.
--
-- 1) positions.initial_stop — the stop recorded at OPEN, never updated by /stop,
--    so reconcile_fills computes realized R against the INITIAL risk unit (the
--    backtester convention) instead of a later trailed stop_price.
-- 2) backtest_trades.effective_r / size_mult / borrow_annual_rate — so a journal
--    stores the size-weighted R that sums to backtest_runs.total_r. The live
--    table predates these columns; without them backtest.db falls back to a
--    legacy r_multiple-only insert.
--
-- Run once. Re-running a completed ADD COLUMN errors (column already exists);
-- the Python runner in this repo applies each step only if the column is absent.

ALTER TABLE positions
    ADD COLUMN initial_stop DECIMAL(12,4) DEFAULT NULL AFTER stop_price;
UPDATE positions SET initial_stop = stop_price WHERE initial_stop IS NULL;

ALTER TABLE backtest_trades
    ADD COLUMN effective_r        DECIMAL(10,4) NULL AFTER r_multiple,
    ADD COLUMN size_mult          DECIMAL(6,4)  NULL AFTER effective_r,
    ADD COLUMN borrow_annual_rate DECIMAL(8,5)  NULL AFTER size_mult;
