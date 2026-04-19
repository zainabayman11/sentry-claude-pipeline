# =============================================================================
# FULL PIPELINE WORKFLOW — Sentry → Claude → GitHub PR
# =============================================================================
#
# Prereqs (one-time):
#   pip install -r requirements.txt
#   claude auth login
#   copy .env.example .env        # edit PIPELINE_USER=yourname
#
# ─── Single cluster ───────────────────────────────────────────────────────────
#
#   STEP 1 — Plan (read-only, no file changes)
#   python -m app.pipeline plan data/sample_debug_packet.json
#     → reads the stack trace, explores the repo, writes a fix plan
#     → output: .pipeline_state/<cluster_id>/plan.md
#
#   STEP 2 — Execute (applies the fix in an isolated git worktree)
#   python -m app.pipeline execute data/sample_debug_packet.json
#     → implements the plan, edits files, writes execution summary
#     → output: .pipeline_state/<cluster_id>/execution_summary.md
#     → worktree:  <repo>/worktrees/fix-<cluster_id>/
#     → if interrupted: run `resume-execute` then `execute` again
#
#   STEP 3 — Review (read-only, checks the diff)
#   python -m app.pipeline review data/sample_debug_packet.json
#     → reviews the git diff, writes a PR description
#     → output: .pipeline_state/<cluster_id>/review.md
#              <worktree>/pr_draft.md
#
#   STEP 4 — Push (commit + push branch + open GitHub PR)
#   python -m app.pipeline push data/sample_debug_packet.json
#     → git commit → git push → gh pr create
#     → saves fix to memory for future similar bugs
#
# ─── Multiple clusters (batch) ────────────────────────────────────────────────
#
#   Run each step across ALL clusters in the file:
#   python -m app.pipeline plan    data/sample_debug_packet.json
#   python -m app.pipeline execute data/sample_debug_packet.json
#   python -m app.pipeline review  data/sample_debug_packet.json
#   python -m app.pipeline push    data/sample_debug_packet.json
#
#   First 2 clusters only (test before full run):
#   python -m app.pipeline plan data/sample_debug_packet.json --limit 2
#
#   Clusters 3–4 (second batch):
#   python -m app.pipeline plan data/sample_debug_packet.json --limit 2 --offset 2
#
#   Run in parallel (faster, higher cost):
#   python -m app.pipeline plan data/sample_debug_packet.json --parallel
#
# ─── Monitoring ───────────────────────────────────────────────────────────────
#
#   Cost + token log (always written, local only):
#   .pipeline_state/cost_log.jsonl
#
#   Rich terminal panels (set in .env):
#   PIPELINE_VERBOSE=1    → per-phase token/cost/cache table after each run
#
# =============================================================================

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load .env before reading any env vars — this is a no-op if .env is missing
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Developer monitoring controls
#
# PIPELINE_VERBOSE=1  → show detailed token/cost panels in the terminal.
#                        Off by default so people you share the code with
#                        get clean output.  This is the ONE place to check
#                        before sharing.
#
# Cost logs are ALWAYS written to .pipeline_state/cost_log.jsonl regardless
# of PIPELINE_VERBOSE.  This gives you a persistent history to analyze how
# prompt changes affect cost over time.  The file is git-ignored so it stays
# local — recipients never see your run data.
# ---------------------------------------------------------------------------
_VERBOSE = os.environ.get("PIPELINE_VERBOSE", "0") == "1"

# Path for the persistent cost log (JSONL — one JSON object per line)
_COST_LOG = Path(".pipeline_state") / "cost_log.jsonl"

# User identity for multi-user JSONL comparison (set PIPELINE_USER=yourname locally)
_USER_ID: str = os.environ.get("PIPELINE_USER", "").strip()

# Optional experiment labels for local before/after comparisons.
# Example:
#   PIPELINE_EXPERIMENT_ID=skill-odoo-fix-v2
#   PIPELINE_CHANGE_ID=cursor-db-timeout-line-42
#   PIPELINE_SKILLS=odoo-fix
#   PIPELINE_PLUGINS=repo-linter
_EXPERIMENT_ID: str = os.environ.get("PIPELINE_EXPERIMENT_ID", "").strip()
_CHANGE_ID: str = os.environ.get("PIPELINE_CHANGE_ID", "").strip()
_PROMPT_VARIANT: str = os.environ.get("PIPELINE_PROMPT_VARIANT", "").strip()
_SKILLS: str = os.environ.get("PIPELINE_SKILLS", "").strip()
_PLUGINS: str = os.environ.get("PIPELINE_PLUGINS", "").strip()


