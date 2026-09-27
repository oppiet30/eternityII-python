CREATE DATABASE IF NOT EXISTS eternity2;
use eternity2;

CREATE TABLE motifs (
    motif_id INT PRIMARY KEY,
    motif_name VARCHAR(32),
    color_group VARCHAR(16),
    compatible_with JSON
);

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
);
CREATE TABLE boards (
    board_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64),
    width INT DEFAULT 16,
    height INT DEFAULT 16,
    score INT,
    solver VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE board_cells (
    board_id BIGINT,
    row_idx INT,
    col_idx INT,
    piece_id INT,
    rotation INT,
    PRIMARY KEY (board_id, row_idx, col_idx),
    FOREIGN KEY (board_id) REFERENCES boards(board_id),
    FOREIGN KEY (piece_id) REFERENCES pieces(piece_id)
);
CREATE TABLE rigidity_clusters (
    cluster_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    board_id BIGINT,
    depth INT,
    piece_count INT,
    description TEXT,
    FOREIGN KEY (board_id) REFERENCES boards(board_id)
);
CREATE TABLE no_goods (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    piece_a INT,
    piece_b INT,
    rotation_a INT,
    rotation_b INT,
    reason VARCHAR(128),
    FOREIGN KEY (piece_a) REFERENCES pieces(piece_id),
    FOREIGN KEY (piece_b) REFERENCES pieces(piece_id)
);
CREATE TABLE prefixes (
    prefix_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    prefix_type ENUM('row','band','cluster') NOT NULL,
    prefix_string VARCHAR(128),
    width INT,
    height INT,
    score_hint INT,
    UNIQUE(prefix_type, prefix_string)
);
CREATE TABLE quota_schedules (
    quota_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(64),
    description TEXT,
    schedule JSON
);
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
);
CREATE TABLE jobs (
    job_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    prefix_id BIGINT NULL,
    prefix VARCHAR(64),
    parameters JSON,
    quota_id BIGINT NULL,
    assigned_worker BIGINT NULL,
    status ENUM('queued','running','completed','failed') DEFAULT 'queued',
    depth_target INT,
    score_target INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    finished_at TIMESTAMP NULL,
    FOREIGN KEY (prefix_id) REFERENCES prefixes(prefix_id),
    FOREIGN KEY (quota_id) REFERENCES quota_schedules(quota_id),
    FOREIGN KEY (assigned_worker) REFERENCES workers(worker_id)
);

CREATE TABLE reservations (
    reservation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id BIGINT,
    worker_id BIGINT,
    reserved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
);

CREATE TABLE job_results (
    result_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id BIGINT,
    board_id BIGINT NULL,
    depth_reached INT,
    score INT,
    rigidity_cluster JSON,
    duration_seconds INT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (board_id) REFERENCES boards(board_id)
);
DELIMITER //
CREATE PROCEDURE proc_assign_job_throttled()
BEGIN
    DECLARE v_job BIGINT;
    DECLARE v_worker BIGINT;

    SELECT job_id INTO v_job
    FROM jobs
    WHERE status='queued'
    ORDER BY created_at ASC
    LIMIT 1;

    SELECT worker_id INTO v_worker
    FROM workers w
    WHERE w.status IN ('idle','running')
      AND (SELECT COUNT(*) FROM jobs j
           WHERE j.assigned_worker = w.worker_id
             AND j.status='running') < w.max_jobs
    ORDER BY w.health_score DESC, w.last_heartbeat DESC
    LIMIT 1;

    IF v_job IS NOT NULL AND v_worker IS NOT NULL THEN
        UPDATE jobs
        SET assigned_worker = v_worker,
            status='running',
            started_at=NOW()
        WHERE job_id=v_job;

        UPDATE workers SET status='running' WHERE worker_id=v_worker;
    END IF;
END //
DELIMITER ;
DELIMITER //
CREATE PROCEDURE proc_cleanup_expired_reservations()
BEGIN
    DELETE FROM reservations WHERE expires_at < NOW();
END //
DELIMITER ;
DELIMITER //
CREATE PROCEDURE proc_complete_job(IN p_job BIGINT, IN p_score INT, IN p_depth INT, IN p_duration INT)
BEGIN
    UPDATE jobs
    SET status='completed', finished_at=NOW()
    WHERE job_id=p_job;

    INSERT INTO job_results(job_id, depth_reached, score, duration_seconds)
    VALUES(p_job, p_depth, p_score, p_duration);

    UPDATE workers
    SET status='idle'
    WHERE worker_id=(SELECT assigned_worker FROM jobs WHERE job_id=p_job);
END //
DELIMITER ;
DELIMITER //
CREATE TRIGGER trg_worker_idle
AFTER UPDATE ON workers
FOR EACH ROW
BEGIN
    IF NEW.status='idle' THEN
        CALL proc_assign_job_throttled();
    END IF;
END //
DELIMITER ;
DELIMITER //
CREATE TRIGGER trg_job_insert
AFTER INSERT ON jobs
FOR EACH ROW
BEGIN
    IF NEW.status='queued' THEN
        CALL proc_assign_job_throttled();
    END IF;
END //
DELIMITER ;
DELIMITER //
CREATE TRIGGER trg_worker_offline
AFTER UPDATE ON workers
FOR EACH ROW
BEGIN
    IF NEW.status='offline' THEN
        UPDATE jobs
        SET status='failed'
        WHERE assigned_worker=NEW.worker_id
          AND status='running';
    END IF;
END //
DELIMITER ;
CREATE EVENT ev_worker_heartbeat_check
ON SCHEDULE EVERY 1 MINUTE
DO
  UPDATE workers
  SET status='offline'
  WHERE last_heartbeat < NOW() - INTERVAL 5 MINUTE
    AND status <> 'offline';
DELIMITER //
CREATE PROCEDURE proc_update_worker_health()
BEGIN
    UPDATE workers
    SET health_score = GREATEST(
        0,
        100
        - (SELECT COUNT(*) FROM jobs
           WHERE assigned_worker = workers.worker_id
             AND status='failed')
        - (CASE WHEN status='offline' THEN 50 ELSE 0 END)
    );
END //
DELIMITER ;

CREATE EVENT ev_worker_health_update
ON SCHEDULE EVERY 10 MINUTE
DO
  CALL proc_update_worker_health();
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

