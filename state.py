import json
from pathlib import Path

_STATE_FILE = Path(__file__).parent / "processed_pos.json"


def load_processed_ids() -> set[str]:
    if not _STATE_FILE.exists():
        return set()
    return set(json.loads(_STATE_FILE.read_text()))


def mark_processed(po_id: str) -> None:
    ids = load_processed_ids()
    ids.add(po_id)
    _STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))
