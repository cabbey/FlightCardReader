"""Image storage service (save/retrieve card images).

Provides functions to save uploaded image bytes to the Image Store using
UUID4-based filenames, and to delete images for rollback on DB failure.
"""

import logging
import secrets
import uuid
from pathlib import Path

from ..exceptions import ImageStorageError

logger = logging.getLogger(__name__)


def save_image(file_bytes: bytes, ext: str, store_path: Path) -> str:
    """Save image bytes to the Image Store with a UUID4-based filename.

    The file is written byte-for-byte (no re-encoding or resizing) to preserve
    lossless fidelity with the submitted content.

    Args:
        file_bytes: Raw image bytes to store.
        ext: File extension (e.g. "jpg", "png"). Leading dot is handled.
        store_path: Path to the image store directory.

    Returns:
        The relative filename (e.g. "a1b2c3d4-...uuid....jpg") within store_path.

    Raises:
        ImageStorageError: If the directory is not writable or the write fails.
    """
    # Normalise extension: ensure it has no leading dot for consistent handling
    ext = ext.lstrip(".")
    filename = f"{uuid.uuid4()}.{ext}"
    target = store_path / filename

    # Check directory is writable before attempting write
    if not store_path.exists():
        raise ImageStorageError(
            f"Image store directory does not exist: {store_path}"
        )
    if not store_path.is_dir():
        raise ImageStorageError(
            f"Image store path is not a directory: {store_path}"
        )

    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise ImageStorageError(
            f"Failed to write image to {target}: {exc}"
        ) from exc

    return filename


def _derive_filename(front_filename: str, suffix: str) -> str:
    """Derive a variant filename by inserting a suffix before the extension.

    Args:
        front_filename: The original front image filename (e.g. "a1b2c3d4.jpg").
        suffix: The suffix to insert (e.g. "-back", "-preflight").

    Returns:
        The derived filename (e.g. "a1b2c3d4-back.jpg").
    """
    stem, dot, ext = front_filename.rpartition(".")
    if not dot:
        # No extension found; just append the suffix
        return f"{front_filename}{suffix}"
    return f"{stem}{suffix}.{ext}"


def get_back_image_path(front_filename: str) -> str:
    """Return the expected back image filename for a given front filename.

    Args:
        front_filename: The front image filename (e.g. "a1b2c3d4.jpg").

    Returns:
        The back image filename (e.g. "a1b2c3d4-back.jpg").
    """
    return _derive_filename(front_filename, "-back")


def get_preflight_image_path(front_filename: str) -> str:
    """Return the expected preflight image filename for a given front filename.

    Args:
        front_filename: The front image filename (e.g. "a1b2c3d4.jpg").

    Returns:
        The preflight image filename (e.g. "a1b2c3d4-preflight.jpg").
    """
    return _derive_filename(front_filename, "-preflight")


def save_back_image(front_filename: str, file_bytes: bytes, store_path: Path) -> str:
    """Save a back-of-card image, deriving filename from the front image.

    The back image filename is the front filename with '-back' inserted before
    the extension (e.g. "a1b2c3d4.jpg" -> "a1b2c3d4-back.jpg").

    Args:
        front_filename: The front image filename to derive the back name from.
        file_bytes: Raw image bytes to store.
        store_path: Path to the image store directory.

    Returns:
        The back image filename.

    Raises:
        ImageStorageError: If the directory is not writable or the write fails.
    """
    filename = get_back_image_path(front_filename)
    target = store_path / filename

    if not store_path.exists():
        raise ImageStorageError(
            f"Image store directory does not exist: {store_path}"
        )
    if not store_path.is_dir():
        raise ImageStorageError(
            f"Image store path is not a directory: {store_path}"
        )

    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise ImageStorageError(
            f"Failed to write image to {target}: {exc}"
        ) from exc

    return filename


