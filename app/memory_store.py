import json
from pathlib import Path
from typing import Any, Dict, List, Optional


MEMORY_FILE = Path("memory/incidents.json")

# Weight of each field in fuzzy matching (must sum to 1.0)
_FIELD_WEIGHTS = {
    "error_type": 0.40,
    "exc_module": 0.30,
    "location":   0.20,
    "platform":   0.10,
}

# Minimum score to forward a record to Claude for consideration
# Claude himself decides if the match is truly relevant — this is just a pre-filter
_MATCH_THRESHOLD = 0.40


def load_memory() -> List[Dict[str, Any]]:
    if not MEMORY_FILE.exists():
        return []
    raw = MEMORY_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return json.loads(raw)


def save_memory(records: List[Dict[str, Any]]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def build_signature(packet: Dict[str, Any]) -> str:
    """Exact signature used as record key — release excluded intentionally."""
    cp = packet["common_patterns"]
    return "|".join([
        cp.get("platform", ""),
        cp.get("error_type", ""),
        cp.get("exc_module", ""),
        cp.get("location", ""),
    ])


def _score(packet: Dict[str, Any], record: Dict[str, Any]) -> float:
    """
    Fuzzy similarity score between a packet and a stored record.
    Compares individual fields with weights instead of exact signature match.
    Returns a float in [0.0, 1.0].
    """
    cp = packet["common_patterns"]
    sig_parts = record.get("signature", "").split("|")

    # signature format: platform|error_type|exc_module|location
    if len(sig_parts) != 4:
        return 0.0

    record_fields = {
        "platform":   sig_parts[0],
        "error_type": sig_parts[1],
        "exc_module": sig_parts[2],
        "location":   sig_parts[3],
    }

    score = 0.0
    for field, weight in _FIELD_WEIGHTS.items():
        packet_val = cp.get(field, "").lower().strip()
        record_val = record_fields[field].lower().strip()

        if not packet_val or not record_val:
            continue

        if packet_val == record_val:
            score += weight
        elif packet_val in record_val or record_val in packet_val:
            # partial match — e.g. same module, different subpath
            score += weight * 0.5

    return score


def find_similar_incident(packet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Find the best matching past incident using weighted fuzzy scoring.
    Returns the record with the highest score above MATCH_THRESHOLD, or None.
    """
    records = load_memory()
    if not records:
        return None

    best_record = None
    best_score = 0.0

    for record in records:
        score = _score(packet, record)
        if score > best_score:
            best_score = score
            best_record = record

    if best_score >= _MATCH_THRESHOLD:
        best_record = dict(best_record)
        best_record["_match_score"] = round(best_score, 2)
        return best_record

    return None


def append_memory_record(packet: Dict[str, Any], execution_summary: str) -> None:
    """
    Append a new record to memory after execution is complete.
    Updates existing record if same cluster_id already stored.
    """
    records = load_memory()
    cluster_id = packet["summary"]["cluster_id"]

    new_record = {
        "id": cluster_id,
        "signature": build_signature(packet),
        "title": packet["summary"]["title"],
        "root_cause_hint": packet["diagnostic_context"]["primary_error_message"],
        "accepted_fix_summary": execution_summary,
        "files_touched_hint": packet["code_hints"]["files"],
    }

    # Update in-place if same cluster already exists
    for i, rec in enumerate(records):
        if rec.get("id") == cluster_id:
            records[i] = new_record
            save_memory(records)
            return

    records.append(new_record)
    save_memory(records)
