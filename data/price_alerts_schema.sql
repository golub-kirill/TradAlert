-- Price alerts — owner-set target crossings, checked by the Telegram daemon's
-- in-process poller (journal-only; alerting only, never places an order).
-- Owner-applied once, then restart the daemon. Idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS price_alerts (
    id          INT           NOT NULL AUTO_INCREMENT,
    ticker      VARCHAR(12)   NOT NULL,
    direction   ENUM('above','below') NOT NULL,
    price       DECIMAL(12,4) NOT NULL,
    active      TINYINT(1)    NOT NULL DEFAULT 1,
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fired_at    DATETIME      DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_active (active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
