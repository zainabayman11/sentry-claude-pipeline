## Root Cause

`SyncJob.run()` opens a cursor but only closes it on the happy path. When `_process_batch` raises, execution jumps past `cr.close()` — the connection leaks. Repeated failures exhaust the Postgres pool → `OperationalError: too many clients already`.

## Plan Summary

**T-1** — Wrap cursor in `try/except/finally` in `addons/sync/models/sync_job.py`:
- `finally: cr.close()` — guarantees close on every path
- `except: cr.rollback(); raise` — rolls back partial writes, preserves the exception

**T-2** — Add a unit test in `addons/sync/tests/test_sync_job.py` using a mock cursor to assert `close()` is always called, even on failure.