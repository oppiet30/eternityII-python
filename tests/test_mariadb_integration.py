"""Real MariaDB tests, opt-in only, always in a newly created random schema."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import mysql.connector
import pytest

import eternityII as worker
from checkpoint_solver import solve_resumable
from solver import Piece


pytestmark = pytest.mark.skipif(
    os.environ.get('ETERNITY_TEST_MARIADB') != '1',
    reason='Set ETERNITY_TEST_MARIADB=1 to test a disposable MariaDB server')


def statements(script):
    delimiter, pending = ';', []
    for line in script.splitlines():
        if line.startswith('DELIMITER '):
            assert not pending
            delimiter = line.split()[1]
        elif not pending and (not line.strip() or line.lstrip().startswith('--')):
            continue
        else:
            pending.append(line)
            if line.rstrip().endswith(delimiter):
                yield '\n'.join(pending).rstrip()[:-len(delimiter)]
                pending = []
    assert not pending


@pytest.fixture(scope='module', params=('fresh', 'upgrade'))
def database(request):
    options = dict(host=os.environ.get('ETERNITY_TEST_HOST', '127.0.0.1'),
                   port=int(os.environ.get('ETERNITY_TEST_PORT', '3306')),
                   user=os.environ.get('ETERNITY_TEST_USER', 'root'),
                   password=os.environ.get('ETERNITY_TEST_PASSWORD', ''),
                   autocommit=True, connection_timeout=10)
    name = 'eternity2_test_' + uuid.uuid4().hex
    assert re.fullmatch(r'eternity2_test_[a-f0-9]{32}', name)
    conn = mysql.connector.connect(**options)
    cur = conn.cursor()
    try:
        script = (Path(__file__).parents[1] / 'eternityII.sql').read_text()
        assert script.startswith('CREATE DATABASE IF NOT EXISTS eternity2;')
        script = script.replace('CREATE DATABASE IF NOT EXISTS eternity2;',
                                f'CREATE DATABASE `{name}`;', 1)
        script = script.replace('use eternity2;', f'USE `{name}`;', 1)
        if request.param == 'upgrade':
            script = script.split('-- Additive upgrade from PR #6.')[0]
        for statement in statements(script):
            cur.execute(statement)
        if request.param == 'upgrade':
            cur.execute("INSERT INTO workers(worker_id,hostname) VALUES(987,'existing')")
            cur.execute("INSERT INTO jobs(job_id,parameters) VALUES(987,'{}')")
            cur.execute("INSERT INTO job_checkpoints(job_id,state) VALUES(987,'{\"version\":1,\"nodes\":42}')")
            migration = (Path(__file__).parents[1] / 'migrations/007_search_progress.sql').read_text()
            migration = migration.replace('USE eternity2;', f'USE `{name}`;')
            for _ in range(2):
                for statement in statements(migration):
                    cur.execute(statement)
            cur.execute('SELECT state FROM job_checkpoints WHERE job_id=987')
            assert json.loads(cur.fetchone()[0]) == dict(version=1, nodes=42)
        yield dict(options, database=name)
    finally:
        # Only our freshly generated test schema is ever removed.
        cur.execute(f'DROP DATABASE IF EXISTS `{name}`')
        cur.close()
        conn.close()


@pytest.fixture
def backend(database, monkeypatch):
    def connect():
        return mysql.connector.connect(**database)

    monkeypatch.setattr(worker, 'db', connect)
    monkeypatch.setattr(worker, 'WORKER_ID', 1)
    execute(database, 'ALTER EVENT ev_worker_heartbeat_check DISABLE')
    execute(database, 'ALTER EVENT ev_worker_health_update DISABLE')
    for table in ('job_results', 'job_partitions', 'job_batches', 'job_progress',
                  'job_checkpoints', 'reservations', 'jobs', 'board_cells', 'boards', 'pieces', 'motifs', 'workers'):
        execute(database, f'DELETE FROM {table}')
    execute(database, "INSERT INTO workers(worker_id,hostname,max_jobs) VALUES(1,'one',2),(2,'two',2)")
    return database


def execute(options, sql, args=()):
    conn = mysql.connector.connect(**options)
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, args)
            return cur.fetchall() if cur.with_rows else cur.lastrowid
        finally:
            cur.close()
    finally:
        conn.close()


def enqueue(backend, count=1):
    for _ in range(count):
        execute(backend, 'INSERT INTO jobs(parameters) VALUES(%s)', ('{}',))


def claim(worker_id):
    rows = worker.call_procedure('proc_claim_job', (worker_id,))
    return rows[0] if rows else None


def test_import_preserves_objects_and_no_triggers(backend):
    assert not execute(backend, 'SHOW TRIGGERS')
    assert len(execute(backend, 'SHOW TABLES')) == 21
    assert execute(backend, 'SELECT * FROM view_worker_status')[0]['running_jobs'] == 0
    assert execute(backend, 'SELECT * FROM view_active_jobs') == []
    worker.call_procedure('proc_update_worker_health')
    worker.call_procedure('proc_cleanup_expired_reservations')
    assert worker.claim_job() is None


def test_simultaneous_claims_respect_capacity(backend):
    enqueue(backend, 12)
    barrier = threading.Barrier(8)

    def attempt(i):
        barrier.wait(timeout=10)
        return claim(i % 2 + 1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [job for job in pool.map(attempt, range(8)) if job]
    assert len(results) == 4
    assert len({job['job_id'] for job in results}) == 4
    assert len({job['claim_token'] for job in results}) == 4
    assert [row['running_jobs'] for row in execute(backend,
            'SELECT * FROM view_worker_status ORDER BY worker_id')] == [2, 2]


def test_completion_atomic_idempotent_and_keeps_busy_status(backend):
    enqueue(backend, 2)
    first, second = worker.claim_job(), worker.claim_job()
    with pytest.raises(mysql.connector.Error):
        worker.call_procedure('proc_complete_job',
                             (first['job_id'], 2, first['claim_token'], 4, 4, 1))
    barrier = threading.Barrier(2)

    def finish(_):
        barrier.wait(timeout=10)
        worker.complete_job(first, 4, 4, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, range(2)))
    assert len(execute(backend, 'SELECT * FROM job_results')) == 1
    assert execute(backend, 'SELECT status FROM workers WHERE worker_id=1')[0]['status'] == 'running'
    worker.complete_job(second, 4, 4, 1)
    assert execute(backend, 'SELECT status FROM workers WHERE worker_id=1')[0]['status'] == 'idle'
    assert not execute(backend, 'SELECT * FROM reservations')


def test_expiry_recovery_retains_checkpoint_and_fences_old_attempt(backend):
    enqueue(backend)
    old = worker.claim_job()
    state = dict(version=1, next_indices=[1, 0])
    worker.call_procedure('proc_save_checkpoint',
                          (old['job_id'], 1, old['claim_token'], json.dumps(state)))
    worker.heartbeat(old)
    execute(backend, 'UPDATE reservations SET expires_at=NOW() - INTERVAL 1 SECOND')
    for action in (lambda: worker.heartbeat(old), lambda: worker.save_checkpoint(old, {}),
                   lambda: worker.complete_job(old, 4, 4, 1)):
        with pytest.raises(mysql.connector.Error):
            action()
    worker.call_procedure('proc_cleanup_expired_reservations')
    new = claim(2)
    assert new['job_id'] == old['job_id']
    assert new['claim_token'] != old['claim_token']
    assert new['attempts'] == 2
    assert json.loads(new['checkpoint']) == state
    for action in (lambda: worker.mark_failed(old, 'late'), lambda: worker.save_checkpoint(old, {}),
                   lambda: worker.complete_job(old, 4, 4, 1)):
        with pytest.raises(mysql.connector.Error):
            action()
    worker.call_procedure('proc_complete_job', (new['job_id'], 2, new['claim_token'], 4, 4, 1))


def test_offline_worker_recovery_and_failure(backend):
    enqueue(backend)
    old = worker.claim_job()
    execute(backend, 'UPDATE workers SET last_heartbeat=NOW() - INTERVAL 6 MINUTE WHERE worker_id=1')
    worker.call_procedure('proc_recover_workers')
    assert execute(backend, 'SELECT status FROM workers WHERE worker_id=1')[0]['status'] == 'offline'
    with pytest.raises(mysql.connector.Error):
        worker.claim_job()
    worker.heartbeat()
    new = worker.claim_job()
    assert new['job_id'] == old['job_id']
    worker.mark_failed(new, 'invalid puzzle')
    worker.mark_failed(new, 'invalid puzzle')
    assert execute(backend, 'SELECT failure_reason FROM jobs')[0]['failure_reason'] == 'invalid puzzle'
    assert not execute(backend, 'SELECT * FROM job_results')


def test_result_error_rolls_back_job_and_reservation(backend):
    enqueue(backend)
    job = worker.claim_job()
    execute(backend, "CREATE TRIGGER test_result_error BEFORE INSERT ON job_results FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='test rollback'")
    try:
        with pytest.raises(mysql.connector.Error):
            worker.complete_job(job, 4, 4, 1)
        assert execute(backend, 'SELECT status FROM jobs')[0]['status'] == 'running'
        assert len(execute(backend, 'SELECT * FROM reservations')) == 1
        assert not execute(backend, 'SELECT * FROM job_results')
    finally:
        execute(backend, 'DROP TRIGGER test_result_error')


def test_worker_resumes_persisted_search_after_reclaim(backend, monkeypatch):
    pieces = [Piece(1, (0, 1, 2, 0)), Piece(2, (0, 0, 3, 1)),
              Piece(3, (2, 4, 0, 0)), Piece(4, (3, 0, 0, 4))]
    monkeypatch.setattr(worker, 'load_pieces', lambda path: pieces)
    register(pieces)
    execute(backend, 'INSERT INTO jobs(parameters) VALUES(%s)',
            (json.dumps(dict(size=2, checkpoint_interval=0)),))
    old = worker.claim_job()

    def interrupt(prefix, params, checkpoint, save, alive):
        def persist(state):
            save(state)
            if state['nodes'] == 2:
                raise worker.ClaimLost('simulated process interruption')

        solve_resumable(pieces, save_checkpoint=persist, checkpoint_interval=0)

    worker.process_job(old, interrupt)
    stored = execute(backend, 'SELECT state FROM job_checkpoints')[0]['state']
    assert json.loads(stored)['nodes'] == 2
    execute(backend, 'UPDATE reservations SET expires_at=NOW() - INTERVAL 1 SECOND')
    worker.call_procedure('proc_recover_workers')
    new = worker.claim_job()
    worker.process_job(new)
    assert execute(backend, 'SELECT status FROM jobs')[0]['status'] == 'completed'
    result = execute(backend, 'SELECT score,depth_reached FROM job_results')[0]
    assert result == dict(score=4, depth_reached=4)
    assert json.loads(execute(backend, 'SELECT state FROM job_checkpoints')[0]['state'])['nodes'] == 4
    progress = execute(backend, 'SELECT * FROM job_progress')[0]
    assert progress['best_score'] == 4
    assert progress['outcome'] == 'solved'
    assert execute(backend, 'SELECT board_id FROM job_results')[0]['board_id'] == progress['best_board_id']
    assert len(execute(backend, 'SELECT * FROM board_cells')) == 4


def register(pieces):
    worker.call_procedure('proc_register_puzzle', (json.dumps([
        dict(piece_id=p.id, top=p.edges[0], right=p.edges[1], bottom=p.edges[2],
             left=p.edges[3], edges=p.edges) for p in pieces]),))


def test_catalog_preserves_existing_data_and_rejects_conflicts(backend):
    piece = Piece(1, (0, 0, 0, 0))
    register([piece])
    execute(backend, "UPDATE motifs SET motif_name='border' WHERE motif_id=0")
    register([piece])
    assert execute(backend, 'SELECT motif_name FROM motifs')[0]['motif_name'] == 'border'
    with pytest.raises(mysql.connector.Error, match='Catalog conflicts'):
        register([Piece(1, (1, 0, 0, 0))])
    assert execute(backend, 'SELECT top_motif FROM pieces')[0]['top_motif'] == 0


def test_progress_board_and_checkpoint_rollback_together(backend):
    pieces = [Piece(1, (0, 1, 2, 0)), Piece(2, (0, 0, 3, 1)),
              Piece(3, (2, 4, 0, 0)), Piece(4, (3, 0, 0, 4))]
    register(pieces)
    enqueue(backend)
    job = worker.claim_job()
    states = []
    solve_resumable(pieces, save_checkpoint=states.append)
    worker.save_checkpoint(job, states[-1])
    before = execute(backend, 'SELECT state FROM job_checkpoints')[0]['state']
    invalid = dict(states[-1], best_board=[dict(piece_id=1, rotation=1)], best_score=0)
    with pytest.raises(mysql.connector.Error, match='edge or border'):
        worker.save_checkpoint(job, invalid)
    assert execute(backend, 'SELECT state FROM job_checkpoints')[0]['state'] == before
    assert len(execute(backend, 'SELECT * FROM board_cells')) == 4
    execute(backend, 'UPDATE reservations SET expires_at=SYSDATE() - INTERVAL 1 SECOND')
    with pytest.raises(mysql.connector.Error, match='Claim lost'):
        worker.save_checkpoint(job, states[-1])
    assert execute(backend, 'SELECT state FROM job_checkpoints')[0]['state'] == before


def test_partition_batch_atomic_replacement_and_idempotency(backend):
    enqueue(backend)
    parent = worker.claim_job()
    batch = str(uuid.uuid4())
    prefixes = [[dict(piece_id=i, rotation=1)] for i in (1, 2, 3, 4)]
    args = (batch, json.dumps(prefixes), '{}', '[]', parent['job_id'])
    jobs = worker.call_procedure('proc_enqueue_partition_batch', args)
    assert len(jobs) == 4
    assert worker.call_procedure('proc_enqueue_partition_batch', args) == jobs
    assert execute(backend, 'SELECT status FROM jobs WHERE job_id=%s',
                   (parent['job_id'],))[0]['status'] == 'failed'
    assert not execute(backend, 'SELECT * FROM reservations')
    with pytest.raises(mysql.connector.Error):
        worker.complete_job(parent, 480, 256, 10)
    with pytest.raises(mysql.connector.Error, match='different contents'):
        worker.call_procedure('proc_enqueue_partition_batch', (batch, json.dumps(prefixes), '{"size":2}', '[]', parent['job_id']))


def test_invalid_batch_does_not_cancel_source(backend):
    enqueue(backend)
    parent = worker.claim_job()
    duplicate = [[dict(piece_id=1, rotation=1)]] * 2
    with pytest.raises(mysql.connector.Error):
        worker.call_procedure('proc_enqueue_partition_batch',
                             (str(uuid.uuid4()), json.dumps(duplicate), '{}', '[]', parent['job_id']))
    assert execute(backend, 'SELECT status FROM jobs')[0]['status'] == 'running'
    assert not execute(backend, 'SELECT * FROM job_batches')
    assert len(execute(backend, 'SELECT * FROM reservations')) == 1


def test_catalog_conflict_fails_job_instead_of_retrying_forever(backend, monkeypatch):
    register([Piece(1, (1, 0, 0, 0))])
    monkeypatch.setattr(worker, 'load_pieces', lambda path: [Piece(1, (0, 0, 0, 0))])
    enqueue(backend)
    job = worker.claim_job()
    worker.process_job(job)
    row = execute(backend, 'SELECT status,failure_reason FROM jobs')[0]
    assert row['status'] == 'failed'
    assert 'Catalog conflicts' in row['failure_reason']


def test_partition_batch_subdivides_an_existing_prefix(backend):
    base = [dict(piece_id=1, rotation=1)]
    parent_id = execute(backend, 'INSERT INTO jobs(prefix,parameters) VALUES(%s,%s)',
                        (json.dumps(base), '{}'))
    leaves = [base + [dict(piece_id=i, rotation=2)] for i in (5, 7)]
    jobs = worker.call_procedure('proc_enqueue_partition_batch',
                                (str(uuid.uuid4()), json.dumps(leaves), '{}', json.dumps(base), parent_id))
    assert len(jobs) == 2
    assert all(json.loads(job['prefix'])[0] == base[0] for job in jobs)
    assert execute(backend, 'SELECT status FROM jobs WHERE job_id=%s',
                   (parent_id,))[0]['status'] == 'failed'


def test_completion_and_recovery_race_on_expired_claim(backend):
    enqueue(backend)
    job = worker.claim_job()
    execute(backend, 'UPDATE reservations SET expires_at=NOW() - INTERVAL 1 SECOND')
    barrier = threading.Barrier(2)

    def complete():
        barrier.wait(timeout=10)
        with pytest.raises(mysql.connector.Error):
            worker.complete_job(job, 4, 4, 1)

    def recover():
        barrier.wait(timeout=10)
        worker.call_procedure('proc_recover_workers')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete), pool.submit(recover)]
        for future in futures:
            future.result(timeout=20)
    assert execute(backend, 'SELECT status,assigned_worker,claim_token FROM jobs')[0] == dict(
        status='queued', assigned_worker=None, claim_token=None)
    assert not execute(backend, 'SELECT * FROM job_results')


def test_claim_expiring_while_completion_waits_is_rejected(backend):
    enqueue(backend)
    job = worker.claim_job()
    execute(backend, 'UPDATE reservations SET expires_at=SYSDATE() + INTERVAL 3 SECOND')
    blocker = mysql.connector.connect(**backend)
    cur = blocker.cursor()
    blocker.start_transaction()
    cur.execute('SELECT id FROM scheduler_lock WHERE id=1 FOR UPDATE')
    cur.fetchall()

    def finish():
        with pytest.raises(mysql.connector.Error, match='Claim lost or expired'):
            worker.complete_job(job, 4, 4, 1)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish)
            try:
                deadline = time.monotonic() + 10
                while not execute(backend,
                        "SELECT ID FROM information_schema.PROCESSLIST "
                        "WHERE DB=%s AND ID <> CONNECTION_ID() AND COMMAND <> 'Sleep' "
                        "AND (INFO LIKE %s OR LOCATE('scheduler_lock',INFO)>0)",
                        (backend['database'], 'CALL proc_complete_job%')):
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                while not execute(backend,
                        'SELECT expires_at <= SYSDATE() AS expired FROM reservations')[0]['expired']:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            finally:
                blocker.rollback()
            future.result(timeout=10)
    finally:
        cur.close()
        blocker.close()
    assert execute(backend, 'SELECT status FROM jobs')[0]['status'] == 'running'
    assert not execute(backend, 'SELECT * FROM job_results')
