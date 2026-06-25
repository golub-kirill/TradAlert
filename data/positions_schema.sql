

CREATE TABLE IF NOT EXISTS positions (
    id          INT           NOT NULL AUTO_INCREMENT,
    ticker      VARCHAR(12)   NOT NULL,
    side        ENUM('long','short') NOT NULL DEFAULT 'long',
    entry_price DECIMAL(12,4) NOT NULL,
    entry_date  DATE          NOT NULL,
    stop_price  DECIMAL(12,4) DEFAULT NULL,
    initial_stop DECIMAL(12,4) DEFAULT NULL,
    exit_price  DECIMAL(12,4) DEFAULT NULL,
    exit_date   DATE          DEFAULT NULL,
    notes       VARCHAR(255)  DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_open (ticker, exit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS position_partials (
    id          INT           NOT NULL AUTO_INCREMENT,
    position_id INT           NOT NULL,
    exit_price  DECIMAL(12,4) NOT NULL,
    exit_date   DATE          NOT NULL,
    fraction    DECIMAL(6,4)  NOT NULL,
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_partials_position (position_id),
    CONSTRAINT fk_partials_position
        FOREIGN KEY (position_id) REFERENCES positions (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
