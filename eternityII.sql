CREATE DATABASE IF NOT EXISTS eternity2;
use eternity2;

CREATE TABLE motifs (
    motif_id INT PRIMARY KEY,
    motif_name VARCHAR(32),
    color_group VARCHAR(16),
    compatible_with JSON
) ENGINE=InnoDB;

CREATE TABLE pieces (
    piece_id INT PRIMARY KEY,
    top_motif INT,
    right_motif INT,
    bottom_motif INT,
    left_motif INT,
    svg MEDIUMTEXT,  -- store SVG like the one you uploaded
    FOREIGN KEY (top_motif) REFERENCES motifs(motif_id),
    FOREIGN KEY (right_motif) REFERENCES motifs(motif_id),
    FOREIGN KEY (bottom_motif) REFERENCES motifs(motif_id),
    FOREIGN KEY (left_motif) REFERENCES motifs(motif_id)
) ENGINE=InnoDB;
CREATE TABLE boards (
    board_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64),
    width INT DEFAULT 16,
    height INT DEFAULT 16,
    score INT,
    solver VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE board_cells (
    board_id BIGINT,
    row_idx INT,
    col_idx INT,
    piece_id INT,
    rotation INT,
    PRIMARY KEY (board_id, row_idx, col_idx),
    FOREIGN KEY (board_id) REFERENCES boards(board_id),
    FOREIGN KEY (piece_id) REFERENCES pieces(piece_id)
) ENGINE=InnoDB;
CREATE TABLE rigidity_clusters (
    cluster_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    board_id BIGINT,
    depth INT,
    piece_count INT,
    description TEXT,
    FOREIGN KEY (board_id) REFERENCES boards(board_id)
) ENGINE=InnoDB;
CREATE TABLE no_goods (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    piece_a INT,
    piece_b INT,
    rotation_a INT,
    rotation_b INT,
    reason VARCHAR(128),
    FOREIGN KEY (piece_a) REFERENCES pieces(piece_id),
    FOREIGN KEY (piece_b) REFERENCES pieces(piece_id)
) ENGINE=InnoDB;
CREATE TABLE prefixes (
    prefix_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    prefix_type ENUM('row','band','cluster') NOT NULL,
    prefix_string VARCHAR(128),
    width INT,
    height INT,
    score_hint INT,
    UNIQUE(prefix_type, prefix_string)
) ENGINE=InnoDB;
CREATE TABLE quota_schedules (
    quota_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64),
    description TEXT,
    schedule JSON
) ENGINE=InnoDB;
CREATE TABLE workers (
    worker_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    hostname VARCHAR(128),
    ip_address VARCHAR(64),
    cores INT,
    status ENUM('idle','running','error','offline') DEFAULT 'idle',
    health_score INT DEFAULT 100,
    max_jobs INT DEFAULT 4,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
) ENGINE=InnoDB;
CREATE TABLE jobs (
    job_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    prefix_id BIGINT NULL,
    prefix TEXT,
    parameters JSON,
    quota_id BIGINT NULL,
    assigned_worker BIGINT NULL,
    status ENUM('queued','running','completed','failed') DEFAULT 'queued',
    depth_target INT,
    score_target INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    finished_at TIMESTAMP NULL,
    claim_token CHAR(36) NULL,
    attempts INT NOT NULL DEFAULT 0,
    failure_reason TEXT,
    INDEX queue_order(status, created_at, job_id),
    INDEX worker_jobs(assigned_worker, status),
    FOREIGN KEY (prefix_id) REFERENCES prefixes(prefix_id),
    FOREIGN KEY (quota_id) REFERENCES quota_schedules(quota_id),
    FOREIGN KEY (assigned_worker) REFERENCES workers(worker_id)
) ENGINE=InnoDB;

CREATE TABLE reservations (
    reservation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id BIGINT,
    worker_id BIGINT,
    claim_token CHAR(36) NOT NULL,
    UNIQUE(job_id),
    reserved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
) ENGINE=InnoDB;

CREATE TABLE job_results (
    result_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id BIGINT,
    board_id BIGINT NULL,
    UNIQUE(job_id),
    depth_reached INT,
    score INT,
    rigidity_cluster JSON,
    duration_seconds INT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (board_id) REFERENCES boards(board_id)
) ENGINE=InnoDB;
-- All lifecycle operations acquire this short-lived InnoDB row lock first.
-- This deliberately serializes scheduling, not solver execution, and makes
-- capacity checks, recovery and fencing atomic without SKIP LOCKED support.
CREATE TABLE scheduler_lock (id INT PRIMARY KEY) ENGINE=InnoDB;
INSERT INTO scheduler_lock VALUES (1);

