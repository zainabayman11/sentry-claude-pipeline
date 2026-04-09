import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from app.models import validate_debug_packet
from app.memory_store import find_similar_incident, append_memory_record
from app.git_ops import create_worktree
from app.prompts import planning_prompt, execution_prompt, review_prompt
from app.claude_runner import run_phase_sync

console = Console()


def write_text_file(path: Path, content: str) -> None:
    """
    Utility helper to write text content to a file.
    Ensures parent directories exist before writing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main():
    """
    Main Entry Point:
    Usage: python -m app.pipeline data/sample_debug_packet.json
    """
    if len(sys.argv) < 2:
        console.print(
            "[red]Usage:[/red] python -m app.pipeline data/sample_debug_packet.json"
        )
        sys.exit(1)

    packet_path = Path(sys.argv[1])
    if not packet_path.exists():
        console.print(f"[red]Packet file not found:[/red] {packet_path}")
        sys.exit(1)

    console.rule("[bold magenta]Claude Bug Pipeline")

    # 1) Load debug packet
    packet = json.loads(packet_path.read_text(encoding="utf-8"))

    # 2) Validate packet structure
    validate_debug_packet(packet)
    console.print("[green]✓ Packet validated[/green]")

    # 3) Extract metadata
    repo_path = packet["code_hints"]["repo"]
    cluster_id = packet["summary"]["cluster_id"]

    # 4) Historical memory lookup for deduplication and context
    memory_match = find_similar_incident(packet)
    if memory_match:
        console.print(
            Panel(
                json.dumps(memory_match, indent=2, ensure_ascii=False),
                title="Memory Match Found",
                border_style="green",
            )
        )
    else:
        console.print("[yellow]No similar incident found in memory[/yellow]")

    # 5) Phase: Planning
    # Focus on analysis and mapping the fix strategy
    plan_result = run_phase_sync(
        title="Planning",
        prompt=planning_prompt(packet, memory_match),
        cwd=repo_path,
        allowed_tools=["Read", "Glob", "Grep", "Skill"],
        permission_mode="plan",
        max_turns=15,
        append_system_prompt="Focus on root cause analysis and produce a concrete markdown plan.",
    )

    console.print(Panel(plan_result or "(empty plan)", title="Plan Result", border_style="cyan"))

    # 6) Environment Setup: Create an isolated Git Worktree
    branch_name = f"fix-{cluster_id}"
    worktree_path = create_worktree(repo_path, branch_name)
    console.print(f"[green]✓ Worktree created:[/green] {worktree_path}")

    worktree = Path(worktree_path)

    # 7) Sync artifacts into the new worktree
    write_text_file(worktree / "debug_packet.json", json.dumps(packet, indent=2, ensure_ascii=False))
    write_text_file(worktree / "plan.md", plan_result)

    # 8) Phase: Execution
    # Performing the actual code changes and implementation
    execution_result = run_phase_sync(
        title="Execution",
        prompt=execution_prompt(packet),
        cwd=worktree_path,
        allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Skill"],
        permission_mode="acceptEdits",
        max_turns=30,
        append_system_prompt="Implement the fix safely inside the current worktree only.",
    )

    console.print(
        Panel(
            execution_result or "(empty execution summary)",
            title="Execution Result",
            border_style="blue",
        )
    )

    write_text_file(worktree / "execution_summary.md", execution_result)

    # 9) Phase: Review
    # Final check of the changes and PR drafting
    review_result = run_phase_sync(
        title="Review",
        prompt=review_prompt(packet),
        cwd=worktree_path,
        allowed_tools=["Read", "Glob", "Grep", "Skill"],
        permission_mode="plan",
        max_turns=15,
        append_system_prompt="Review the current changes only. Do not edit files in this phase.",
    )

    console.print(
        Panel(
            review_result or "(empty review summary)",
            title="Review Result",
            border_style="magenta",
        )
    )

    write_text_file(worktree / "pr_draft.md", review_result)

    # 10) Update memory store for future reference
    append_memory_record(packet, execution_result)
    console.print("[green]✓ Memory updated[/green]")

    console.rule("[bold green]Pipeline Finished")
    console.print(f"[bold]Worktree:[/bold] {worktree_path}")
    console.print(f"[bold]Action Required:[/bold] Inspect the worktree and review the PR draft.")


if __name__ == "__main__":
    main()