def _prompt_hash(prompt: str) -> str:
    """First 12 hex chars of SHA-256 of the prompt.

    Short enough to read, long enough to detect any change.
    Use this to correlate cost/quality shifts with prompt edits across runs.
    """
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


def _prompt_id(phase_name: str, prompt_hash: str) -> str:
    """Stable readable ID for correlating one phase prompt across runs."""
    return f"{phase_name.lower()}:{prompt_hash}"


def _experiment_tags() -> dict[str, str]:
    """Optional labels written to cost_log.jsonl for before/after analysis."""
    tags = {
        "experiment_id": _EXPERIMENT_ID,
        "change_id": _CHANGE_ID,
        "prompt_variant": _PROMPT_VARIANT,
        "skills": _SKILLS,
        "plugins": _PLUGINS,
    }
    return {k: v for k, v in tags.items() if v}

from rich.console import Console
from rich.panel import Panel

from app.models import validate_debug_packet, validate_source_files_exist, load_packets
from app.memory_store import find_similar_incident, append_memory_record
from app.git_ops import create_worktree, commit_and_push_worktree, create_github_pr, PushResult
from app.prompts import planning_prompt, execution_prompt, review_prompt
from app.claude_runner import run_phase_sync, PhaseStats  # also sets UTF-8 stdout

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


def _get_worktree_diff(worktree_path: str, max_chars: int = 8000) -> str:
    """
    Get the git diff of changes made in the worktree.
    Tries HEAD~1 first, falls back to staged+unstaged diff.
    Truncates to max_chars to keep prompt size controlled.
    """
    import subprocess

    for args in [["git", "diff", "HEAD~1"], ["git", "diff", "HEAD"], ["git", "diff"]]:
        result = subprocess.run(args, cwd=worktree_path, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            diff = result.stdout.strip()
            if len(diff) > max_chars:
                diff = diff[:max_chars] + f"\n... [truncated — {len(diff)} chars total]"
            return diff

    return "No diff available."


def _load_meta(meta_file: Path) -> dict:
    return json.loads(require_file(meta_file, "Metadata"))


def _save_meta(meta_file: Path, data: dict) -> None:
    write_text_file(meta_file, json.dumps(data, indent=2))


def _append_cost_log(
    cluster_id: str,
    command: str,
    # (phase_name, stats, prompt_hash, extra_fields)
    # extra_fields: {} for LLM phases; git/PR dict for Push phase
    phases: list[tuple[str, "PhaseStats", str, dict]],
    branch_name: str = "",
    run_id: str = "",
    success: bool = True,
    error_message: str | None = None,
    memory_hit: bool = False,
    repo_name: str = "",
) -> None:
    """Always-on: append one JSONL entry to .pipeline_state/cost_log.jsonl.

    Written regardless of PIPELINE_VERBOSE. The file is git-ignored and local-only.
    Use run_id + prompt_hash to correlate cost/quality across prompt variants.
    """
    _COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or None,
        "user_id": _USER_ID or None,
        "cluster_id": cluster_id,
        "repo_name": repo_name or None,
        "command": command,
        "runner": "agent_sdk",
        "branch_name": branch_name or None,
        "memory_hit": memory_hit,
        "success": success,
        "error_message": error_message,
        "experiment": _experiment_tags() or None,
        "phases": [
            {
                "phase": name,
                "status": s.status,
                "duration_sec": s.duration_sec,
                "prompt_id": _prompt_id(name, phash) if phash else None,
                "prompt_hash": phash or None,
                "cost_usd": s.total_cost_usd,
                "num_turns": s.num_turns,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_creation_tokens": s.cache_creation_tokens,
                "tool_counts": s.tool_counts,
                "files_read": s.files_read or [],
                "files_edited": s.files_edited or [],
                **extra,
            }
            for name, s, phash, extra in phases
        ],
        "total_cost_usd": sum(s.total_cost_usd for _, s, _, _ in phases),
    }
    with _COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _print_cost_summary(phases: list[tuple[str, "PhaseStats", str, dict]]) -> None:
    """Print a cost and token summary across one or more phases."""
    if not phases:
        return

    total_cost = sum(s.total_cost_usd for _, s, _, _ in phases)
    total_input = sum(s.input_tokens for _, s, _, _ in phases)
    total_output = sum(s.output_tokens for _, s, _, _ in phases)
    total_cache_read = sum(s.cache_read_tokens for _, s, _, _ in phases)
    total_cache_create = sum(s.cache_creation_tokens for _, s, _, _ in phases)

    lines = []
    for phase_name, s, phash, _extra in phases:
        cache_note = ""
        if s.cache_read_tokens > 0:
            cache_note = f"  cache {s.cache_read_tokens:,} read"
        hash_note = f"  [dim]prompt:{phash}[/dim]" if phash else ""
        lines.append(
            f"  [dim]{phase_name:<12}[/dim]  "
            f"[yellow]${s.total_cost_usd:.4f}[/yellow]  "
            f"[white]{s.input_tokens:,}[/white][dim] in /[/dim] "
            f"[white]{s.output_tokens:,}[/white][dim] out[/dim]"
            f"[green]{cache_note}[/green]"
            f"{hash_note}"
        )

    if len(phases) > 1:
        effective_input = total_input + total_cache_read + total_cache_create
        cache_pct = (total_cache_read / effective_input * 100) if effective_input else 0
        cache_line = ""
        if total_cache_read > 0:
            cache_line = f"  [green]cache {total_cache_read:,} read ({cache_pct:.0f}% hit)[/green]"
        lines.append(
            f"  [bold]{'TOTAL':<12}[/bold]  "
            f"[bold yellow]${total_cost:.4f}[/bold yellow]  "
            f"[white]{total_input:,}[/white][dim] in /[/dim] "
            f"[white]{total_output:,}[/white][dim] out[/dim]"
            f"{cache_line}"
        )

    console.print(Panel(
        "\n".join(lines),
        title="[bold]Cost & Token Usage",
        border_style="dim",
        padding=(0, 1),
    ))


