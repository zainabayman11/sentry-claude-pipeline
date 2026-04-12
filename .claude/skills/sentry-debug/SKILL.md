---
name: sentry-debug
description: Analyze a Sentry debug packet, extract root cause hypotheses, and guide safe debugging steps.
---

# Sentry Debug Skill

## Instructions
1. Read the packet carefully.
2. Distinguish symptom from root cause.
3. Prioritize the files mentioned in code_hints and representative_traces.
4. Reuse prior incident memory if available.
5. Prefer small fixes over large refactors.
6. If context is insufficient, say exactly what is missing.

## Examples
- Cursor leaks
- Cron failures
- ORM misuse
- Missing transaction cleanup