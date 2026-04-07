## Plan Summary

**Root cause:** `SyncJob.run()` opens a cursor but only closes it on the happy path. When `_process_batch()` raises, `cr.close()` is skipped → connections accumulate → `OperationalError: too many clients already`.

**Fix is minimal — one method, two changes:**

### T-1 — Wrap cursor in `try/finally` (`addons/sync/models/sync_job.py`)

```python
def run(self):
    cr = self.registry.cursor()
    try:
        self._process_batch(cr)
        cr.commit()
    except Exception:
        cr.rollback()  # discard partial writes
        raise          # let job runner see the failure
    finally:
        cr.close()     # always runs, no more leaks
```

### T-2 — Replace the `raise Exception("simulated batch failure")` stub with real logic (keeping the T-1 pattern)