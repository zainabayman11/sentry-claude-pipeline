import json
from typing import Any, Dict, Optional


def planning_prompt(packet: Dict[str, Any], memory_match: Optional[Dict[str, Any]]) -> str:
    """
    Generates the prompt for the PLANNING phase.
    Provides the agent with debug data and historical memory context.
    """
    memory_section = "No similar incident found in memory."
    if memory_match:
        memory_section = f"""
Similar incident found in memory:
- Title: {memory_match.get("title")}
- Root cause hint: {memory_match.get("root_cause_hint")}
- Accepted fix summary: {memory_match.get("accepted_fix_summary")}
"""

    return f"""
You are in PLANNING mode.

Input debug packet:
{json.dumps(packet, indent=2, ensure_ascii=False)}

Memory context:
{memory_section}

Your job:
1. Understand the likely root cause.
2. Identify the most relevant files.
3. Produce a short and concrete implementation plan in markdown.

The plan must contain:
# Plan
## Summary
## Tasks

Each task must include:
- task id
- title
- files
- detailed implementation notes

Do not edit code in this phase.
"""


def execution_prompt(packet: Dict[str, Any]) -> str:
    """
    Generates the prompt for the EXECUTION phase.
    Instructs the agent to perform the actual code changes and testing.
    """
    return f"""
You are in EXECUTION mode.

Input packet:
{json.dumps(packet, indent=2, ensure_ascii=False)}

Rules:
- Work only inside the current worktree.
- Prefer minimal and safe changes.
- Inspect the files mentioned in code_hints first.
- Add tests if feasible.
- At the end, write a concise execution summary.

Also create/update:
- plan.md if needed
- pr_draft.md with a suggested PR body
"""


def review_prompt(packet: Dict[str, Any]) -> str:
    """
    Generates the prompt for the REVIEW phase.
    Instructs the agent to verify the fix and draft the final PR description.
    """
    return f"""
You are in REVIEW mode.

Input packet:
{json.dumps(packet, indent=2, ensure_ascii=False)}

Review the current worktree changes and answer:
1. Does the fix address the likely root cause?
2. Are there obvious gaps or edge cases?
3. Are the changes minimal enough?
4. What tests are still missing?
5. Draft a final PR body in markdown.

Do not make code changes in this phase.
"""