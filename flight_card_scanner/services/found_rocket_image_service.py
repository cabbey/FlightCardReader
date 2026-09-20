"""Image storage service for Found Rockets images.

Found rockets are reported outside the construct of a launch event, so their
photos are stored in a dedicated image store (see
``ServerConfig.found_rockets_images_path``) rather than a per-event store.

Filenames embed an unguessable, cryptographically random token so the image
URL cannot be guessed before an admin approves it (the ``approved`` flag on the
``FoundRocket`` row additionally gates whether the URL is ever surfaced in the
UI).
"""

import logging
import secrets
import uuid
from pathlib import Path

from ..exceptions import ImageStorageError

logger = logging.getLogger(__name__)


def generate_image_token() -> str:
    """Generate a cryptographically random URL-safe token for image filenames.

    Returns ~128 bits of entropy encoded as 22 URL-safe base64 characters,
    making the resulting filename effectively unguessable.
    """
    return secrets.token_urlsafe(16)


def build_found_rocket_filename(ext: str, token: str) -> str:
    """Build a tokenized found-rocket image filename.

    The filename is ``found-<uuid4>-<token>.<ext>``, combining a random UUID
    (uniqueness) with a random token (unguessability).

    Args:
        ext: File extension (e.g. "jpg", "png"); a leading dot is tolerated.
        token: A random URL-safe token (see ``generate_image_token``).

    Returns:
        The generated filename.
    """
    ext = ext.lstrip(".").lower()
    return f"found-{uuid.uuid4()}-{token}.{ext}"


def save_found_rocket_image(
    file_bytes: bytes, ext: str, store_path: Path, token: str
) -> str:
    """Save a found-rocket image to the found rockets image store.

    The file is written byte-for-byte (no re-encoding) to preserve fidelity —
    including the EXIF metadata used for GPS extraction.

    Args:
        file_bytes: Raw image bytes to store.
        ext: File extension (e.g. "jpg", "png").
        store_path: Path to the found rockets image store directory.
        token: Random token to embed in the filename for unguessability.

    Returns:
        The relative filename within ``store_path``.

    Raises:
        ImageStorageError: If the directory is not writable or the write fails.
    """
    filename = build_found_rocket_filename(ext, token)
    target = store_path / filename

    if not store_path.exists():
        raise ImageStorageError(
            f"Found rockets image store directory does not exist: {store_path}"
        )
    if not store_path.is_dir():
        raise ImageStorageError(
            f"Found rockets image store path is not a directory: {store_path}"
        )

    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise ImageStorageError(
            f"Failed to write found rocket image to {target}: {exc}"
        ) from exc

    return filename


def delete_found_rocket_image(store_path: Path, filename: str) -> None:
    """Delete a found-rocket image file (best-effort, idempotent).

    Args:
        store_path: Path to the found rockets image store directory.
        filename: The filename to remove.
    """
    if not filename:
        return
    try:
        (store_path / filename).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Failed to delete found rocket image %s in %s: %s",
            filename,
            store_path,
            exc,
        )
