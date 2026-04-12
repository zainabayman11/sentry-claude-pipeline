import asyncio
import sys
from typing import Optional

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


def _make_status_table(title: str, turn: int, tool_log: list[str], last_text: str) -> Table:
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

    return table


async def run_agent_phase(
    *,
    title: str,
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    permission_mode: str,
    max_turns: int = 20,
    append_system_prompt: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Returns (result_text, session_id).
    session_id can be passed back as resume_session_id to continue if interrupted.
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
        max_budget_usd=0.5,
        system_prompt=system_prompt,
        setting_sources=["project"],
        resume=resume_session_id,
    )

    final_result = ""
    last_assistant_text = ""
    session_id: Optional[str] = None
    turn = 0
    tool_log: list[str] = []

    with Live(console=console, refresh_per_second=8, vertical_overflow="visible") as live:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
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

                    elif isinstance(block, ToolResultBlock):
                        pass

                if current_text_parts:
                    last_assistant_text = "\n\n".join(current_text_parts)

                live.update(_make_status_table(title, turn, tool_log, last_assistant_text))

            elif isinstance(message, ResultMessage):
                live.stop()
                session_id = getattr(message, "session_id", None)

                if message.subtype == "success":
                    final_result = (message.result or "").strip()
                    console.print(Panel(
                        f"[bold green]Done[/bold green]  |  "
                        f"Turns: [cyan]{message.num_turns}[/cyan]  |  "
                        f"Cost: [yellow]${message.total_cost_usd:.4f}[/yellow]",
                        border_style="green",
                        title=f"[bold]{title} complete",
                    ))
                else:
                    console.print(Panel(
                        f"[bold red]Phase ended:[/bold red] {message.subtype}",
                        border_style="red",
                        title=title,
                    ))

    if not final_result and last_assistant_text:
        final_result = last_assistant_text

    return final_result, session_id


def run_phase_sync(
    resume_session_id: Optional[str] = None,
    **kwargs,
) -> tuple[str, Optional[str]]:
    return asyncio.run(run_agent_phase(resume_session_id=resume_session_id, **kwargs))