def run_cluster(command: str, packet: dict) -> float:
    """
    Run a single pipeline command against one cluster packet.
    Returns the total cost in USD for the phases executed.
    """
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

    # Each entry: (phase_name, PhaseStats, prompt_hash, extra_fields)
    # extra_fields is {} for LLM phases and a git/PR dict for the Push phase
    phase_stats: list[tuple[str, PhaseStats, str, dict]] = []
    _cmd_start = time.monotonic()

    # Run-level tracking — written to JSONL via _append_cost_log in the finally block
    _run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    _success = True
    _error_msg: str | None = None
    _memory_hit = False

    try:
        if command == "plan":
            state_dir.mkdir(parents=True, exist_ok=True)

            memory_match = find_similar_incident(packet)
            _memory_hit = memory_match is not None
            if memory_match:
                console.print(Panel(
                    json.dumps(memory_match, indent=2, ensure_ascii=False),
                    title="[bold green]Memory Match Found — reusing past fix",
                    border_style="green",
                ))
            else:
                console.print("[yellow]No similar incident found in memory — investigating from scratch[/yellow]")

            plan_prompt = planning_prompt(packet, memory_match)
            plan_result, session_id, stats = run_phase_sync(
                title="Planning",
                prompt=plan_prompt,
                cwd=str(repo_path),
                allowed_tools=["Read", "Glob", "Grep", "Skill", "Agent"],
                permission_mode="plan",
                max_turns=8,
                max_budget_usd=0.20,
                append_system_prompt="Do not edit files. Produce plan only. Use Agent tool to explore files in parallel when possible.",
            )
            phase_stats.append(("Planning", stats, _prompt_hash(plan_prompt), {}))

            meta = {"worktree_path": None, "sessions": {"plan": session_id}}
            _save_meta(meta_file, meta)
            write_text_file(plan_file, plan_result)
            _append_session_note(session_notes_file, "plan", plan_result)
            console.print(f"[green]✓ Plan written:[/green] {plan_file}")

        elif command == "execute":
            if not plan_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] Plan file not found. Run plan first.")

            state_dir.mkdir(parents=True, exist_ok=True)
            meta = _load_meta(meta_file) if meta_file.exists() else {}

            worktree_path = create_worktree(str(repo_path), branch_name)
            console.print(f"[green]✓ Worktree created:[/green] {worktree_path}")

            worktree = Path(worktree_path)
            write_text_file(worktree / "debug_packet.json", json.dumps(packet, indent=2, ensure_ascii=False))
            write_text_file(worktree / "plan.md", require_file(plan_file, "Plan"))

            resume_id = meta.get("sessions", {}).get("execute_resume")
            exec_prompt = execution_prompt(packet)
            execution_result, session_id, stats = run_phase_sync(
                title="Execution",
                prompt=exec_prompt,
                cwd=worktree_path,
                allowed_tools=["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Skill"],
                permission_mode="acceptEdits",
                max_turns=20,
                max_budget_usd=0.40,
                append_system_prompt="Implement only after approved plan. Do not scaffold missing source files. If file missing, stop.",
                resume_session_id=resume_id,
            )
            phase_stats.append(("Execution", stats, _prompt_hash(exec_prompt), {}))

            meta["worktree_path"] = worktree_path
            meta.setdefault("sessions", {})["execute"] = session_id
            meta["sessions"].pop("execute_resume", None)
            _save_meta(meta_file, meta)

            write_text_file(execution_file, execution_result)
            _append_session_note(session_notes_file, "execute", execution_result)
            console.print(f"[green]✓ Execution done:[/green] {execution_file}")

        elif command == "resume-execute":
            if not meta_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] No meta file found. Run execute first.")

            meta = _load_meta(meta_file)
            execute_session = meta.get("sessions", {}).get("execute")
            if not execute_session:
                raise ValueError(f"[{cluster_id}] No execute session ID found to resume.")

            meta.setdefault("sessions", {})["execute_resume"] = execute_session
            _save_meta(meta_file, meta)
            console.print(f"[cyan]Saved execute session {execute_session} for resume — run execute again.[/cyan]")

        elif command == "review":
            if not meta_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] Execution metadata not found. Run execute first.")

            meta = _load_meta(meta_file)
            worktree_path = meta["worktree_path"]

            # Get the actual diff so Claude doesn't need to explore files himself
            diff_result = _get_worktree_diff(worktree_path)
            rev_prompt = review_prompt(packet, diff_result)
            review_result, session_id, stats = run_phase_sync(
                title="Review",
                prompt=rev_prompt,
                cwd=worktree_path,
                allowed_tools=["Skill"],
                permission_mode="plan",
                max_turns=3,
                max_budget_usd=0.08,
                append_system_prompt="Review only. Do not edit files. The diff is already provided in the prompt — do not use any tools.",
            )
            phase_stats.append(("Review", stats, _prompt_hash(rev_prompt), {}))

            meta.setdefault("sessions", {})["review"] = session_id
            _save_meta(meta_file, meta)

            state_dir.mkdir(parents=True, exist_ok=True)
            write_text_file(review_file, review_result)
            write_text_file(Path(worktree_path) / "pr_draft.md", review_result)
            _append_session_note(session_notes_file, "review", review_result)
            console.print(f"[green]✓ Review written:[/green] {review_file}")

        elif command == "push":
            if not review_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] Review file not found. Run review first.")

            meta = _load_meta(meta_file)
            worktree_path = meta["worktree_path"]

            pr_title = f"fix: {packet['summary']['title']} ({cluster_id})"
            pr_body_path = Path(worktree_path) / "pr_draft.md"

            if not pr_body_path.exists():
                raise FileNotFoundError(f"PR draft file not found: {pr_body_path}")

            # Accumulate git/PR metadata for the JSONL Push phase entry
            _push_meta: dict = {
                "git_commit_created": False,
                "git_push_done": False,
                "pr_created": False,
                "pr_url": None,
                "commit_sha": None,
                "changed_files_count": 0,
                "branch_pushed": branch_name,
            }

            try:
                push_result = commit_and_push_worktree(
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                    commit_message=pr_title,
                )
                _push_meta["git_commit_created"] = push_result.commit_created
                _push_meta["git_push_done"] = push_result.push_done
                _push_meta["commit_sha"] = push_result.commit_sha or None
                _push_meta["changed_files_count"] = push_result.changed_files_count
            except Exception as e:
                console.print(f"[yellow]Warning during push:[/yellow] {e}")
                console.print("[yellow]Continuing to PR creation attempt...[/yellow]")

            pr_url = create_github_pr(
                worktree_path=worktree_path,
                title=pr_title,
                body_file=str(pr_body_path),
            )
            _push_meta["pr_created"] = True
            _push_meta["pr_url"] = pr_url or None

            append_memory_record(packet, require_file(execution_file, "Execution"))
            console.print(f"[green]✓ PR created and memory updated:[/green] {cluster_id}")
            if pr_url:
                console.print(f"[green]✓ PR URL:[/green] {pr_url}")

            phase_stats.append((
                "Push",
                PhaseStats(status="success", duration_sec=round(time.monotonic() - _cmd_start, 2)),
                "",
                _push_meta,
            ))

        elif command == "memory":
            if not execution_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] Execution summary not found. Run execute first.")
            if not review_file.exists():
                raise FileNotFoundError(f"[{cluster_id}] Review file not found. Run review first.")

            execution_summary = require_file(execution_file, "Execution")
            append_memory_record(packet, execution_summary)
            console.print(f"[green]✓ Memory updated:[/green] {cluster_id}")

        else:
            console.print(f"[red]Unknown command:[/red] {command}")
            console.print("Valid commands: plan, execute, review, push, memory, resume-execute")
            sys.exit(1)

    except Exception as e:
        _success = False
        _error_msg = str(e)
        raise

    finally:
        # Always write to JSONL — even on failure, to capture what ran before the error
        _append_cost_log(
            cluster_id=cluster_id,
            command=command,
            phases=phase_stats,
            branch_name=branch_name,
            run_id=_run_id,
            success=_success,
            error_message=_error_msg,
            memory_hit=_memory_hit,
            repo_name=repo_path.name,
        )
        if _VERBOSE and phase_stats:
            _print_cost_summary(phase_stats)

    return sum(s.total_cost_usd for _, s, _, _ in phase_stats)


