# Project Rules

You are working on an Odoo-based Python project fixing bugs reported via Sentry.

## Core Rules
- Never modify core Odoo files unless explicitly allowed.
- Prefer minimal and targeted changes — one bug, one fix.
- Explain the root cause before proposing a fix.
- If a cursor is opened manually, ensure it is safely closed in a finally block.
- Add or update regression tests when feasible.
- If the packet is missing critical context, say so clearly instead of guessing.
- Respect existing project patterns and module boundaries.

## Systematic Debugging (from gstack/investigate)
When investigating a bug:
1. Read the stack trace top-to-bottom — find the first frame inside custom code (not Odoo core).
2. Identify: what was the state when it failed? what was expected?
3. Check if similar patterns exist elsewhere in the same module.
4. Propose the minimal fix — not a refactor.
5. Verify the fix doesn't break the happy path.

## Odoo-Specific Safety
- Use `self.env.registry.cursor()` not `registry.cursor()` directly.
- Always wrap manual cursors in try/except/finally.
- Never call `cr.close()` without a paired `cr.commit()` or `cr.rollback()`.
- Do not change model field definitions without a migration script.
- Do not add new `@api.depends` without checking compute method side effects.