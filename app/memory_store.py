import json
from pathlib import Path
from typing import Any, Dict, List, Optional


MEMORY_FILE = Path("memory/incidents.json")


def load_memory() -> List[Dict[str, Any]]:
    """
    Loads incident memory from a JSON file.
    Returns an empty list if the file is missing or empty.
    """
    if not MEMORY_FILE.exists():
        return []

    raw = MEMORY_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    return json.loads(raw)


def save_memory(records: List[Dict[str, Any]]) -> None:
    """
    Persists the entire memory state to a JSON file after updates.
    """
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def build_signature(packet: Dict[str, Any]) -> str:
    """
    Builds a unique signature to identify similar incidents.
    Initial rule-based implementation using common patterns.
    """
    cp = packet["common_patterns"]

    return "|".join(
        [
            cp.get("platform", ""),
            cp.get("error_type", ""),
            cp.get("exc_module", ""),
            cp.get("location", ""),
            cp.get("release", ""),
        ]
    )


def find_similar_incident(packet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Searches for historical incidents that share the same signature.
    """
    signature = build_signature(packet)
    records = load_memory()

    for record in records:
        if record.get("signature") == signature:
            return record

    return None


def append_memory_record(packet: Dict[str, Any], execution_summary: str) -> None:
    """
    Appends a new record to memory after execution is complete.
    Stores key metadata for future deduplication and learning.
    """
    records = load_memory()

    new_record = {
        "id": packet["summary"]["cluster_id"],
        "signature": build_signature(packet),
        "title": packet["summary"]["title"],
        "root_cause_hint": packet["diagnostic_context"]["primary_error_message"],
        "accepted_fix_summary": execution_summary,
        "files_touched_hint": packet["code_hints"]["files"],
    }

    records.append(new_record)
    save_memory(records)