def run_cluster_full(packet: dict) -> float:
    """Run the complete pipeline (plan → execute → review → push) for one cluster.

    Stops at the first failed step and reports which step failed.
    Use this when you want a fully automated end-to-end run without manual checkpoints.

    Usage:
        python -m app.pipeline full data/sample_debug_packet.json
        python -m app.pipeline full data/sample_debug_packet.json --limit 1
    """
    cluster_id = packet["summary"]["cluster_id"]
    total_cost = 0.0

    for step in ("plan", "execute", "review", "push"):
        console.rule(f"[bold cyan]{cluster_id} — {step.upper()}")
        try:
            total_cost += run_cluster(step, packet)
        except Exception as e:
            console.print(Panel(
                str(e),
                title=f"[bold red]Full pipeline stopped at '{step}' for {cluster_id}",
                border_style="red",
            ))
            raise RuntimeError(f"full pipeline failed at '{step}': {e}") from e

    console.print(f"[green]✓ Full pipeline done:[/green] {cluster_id}  [bold yellow]${total_cost:.4f}[/bold yellow]")
    return total_cost


def _run_cluster_safe(command: str, packet: dict) -> tuple[str, float, Exception | None]:
    """Wrapper for parallel execution — returns (cluster_id, cost_usd, error or None)."""
    cluster_id = packet["summary"]["cluster_id"]
    try:
        if command == "full":
            cost = run_cluster_full(packet)
        else:
            cost = run_cluster(command, packet)
        return cluster_id, cost, None
    except Exception as e:
        return cluster_id, 0.0, e


