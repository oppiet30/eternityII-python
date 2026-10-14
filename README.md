[![Python application](https://github.com/oppiet30/eternityII-python/actions/workflows/python-app.yml/badge.svg)](https://github.com/oppiet30/eternityII-python/actions/workflows/python-app.yml)
# eternityII-python
An EternityII solver with MariaDB backend.

## Database and worker

`eternityII.sql` is a fresh-install schema for MariaDB 10.11+ (CI uses 11.4).
It retains database name `eternity2`, the puzzle/board tables, reservations,
results, health procedure and monitoring views. Import into an empty database;
this file is not an in-place migration. Back up any data you want to retain
before you choose to replace an existing installation. The worker never drops
databases. No solver jobs are automatically assigned by table triggers.

Register a worker once, for example:

```sql
INSERT INTO workers(worker_id, hostname, cores, max_jobs)
VALUES (1, 'solver-one', 4, 1);
INSERT INTO jobs(parameters) VALUES ('{"size":16,"checkpoint_interval":30}');
```

Set `ETERNITY_WORKER_ID`, `ETERNITY_DB_HOST`, `ETERNITY_DB_PORT`,
`ETERNITY_DB_USER` and `ETERNITY_DB_PASSWORD`, then run `python eternityII.py`.
The database name is fixed to `eternity2`. There is no built-in password.
Use a database account with EXECUTE on lifecycle routines and read access
appropriate to your monitoring needs; routine definers need table permissions.
The worker requires a pre-existing worker row and pulls one job at a time.
Multiple processes sharing a worker ID collectively respect `max_jobs`.

Jobs use the indexed exact solver, not simulated results. `parameters` accepts
`pieces_file` (default: bundled `e2pieces.txt`), `size`, and
`checkpoint_interval` in seconds. `prefix` is an optional JSON array of
row-major placements, e.g. `[{"piece_id":1,"rotation":0}]`; rotations are
clockwise quarter turns. Invalid or incompatible input fails the job with a
stored reason. `depth_target`, `score_target`, quota and other metadata remain
available for future search policies; this worker performs exact search and
does not interpret them. Exhaustive 16x16 search is still expensive. Score is
the number of matched internal edges (480 on a solved 16x16 board), or zero
if the search is exhausted without a solution. Results record score, depth and
duration; board persistence remains a separate application concern.

### Ownership and recovery

All lifecycle procedures own their transactions and take the scheduler row
lock first. Call them from autocommit connections, outside an existing
transaction. This serializes short database operations while solvers run in
parallel. Do not update job ownership/status directly from another scheduler.
Lease checks use execution-time `SYSDATE()` after acquiring the lock, so time
spent waiting cannot validate an expired claim. Leave `sysdate-is-now` disabled;
use row-based binary logging if replicating this database (see the
[MariaDB SYSDATE documentation](https://mariadb.com/docs/server/reference/sql-functions/date-time-functions/sysdate)).

- `proc_claim_job(worker_id)` (also `proc_assign_job_throttled(worker_id)`)
  returns at most one job, its unique attempt token and saved checkpoint.
  Empty queues or full worker capacity return an empty result set.
- `proc_worker_heartbeat(worker_id, job_id, token)` renews that job's
  five-minute lease. Use NULL job/token to announce worker availability.
  The Python worker renews every 30 seconds in a separate thread.
- `proc_save_checkpoint(job_id, worker_id, token, JSON)` replaces the saved
  cursor stack. Checkpoints include an input fingerprint and version, so
  changed puzzle data cannot silently resume the wrong search.
- `proc_complete_job(job_id, worker_id, token, score, depth, duration)` and
  `proc_finish_job(job_id, worker_id, token, 'failed', NULL, NULL, NULL, reason)`
  require a live claim. Completion and result insertion commit atomically.
  Repeating the same terminal operation is harmless. A different or expired
  attempt cannot complete, fail, renew or overwrite a checkpoint.
- `proc_recover_workers()` and `proc_cleanup_expired_reservations()` mark
  stale workers offline and requeue jobs with expired/missing leases or offline
  owners. Checkpoints survive; a new claim gets a new token. Heartbeating an
  offline worker restores its availability. No retry limit is imposed.

The retained heartbeat and health events require the MariaDB event scheduler
and EVENT privilege at import time. The worker also calls recovery on every
poll, so recovery works with the event scheduler disabled while at least one
worker is polling. Database outages abandon an attempt for lease recovery;
solver input errors fail it. Explicitly set a worker offline and call recovery
to release its running jobs immediately.

Procedure signatures have changed deliberately: old unfenced completion and
push-style assignment callers must be updated along with the SQL reimport.
Reservation rows now represent one active attempt per job; results are unique
per job. All tables explicitly use InnoDB. Checkpoints are retained after
completion for inspection and can be cleaned up by an administrator later.

## Tests

`python -m pytest` runs solver/worker tests and skips real database tests by
default. To run integration tests against a disposable MariaDB instance:

```sh
ETERNITY_TEST_MARIADB=1 ETERNITY_TEST_PASSWORD=integration-test python -m pytest -q
```

Optional connection settings: `ETERNITY_TEST_HOST`, `ETERNITY_TEST_PORT`,
`ETERNITY_TEST_USER`. Tests create and remove only a random
`eternity2_test_<uuid>` schema, never the actual `eternity2` database. The test
account needs CREATE/DROP database and routine/event/trigger privileges.
GitHub Actions runs the same suite against an isolated MariaDB 11.4 service.
