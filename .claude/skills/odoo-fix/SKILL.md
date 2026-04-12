---
name: odoo-fix
description: Apply safe Odoo-oriented fixes while respecting module boundaries and existing patterns.
---

# Odoo Fix Skill

## Instructions
1. Respect Odoo module boundaries.
2. Avoid touching core Odoo unless explicitly allowed.
3. Be careful with env.cr, manual cursors, cron jobs, and transaction boundaries.
4. Add or update tests if possible.
5. Keep the patch minimal and easy to review.