def main():
    if len(sys.argv) < 3:
        console.print("[red]Usage:[/red] python -m app.pipeline <command> data/packet.json [options]")
        console.print("")
        console.print("[bold]Commands:[/bold]")
        console.print("  plan            Analyze bug and produce fix plan (read-only)")
        console.print("  execute         Implement the fix in an isolated worktree")
        console.print("  review          Review the changes (read-only)")
        console.print("  push            Commit, push branch, open GitHub PR")
        console.print("  full            Run plan → execute → review → push in one shot")
        console.print("  memory          Save fix to memory without pushing")
        console.print("  resume-execute  Resume an interrupted execute phase")
        console.print("")
        console.print("[bold]Options:[/bold]")
        console.print("  --limit N     Process only N clusters (default: all)")
        console.print("  --offset N    Skip first N clusters (default: 0)")
        console.print("  --parallel    Run clusters in parallel (default: sequential)")
        console.print("")
        console.print("[bold]Examples:[/bold]")
        console.print("  # Run plan on all clusters")
        console.print("  python -m app.pipeline plan data/bugs.json")
        console.print("")
        console.print("  # Run plan on first 5 clusters only")
        console.print("  python -m app.pipeline plan data/bugs.json --limit 5")
        console.print("")
        console.print("  # Run plan on clusters 6-10 (second batch)")
        console.print("  python -m app.pipeline plan data/bugs.json --limit 5 --offset 5")
        console.print("")
        console.print("  # Run plan on a single cluster (index 0)")
        console.print("  python -m app.pipeline plan data/bugs.json --limit 1 --offset 0")
        console.print("")
        console.print("  # Run plan on first 3 clusters in parallel")
        console.print("  python -m app.pipeline plan data/bugs.json --limit 3 --parallel")
        sys.exit(1)

    command = sys.argv[1]
    packet_path = Path(sys.argv[2])

    # Parse options
    args = sys.argv[3:]
    limit = None
    offset = 0
    parallel = "--parallel" in args

    try:
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        if "--offset" in args:
            offset = int(args[args.index("--offset") + 1])
    except (ValueError, IndexError):
        console.print("[red]Invalid --limit or --offset value[/red]")
        sys.exit(1)

    if not packet_path.exists():
        console.print(f"[red]Packet file not found:[/red] {packet_path}")
        sys.exit(1)

    console.rule("[bold magenta]Claude Bug Pipeline")

    raw = json.loads(read_text_file(packet_path))
    packets = load_packets(raw)

    # Apply offset and limit
    packets = packets[offset:]
    if limit is not None:
        packets = packets[:limit]

    # Validate all selected packets upfront before starting anything
    for packet in packets:
        validate_debug_packet(packet)

    total = len(packets)
    failed = []

    mode_label = "[yellow]parallel[/yellow]" if parallel else "[cyan]sequential[/cyan]"
    console.print(f"Running [bold]{command}[/bold] on [bold]{total}[/bold] cluster(s) — {mode_label}")

    total_cost = 0.0

    if parallel and total > 1:
        # Parallel: all clusters run at the same time via threads
        # Warning: may hit rate limits for > 5 clusters on Claude Max
        with ThreadPoolExecutor(max_workers=total) as executor:
            futures = {
                executor.submit(_run_cluster_safe, command, packet): packet
                for packet in packets
            }
            for future in as_completed(futures):
                cluster_id, cost, error = future.result()
                if error:
                    console.print(Panel(
                        str(error),
                        title=f"[bold red]Failed: {cluster_id}",
                        border_style="red",
                    ))
                    failed.append(cluster_id)
                else:
                    total_cost += cost
                    console.print(f"[green]✓ Done:[/green] {cluster_id}  [yellow]${cost:.4f}[/yellow]")
    else:
        # Sequential: one cluster at a time
        for i, packet in enumerate(packets, 1):
            cluster_id = packet["summary"]["cluster_id"]
            console.rule(f"[bold cyan]Cluster {i}/{total} — {cluster_id}")
            try:
                if command == "full":
                    cost = run_cluster_full(packet)
                else:
                    cost = run_cluster(command, packet)
                total_cost += cost
            except Exception as e:
                console.print(Panel(
                    str(e),
                    title=f"[bold red]Failed: {cluster_id}",
                    border_style="red",
                ))
                failed.append(cluster_id)
                continue

    # Summary (only when more than 1 cluster)
    if total > 1:
        console.rule("[bold magenta]Done")
        done = total - len(failed)
        console.print(f"[green]✓ {done}/{total} clusters succeeded[/green]  [bold yellow]total cost: ${total_cost:.4f}[/bold yellow]")
        if failed:
            console.print(f"[red]✗ Failed: {', '.join(failed)}[/red]")


if __name__ == "__main__":
    main()
