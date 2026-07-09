"""Audit logging service (structured JSON Lines to a dedicated file).

Provides a fire-and-forget interface for recording user actions. The audit
log uses Python's logging module with a dedicated "audit" logger and
FileHandler so entries never propagate to the root/app logger or block
the request lifecycle.

Never logs plaintext passwords.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_audit_logger: logging.Logger | None = None


def init_audit_logger(log_path: Path) -> None:
    """Configure the 'audit' logger with a dedicated FileHandler.

    - Logger name: "audit"
    - Handler: FileHandler (append mode)
    - Formatter: raw message only (we format the JSON ourselves)
    - Propagate: False (don't emit to root logger)

    Creates the log file if it does not exist; appends to existing file
    without truncation.

    Args:
        log_path: Path to the audit log file on disk.
    """
    global _audit_logger

    logger = logging.getLogger("audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Ensure parent directory exists so FileHandler can create the file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(str(log_path), mode="a")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    _audit_logger = logger


def log_action(
    actor: str,
    action: str,
    object_type: str,
    object_id: int | str,
    details: dict | None = None,
) -> None:
    """Write a single audit entry as a JSON line.

    This function is fire-and-forget: it catches all exceptions internally
    and logs failures to the application logger. It never raises or blocks
    the calling request.

    Args:
        actor: User email or "anonymous".
        action: One of "created", "updated", "deleted", "extracted",
            "requeued", "login", "logout", "login_failed", "ip_changed".
        object_type: One of "flight_record", "user", "session".
        object_id: The integer record/user ID or string session ID.
        details: Optional dict with action-specific data (e.g. field
            changes). Callers MUST NOT include plaintext passwords.
    """
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "details": details or {},
        }

        if _audit_logger:
            _audit_logger.info(json.dumps(entry, default=str))
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Failed to write audit entry: %s", exc
        )