def save_preflight_image(
    front_filename: str, file_bytes: bytes, store_path: Path
) -> str:
    """Save a preflight image, deriving filename from the front image.

    The preflight image filename is the front filename with '-preflight'
    inserted before the extension (e.g. "a1b2c3d4.jpg" -> "a1b2c3d4-preflight.jpg").

    Args:
        front_filename: The front image filename to derive the preflight name from.
        file_bytes: Raw image bytes to store.
        store_path: Path to the image store directory.

    Returns:
        The preflight image filename.

    Raises:
        ImageStorageError: If the directory is not writable or the write fails.
    """
    filename = get_preflight_image_path(front_filename)
    target = store_path / filename

    if not store_path.exists():
        raise ImageStorageError(
            f"Image store directory does not exist: {store_path}"
        )
    if not store_path.is_dir():
        raise ImageStorageError(
            f"Image store path is not a directory: {store_path}"
        )

    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise ImageStorageError(
            f"Failed to write image to {target}: {exc}"
        ) from exc

    return filename


def delete_image(path: Path) -> None:
    """Delete an image file (used for rollback on DB failure).

    This operation is idempotent: if the file does not exist, it is a no-op.
    Other errors are logged as warnings but not raised, since this is a
    best-effort cleanup path.

    Args:
        path: Full path to the image file to delete.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to delete image at %s during rollback: %s", path, exc)


def generate_image_token() -> str:
    """Generate a cryptographically random URL-safe token for image filenames.

    The token is 16 bytes of randomness encoded as 22 URL-safe base64
    characters (no padding), providing ~128 bits of entropy — making the
    resulting filename effectively unguessable.

    Returns:
        A 22-character URL-safe random string.
    """
    return secrets.token_urlsafe(16)


def get_preflight_image_path(front_filename: str) -> str:
    """Derive the preflight image filename from the front image filename.

    Inserts '-preflight' before the file extension.

    Args:
        front_filename: The front image filename (e.g. "a1b2c3d4-uuid.jpg").

    Returns:
        The preflight image filename (e.g. "a1b2c3d4-uuid-preflight.jpg").
    """
    stem, dot, ext = front_filename.rpartition(".")
    if not dot:
        return front_filename + "-preflight"
    return f"{stem}-preflight.{ext}"


def get_tokenized_preflight_image_path(front_filename: str, token: str) -> str:
    """Derive a preflight image filename that includes an unguessable token.

    The resulting filename is ``<stem>-preflight-<token>.<ext>``, making the
    URL unguessable without knowledge of the token.

    Args:
        front_filename: The front image filename (e.g. "a1b2c3d4-uuid.jpg").
        token: A random URL-safe token string (see ``generate_image_token``).

    Returns:
        The tokenized preflight filename (e.g. "a1b2c3d4-uuid-preflight-AbC123xYz.jpg").
    """
    stem, dot, ext = front_filename.rpartition(".")
    if not dot:
        return f"{front_filename}-preflight-{token}"
    return f"{stem}-preflight-{token}.{ext}"


def save_preflight_image(
    front_filename: str, file_bytes: bytes, store_path: Path, token: str | None = None
) -> str:
    """Save a preflight image alongside the front image.

    If a token is provided the filename includes the token, making the URL
    unguessable (e.g. "uuid-preflight-<token>.jpg"). Without a token it falls
    back to the legacy deterministic naming ("uuid-preflight.jpg").

    Args:
        front_filename: The front image filename used as the naming base.
        file_bytes: Raw image bytes to store.
        store_path: Path to the image store directory.
        token: Optional random token to embed in the filename for obscurity.

    Returns:
        The preflight image filename (including token if provided).

    Raises:
        ImageStorageError: If the directory is not writable or the write fails.
    """
    if token:
        preflight_filename = get_tokenized_preflight_image_path(front_filename, token)
    else:
        preflight_filename = get_preflight_image_path(front_filename)
    target = store_path / preflight_filename

    if not store_path.exists():
        raise ImageStorageError(
            f"Image store directory does not exist: {store_path}"
        )
    if not store_path.is_dir():
        raise ImageStorageError(
            f"Image store path is not a directory: {store_path}"
        )

    try:
        target.write_bytes(file_bytes)
    except OSError as exc:
        raise ImageStorageError(
            f"Failed to write preflight image to {target}: {exc}"
        ) from exc

    return preflight_filename
