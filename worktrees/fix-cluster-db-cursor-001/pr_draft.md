I now have enough context. Here is the full review.

---

## Review: `cluster-db-cursor-001` — Database cursor leak in sync job

---

### 1. Does the fix address the likely root cause?

**Yes — but the fix was never applied.**

The root cause diagnosis is correct: `SyncJob.run()` opens `cr = registry.cursor()` and only calls `cr.close()` on the success path. An exception in `_process_batch()` at line 148 exits the frame without closing the cursor, leaking a connection. Under load, this exhausts PostgreSQL's `max_connections`.

The planned fix (wrapping in `try/finally` to guarantee `cr.close()`) is the right and idiomatic Odoo solution.

**Critical gap:** the execution agent correctly stopped because `addons/sync/models/sync_job.py` does not exist inside the worktree. The worktree was created from `sentry-claude-pipeline` (the pipeline tool repo), not from `demo-odoo-backend` (the target Odoo repo). **No code was changed.** The pipeline itself has a bug in `pipeline.py` — `create_worktree` is called with the pipeline's own `repo_path` when it should use `packet["code_hints"]["repo"]` pointing to `demo-odoo-backend`.

---

### 2. Obvious gaps and edge cases

| Gap | Severity |
|---|---|
| Fix not applied — worktree created from wrong repo | **Blocker** |
| `cr.rollback()` itself can raise (e.g., on a broken connection) — not handled | Low (Odoo's `sql_db` is tolerant, but worth noting) |
| `_process_batch` may open sub-cursors of its own — not reviewed | Unknown — needs inspection |
| T-2 ("replace simulated stub") is vague — no concrete code shown | Medium — leaves ambiguity for the executor |
| No mention of savepoints if `_process_batch` needs partial rollback | Out of scope for this P1, acceptable |

---

### 3. Are the changes minimal enough?

The **planned** change is minimal — one method, two structural changes (add `try/except/finally`, add `cr.rollback()` on failure). Nothing else touched.

The pipeline infrastructure files (`claude_runner.py`, `git_ops.py`, `pipeline.py`) were also modified, but those changes are unrelated to the Odoo fix and appear to be pipeline improvements (fallback for empty `ResultMessage.result`, worktree existence check, `ensure_source_files_exist` guard). These are clean and well-scoped.

---

### 4. Tests still missing

- **Regression test** simulating an exception in `_process_batch` — asserts `cr.close()` is called exactly once (via mock or a test-double registry).
- **Test** verifying `cr.rollback()` is called on failure, not `cr.commit()`.
- **Test** for the happy path: `cr.commit()` called, `cr.close()` called, no rollback.
- No tests were added in this cycle. The plan does not mention tests at all — a gap for a P1 fix.

---

### 5. PR Body (draft)

```markdown
## fix: close DB cursor on exception in SyncJob.run (ODOO-BE-412, ODOO-BE-413)

### Root cause

`SyncJob.run()` opened a cursor with `registry.cursor()` but only called
`cr.close()` on the success path. Any exception raised inside `_process_batch()`
caused the cursor to leak. Under sustained load this exhausted PostgreSQL's
`max_connections`, producing:

```
OperationalError: too many clients already
```

Sentry traces: [ODOO-BE-412](https://sentry.example/issue/412) · [ODOO-BE-413](https://sentry.example/issue/413)

### Fix

Wrapped the cursor lifecycle in `try/except/finally` so `cr.close()` is
**always** executed, and `cr.rollback()` is called before re-raising to discard
partial writes:

```python
def run(self):
    cr = self.registry.cursor()
    try:
        self._process_batch(cr)
        cr.commit()
    except Exception:
        cr.rollback()
        raise
    finally:
        cr.close()
```

Change is confined to `addons/sync/models/sync_job.py` — one method, no
interface changes.

### Testing

- [ ] Manually trigger a sync job that raises mid-batch; verify connection count
      does not climb in `pg_stat_activity`.
- [ ] Unit test: mock `_process_batch` to raise; assert `cr.close()` called and
      `cr.commit()` not called.
- [ ] Unit test: happy path — `cr.commit()` and `cr.close()` both called.

### Notes

- No Odoo core files modified.
- `_process_batch` sub-cursor usage should be audited in a follow-up if batch
  sizes remain large.
```

---

### Summary verdict

The diagnosis and plan are sound. The fix itself was **not applied** due to a pipeline misconfiguration (worktree points to the wrong repo). Before pushing, the worktree must be recreated from `demo-odoo-backend`, the fix applied, and at minimum a mock-based regression test added.