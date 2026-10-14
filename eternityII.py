"""MariaDB worker with fenced claims, independent heartbeats and checkpoints."""
import json
import logging
import os
from pathlib import Path
import threading
import time

import mysql.connector

from checkpoint_solver import solve_resumable
from piece_loader import load_pieces

LOG = logging.getLogger(__name__)
WORKER_ID = int(os.environ.get('ETERNITY_WORKER_ID', '1'))
HEARTBEAT_INTERVAL = 30
JOB_POLL_INTERVAL = 5


def db():
    return mysql.connector.connect(
        host=os.environ.get('ETERNITY_DB_HOST', 'localhost'),
        port=int(os.environ.get('ETERNITY_DB_PORT', '3306')),
        user=os.environ.get('ETERNITY_DB_USER', 'solver'),
        password=os.environ.get('ETERNITY_DB_PASSWORD', ''),
        database='eternity2', connection_timeout=10, autocommit=True)


def call_procedure(name, args=()):
    """Use a fresh connection and drain CALL result sets before closing it."""
    conn = db()
    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.callproc(name, args)
            rows = []
            for result in cur.stored_results():
                rows.extend(result.fetchall())
            return rows
        finally:
            cur.close()
    finally:
        conn.close()


def heartbeat(job=None):
    call_procedure('proc_worker_heartbeat',
                   (WORKER_ID, job['job_id'] if job else None,
                    job['claim_token'] if job else None))


def claim_job():
    rows = call_procedure('proc_claim_job', (WORKER_ID,))
    return rows[0] if rows else None


def save_checkpoint(job, state):
    call_procedure('proc_save_checkpoint',
                   (job['job_id'], WORKER_ID, job['claim_token'], json.dumps(state)))


def complete_job(job, score, depth, duration):
    call_procedure('proc_complete_job',
                   (job['job_id'], WORKER_ID, job['claim_token'], score, depth, duration))


def mark_failed(job, reason):
    call_procedure('proc_finish_job',
                   (job['job_id'], WORKER_ID, job['claim_token'], 'failed',
                    None, None, None, str(reason)))


class ClaimLost(RuntimeError):
    pass


def run_solver(prefix, parameters, checkpoint, save, check_alive):
    """Run exact search. Prefix is a JSON array of piece_id/rotation objects."""
    pieces = load_pieces(parameters.get(
        'pieces_file', str(Path(__file__).with_name('e2pieces.txt'))))
    by_id = {p.id: p for p in pieces}
    placements = [by_id[item['piece_id']].rotated(item.get('rotation', 0))
                  for item in json.loads(prefix or '[]')]
    start = time.monotonic()
    result = solve_resumable(
        pieces, size=parameters.get('size'), prefix=placements, checkpoint=checkpoint,
        save_checkpoint=save, check_alive=check_alive,
        checkpoint_interval=parameters.get('checkpoint_interval', 30))
    # Exhaustive no-solution search is successful job completion with score 0.
    n = parameters.get('size') or int(len(pieces) ** 0.5)
    return dict(score=2 * n * (n - 1) if result['board'] else 0,
                depth=result['depth'], duration=int(time.monotonic() - start))


def process_job(job, solver=run_solver, heartbeat_interval=HEARTBEAT_INTERVAL):
    """Renew only this attempt; any renewal error stops result publication."""
    stop, lost = threading.Event(), threading.Event()

    def renew():
        while not stop.wait(heartbeat_interval):
            try:
                heartbeat(job)
            except Exception:
                LOG.exception('Heartbeat failed for job %s', job['job_id'])
                lost.set()
                return

    def check_alive():
        if lost.is_set():
            raise ClaimLost('Heartbeat failed; abandon this attempt')

    def save(state):
        check_alive()
        save_checkpoint(job, state)

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        try:
            parameters = json.loads(job['parameters']) if job['parameters'] else {}
            checkpoint = json.loads(job['checkpoint']) if job.get('checkpoint') else None
            result = solver(job['prefix'], parameters, checkpoint, save, check_alive)
        except ClaimLost:
            LOG.warning('Abandoned claim for job %s', job['job_id'])
            return
        except mysql.connector.Error:
            # A checkpoint may have committed despite a lost acknowledgement.
            LOG.exception('Database unavailable for job %s', job['job_id'])
            return
        except Exception as exc:
            if not lost.is_set():
                mark_failed(job, exc)
            return
        check_alive()
        # Never mark failed after completion may have committed successfully.
        complete_job(job, result['score'], result['depth'], result['duration'])
    finally:
        stop.set()
        thread.join()


def worker_loop():
    while True:
        try:
            # Recovery works even when the optional event scheduler is disabled.
            call_procedure('proc_recover_workers')
            heartbeat()
            job = claim_job()
            if job:
                process_job(job)
                continue
        except (mysql.connector.Error, ClaimLost):
            LOG.exception('Worker operation failed; retrying after poll interval')
        time.sleep(JOB_POLL_INTERVAL)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    worker_loop()
