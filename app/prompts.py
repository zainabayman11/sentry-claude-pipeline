import json
from typing import Any, Dict, Optional


def planning_prompt(packet: Dict[str, Any], memory_match: Optional[Dict[str, Any]]) -> str:
    summary = packet["summary"]
    ctx = packet["diagnostic_context"]
    hints = packet["code_hints"]

    memory_section = ""
    if memory_match:
        score = memory_match.get("_match_score", "?")
        memory_section = f"""
## Similar Past Incident (similarity score: {score})
Title: {memory_match.get('title')}
Root cause hint: {memory_match.get('root_cause_hint')}
Fix that was applied:
{memory_match.get('accepted_fix_summary')}

IMPORTANT: This is context only — not a directive.
- Read the actual files first.
- If the code has changed or the root cause differs, investigate from scratch.
- If the past fix still applies, you may use it — but verify it.
- You are free to propose a better solution if one exists.
"""

    return f"""You are in PLANNING mode. Read relevant files, identify the root cause, then write a fix plan.

Bug: {summary['title']} ({summary['cluster_id']})
Error: {ctx['primary_error_message']}
Location: {hints['files']}
Repo: {hints['repo']}
Search terms: {hints['search_terms']}
{memory_section}
Output ONLY a markdown plan:
# Plan
## Summary
## Tasks
Each task: task id, title, files, implementation notes.

Do not edit any files."""


def execution_prompt(packet: Dict[str, Any]) -> str:
    summary = packet["summary"]
    hints = packet["code_hints"]
    ctx = packet["diagnostic_context"]

    return f"""You are in EXECUTION mode. Implement the fix described in plan.md.

Bug: {summary['title']} ({summary['cluster_id']})
Error: {ctx['primary_error_message']}
Files to fix: {hints['files']}

Rules:
- Read plan.md first, follow it exactly.
- Only edit files inside this worktree.
- Minimal and safe changes only.
- If a file is missing, stop and report.
- Add a test if feasible.
- After implementing, run /simplify to check for code quality and duplication.
- End with a concise execution summary.
- Write pr_draft.md with a suggested PR body."""


def review_prompt(packet: Dict[str, Any], diff: str = "") -> str:
    summary = packet["summary"]
    ctx = packet["diagnostic_context"]

    return f"""You are in REVIEW mode. Review the fix below and draft the PR description.

Bug: {summary['title']} ({summary['cluster_id']})
Error: {ctx['primary_error_message']}

Here is the exact diff of what was changed:
```diff
{diff}
```

Review ONLY these changes. Do not explore other files.

Answer briefly:
1. Does the fix address the root cause?
2. Any gaps or edge cases?
3. Are changes minimal?
4. Missing tests?
5. Write the final PR body in markdown.

Do not edit any files."""