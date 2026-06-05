-- TradAlert backtest persistence schema (backtest_runs + backtest_trades).
--
-- Referenced by backtest/db.py + README but previously absent from the tree.
-- backtest_runs DDL mirrors the live table (SHOW CREATE); backtest_trades types
-- are best-fit for the values written by backtest/db.py::_trade_to_row —
-- reconcile against your live table if it differs. Run once on a fresh deploy.
--
--   mysql -u <user> -p <db> < data/backtest_schema.sql
--
-- Populated by `python -m backtest.run_backtest` (journaling is ON by default).

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INT           NOT NULL AUTO_INCREMENT,
    started_at      DATETIME      DEFAULT CURRENT_TIMESTAMP,
    start_date      DATE          DEFAULT NULL,
    end_date        DATE          DEFAULT NULL,
    tickers_count   INT           NOT NULL DEFAULT 0,
    trades_count    INT           NOT NULL DEFAULT 0,
    total_r         DECIMAL(10,4) DEFAULT NULL,
    expectancy_r    DECIMAL(10,4) DEFAULT NULL,
    profit_factor   DECIMAL(10,4) DEFAULT NULL,
    win_rate        DECIMAL(6,4)  DEFAULT NULL,
    max_drawdown_r  DECIMAL(10,4) DEFAULT NULL,
    config_json     TEXT,
    notes           TEXT,
    PRIMARY KEY (id),
    KEY idx_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS backtest_trades (
    id              INT           NOT NULL AUTO_INCREMENT,
    run_id          INT           NOT NULL,
    ticker          VARCHAR(16)   NOT NULL,
    signal_type     VARCHAR(24)   NULL,            -- momentum | mean_reversion
    direction       ENUM('long','short') NOT NULL DEFAULT 'long',
    entry_date      DATE          NULL,
    entry_price     DOUBLE        NULL,
    initial_stop    DOUBLE        NULL,
    initial_target  DOUBLE        NULL,
    exit_date       DATE          NULL,
    exit_price      DOUBLE        NULL,
    exit_reason     VARCHAR(16)   NULL,            -- stop|target|engine_exit|open_eod|time_stop
    bars_held       INT           NULL,
    r_multiple      DECIMAL(10,4) NULL,
    market_regime   VARCHAR(32)   NULL,
    ticker_trend    VARCHAR(16)   NULL,
    entry_score     DECIMAL(5,1)  NULL,
    PRIMARY KEY (id),
    KEY idx_bt_trades_run (run_id),
    CONSTRAINT fk_bt_trades_run
        FOREIGN KEY (run_id) REFERENCES backtest_runs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
