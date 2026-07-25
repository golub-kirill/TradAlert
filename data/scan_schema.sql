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

    signal_kind  ENUM('none','entry_long','exit_long','entry_short','exit_short')
                              NOT NULL DEFAULT 'none',

    tier         ENUM('LIVE','NEEDS_REVIEW') NOT NULL DEFAULT 'LIVE',
    review_reason VARCHAR(255) NULL,

    advisor_note VARCHAR(512) NULL,
    declined     TINYINT(1)   NOT NULL DEFAULT 0,
    score        DECIMAL(5,2) NULL,
    reason       VARCHAR(255) NULL,
    `close`      DOUBLE       NULL,
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


-- One row per DELIVERED regime caution, written only after the Telegram send
-- succeeded. The caution dedup reads the LAST row, so a same-episode repeat is
-- suppressed only when the reader actually saw the episode — the scan journal
-- alone records that the signal FIRED, not that the message arrived. Until this
-- table exists the dedup fails open (caution repeats every scan, never crashes).
CREATE TABLE IF NOT EXISTS telegram_cautions (
    id      INT       NOT NULL AUTO_INCREMENT,
    run_id  INT       NOT NULL,
    tickers TEXT      NOT NULL,             -- comma-joined advisory set as sent
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_telegram_cautions_run (run_id),
    CONSTRAINT fk_telegram_cautions_run
        FOREIGN KEY (run_id) REFERENCES scan_runs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


