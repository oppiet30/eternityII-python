-- Additive upgrade from PR #6. Stop workers during installation, then restart
-- the updated worker. Existing jobs, results and checkpoints are preserved.
USE eternity2;

CREATE TABLE IF NOT EXISTS job_progress (
    job_id BIGINT PRIMARY KEY,
    nodes BIGINT UNSIGNED NOT NULL,
    current_depth INT NOT NULL,
    max_depth INT NOT NULL,
    pruned BIGINT UNSIGNED NOT NULL,
    elapsed_seconds DOUBLE NOT NULL,
    nodes_per_second DOUBLE NOT NULL,
    best_score INT NOT NULL,
    best_board_id BIGINT NULL,
    outcome ENUM('searching','solved','exhausted') NOT NULL,
    official_starter BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (best_board_id) REFERENCES boards(board_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS job_batches (
    batch_id CHAR(36) PRIMARY KEY,
    source_job_id BIGINT NULL,
    prefixes JSON NOT NULL,
    parameters JSON NOT NULL,
    base_prefix JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_job_id) REFERENCES jobs(job_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS job_partitions (
    job_id BIGINT PRIMARY KEY,
    batch_id CHAR(36) NOT NULL,
    prefix_hash CHAR(64) NOT NULL,
    UNIQUE(batch_id,prefix_hash),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (batch_id) REFERENCES job_batches(batch_id)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW view_oriented_board_cells AS
SELECT c.board_id,c.row_idx,c.col_idx,c.piece_id,c.rotation,b.width,b.height,
  CASE c.rotation WHEN 0 THEN p.top_motif WHEN 1 THEN p.left_motif WHEN 2 THEN p.bottom_motif ELSE p.right_motif END AS top_edge,
  CASE c.rotation WHEN 0 THEN p.right_motif WHEN 1 THEN p.top_motif WHEN 2 THEN p.left_motif ELSE p.bottom_motif END AS right_edge,
  CASE c.rotation WHEN 0 THEN p.bottom_motif WHEN 1 THEN p.right_motif WHEN 2 THEN p.top_motif ELSE p.left_motif END AS bottom_edge,
  CASE c.rotation WHEN 0 THEN p.left_motif WHEN 1 THEN p.bottom_motif WHEN 2 THEN p.right_motif ELSE p.top_motif END AS left_edge
FROM board_cells c JOIN pieces p ON p.piece_id=c.piece_id JOIN boards b ON b.board_id=c.board_id;

DELIMITER //
CREATE OR REPLACE PROCEDURE proc_register_puzzle(IN p_pieces LONGTEXT)
BEGIN
    DECLARE v_lock INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF JSON_VALID(p_pieces)=0 OR JSON_TYPE(p_pieces)<>'ARRAY' OR JSON_LENGTH(p_pieces)=0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid puzzle catalog';
    END IF;
    IF EXISTS(SELECT 1 FROM JSON_TABLE(p_pieces,'$[*]' COLUMNS(
      id INT PATH '$.piece_id', t INT PATH '$.top', r INT PATH '$.right',
      b INT PATH '$.bottom', l INT PATH '$.left')) p
      WHERE p.id IS NULL OR p.id<1 OR p.t IS NULL OR p.r IS NULL OR p.b IS NULL OR p.l IS NULL
        OR LEAST(p.t,p.r,p.b,p.l)<0) OR
      (SELECT COUNT(DISTINCT id) FROM JSON_TABLE(p_pieces,'$[*]' COLUMNS(id INT PATH '$.piece_id')) p)
        <> JSON_LENGTH(p_pieces) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid or duplicate catalog piece';
    END IF;
    IF EXISTS(SELECT 1 FROM JSON_TABLE(p_pieces,'$[*]' COLUMNS(
      id INT PATH '$.piece_id', t INT PATH '$.top', r INT PATH '$.right',
      b INT PATH '$.bottom', l INT PATH '$.left')) p JOIN pieces e ON e.piece_id=p.id
      WHERE NOT(e.top_motif<=>p.t AND e.right_motif<=>p.r AND e.bottom_motif<=>p.b AND e.left_motif<=>p.l)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Catalog conflicts with existing pieces';
    END IF;
    INSERT IGNORE INTO motifs(motif_id,motif_name)
      SELECT DISTINCT value,CONCAT('motif_',value) FROM JSON_TABLE(p_pieces,
        '$[*]' COLUMNS(NESTED PATH '$.edges[*]' COLUMNS(value INT PATH '$'))) m;
    INSERT IGNORE INTO pieces(piece_id,top_motif,right_motif,bottom_motif,left_motif)
      SELECT id,t,r,b,l FROM JSON_TABLE(p_pieces,'$[*]' COLUMNS(
        id INT PATH '$.piece_id', t INT PATH '$.top', r INT PATH '$.right',
        b INT PATH '$.bottom', l INT PATH '$.left')) p;
    COMMIT;
END //

CREATE OR REPLACE PROCEDURE proc_save_search_checkpoint(IN p_job BIGINT,
    IN p_worker BIGINT, IN p_token CHAR(36), IN p_state LONGTEXT)
BEGIN
    DECLARE v_lock INT;
    DECLARE v_board BIGINT DEFAULT NULL;
    DECLARE v_size INT;
    DECLARE v_depth INT;
    DECLARE v_score INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF NOT EXISTS(SELECT 1 FROM jobs j JOIN reservations r ON r.job_id=j.job_id
      WHERE j.job_id=p_job AND j.assigned_worker=p_worker AND j.claim_token=p_token
        AND j.status='running' AND r.claim_token=p_token AND r.expires_at>SYSDATE()) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Claim lost or expired';
    END IF;
    IF JSON_VALID(p_state)=0 OR JSON_TYPE(JSON_EXTRACT(p_state,'$.best_board'))<>'ARRAY' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid search checkpoint';
    END IF;
    SET v_size=JSON_VALUE(p_state,'$.size');
    SET v_depth=JSON_LENGTH(JSON_EXTRACT(p_state,'$.best_board'));
    SET v_score=GREATEST(0,v_depth-v_size)+v_depth-CEILING(v_depth/v_size);
    IF v_size IS NULL OR v_size<1 OR v_depth>v_size*v_size
      OR JSON_VALUE(p_state,'$.best_score') IS NULL OR JSON_VALUE(p_state,'$.best_score')<>v_score THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid partial board score or size';
    END IF;
    IF EXISTS(SELECT 1 FROM job_progress WHERE job_id=p_job) THEN
        SELECT best_board_id INTO v_board FROM job_progress WHERE job_id=p_job;
    END IF;
    IF v_board IS NULL THEN
        INSERT INTO boards(name,width,height,score,solver)
          VALUES(CONCAT('job_',p_job,'_best_partial'),v_size,v_size,v_score,'indexed_dfs');
        SET v_board=LAST_INSERT_ID();
    ELSE
        UPDATE boards SET score=v_score WHERE board_id=v_board;
    END IF;
    DELETE FROM board_cells WHERE board_id=v_board;
    INSERT INTO board_cells(board_id,row_idx,col_idx,piece_id,rotation)
      SELECT v_board,FLOOR((pos-1)/v_size),MOD(pos-1,v_size),id,rotation
      FROM JSON_TABLE(p_state,'$.best_board[*]' COLUMNS(
        pos FOR ORDINALITY,id INT PATH '$.piece_id',rotation INT PATH '$.rotation')) cells;
    IF EXISTS(SELECT 1 FROM board_cells WHERE board_id=v_board AND (rotation NOT BETWEEN 0 AND 3 OR rotation IS NULL))
      OR (SELECT COUNT(DISTINCT piece_id) FROM board_cells WHERE board_id=v_board)<>v_depth THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid partial board cells';
    END IF;
    IF EXISTS(SELECT 1 FROM view_oriented_board_cells c
      LEFT JOIN view_oriented_board_cells above ON above.board_id=c.board_id AND above.row_idx=c.row_idx-1 AND above.col_idx=c.col_idx
      LEFT JOIN view_oriented_board_cells previous ON previous.board_id=c.board_id AND previous.row_idx=c.row_idx AND previous.col_idx=c.col_idx-1
      WHERE c.board_id=v_board AND (
        (c.top_edge=0)<>(c.row_idx=0) OR (c.left_edge=0)<>(c.col_idx=0)
        OR (c.bottom_edge=0)<>(c.row_idx=c.height-1) OR (c.right_edge=0)<>(c.col_idx=c.width-1)
        OR above.bottom_edge<>c.top_edge OR previous.right_edge<>c.left_edge)) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Partial board violates edge or border rules';
    END IF;
    IF JSON_VALUE(p_state,'$.official_starter')=1 AND (
      v_size<>16 OR EXISTS(SELECT 1 FROM board_cells WHERE board_id=v_board AND piece_id=139
        AND (row_idx<>8 OR col_idx<>7 OR rotation<>2))
      OR (v_depth>135 AND NOT EXISTS(SELECT 1 FROM board_cells WHERE board_id=v_board
        AND row_idx=8 AND col_idx=7 AND piece_id=139 AND rotation=2))) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Partial board violates mandatory starter';
    END IF;
    INSERT INTO job_checkpoints(job_id,state) VALUES(p_job,p_state)
      ON DUPLICATE KEY UPDATE state=VALUES(state),updated_at=SYSDATE(6);
    INSERT INTO job_progress(job_id,nodes,current_depth,max_depth,pruned,elapsed_seconds,
      nodes_per_second,best_score,best_board_id,outcome,official_starter)
      VALUES(p_job,JSON_VALUE(p_state,'$.nodes'),JSON_VALUE(p_state,'$.current_depth'),
        JSON_VALUE(p_state,'$.max_depth'),JSON_VALUE(p_state,'$.pruned'),
        JSON_VALUE(p_state,'$.elapsed_seconds'),JSON_VALUE(p_state,'$.nodes_per_second'),
        v_score,v_board,JSON_VALUE(p_state,'$.outcome'),COALESCE(JSON_VALUE(p_state,'$.official_starter'),0))
      ON DUPLICATE KEY UPDATE nodes=VALUES(nodes),current_depth=VALUES(current_depth),
        max_depth=VALUES(max_depth),pruned=VALUES(pruned),elapsed_seconds=VALUES(elapsed_seconds),
        nodes_per_second=VALUES(nodes_per_second),best_score=VALUES(best_score),
        best_board_id=VALUES(best_board_id),outcome=VALUES(outcome),
        official_starter=VALUES(official_starter),updated_at=SYSDATE(6);
    COMMIT;
END //

CREATE OR REPLACE PROCEDURE proc_enqueue_partition_batch(IN p_batch CHAR(36),
    IN p_prefixes LONGTEXT, IN p_parameters LONGTEXT, IN p_base LONGTEXT, IN p_source BIGINT)
BEGIN
    DECLARE v_lock INT;
    DECLARE v_index INT DEFAULT 0;
    DECLARE v_prefix LONGTEXT;
    DECLARE v_job BIGINT;
    DECLARE v_worker BIGINT;
    DECLARE v_depth INT;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; RESIGNAL; END;
    START TRANSACTION;
    SELECT id INTO v_lock FROM scheduler_lock WHERE id=1 FOR UPDATE;
    IF JSON_VALID(p_prefixes)=0 OR JSON_TYPE(p_prefixes)<>'ARRAY' OR JSON_LENGTH(p_prefixes)=0
      OR JSON_VALID(p_parameters)=0 OR JSON_TYPE(p_parameters)<>'OBJECT'
      OR JSON_VALID(p_base)=0 OR JSON_TYPE(p_base)<>'ARRAY' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid partition batch';
    END IF;
    IF EXISTS(SELECT 1 FROM job_batches WHERE batch_id=p_batch) THEN
        IF NOT EXISTS(SELECT 1 FROM job_batches WHERE batch_id=p_batch
          AND JSON_EQUALS(prefixes,p_prefixes) AND JSON_EQUALS(parameters,p_parameters)
          AND JSON_EQUALS(base_prefix,p_base) AND source_job_id<=>p_source) THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Batch ID reused with different contents';
        END IF;
    ELSE
        SET v_depth=JSON_LENGTH(JSON_EXTRACT(p_prefixes,'$[0]'));
        IF p_source IS NOT NULL THEN
            IF NOT EXISTS(SELECT 1 FROM jobs WHERE job_id=p_source AND status IN ('queued','running')
              AND JSON_EQUALS(COALESCE(prefix,'[]'),p_base)) THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Source job is unavailable or base prefix differs';
            END IF;
            SELECT assigned_worker INTO v_worker FROM jobs WHERE job_id=p_source;
            UPDATE jobs SET status='failed',finished_at=SYSDATE(),claim_token=NULL,
              failure_reason=CONCAT('Replaced by partition batch ',p_batch) WHERE job_id=p_source;
            DELETE FROM reservations WHERE job_id=p_source;
            UPDATE workers SET status='idle' WHERE worker_id=v_worker AND status='running'
              AND NOT EXISTS(SELECT 1 FROM jobs WHERE assigned_worker=v_worker AND status='running');
        END IF;
        INSERT INTO job_batches(batch_id,source_job_id,prefixes,parameters,base_prefix)
          VALUES(p_batch,p_source,p_prefixes,p_parameters,p_base);
        WHILE v_index<JSON_LENGTH(p_prefixes) DO
            SET v_prefix=JSON_EXTRACT(p_prefixes,CONCAT('$[',v_index,']'));
            IF JSON_TYPE(v_prefix)<>'ARRAY' OR JSON_LENGTH(v_prefix)<>v_depth
              OR JSON_LENGTH(v_prefix)<=JSON_LENGTH(p_base) THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid partition prefix';
            END IF;
            IF JSON_LENGTH(p_base)>0 AND NOT JSON_EQUALS(
              JSON_EXTRACT(v_prefix,CONCAT('$[0 to ',JSON_LENGTH(p_base)-1,']')),p_base) THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Partition does not extend base prefix';
            END IF;
            INSERT INTO jobs(prefix,parameters) VALUES(v_prefix,p_parameters);
            SET v_job=LAST_INSERT_ID();
            INSERT INTO job_partitions(job_id,batch_id,prefix_hash)
              VALUES(v_job,p_batch,SHA2(JSON_NORMALIZE(v_prefix),256));
            SET v_index=v_index+1;
        END WHILE;
    END IF;
    COMMIT;
    SELECT j.job_id,j.prefix,j.status FROM jobs j JOIN job_partitions p ON p.job_id=j.job_id WHERE p.batch_id=p_batch;
END //
DELIMITER ;

CREATE OR REPLACE VIEW view_job_progress AS
SELECT j.job_id,j.status,w.hostname,p.nodes,p.current_depth,p.max_depth,p.pruned,
  p.elapsed_seconds,p.nodes_per_second,p.best_score,p.best_board_id,p.outcome,
  p.official_starter,p.updated_at FROM jobs j
LEFT JOIN workers w ON w.worker_id=j.assigned_worker
LEFT JOIN job_progress p ON p.job_id=j.job_id;

DELIMITER //
CREATE OR REPLACE PROCEDURE proc_finish_job(IN p_job BIGINT, IN p_worker BIGINT,
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
            INSERT INTO job_results(job_id,depth_reached,score,duration_seconds,board_id)
              VALUES(p_job,p_depth,p_score,p_duration,(SELECT best_board_id FROM job_progress WHERE job_id=p_job));
        END IF;
        DELETE FROM reservations WHERE job_id=p_job;
        UPDATE workers SET status=IF(
          EXISTS(SELECT 1 FROM jobs WHERE assigned_worker=p_worker AND status='running'),
          'running','idle') WHERE worker_id=p_worker;
        COMMIT;
    END IF;
END //
DELIMITER ;
