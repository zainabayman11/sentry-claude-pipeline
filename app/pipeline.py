import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from app.models import validate_debug_packet, validate_source_files_exist
from app.memory_store import find_similar_incident, append_memory_record
from app.git_ops import create_worktree, commit_and_push_worktree, create_github_pr
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


def read_text_file(path: Path) -> str:
    """
    Read text file with standard UTF-8 encoding.
    Raises FileNotFoundError if the file does not exist.
    """
    return path.read_text(encoding="utf-8")


def require_file(path: Path, phase_name: str) -> str:
    """
    Read a file required for a specific pipeline phase.
    Raises FileNotFoundError with a helpful message if missing.
    """
    try:
        return read_text_file(path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{phase_name} file not found: {path}. "
            f"Ensure '{phase_name}' has run successfully first."
        )


def ensure_source_files_exist(packet: dict) -> None:
    repo_path = Path(packet["code_hints"]["repo"])
    missing = []

    for rel_file in packet["code_hints"]["files"]:
        full_path = repo_path / rel_file
        if not full_path.exists():
            missing.append(str(full_path))

    if missing:
        raise FileNotFoundError(
            "Missing source files:\n" + "\n".join(missing)
        )


def main():
    if len(sys.argv) < 3:
        console.print(
            "[red]Usage:[/red] python -m app.pipeline <plan|execute|review|push|memory> data/sample_debug_packet.json"
        )
        sys.exit(1)

    command = sys.argv[1]
    packet_path = Path(sys.argv[2])

    if not packet_path.exists():
        console.print(f"[red]Packet file not found:[/red] {packet_path}")
        sys.exit(1)

    console.rule("[bold magenta]Claude Bug Pipeline")

    # Load and validate packet once for all commands
    packet = json.loads(read_text_file(packet_path))
    validate_debug_packet(packet)

    cluster_id = packet["summary"]["cluster_id"]
    repo_path = Path(packet["code_hints"]["repo"])
    branch_name = f"fix-{cluster_id}"

    # Validate source files exist for plan and execute commands
    if command in ("plan", "execute"):
        validate_source_files_exist(packet)

    # Set up state directory (created only when needed)
    state_dir = Path(".pipeline_state") / cluster_id
    plan_file = state_dir / "plan.md"
    execution_file = state_dir / "execution_summary.md"
    review_file = state_dir / "review.md"
    meta_file = state_dir / "meta.json"

    if command == "plan":
        state_dir.mkdir(parents=True, exist_ok=True)

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

        plan_result = run_phase_sync(
            title="Planning",
            prompt=planning_prompt(packet, memory_match),
            cwd=str(repo_path),
            allowed_tools=["Read", "Glob", "Grep", "Skill"],
            permission_mode="plan",
            max_turns=15,
            append_system_prompt="Do not edit files. Produce plan only.",
        )

        write_text_file(plan_file, plan_result)
        console.print(f"[green]✓ Plan written:[/green] {plan_file}")

    elif command == "execute":
        if not plan_file.exists():
            raise FileNotFoundError("Plan file not found. Run plan first.")

        state_dir.mkdir(parents=True, exist_ok=True)

        worktree_path = create_worktree(str(repo_path), branch_name)
        console.print(f"[green]✓ Worktree created:[/green] {worktree_path}")

        worktree = Path(worktree_path)
        write_text_file(worktree / "debug_packet.json", json.dumps(packet, indent=2, ensure_ascii=False))
        write_text_file(worktree / "plan.md", require_file(plan_file, "Plan"))

        execution_result = run_phase_sync(
            title="Execution",
            prompt=execution_prompt(packet),
            cwd=worktree_path,
            allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Skill"],
            permission_mode="acceptEdits",
            max_turns=30,
            append_system_prompt="Implement only after approved plan. Do not scaffold missing source files. If file missing, stop.",
        )

        write_text_file(execution_file, execution_result)
        write_text_file(meta_file, json.dumps({"worktree_path": worktree_path}, indent=2))
        console.print(f"[green]✓ Execution done:[/green] {execution_file}")

    elif command == "review":
        if not meta_file.exists():
            raise FileNotFoundError("Execution metadata not found. Run execute first.")

        meta = json.loads(require_file(meta_file, "Metadata"))
        worktree_path = meta["worktree_path"]

        review_result = run_phase_sync(
            title="Review",
            prompt=review_prompt(packet),
            cwd=worktree_path,
            allowed_tools=["Read", "Glob", "Grep", "Skill"],
            permission_mode="plan",
            max_turns=15,
            append_system_prompt="Review only. Do not edit files.",
        )

        state_dir.mkdir(parents=True, exist_ok=True)
        write_text_file(review_file, review_result)
        write_text_file(Path(worktree_path) / "pr_draft.md", review_result)
        console.print(f"[green]✓ Review written:[/green] {review_file}")

    elif command == "push":
        if not review_file.exists():
            raise FileNotFoundError("Review file not found. Run review first.")

        meta = json.loads(require_file(meta_file, "Metadata"))
        worktree_path = meta["worktree_path"]

        pr_title = f"fix: {packet['summary']['title']} ({cluster_id})"
        pr_body_path = Path(worktree_path) / "pr_draft.md"

        if not pr_body_path.exists():
            raise FileNotFoundError(f"PR draft file not found: {pr_body_path}")

        try:
            commit_and_push_worktree(
                worktree_path=worktree_path,
                branch_name=branch_name,
                commit_message=pr_title,
            )
        except Exception as e:
            console.print(f"[yellow]Warning during push:[/yellow] {e}")
            console.print("[yellow]Continuing to PR creation attempt...[/yellow]")

        create_github_pr(
            worktree_path=worktree_path,
            title=pr_title,
            body_file=str(pr_body_path),
        )

        append_memory_record(packet, require_file(execution_file, "Execution"))
        console.print("[green]✓ PR created and memory updated[/green]")

    elif command == "memory":
        if not execution_file.exists():
            raise FileNotFoundError("Execution summary not found. Run execute first.")

        if not review_file.exists():
            raise FileNotFoundError("Review file not found. Run review first.")

        execution_summary = require_file(execution_file, "Execution")
        append_memory_record(packet, execution_summary)
        console.print("[green]✓ Memory updated with execution summary[/green]")

    else:
        console.print(f"[red]Unknown command:[/red] {command}")
        console.print("Valid commands: plan, execute, review, push, memory")
        sys.exit(1)


if __name__ == "__main__":
    main()
