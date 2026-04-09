from typing import Any, Dict
from pathlib import Path

def validate_debug_packet(packet: Dict[str, Any]) -> None:
    """
    Validation function to ensure the debug packet contains the minimum required data.
    If any key is missing, raises a ValueError to prevent the agent from proceeding 
    with incomplete context.
    """

    # Top-level structure verification
    required_top_level = [
        "summary",
        "common_patterns",
        "diagnostic_context",
        "representative_traces",
        "code_hints",
    ]

    for key in required_top_level:
        if key not in packet:
            raise ValueError(f"Missing required top-level key: {key}")

    # Summary internal structure verification
    summary_required = ["cluster_id", "title", "priority", "projects", "issue_ids"]
    for key in summary_required:
        if key not in packet["summary"]:
            raise ValueError(f"Missing summary key: {key}")

    # Code hints structure verification
    code_hints_required = ["repo", "files"]
    for key in code_hints_required:
        if key not in packet["code_hints"]:
            raise ValueError(f"Missing code_hints key: {key}")

    # Data availability check
    if not packet["representative_traces"]:
        raise ValueError("representative_traces must not be empty")

    if not packet["code_hints"]["files"]:
        raise ValueError("code_hints.files must not be empty")


def validate_source_files_exist(packet: Dict[str, Any]) -> None:
    """
    Verify that all source files referenced in code_hints exist on disk.
    Raises FileNotFoundError with a list of missing files.
    """
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