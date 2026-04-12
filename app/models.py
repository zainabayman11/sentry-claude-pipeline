from typing import Any, Dict, List, Union
from pathlib import Path


def validate_debug_packet(packet: Dict[str, Any]) -> None:
    """Validate a single cluster packet has all required fields."""

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

    summary_required = ["cluster_id", "title", "priority", "projects", "issue_ids"]
    for key in summary_required:
        if key not in packet["summary"]:
            raise ValueError(f"Missing summary key: {key}")

    code_hints_required = ["repo", "files"]
    for key in code_hints_required:
        if key not in packet["code_hints"]:
            raise ValueError(f"Missing code_hints key: {key}")

    if not packet["representative_traces"]:
        raise ValueError("representative_traces must not be empty")

    if not packet["code_hints"]["files"]:
        raise ValueError("code_hints.files must not be empty")


def validate_source_files_exist(packet: Dict[str, Any]) -> None:
    """Verify all source files referenced in code_hints exist on disk."""
    repo_path = Path(packet["code_hints"]["repo"])
    missing = []
    for rel_file in packet["code_hints"]["files"]:
        full_path = repo_path / rel_file
        if not full_path.exists():
            missing.append(str(full_path))
    if missing:
        raise FileNotFoundError("Missing source files:\n" + "\n".join(missing))


def load_packets(raw: Any) -> List[Dict[str, Any]]:
    """
    Accept either a single cluster object or an array of clusters.
    Always returns a list.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    raise ValueError("Packet file must be a JSON object or array of objects.")
