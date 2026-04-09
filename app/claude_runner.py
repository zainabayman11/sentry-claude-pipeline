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
    Execute a single phase of the pipeline using Claude Agent SDK.
    If ResultMessage.result is empty, fall back to the last assistant message.
    """

    console.rule(f"[bold cyan]{title}")

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
        max_budget_usd=2.0,
        system_prompt=system_prompt,
        setting_sources=["project"],
    )

    final_result = ""
    last_assistant_text = ""

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            current_text_parts = []

            for block in message.content:
                if isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        current_text_parts.append(text)
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
                    console.print("[green]Tool finished[/green]")

            if current_text_parts:
                last_assistant_text = "\n\n".join(current_text_parts)

        elif isinstance(message, ResultMessage):
            if message.subtype == "success":
                final_result = (message.result or "").strip()
                console.print(
                    f"[bold green]Success[/bold green] | turns={message.num_turns} | cost=${message.total_cost_usd}"
                )
            else:
                console.print(
                    f"[bold red]Phase ended with subtype={message.subtype}[/bold red]"
                )

    # Fallback: if ResultMessage.result is empty, use the last assistant text
    if not final_result and last_assistant_text:
        final_result = last_assistant_text

    return final_result


def run_phase_sync(**kwargs) -> str:
    return asyncio.run(run_agent_phase(**kwargs))