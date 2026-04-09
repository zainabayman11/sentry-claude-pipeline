I now have a full picture of all changes. Here is the complete review:

---

## Review: `cluster-db-cursor-001` — Database cursor leak in sync job

---

### 1. Does the fix address the likely root cause?

**Partially — the plan is correct but the Odoo fix was never applied.**

The root cause diagnosis is accurate: `SyncJob.run()` opens `cr = registry.cursor()` without a `try/finally`, so any exception in `_process_batch()` skips `cr.close()` and leaks a Postgres connection. The proposed `try/except/finally` pattern is the correct Odoo-idiomatic fix.

**Critical gap:** the only commit (`d7930ea`) adds `debug_packet.json`, `plan.md`, and `pr_draft.md` to the worktree — **no source file in `demo-odoo-backend` was touched.** The execution agent ran in `sentry-claude-pipeline`'s own worktree instead of the `demo-odoo-backend` repo. The Odoo file `addons/sync/models/sync_job.py` remains unchanged.

---

### 2. Obvious gaps and edge cases

| # | Gap | Severity |
|---|-----|----------|
| 1 | **Fix not applied** — `sync_job.py` unchanged | **Blocker** |
| 2 | Pipeline bug: `create_worktree` is called from the pipeline repo root, not from `demo-odoo-backend`; the execution agent wrote into the wrong directory | **Blocker** |
| 3 | `run_git_command` now captures stdout/stderr but callers never inspect it — silent failures remain possible | Low |
| 4 | `commit_and_push_worktree` uses `git push` without `capture_output=True` (inconsistent with the rest of `run_git_command`) | Low |
| 5 | `_process_batch` stub behaviour (always raises) not addressed — the underlying logic still needs implementing | Medium |
| 6 | No mention of `savepoints` if batch processing needs partial rollback | Out of scope / acceptable |

---

### 3. Are the changes minimal enough?

The **pipeline** changes (`claude_runner.py`, `git_ops.py`, `pipeline.py`) are reasonable clean-ups (fallback for empty `ResultMessage.result`, `commit_and_push_worktree`, `ensure_source_files_exist`, multi-command CLI). They are well-scoped, though they go slightly beyond the immediate bug fix and belong in a separate PR.

The worktree commit is over-scoped in the wrong direction: it committed orchestration files (`plan.md`, `pr_draft.md`, `debug_packet.json`) into the target repo worktree, which pollutes it with pipeline artefacts.

---

### 4. Tests still missing

- Unit test: mock `_process_batch` to raise → assert `cr.close()` called, `cr.rollback()` called, `cr.commit()` **not** called.
- Unit test: happy path → assert `cr.commit()` and `cr.close()` each called exactly once.
- Integration/smoke test: trigger a real sync job that raises mid-batch; verify no connection growth in `pg_stat_activity`.
- The plan mentions no tests at all — a gap for a P1 fix.

---

### 5. Final PR Body (draft)

```markdown
## fix: close DB cursor on exception in SyncJob.run (ODOO-BE-412, ODOO-BE-413)

### Root cause

`SyncJob.run()` opened a cursor with `self.env.registry.cursor()` but only
called `cr.close()` on the success path. Any exception raised inside
`_process_batch()` caused the cursor to leak, exhausting PostgreSQL's
`max_connections` under sustained failures:

    OperationalError: too many clients already

Sentry: ODOO-BE-412 · ODOO-BE-413

### Fix

Wrapped the cursor lifecycle in `try/except/finally`:

```python
def run(self):
    cr = self.env.registry.cursor()
    try:
        self._process_batch(cr)
        cr.commit()
    except Exception:
        cr.rollback()   # discard partial writes
        raise
    finally:
        cr.close()      # guaranteed on every code path
```

Change is confined to `addons/sync/models/sync_job.py` — one method,
no interface or schema changes.

### Tests

- [ ] Unit: mock `_process_batch` to raise → `cr.close()` called,
      `cr.commit()` NOT called, `cr.rollback()` called.
- [ ] Unit: happy path → `cr.commit()` and `cr.close()` each called once.
- [ ] Manual: trigger sync job with failing batch; confirm
      `pg_stat_activity` connection count does not climb.

### Notes

- No Odoo core files modified.
- `_process_batch` sub-cursor usage should be audited in a follow-up
  if batch sizes remain large.

Fixes: ODOO-BE-412, ODOO-BE-413
```

---

### Summary verdict

| Question | Answer |
|---|---|
| Fix addresses root cause? | Plan yes; code **not applied** (blocker) |
| Gaps / edge cases? | Pipeline creates worktree in wrong repo |
| Changes minimal? | Pipeline changes are clean but belong in a separate PR; worktree polluted with artefacts |
| Tests missing? | All regression tests (none were added) |

**Before pushing:** recreate the worktree from `demo-odoo-backend`, apply the `try/except/finally` fix to `sync_job.py`, add at least the two unit tests, then remove `plan.md`/`pr_draft.md`/`debug_packet.json` from the target repo's worktree.