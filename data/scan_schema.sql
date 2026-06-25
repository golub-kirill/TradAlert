-- TradAlert live-scan persistence schema (scan_runs + scan_results).
--
-- Populated by main.py via persistence.db.save_scan_run / save_scan_results.

CREATE TABLE IF NOT EXISTS scan_runs (
    id                INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    forced            TINYINT(1)    NOT NULL DEFAULT 0,
    tickers_attempted INT           NOT NULL DEFAULT 0,
    tickers_fetched   INT           NOT NULL DEFAULT 0,
    tickers_scanned   INT           NOT NULL DEFAULT 0,
    scan_passed       INT           NOT NULL DEFAULT 0,
    signals_fired     INT           NOT NULL DEFAULT 0,
    market_regime     VARCHAR(32)   NULL,
    notes             VARCHAR(255)  NULL,
    created_at        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS scan_results (
    id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    run_id       INT UNSIGNED NOT NULL,
    ticker       VARCHAR(16)  NOT NULL,
    passed       TINYINT(1)   NOT NULL DEFAULT 0,
    -- full set of directions the scorer/engine can emit (long + short entries/exits)
    signal_kind  ENUM('none','entry_long','exit_long','entry_short','exit_short')
                              NOT NULL DEFAULT 'none',
    -- live data-freshness tier (LIVE path only; the backtester never writes scan_results).
    -- NEEDS_REVIEW = fired on stale/gapped data; reconcile_live.py excludes these.
    tier         ENUM('LIVE','NEEDS_REVIEW') NOT NULL DEFAULT 'LIVE',
    review_reason VARCHAR(255) NULL,
    -- owner skipped this FIRED entry via the Telegram 🚫 Skip button (set later by
    -- db.mark_declined). opportunity_tracker counts a declined fire as passed-on
    -- (gate='declined'). Not written by the INSERT — defaults 0, updated on tap.
    declined     TINYINT(1)   NOT NULL DEFAULT 0,
    score        DECIMAL(5,2) NULL,
    reason       VARCHAR(255) NULL,
    `close`      DOUBLE       NULL,
    -- fired-signal geometry (stop/target/etc.); NULL for non-signals
    stop_price   DOUBLE       NULL,
    target_price DOUBLE       NULL,
    signal_type  VARCHAR(24)  NULL,
    atr          DOUBLE       NULL,
    atr_pct      DOUBLE       NULL,
    dv20         DOUBLE       NULL,
    market_cap   DOUBLE       NULL,
    rsi          DOUBLE       NULL,
    macd         DOUBLE       NULL,
    macd_signal  DOUBLE       NULL,
    macd_hist    DOUBLE       NULL,
    error        TEXT         NULL,
    PRIMARY KEY (id),
    KEY idx_scan_results_run_id (run_id),
    CONSTRAINT fk_scan_results_run
        FOREIGN KEY (run_id) REFERENCES scan_runs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


