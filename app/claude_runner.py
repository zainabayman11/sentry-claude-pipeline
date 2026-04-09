import asyncio
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

console = Console()


async def run_agent_phase(
    *,
    title: str,
    prompt: str,
    cwd: str,
    allowed_tools: list[str],
    permission_mode: str,
    max_turns: int = 20,
    append_system_prompt: Optional[str] = None,
) -> str:
    """
    Generic function to execute a single phase of the pipeline.
    Utilizes query() as each phase operates in an independent session.
    """

    console.rule(f"[bold cyan]{title}")

    # Use 'claude_code' preset and append extra instructions if provided
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
        max_budget_usd=2.0,  # Safety cap for testing
        system_prompt=system_prompt,
        setting_sources=["project"],  # Ensures CLAUDE.md and skills are loaded
    )

    final_result = ""

    async for message in query(prompt=prompt, options=options):
        # Handle Claude responses and tool calls
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        console.print(
                            Panel(
                                text[:4000],
                                title=f"{title} • Claude says",
                                border_style="blue",
                            )
                        )

                elif isinstance(block, ToolUseBlock):
                    console.print(
                        f"[yellow]Tool Call[/yellow] → {block.name} | input={block.input}"
                    )

                elif isinstance(block, ToolResultBlock):
                    console.print("[green]Tool Execution Finished[/green]")

        # Handle final result message
        elif isinstance(message, ResultMessage):
            if message.subtype == "success":
                final_result = message.result or ""
                console.print(
                    f"[bold green]Success[/bold green] | turns={message.num_turns} | cost=${message.total_cost_usd}"
                )
            else:
                console.print(
                    f"[bold red]Phase ended with subtype={message.subtype}[/bold red]"
                )

    return final_result


def run_phase_sync(**kwargs) -> str:
    """
    Synchronous wrapper to call the async phase function from pipeline.py.
    """
    return asyncio.run(run_agent_phase(**kwargs))