CREATE DATABASE IF NOT EXISTS csi_c6 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE csi_c6;

CREATE TABLE system_state (
    singleton_id TINYINT PRIMARY KEY DEFAULT 1,
    mode ENUM('STANDBY','CALIBRATION','INFERENCE','CAPTURE') NOT NULL DEFAULT 'STANDBY',
    health ENUM('STARTING','OK','WARNING','DEGRADED','ERROR') NOT NULL DEFAULT 'STARTING',
    active_session_id BIGINT UNSIGNED NULL,
    active_calibration_id BIGINT UNSIGNED NULL,
    model_sha CHAR(64) NULL,
    last_seq INT UNSIGNED NULL,
    last_error VARCHAR(1000) NULL,
    updated_ts DOUBLE NOT NULL,
    CHECK (singleton_id = 1)
) ENGINE=InnoDB;

INSERT INTO system_state(singleton_id, updated_ts) VALUES (1, UNIX_TIMESTAMP())
ON DUPLICATE KEY UPDATE singleton_id=VALUES(singleton_id);

CREATE TABLE sessions (
    session_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    mode ENUM('CALIBRATION','CAPTURE') NOT NULL,
    location VARCHAR(255) NOT NULL DEFAULT '',
    behavior VARCHAR(255) NOT NULL DEFAULT '',
    person VARCHAR(255) NOT NULL DEFAULT '',
    start_ts DOUBLE NOT NULL,
    end_ts DOUBLE NULL,
    retained BOOLEAN NOT NULL DEFAULT TRUE,
    KEY idx_sessions_open (end_ts, mode)
) ENGINE=InnoDB;

CREATE TABLE reports (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    connection_id BINARY(16) NOT NULL,
    recv_ts DOUBLE NOT NULL,
    seq INT UNSIGNED NOT NULL,
    rx_id TINYINT UNSIGNED NOT NULL,
    rssi TINYINT NOT NULL,
    gain FLOAT NOT NULL,
    csi_len SMALLINT UNSIGNED NOT NULL,
    meta VARBINARY(46) NOT NULL,
    csi VARBINARY(512) NOT NULL,
    mode ENUM('STANDBY','CALIBRATION','INFERENCE','CAPTURE') NOT NULL,
    session_id BIGINT UNSIGNED NULL,
    CHECK (rx_id BETWEEN 1 AND 4),
    CHECK (csi_len = 512),
    UNIQUE KEY uq_connection_seq_rx (connection_id, seq, rx_id),
    KEY idx_reports_seq_rx (seq, rx_id),
    KEY idx_reports_recv_ts (recv_ts),
    KEY idx_reports_session (session_id, seq),
    CONSTRAINT fk_reports_session FOREIGN KEY (session_id) REFERENCES sessions(session_id)
) ENGINE=InnoDB;

CREATE TABLE calibrations (
    calibration_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id BIGINT UNSIGNED NOT NULL,
    status ENUM('RUNNING','VALID','FAILED') NOT NULL DEFAULT 'RUNNING',
    started_ts DOUBLE NOT NULL,
    completed_ts DOUBLE NULL,
    model_sha CHAR(64) NOT NULL,
    window_count INT UNSIGNED NOT NULL DEFAULT 0,
    threshold DOUBLE NULL,
    score_median DOUBLE NULL,
    score_quantile DOUBLE NULL,
    empty_motion_q95 DOUBLE NULL,
    dsd LONGBLOB NULL,
    error_message VARCHAR(1000) NULL,
    CONSTRAINT fk_calibration_session FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    KEY idx_calibration_status (status, completed_ts)
) ENGINE=InnoDB;

CREATE TABLE inference_results (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    seq_start INT UNSIGNED NOT NULL,
    seq_end INT UNSIGNED NOT NULL,
    probability DOUBLE NOT NULL,
    threshold DOUBLE NOT NULL,
    quality ENUM('OK','WARNING') NOT NULL,
    hot BOOLEAN NOT NULL,
    motion_energy DOUBLE NULL,
    calibration_id BIGINT UNSIGNED NOT NULL,
    model_sha CHAR(64) NOT NULL,
    UNIQUE KEY uq_inference_seq_model (seq_end, model_sha),
    KEY idx_inference_ts (ts),
    CONSTRAINT fk_inference_calibration FOREIGN KEY (calibration_id) REFERENCES calibrations(calibration_id)
) ENGINE=InnoDB;

CREATE TABLE episodes (
    episode_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    start_ts DOUBLE NOT NULL,
    end_ts DOUBLE NULL,
    peak_probability DOUBLE NOT NULL,
    status ENUM('PENDING','CONFIRMED','REJECTED') NOT NULL,
    reason VARCHAR(255) NULL,
    calibration_id BIGINT UNSIGNED NOT NULL,
    model_sha CHAR(64) NOT NULL,
    KEY idx_episodes_start (start_ts),
    CONSTRAINT fk_episode_calibration FOREIGN KEY (calibration_id) REFERENCES calibrations(calibration_id)
) ENGINE=InnoDB;

CREATE TABLE system_events (
    event_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    level ENUM('INFO','WARNING','ERROR') NOT NULL,
    source VARCHAR(64) NOT NULL,
    message VARCHAR(1000) NOT NULL,
    KEY idx_events_ts (ts)
) ENGINE=InnoDB;
