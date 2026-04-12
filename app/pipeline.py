import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from app.models import validate_debug_packet, validate_source_files_exist
from app.memory_store import find_similar_incident, append_memory_record
from app.git_ops import create_worktree, commit_and_push_worktree, create_github_pr
from app.prompts import planning_prompt, execution_prompt, review_prompt
from app.claude_runner import run_phase_sync  # also sets UTF-8 stdout

console = Console()


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require_file(path: Path, phase_name: str) -> str:
    try:
        return read_text_file(path)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{phase_name} file not found: {path}. "
            f"Ensure '{phase_name}' has run successfully first."
        )


def _append_session_note(session_notes_file: Path, phase: str, content: str) -> None:
    existing = session_notes_file.read_text(encoding="utf-8") if session_notes_file.exists() else ""
    write_text_file(session_notes_file, existing + f"\n## {phase}\n\n{content}\n")


def _load_meta(meta_file: Path) -> dict:
    return json.loads(require_file(meta_file, "Metadata"))


def _save_meta(meta_file: Path, data: dict) -> None:
    write_text_file(meta_file, json.dumps(data, indent=2))


def ensure_source_files_exist(packet: dict) -> None:
    repo_path = Path(packet["code_hints"]["repo"])
    missing = []
    for rel_file in packet["code_hints"]["files"]:
        full_path = repo_path / rel_file
        if not full_path.exists():
            missing.append(str(full_path))
    if missing:
        raise FileNotFoundError("Missing source files:\n" + "\n".join(missing))


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

    packet = json.loads(read_text_file(packet_path))
    validate_debug_packet(packet)

    cluster_id = packet["summary"]["cluster_id"]
    repo_path = Path(packet["code_hints"]["repo"])
    branch_name = f"fix-{cluster_id}"

    if command in ("plan", "execute"):
        validate_source_files_exist(packet)

    state_dir = Path(".pipeline_state") / cluster_id
    plan_file = state_dir / "plan.md"
    execution_file = state_dir / "execution_summary.md"
    review_file = state_dir / "review.md"
    meta_file = state_dir / "meta.json"
    session_notes_file = state_dir / "session_notes.md"

    if command == "plan":
        state_dir.mkdir(parents=True, exist_ok=True)

        memory_match = find_similar_incident(packet)
        if memory_match:
            console.print(Panel(
                json.dumps(memory_match, indent=2, ensure_ascii=False),
                title="[bold green]Memory Match Found — reusing past fix",
                border_style="green",
            ))
        else:
            console.print("[yellow]No similar incident found in memory — investigating from scratch[/yellow]")

        plan_result, session_id = run_phase_sync(
            title="Planning",
            prompt=planning_prompt(packet, memory_match),
            cwd=str(repo_path),
            allowed_tools=["Read", "Glob", "Grep", "Skill", "Agent"],
            permission_mode="plan",
            max_turns=8,
            append_system_prompt="Do not edit files. Produce plan only. Use Agent tool to explore files in parallel when possible.",
        )

        meta = {"worktree_path": None, "sessions": {"plan": session_id}}
        _save_meta(meta_file, meta)
        write_text_file(plan_file, plan_result)
        _append_session_note(session_notes_file, "plan", plan_result)
        console.print(f"[green]✓ Plan written:[/green] {plan_file}")

    elif command == "execute":
        if not plan_file.exists():
            raise FileNotFoundError("Plan file not found. Run plan first.")

        state_dir.mkdir(parents=True, exist_ok=True)
        meta = _load_meta(meta_file) if meta_file.exists() else {}

        worktree_path = create_worktree(str(repo_path), branch_name)
        console.print(f"[green]✓ Worktree created:[/green] {worktree_path}")

        worktree = Path(worktree_path)
        write_text_file(worktree / "debug_packet.json", json.dumps(packet, indent=2, ensure_ascii=False))
        write_text_file(worktree / "plan.md", require_file(plan_file, "Plan"))

        # Resume previous execute session if interrupted
        resume_id = meta.get("sessions", {}).get("execute_resume")

        execution_result, session_id = run_phase_sync(
            title="Execution",
            prompt=execution_prompt(packet),
            cwd=worktree_path,
            allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Skill"],
            permission_mode="acceptEdits",
            max_turns=20,
            append_system_prompt="Implement only after approved plan. Do not scaffold missing source files. If file missing, stop.",
            resume_session_id=resume_id,
        )

        meta["worktree_path"] = worktree_path
        meta.setdefault("sessions", {})["execute"] = session_id
        meta["sessions"].pop("execute_resume", None)
        _save_meta(meta_file, meta)

        write_text_file(execution_file, execution_result)
        _append_session_note(session_notes_file, "execute", execution_result)
        console.print(f"[green]✓ Execution done:[/green] {execution_file}")

    elif command == "resume-execute":
        # Resume an interrupted execute phase
        if not meta_file.exists():
            raise FileNotFoundError("No meta file found. Run execute first.")

        meta = _load_meta(meta_file)
        execute_session = meta.get("sessions", {}).get("execute")
        if not execute_session:
            raise ValueError("No execute session ID found to resume.")

        meta.setdefault("sessions", {})["execute_resume"] = execute_session
        _save_meta(meta_file, meta)
        console.print(f"[cyan]Saved execute session {execute_session} for resume — run execute again.[/cyan]")

    elif command == "review":
        if not meta_file.exists():
            raise FileNotFoundError("Execution metadata not found. Run execute first.")

        meta = _load_meta(meta_file)
        worktree_path = meta["worktree_path"]

        review_result, session_id = run_phase_sync(
            title="Review",
            prompt=review_prompt(packet),
            cwd=worktree_path,
            allowed_tools=["Read", "Glob", "Grep", "Skill"],
            permission_mode="plan",
            max_turns=8,
            append_system_prompt="Review only. Do not edit files.",
        )

        meta.setdefault("sessions", {})["review"] = session_id
        _save_meta(meta_file, meta)

        state_dir.mkdir(parents=True, exist_ok=True)
        write_text_file(review_file, review_result)
        write_text_file(Path(worktree_path) / "pr_draft.md", review_result)
        _append_session_note(session_notes_file, "review", review_result)
        console.print(f"[green]✓ Review written:[/green] {review_file}")

    elif command == "push":
        if not review_file.exists():
            raise FileNotFoundError("Review file not found. Run review first.")

        meta = _load_meta(meta_file)
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
        console.print("Valid commands: plan, execute, review, push, memory, resume-execute")
        sys.exit(1)


if __name__ == "__main__":
    main()
