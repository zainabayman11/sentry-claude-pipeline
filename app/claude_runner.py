import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Developer monitoring flag — set PIPELINE_VERBOSE=1 in your shell to see
# the detailed token/cost breakdown panels.  Off by default so recipients
# of this codebase get clean output without the monitoring noise.
# ---------------------------------------------------------------------------
_VERBOSE = os.environ.get("PIPELINE_VERBOSE", "0") == "1"

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.spinner import Spinner
from rich.columns import Columns
from rich import box

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

# Force UTF-8 output on Windows to avoid cp1256 UnicodeEncodeError
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

console = Console()


PHASE_COLORS = {
    "Planning": "cyan",
    "Execution": "yellow",
    "Review": "magenta",
}

TOOL_ICONS = {
    "Read": "[blue]R[/blue]",
    "Edit": "[yellow]E[/yellow]",
    "Write": "[green]W[/green]",
    "Grep": "[cyan]G[/cyan]",
    "Glob": "[cyan]L[/cyan]",
    "Bash": "[red]B[/red]",
    "Skill": "[magenta]S[/magenta]",
    "Agent": "[white]A[/white]",
}


@dataclass
class PhaseStats:
    """Token usage, cost, timing, and tool-call stats for a single agent phase."""
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    model_usage: dict = field(default_factory=dict)
    # Tool call counts by name — used to compare tool usage across prompt variants.
    # Equivalent to the tool_name breakdowns in claude_code.tool_result events.
    tool_counts: dict[str, int] = field(default_factory=dict)
    # Unique file paths seen per tool category (order-preserving, capped at 20).
    # Read  → files_read   (Read, Glob, Grep)
    # Write → files_edited (Edit, Write, MultiEdit)
    files_read: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    # Phase outcome — mirrors ResultMessage.subtype values:
    #   "success"      completed normally
    #   "error_limit"  hit max_turns or max_budget
    #   "error"        SDK-level error
    status: str = "success"
    # Wall-clock seconds from first query() call to ResultMessage received
    duration_sec: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def effective_input_tokens(self) -> int:
        """Total input including cache-related tokens."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @property
    def cache_hit_pct(self) -> float:
        """Percentage of effective input tokens served from cache."""
        if self.effective_input_tokens == 0:
            return 0.0
        return (self.cache_read_tokens / self.effective_input_tokens) * 100


def _make_status_table(
    title: str,
    turn: int,
    tool_log: list[str],
    last_text: str,
    live_input_tokens: int = 0,
    live_output_tokens: int = 0,
) -> Table:
    color = PHASE_COLORS.get(title, "white")
    table = Table(box=box.ROUNDED, border_style=color, show_header=False, expand=True)
    table.add_column("", ratio=1)

    spinner = Spinner("dots", style=f"bold {color}")
    header = Text(f" {title}  |  Turn {turn}", style=f"bold {color}")
    table.add_row(Columns([spinner, header]))

    if tool_log:
        recent = tool_log[-5:]
        tools_text = Text()
        for entry in recent:
            tools_text.append(f"  {entry}\n", style="dim")
        table.add_row(tools_text)

    if last_text:
        snippet = last_text[:300].replace("\n", " ")
        table.add_row(Text(f'  "{snippet}..."', style="italic dim white"))

    # Dev-only: live token counter (hidden unless PIPELINE_VERBOSE=1)
    if _VERBOSE and (live_input_tokens > 0 or live_output_tokens > 0):
        token_text = Text(
            f"  Tokens: {live_input_tokens:,} in / {live_output_tokens:,} out",
            style="dim cyan",
        )
        table.add_row(token_text)

    return table


def _format_done_panel(
    title: str,
    stats: "PhaseStats",
    subtype: str = "success",
) -> Panel:
    """Build the completion panel with full usage breakdown."""
    color = "green" if subtype == "success" else "red"

    if subtype != "success":
        body = Text.assemble(
            ("Phase ended: ", "bold red"),
            (subtype, "red"),
        )
        return Panel(body, border_style="red", title=title)

    lines = Text()

    # Always visible: headline with turns + cost
    lines.append("Done", style="bold green")
    lines.append("  |  Turns: ", style="dim")
    lines.append(str(stats.num_turns), style="cyan")
    lines.append("  |  Cost: ", style="dim")
    lines.append(f"${stats.total_cost_usd:.4f}", style="bold yellow")

    # Dev-only detail below (hidden unless PIPELINE_VERBOSE=1)
    if _VERBOSE:
        # Token counts
        lines.append("\n  Tokens  ", style="dim")
        lines.append(f"{stats.input_tokens:,}", style="white")
        lines.append(" in  /  ", style="dim")
        lines.append(f"{stats.output_tokens:,}", style="white")
        lines.append(" out", style="dim")

        # Cache stats
        if stats.cache_read_tokens > 0 or stats.cache_creation_tokens > 0:
            lines.append("\n  Cache   ", style="dim")
            lines.append(f"{stats.cache_read_tokens:,}", style="green")
            lines.append(" read  /  ", style="dim")
            lines.append(f"{stats.cache_creation_tokens:,}", style="yellow")
            lines.append(" created", style="dim")
            if stats.cache_hit_pct > 0:
                lines.append(f"  ({stats.cache_hit_pct:.0f}% hit rate)", style="bold green")

        # Per-model breakdown (only when multiple models used)
        if len(stats.model_usage) > 1:
            lines.append("\n  ── Models ──", style="dim")
            for model_name, mu in stats.model_usage.items():
                cost = mu.get("cost_usd", 0.0) if isinstance(mu, dict) else getattr(mu, "cost_usd", 0.0)
                inp = mu.get("input_tokens", 0) if isinstance(mu, dict) else getattr(mu, "input_tokens", 0)
                out = mu.get("output_tokens", 0) if isinstance(mu, dict) else getattr(mu, "output_tokens", 0)
                lines.append(f"\n    {model_name}", style="dim white")
                lines.append(f"  ${cost:.4f}", style="yellow")
                lines.append(f"  {inp:,} in / {out:,} out", style="dim")

    return Panel(lines, border_style=color, title=f"[bold]{title} complete")


async def run_agent_phase(
    *,
    title: str,
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    permission_mode: str,
    max_turns: int = 20,
    max_budget_usd: float = 0.5,
    append_system_prompt: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> tuple[str, Optional[str], PhaseStats]:
    """
    Returns (result_text, session_id, stats).
    session_id can be passed back as resume_session_id to continue if interrupted.
    stats contains token usage, cost, and cache breakdown for the phase.
    """
    color = PHASE_COLORS.get(title, "white")
    console.rule(f"[bold {color}]{title}")

    if resume_session_id:
        console.print(f"[dim]Resuming session: {resume_session_id}[/dim]")

    system_prompt = {"type": "preset", "preset": "claude_code"}
    if append_system_prompt:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": append_system_prompt,
        }

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        system_prompt=system_prompt,
        setting_sources=["project"],
        resume=resume_session_id,
    )

    final_result = ""
    last_assistant_text = ""
    session_id: Optional[str] = None
    turn = 0
    tool_log: list[str] = []
    _start = time.monotonic()

    # Per-step token tracking — deduplicate by message_id (parallel tool calls share the same ID)
    seen_ids: set[str] = set()
    live_input_tokens = 0
    live_output_tokens = 0
    live_tool_counts: dict[str, int] = {}  # counts tool calls by name for telemetry
    _seen_read: dict[str, None] = {}     # ordered-set of files passed to read-type tools
    _seen_edited: dict[str, None] = {}   # ordered-set of files passed to write-type tools
    _READ_TOOLS  = {"Read", "Glob", "Grep"}
    _WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}

    stats = PhaseStats()

    with Live(console=console, refresh_per_second=8, vertical_overflow="visible") as live:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                # Track per-step token usage, deduplicated by message_id
                msg_id = getattr(message, "message_id", None)
                usage = getattr(message, "usage", None) or {}
                if msg_id and msg_id not in seen_ids:
                    seen_ids.add(msg_id)
                    if isinstance(usage, dict):
                        live_input_tokens += usage.get("input_tokens", 0)
                        live_output_tokens += usage.get("output_tokens", 0)
                    else:
                        live_input_tokens += getattr(usage, "input_tokens", 0)
                        live_output_tokens += getattr(usage, "output_tokens", 0)

                current_text_parts = []

                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text.strip()
                        if text:
                            current_text_parts.append(text)
                            last_assistant_text = text

                    elif isinstance(block, ToolUseBlock):
                        turn += 1
                        icon = TOOL_ICONS.get(block.name, "[white]?[/white]")
                        inp = block.input or {}
                        label = (
                            inp.get("file_path")
                            or inp.get("pattern")
                            or inp.get("command", "")[:40]
                            or inp.get("description", "")[:40]
                            or str(inp)[:40]
                        )
                        tool_log.append(f"{icon} {block.name}: {label}")
                        live_tool_counts[block.name] = live_tool_counts.get(block.name, 0) + 1

                        # Track file paths (capped at 20 unique paths per category)
                        fp = inp.get("file_path") or inp.get("path") or ""
                        if fp:
                            if block.name in _READ_TOOLS and len(_seen_read) < 20:
                                _seen_read[fp] = None
                            elif block.name in _WRITE_TOOLS and len(_seen_edited) < 20:
                                _seen_edited[fp] = None

                    elif isinstance(block, ToolResultBlock):
                        pass

                if current_text_parts:
                    last_assistant_text = "\n\n".join(current_text_parts)

                live.update(_make_status_table(
                    title, turn, tool_log, last_assistant_text,
                    live_input_tokens, live_output_tokens,
                ))

            elif isinstance(message, ResultMessage):
                live.stop()
                session_id = getattr(message, "session_id", None)

                # Build stats from the authoritative ResultMessage fields
                result_usage = getattr(message, "usage", None) or {}
                model_usage = getattr(message, "model_usage", None) or {}

                if isinstance(result_usage, dict):
                    r_input = result_usage.get("input_tokens", live_input_tokens)
                    r_output = result_usage.get("output_tokens", live_output_tokens)
                    r_cache_read = result_usage.get("cache_read_input_tokens", 0)
                    r_cache_create = result_usage.get("cache_creation_input_tokens", 0)
                else:
                    r_input = getattr(result_usage, "input_tokens", live_input_tokens)
                    r_output = getattr(result_usage, "output_tokens", live_output_tokens)
                    r_cache_read = getattr(result_usage, "cache_read_input_tokens", 0)
                    r_cache_create = getattr(result_usage, "cache_creation_input_tokens", 0)

                stats = PhaseStats(
                    total_cost_usd=message.total_cost_usd or 0.0,
                    input_tokens=r_input,
                    output_tokens=r_output,
                    cache_read_tokens=r_cache_read,
                    cache_creation_tokens=r_cache_create,
                    num_turns=message.num_turns or turn,
                    model_usage=model_usage if isinstance(model_usage, dict) else {},
                    tool_counts=live_tool_counts,
                    files_read=list(_seen_read),
                    files_edited=list(_seen_edited),
                    status=message.subtype or "success",
                    duration_sec=round(time.monotonic() - _start, 2),
                )

                console.print(_format_done_panel(title, stats, message.subtype))

                if message.subtype == "success":
                    final_result = (message.result or "").strip()

    if not final_result and last_assistant_text:
        final_result = last_assistant_text

    return final_result, session_id, stats


def run_phase_sync(
    resume_session_id: Optional[str] = None,
    max_budget_usd: float = 0.5,
    **kwargs,
) -> tuple[str, Optional[str], PhaseStats]:
    return asyncio.run(run_agent_phase(
        resume_session_id=resume_session_id,
        max_budget_usd=max_budget_usd,
        **kwargs,
    ))