CREATE TABLE job_checkpoints (
    job_id BIGINT PRIMARY KEY,
    state JSON NOT NULL,
    updated_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
) ENGINE=InnoDB;

DELIMITER //
CREATE PROCEDURE proc_claim_job(IN p_worker BIGINT)
BEGIN
    DECLARE v_lock INT;
    DECLARE v_capacity INT DEFAULT NULL;
    DECLARE v_job BIGINT DEFAULT NULL;
    DECLARE v_token CHAR(36);
    DECLARE CONTINUE HANDLER FOR NOT FOUND BEGIN END;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    SELECT max_jobs INTO v_capacity FROM workers
      WHERE worker_id=p_worker AND status IN ('idle','running')
        AND last_heartbeat >= SYSDATE() - INTERVAL 5 MINUTE FOR UPDATE;
    IF v_capacity IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Worker missing, stale or unavailable';
    END IF;
    IF (SELECT COUNT(*) FROM jobs WHERE assigned_worker=p_worker AND status='running') < v_capacity THEN
        SELECT job_id INTO v_job FROM jobs WHERE status='queued'
          ORDER BY created_at, job_id LIMIT 1 FOR UPDATE;
        IF v_job IS NOT NULL THEN
            SET v_token=UUID();
            UPDATE jobs SET assigned_worker=p_worker, claim_token=v_token,
              status='running', started_at=NOW(), finished_at=NULL,
              attempts=attempts+1, failure_reason=NULL WHERE job_id=v_job;
            INSERT INTO reservations(job_id,worker_id,claim_token,expires_at)
              VALUES(v_job,p_worker,v_token,SYSDATE() + INTERVAL 5 MINUTE);
            UPDATE workers SET status='running', last_heartbeat=SYSDATE() WHERE worker_id=p_worker;
        END IF;
    END IF;
    COMMIT;
    SELECT j.*, c.state AS checkpoint FROM jobs j
      LEFT JOIN job_checkpoints c ON c.job_id=j.job_id
      WHERE j.job_id=v_job AND j.claim_token=v_token AND j.status='running';
END //

-- Retain the original scheduling entry point; workers must explicitly pull.
CREATE PROCEDURE proc_assign_job_throttled(IN p_worker BIGINT)
BEGIN
    CALL proc_claim_job(p_worker);
END //

CREATE PROCEDURE proc_worker_heartbeat(IN p_worker BIGINT, IN p_job BIGINT, IN p_token CHAR(36))
BEGIN
    DECLARE v_lock INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF NOT EXISTS (SELECT 1 FROM workers WHERE worker_id=p_worker) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Unknown worker';
    END IF;
    IF p_job IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM jobs j JOIN reservations r ON r.job_id=j.job_id
          WHERE j.job_id=p_job AND j.assigned_worker=p_worker AND j.claim_token=p_token
            AND j.status='running' AND r.claim_token=p_token AND r.expires_at > SYSDATE()) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Claim lost or expired';
        END IF;
        UPDATE reservations SET expires_at=SYSDATE() + INTERVAL 5 MINUTE
          WHERE job_id=p_job AND claim_token=p_token;
    END IF;
    UPDATE workers SET last_heartbeat=SYSDATE(), status=IF(
      EXISTS(SELECT 1 FROM jobs WHERE assigned_worker=p_worker AND status='running'),
      'running','idle') WHERE worker_id=p_worker;
    COMMIT;
END //

CREATE PROCEDURE proc_save_checkpoint(IN p_job BIGINT, IN p_worker BIGINT,
    IN p_token CHAR(36), IN p_state LONGTEXT)
BEGIN
    DECLARE v_lock INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF NOT EXISTS (SELECT 1 FROM jobs j JOIN reservations r ON r.job_id=j.job_id
      WHERE j.job_id=p_job AND j.assigned_worker=p_worker AND j.claim_token=p_token
        AND j.status='running' AND r.claim_token=p_token AND r.expires_at > SYSDATE()) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Claim lost or expired';
    END IF;
    INSERT INTO job_checkpoints(job_id,state) VALUES(p_job,p_state)
      ON DUPLICATE KEY UPDATE state=VALUES(state), updated_at=NOW(6);
    COMMIT;
END //

CREATE PROCEDURE proc_finish_job(IN p_job BIGINT, IN p_worker BIGINT,
    IN p_token CHAR(36), IN p_status VARCHAR(16), IN p_score INT,
    IN p_depth INT, IN p_duration INT, IN p_reason TEXT)
