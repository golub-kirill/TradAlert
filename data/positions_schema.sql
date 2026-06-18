-- TradAlert manual-positions schema (positions).
--
-- Operated by src/core/position_manager.py (CRUD) and position_CLI.py.
-- Open positions have exit_date IS NULL; a ticker carries at most one open
-- position at a time (enforced by convention in position_manager, not a DB
-- constraint). DDL mirrors the live table (SHOW CREATE TABLE positions) —
-- reconcile against your live table if it differs. Run once on a fresh deploy.
--
--   mysql -u <user> -p <db> < data/positions_schema.sql
--
-- Read by scripts/reconcile_fills.py for live-vs-backtest reconciliation on
-- real fills.

CREATE TABLE IF NOT EXISTS positions (
    id          INT           NOT NULL AUTO_INCREMENT,
    ticker      VARCHAR(12)   NOT NULL,
    side        ENUM('long','short') NOT NULL DEFAULT 'long',
    entry_price DECIMAL(12,4) NOT NULL,
    entry_date  DATE          NOT NULL,
    stop_price  DECIMAL(12,4) DEFAULT NULL,            -- current stop (may trail)
    initial_stop DECIMAL(12,4) DEFAULT NULL,           -- stop at open; risk denominator (never moved)
    exit_price  DECIMAL(12,4) DEFAULT NULL,
    exit_date   DATE          DEFAULT NULL,            -- NULL while open
    notes       VARCHAR(255)  DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_open (ticker, exit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
