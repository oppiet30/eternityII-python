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

Use `worker.cfg` as described below, or set `ETERNITY_WORKER_ID`, `ETERNITY_DB_HOST`, `ETERNITY_DB_PORT`,
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
the number of matched internal edges among pieces actually placed (480 on a
solved 16x16 board). Exhausted jobs record their best valid partial placement
score. This is not a complete board with mismatched edges and cannot be compared
directly to published near-solution records. Best placements and full solutions
are saved in `boards` and `board_cells`; `job_results.board_id` links to them.

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
GitHub Actions runs the same suite against isolated MariaDB 10.11 and 11.4 services.

## Permanent worker settings

Copy `worker.cfg.example` to `worker.cfg` beside `eternityII.py` on each machine:

```ini
[database]
host = dell
port = 3306
user = solver
password = your_actual_database_password

[worker]
id = 2
```

Use ID 1 on dell and ID 2 on optiplex; the host must name the shared MariaDB
server. Values are literal: do not quote them, and percent signs in passwords
do not invoke interpolation. On Linux, run `chmod 600 worker.cfg`. This file
is ignored by Git; the example contains no password. An optional
`ETERNITY_CONFIG` variable selects a different file; an explicitly selected
missing file is an error. The default file is optional, preserving existing
environment-only installations. Database name remains `eternity2`.

Environment variables override corresponding file entries, including an
explicitly empty password. Configuration is read at startup; restart to apply
changes. If old exported values override the new file, unset them first.
The documented password variable is `ETERNITY_DB_PASSWORD`, not `solverpass`.
The latter only worked with a locally modified password lookup.

For an `at` job, use the virtual environment's interpreter explicitly, e.g.
`./venv/bin/python -u eternityII.py >> worker.log 2>&1` after changing to the
repository folder. The new worker reads `worker.cfg` automatically; no venv
activation or exports are necessary when that file supplies your settings.

## Upgrade an existing PR #6 installation

Stop Python workers, back up your database, then import
`migrations/007_search_progress.sql` as the routine-owning administrative
account. **Do not drop or reimport your existing database.** Restart the updated
workers with the same connection settings. The fresh-install `eternityII.sql`
already contains this upgrade. MariaDB 10.11+ is required. Integration tests
exercise both fresh installs and the additive upgrade (including a repeated
upgrade) against MariaDB 10.11 and 11.4.
Stopped attempts may wait up to five minutes for their leases to expire before
being requeued and resumed. Idle worker heartbeats do not renew those old job
leases. Either machine can resume a recovered job; queued work may be claimed
first, so restarting does not guarantee the same machine immediately resumes
its previous job.

Existing version-1 checkpoints resume the original candidate order with no
new starter constraint or pruning. They are labeled `official_starter=0` in
progress, even if new-job defaults have changed. Version-2 checkpoints bind
their fixed placements and pruning policy into the input fingerprint. Changing
those policies requires new jobs, not silently modifying a resumed search.
Existing version-1 checkpoints that never saved a best board cannot reconstruct
a historical best placement; the new worker begins recording the best actual
board available since the upgrade. Historical maximum depth remains recorded.

### Progress and saved boards

Every saved checkpoint prints job ID, visited nodes, current/maximum depth,
matched-edge partial score, cumulative active search seconds, average nodes per
second and pruned branches. Job start, failure and completion also print
messages. Search time resumes cumulatively from the checkpoint and excludes
time offline. This is progress reporting, not an ETA or percentage complete.
Heartbeat messages are independent of the solver's progress interval.

```sql
SELECT * FROM view_job_progress;
SELECT * FROM view_oriented_board_cells WHERE board_id = YOUR_BOARD_ID;
```

`outcome` distinguishes `searching`, `solved` and `exhausted`; job `status`
remains the execution state. A terminal checkpoint can be saved before result
completion commits, so `outcome='solved'` does not alone mean the job has been
completed in the queue. Statistics, best board and checkpoint commit atomically
and reject stale claim tokens. Board cells are checked for uniqueness, borders,
neighbor matches and (when enabled) the mandatory starter.

The file-backed worker now registers numeric motifs and all piece edges in
MariaDB automatically. It preserves existing motif names and refuses to
overwrite conflicting piece definitions. You may set `pieces_source` to
`database` in job parameters to read that registered catalog instead.
One database catalog represents one puzzle encoding; use a separate database
installation for incompatible encodings. Database name remains `eternity2`.

### Starter piece and pruning

New searches on the bundled dataset default to the mandatory starter: piece
139 at zero-based row 8, column 7 (I8), rotation 2 in this file's orientation.
Its oriented edges are `(18,6,6,11)` in top/right/bottom/left order. Published
encodings use different rotation numbers; compare actual edge orientation,
not just the number. Provenance: [TheSil's hint coordinates](https://github.com/TheSil/edge_puzzle/blob/master/data/eternity2/eternity2_256_hints.csv)
and [Anton Fagerberg's I8 / piece 139 / rotation 2 / RFFK table](https://www.antonfagerberg.com/projects/eternity-2-solver/).
Use `official_starter=false` for explicitly unclued experiments. Custom datasets
do not automatically inherit a numbered starter; enabling it requires the
exact bundled piece definitions. `fixed` accepts additional objects with
zero-based `row`, `col`, `piece_id`, and clockwise `rotation` (0..3).

New searches also default to `forward_check=true`. They check remaining
constrained frontier cells and require a matching of distinct unused piece IDs
to those cells, rejecting branches that cannot supply their next tiles. This
is a necessary-condition pruning check, not a claim of practical 16x16 solve
times. `forward_check=false` provides a comparison baseline.

### Automatic task partitioning

Generate a reviewed batch of distinct prefixes without changing the database:

```sh
python job_tools.py --depth 2 --output partition-batch.sql
```

`--depth` is the number of leading row-major placements in each task (depth 1
gives four corner tasks). Each generated task covers a disjoint subtree; known
impossible branches may be excluded by forward checking. `--max-jobs` prevents
accidentally generating an enormous batch and fails rather than truncating it.
Batch parameters include the piece-file hash, so machines using different
files cannot silently search inconsistent partitions.

To replace an existing unrestricted job, explicitly add `--replace-job ID`.
To subdivide a prefixed job, also supply its exact prefix with `--prefix JSON`.
This restarts that subtree: a source checkpoint is kept for inspection and
does not transfer to child searches. The saved SQL calls a procedure that
atomically fences the source attempt, releases its reservation and queues all
children. A repeated import of the same batch is harmless; errors roll back
the entire replacement. A batch with a different UUID is a separate request
and can duplicate an existing search, so import only the intended plan.

Review the file, then import it through the MariaDB client:

```sql
SOURCE /path/to/partition-batch.sql;
```

The generator accepts `--parameters JSON`, `--prefix JSON`, and
`--pieces-file PATH`. If using a custom file, also specify its accessible
worker-side path through `parameters.pieces_file`. The database batch routine
checks shape, equal prefix depth and duplicates; semantic legality and
coverage are supplied by the generator and tested against exhaustive small
puzzles. Only use generated, reviewed batches for replacement.
