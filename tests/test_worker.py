import threading
import time

import mysql.connector
import pytest

import eternityII as worker


JOB = dict(job_id=1, claim_token='token', prefix=None, parameters=None, checkpoint=None)


def test_heartbeat_runs_during_solver(monkeypatch):
    renewed = threading.Event()
    monkeypatch.setattr(worker, 'heartbeat', lambda job: renewed.set())
    completed = []
    monkeypatch.setattr(worker, 'complete_job', lambda *args: completed.append(args))

    def solver(*args):
        assert renewed.wait(2)
        return dict(score=4, depth=4, duration=1)

    worker.process_job(JOB, solver, heartbeat_interval=0.01)
    assert len(completed) == 1


def test_lost_heartbeat_cannot_publish(monkeypatch):
    attempted = threading.Event()

    def fail(job):
        attempted.set()
        raise mysql.connector.Error('connection lost')

    monkeypatch.setattr(worker, 'heartbeat', fail)
    monkeypatch.setattr(worker, 'complete_job', lambda *args: pytest.fail('published lost claim'))
    monkeypatch.setattr(worker, 'mark_failed', lambda *args: pytest.fail('failed lost claim'))

    def solver(prefix, params, checkpoint, save, alive):
        assert attempted.wait(2)
        # Synchronize with the heartbeat's exception handling.
        for _ in range(2000):
            alive()
            time.sleep(0.001)
        raise AssertionError('claim should have been abandoned')

    worker.process_job(JOB, solver, heartbeat_interval=0.01)


def test_ambiguous_completion_is_not_failed(monkeypatch):
    def fail(*args):
        raise mysql.connector.Error('lost completion acknowledgement')

    monkeypatch.setattr(worker, 'complete_job', fail)
    monkeypatch.setattr(worker, 'mark_failed', lambda *args: pytest.fail('failed committed result'))
    with pytest.raises(mysql.connector.Error):
        worker.process_job(JOB, lambda *args: dict(score=4, depth=4, duration=1))


def test_bad_parameters_are_failed(monkeypatch):
    failed = []
    monkeypatch.setattr(worker, 'mark_failed', lambda *args: failed.append(args))
    worker.process_job(dict(JOB, parameters='{bad json'))
    assert len(failed) == 1
