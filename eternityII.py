"""MariaDB worker with fenced claims, independent heartbeats and checkpoints."""
import json
import hashlib
import logging
import os
import re
from pathlib import Path
import threading
import time

import mysql.connector

from checkpoint_solver import solve_resumable
from piece_loader import load_pieces
from job_tools import DATA, puzzle_fingerprint, search_policy
from search_model import SearchModel
from solver import Piece

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
            if not re.fullmatch(r'proc_[a-z_]+', name):
                raise ValueError('invalid procedure name')
            cur.execute(f"CALL {name}({','.join(['%s'] * len(args))})", args)
            rows = []
            while True:
                if cur.with_rows:
                    rows.extend(cur.fetchall())
                if not cur.nextset():
                    break
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
    call_procedure('proc_save_search_checkpoint',
                   (job['job_id'], WORKER_ID, job['claim_token'], json.dumps(state)))
    LOG.info('Job %s: nodes=%s depth=%s/%s best_partial_score=%s elapsed=%.1fs rate=%.1f nodes/s pruned=%s',
             job['job_id'], state['nodes'], state['current_depth'], state['max_depth'],
             state['best_score'], state['elapsed_seconds'], state['nodes_per_second'], state['pruned'])


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
    if parameters.get('pieces_source', 'file') == 'database':
        conn = db()
        try:
            cur = conn.cursor(dictionary=True)
            try:
                cur.execute('SELECT * FROM pieces ORDER BY piece_id')
                pieces = [Piece(p['piece_id'], (p['top_motif'], p['right_motif'],
                                              p['bottom_motif'], p['left_motif'])) for p in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()
    elif parameters.get('pieces_source', 'file') == 'file':
        path = Path(parameters.get('pieces_file', str(DATA)))
        if parameters.get('pieces_sha256') and hashlib.sha256(path.read_bytes()).hexdigest() != parameters['pieces_sha256']:
            raise ValueError('piece file does not match the partition batch fingerprint')
        pieces = load_pieces(path)
    else:
        raise ValueError('pieces_source must be file or database')
    if parameters.get('puzzle_fingerprint') and puzzle_fingerprint(pieces) != parameters['puzzle_fingerprint']:
        raise ValueError('puzzle catalog does not match the partition batch fingerprint')
    legacy = checkpoint is not None and checkpoint.get('version') == 1
    fixed, forward, official = search_policy(pieces, parameters, legacy)
    SearchModel(pieces, parameters.get('size'), fixed)
    catalog = [dict(piece_id=p.id, top=p.edges[0], right=p.edges[1],
                    bottom=p.edges[2], left=p.edges[3], edges=p.edges) for p in pieces]
    call_procedure('proc_register_puzzle', (json.dumps(catalog),))
    if legacy:
        LOG.warning('Resuming legacy checkpoint with original unclued search rules')
    by_id = {p.id: p for p in pieces}
    placements = [by_id[item['piece_id']].rotated(item.get('rotation', 0))
                  for item in json.loads(prefix or '[]')]
    latest = {}

    def persist(state):
        state['official_starter'] = int(official)
        latest.update(state)
        save(state)

    result = solve_resumable(
        pieces, size=parameters.get('size'), prefix=placements, checkpoint=checkpoint,
        save_checkpoint=persist, check_alive=check_alive, fixed=fixed, forward_check=forward,
        checkpoint_interval=parameters.get('checkpoint_interval', 30))
    return dict(score=result['best_score'], depth=result['depth'],
                duration=int(latest['elapsed_seconds']), outcome=latest['outcome'])


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
        except mysql.connector.Error as exc:
            if exc.sqlstate == '45000' and 'Claim lost or expired' not in str(exc):
                mark_failed(job, exc)
                LOG.error('Job %s rejected by database validation: %s', job['job_id'], exc)
                return
            # A checkpoint may have committed despite a lost acknowledgement.
            LOG.exception('Database unavailable for job %s', job['job_id'])
            return
        except Exception as exc:
            if not lost.is_set():
                mark_failed(job, exc)
                LOG.error('Job %s failed: %s', job['job_id'], exc)
            return
        check_alive()
        # Never mark failed after completion may have committed successfully.
        complete_job(job, result['score'], result['depth'], result['duration'])
        LOG.info('Job %s completed: outcome=%s score=%s depth=%s duration=%ss',
                 job['job_id'], result.get('outcome', 'completed'), result['score'],
                 result['depth'], result['duration'])
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
                LOG.info('Claimed job %s on worker %s', job['job_id'], WORKER_ID)
                process_job(job)
                continue
        except (mysql.connector.Error, ClaimLost):
            LOG.exception('Worker operation failed; retrying after poll interval')
        time.sleep(JOB_POLL_INTERVAL)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    worker_loop()