BEGIN
    DECLARE v_lock INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF p_status IS NULL OR p_status NOT IN ('completed','failed') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid terminal status';
    END IF;
    -- Exact retries are harmless; different tokens and expired attempts are fenced.
    IF EXISTS(SELECT 1 FROM jobs WHERE job_id=p_job AND assigned_worker=p_worker
      AND claim_token=p_token AND status=p_status) THEN
        COMMIT;
    ELSE
        IF NOT EXISTS (SELECT 1 FROM jobs j JOIN reservations r ON r.job_id=j.job_id
          WHERE j.job_id=p_job AND j.assigned_worker=p_worker AND j.claim_token=p_token
            AND j.status='running' AND r.claim_token=p_token AND r.expires_at > SYSDATE()) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Claim lost or expired';
        END IF;
        UPDATE jobs SET status=p_status, finished_at=NOW(), failure_reason=p_reason WHERE job_id=p_job;
        IF p_status='completed' THEN
            INSERT INTO job_results(job_id,depth_reached,score,duration_seconds)
              VALUES(p_job,p_depth,p_score,p_duration);
        END IF;
        DELETE FROM reservations WHERE job_id=p_job;
        UPDATE workers SET status=IF(
          EXISTS(SELECT 1 FROM jobs WHERE assigned_worker=p_worker AND status='running'),
          'running','idle') WHERE worker_id=p_worker;
        COMMIT;
    END IF;
END //

CREATE PROCEDURE proc_complete_job(IN p_job BIGINT, IN p_worker BIGINT,
    IN p_token CHAR(36), IN p_score INT, IN p_depth INT, IN p_duration INT)
BEGIN
    CALL proc_finish_job(p_job,p_worker,p_token,'completed',p_score,p_depth,p_duration,NULL);
END //

CREATE PROCEDURE proc_recover_workers()
BEGIN
    DECLARE v_lock INT;
    DECLARE v_now DATETIME;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    SET v_now=SYSDATE();
    UPDATE workers SET status='offline'
      WHERE last_heartbeat < v_now - INTERVAL 5 MINUTE AND status <> 'offline';
    UPDATE jobs j JOIN workers w ON w.worker_id=j.assigned_worker
      LEFT JOIN reservations r ON r.job_id=j.job_id
      SET j.status='queued', j.assigned_worker=NULL, j.claim_token=NULL,
          j.started_at=NULL, j.finished_at=NULL, j.failure_reason='Worker offline or claim expired'
      WHERE j.status='running' AND (w.status='offline' OR r.job_id IS NULL OR r.expires_at <= v_now);
    DELETE r FROM reservations r JOIN jobs j ON j.job_id=r.job_id
      WHERE j.status <> 'running' OR r.expires_at <= v_now;
    UPDATE workers SET status='idle' WHERE status='running'
      AND NOT EXISTS(SELECT 1 FROM jobs WHERE assigned_worker=workers.worker_id AND status='running');
    COMMIT;
END //

CREATE PROCEDURE proc_cleanup_expired_reservations()
BEGIN
    CALL proc_recover_workers();
END //

CREATE PROCEDURE proc_update_worker_health()
BEGIN
    UPDATE workers SET health_score=GREATEST(0,100
      - (SELECT COUNT(*) FROM jobs WHERE assigned_worker=workers.worker_id AND status='failed')
      - (CASE WHEN status='offline' THEN 50 ELSE 0 END));
END //
DELIMITER ;

CREATE EVENT ev_worker_heartbeat_check ON SCHEDULE EVERY 1 MINUTE
  DO CALL proc_recover_workers();
CREATE EVENT ev_worker_health_update ON SCHEDULE EVERY 10 MINUTE
  DO CALL proc_update_worker_health();

CREATE OR REPLACE VIEW view_worker_status AS
SELECT
    worker_id,
    hostname,
    status,
    health_score,
    max_jobs,
    last_heartbeat,
    (SELECT COUNT(*) FROM jobs j
     WHERE j.assigned_worker = workers.worker_id
       AND j.status='running') AS running_jobs
FROM workers;

CREATE OR REPLACE VIEW view_active_jobs AS
SELECT
    j.job_id,
    j.prefix,
    j.status,
    j.depth_target,
    j.score_target,
    w.hostname,
    w.status AS worker_status,
    j.started_at,
    TIMESTAMPDIFF(MINUTE, j.started_at, NOW()) AS minutes_running
FROM jobs j
LEFT JOIN workers w ON j.assigned_worker = w.worker_id
WHERE j.status='running';

