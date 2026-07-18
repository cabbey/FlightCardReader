"""Per-card JSONL audit history service.

Writes a JSONL (JSON Lines) log file next to each flight card image, recording
every action taken on that card. Each line is a JSON object with structured
fields for: when, who, what, and how.

The JSONL file lives in the same directory as the card image with the same
stem name and a `.jsonl` extension (e.g. `abc123.jpg` -> `abc123.jsonl`).

For display, the JSONL entries are rendered into HTML divs with spans for
each value.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standardized action types (the "what" values)
# ---------------------------------------------------------------------------

ACTION_CAPTURED = "captured card"
ACTION_EXTRACTED = "extracted data"
ACTION_EDITED = "edited values"
ACTION_VERIFIED = "verified data"
ACTION_REQUEUED = "requeued extraction"
ACTION_DELETED = "deleted record"


# ---------------------------------------------------------------------------
# JSONL storage
# ---------------------------------------------------------------------------


def _history_path_for_image(image_path: str, store_path: Path) -> Path:
    """Compute the .jsonl history file path for a given image filename.

    Args:
        image_path: Relative image filename (e.g. "abc123-uuid.jpg").
        store_path: The image store directory path.

    Returns:
        Full path to the corresponding .jsonl history file.
    """
    stem = Path(image_path).stem
    return store_path / f"{stem}.jsonl"


def append_history(
    image_path: str,
    store_path: Path,
    who: str,
    what: str,
    how: str = "",
) -> None:
    """Append a history entry to the card's JSONL log file.

    This is the main public interface. It appends a JSON line to the
    JSONL file next to the card image. The operation is fire-and-forget:
    failures are logged but never raised.

    Args:
        image_path: Relative image filename (e.g. "abc123-uuid.jpg").
        store_path: The image store directory path.
        who: Actor identifier (user email, LLM model info, etc.).
        what: Standardized action type (use constants from this module).
        how: Details of how the action was performed. Use <br> to separate
             multiple detail lines (e.g. multiple field edits).
    """
    try:
        history_path = _history_path_for_image(image_path, store_path)

        entry = {
            "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "who": who,
            "what": what,
            "how": how,
        }

        with history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    except Exception as exc:
        logger.warning(
            "Failed to write card history for %s: %s", image_path, exc
        )


def read_history(image_path: str, store_path: Path) -> list[dict]:
    """Read all history entries for a card from its JSONL file.

    Returns a list of dicts, each with keys: when, who, what, how.
    Returns an empty list if the file does not exist or cannot be read.

    Args:
        image_path: Relative image filename.
        store_path: The image store directory path.

    Returns:
        List of history entry dicts.
    """
    try:
        history_path = _history_path_for_image(image_path, store_path)
        if not history_path.exists():
            return []

        entries: list[dict] = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in %s", history_path)
        return entries
    except OSError as exc:
        logger.warning(
            "Failed to read card history for %s: %s", image_path, exc
        )
        return []


def render_history_html(entries: list[dict]) -> str:
    """Render history entries as HTML for display.

    Each entry becomes a <div class="history-entry"> containing spans for
    when, who, what, and how. The "how" field may contain <br> separators
    which are preserved for multi-line details.

    Args:
        entries: List of history entry dicts from read_history().

    Returns:
        HTML string with all entries rendered as divs.
    """
    if not entries:
        return ""

    parts: list[str] = []
    for entry in entries:
        when = html.escape(entry.get("when", ""))
        who = html.escape(entry.get("who", ""))
        what = html.escape(entry.get("what", ""))
        # For "how", preserve <br> tags but escape everything else
        how_raw = entry.get("how", "")
        how_escaped = "<br>".join(
            html.escape(segment) for segment in how_raw.split("<br>")
        )

        div = (
            f'<div class="history-entry">'
            f'<span class="when">{when}</span> '
            f'<span class="who">{who}</span> '
            f'<span class="what">{what}</span>'
        )
        if how_escaped:
            div += f' <span class="how">{how_escaped}</span>'
        div += '</div>'
        parts.append(div)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Helper formatters for the "how" field
# ---------------------------------------------------------------------------


def format_edit_details(changes: dict[str, dict]) -> str:
    """Format field change details into a human-readable 'how' string.

    Takes a changes dict like:
        {"flier_name": {"old": "Den", "new": "Ben"}, "pad": {"old": 3, "new": 5}}

    Returns:
        A <br>-separated string like:
        "changed flier_name from Den to Ben<br>changed pad from 3 to 5"
    """
    parts: list[str] = []
    for field_name, change in changes.items():
        old_val = change.get("old")
        new_val = change.get("new")
        old_display = str(old_val) if old_val is not None else "(empty)"
        new_display = str(new_val) if new_val is not None else "(empty)"
        parts.append(f"changed {field_name} from {old_display} to {new_display}")
    return "<br>".join(parts)


def format_extraction_details(
    elapsed_seconds: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> str:
    """Format LLM extraction details into a 'how' string.

    Args:
        elapsed_seconds: Time taken for the extraction call.
        input_tokens: Number of input tokens (if available).
        output_tokens: Number of output tokens (if available).

    Returns:
        A formatted string like "completed in 12.3s, 1500 input tokens, 800 output tokens"
    """
    parts: list[str] = [f"completed in {elapsed_seconds:.1f}s"]
    if input_tokens is not None:
        parts.append(f"{input_tokens} input tokens")
    if output_tokens is not None:
        parts.append(f"{output_tokens} output tokens")
    return ", ".join(parts)
