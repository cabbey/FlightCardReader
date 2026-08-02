"""Unit tests for the image storage service.

Tests save_image, delete_image, and error handling for ImageStorageError.
Tests save_back_image, save_preflight_image, get_back_image_path, get_preflight_image_path.
"""
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from flight_card_scanner.exceptions import ImageStorageError
from flight_card_scanner.services.image_service import (
    save_image,
    delete_image,
    save_back_image,
    save_preflight_image,
    get_back_image_path,
    get_preflight_image_path,
)


class TestSaveImage:
    """Tests for save_image function."""

    def test_saves_bytes_to_uuid_filename(self, tmp_path: Path):
        """save_image writes bytes and returns a UUID-based filename."""
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        filename = save_image(content, "png", tmp_path)

        # Filename should be <uuid>.png
        stem, ext = filename.rsplit(".", 1)
        assert ext == "png"
        # Validate stem is a valid UUID4
        parsed = uuid.UUID(stem, version=4)
        assert str(parsed) == stem

        # File should exist with exact bytes
        saved = (tmp_path / filename).read_bytes()
        assert saved == content

    def test_byte_for_byte_fidelity(self, tmp_path: Path):
        """Saved file is byte-for-byte identical to input (no re-encoding)."""
        content = bytes(range(256)) * 10
        filename = save_image(content, "jpg", tmp_path)
        assert (tmp_path / filename).read_bytes() == content

    def test_strips_leading_dot_from_extension(self, tmp_path: Path):
        """Leading dot in extension is normalised."""
        filename = save_image(b"data", ".jpeg", tmp_path)
        assert filename.endswith(".jpeg")
        assert not filename.endswith("..jpeg")

    def test_raises_if_directory_does_not_exist(self, tmp_path: Path):
        """Raises ImageStorageError if store directory doesn't exist."""
        nonexistent = tmp_path / "no_such_dir"
        with pytest.raises(ImageStorageError, match="does not exist"):
            save_image(b"data", "png", nonexistent)

    def test_raises_if_path_is_not_directory(self, tmp_path: Path):
        """Raises ImageStorageError if store_path is a file, not a directory."""
        file_path = tmp_path / "afile.txt"
        file_path.write_text("not a dir")
        with pytest.raises(ImageStorageError, match="not a directory"):
            save_image(b"data", "png", file_path)

    def test_raises_on_write_failure(self, tmp_path: Path):
        """Raises ImageStorageError if the write operation fails."""
        with patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            with pytest.raises(ImageStorageError, match="Failed to write"):
                save_image(b"data", "png", tmp_path)

    def test_unique_filenames_on_multiple_saves(self, tmp_path: Path):
        """Each call generates a unique filename."""
        f1 = save_image(b"a", "png", tmp_path)
        f2 = save_image(b"b", "png", tmp_path)
        assert f1 != f2


class TestDeleteImage:
    """Tests for delete_image function."""

    def test_deletes_existing_file(self, tmp_path: Path):
        """delete_image removes an existing file."""
        target = tmp_path / "to_delete.png"
        target.write_bytes(b"image data")
        assert target.exists()

        delete_image(target)
        assert not target.exists()

    def test_no_error_if_file_missing(self, tmp_path: Path):
        """delete_image is idempotent — no error if file doesn't exist."""
        nonexistent = tmp_path / "ghost.png"
        # Should not raise
        delete_image(nonexistent)

    def test_logs_warning_on_other_os_error(self, tmp_path: Path, caplog):
        """delete_image logs a warning on OS errors other than FileNotFoundError."""
        target = tmp_path / "locked.png"
        target.write_bytes(b"data")

        with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
            import logging
            with caplog.at_level(logging.WARNING):
                delete_image(target)
            assert "Failed to delete image" in caplog.text


class TestGetBackImagePath:
    """Tests for get_back_image_path helper."""

    def test_inserts_back_suffix(self):
        """Inserts '-back' before the file extension."""
        assert get_back_image_path("a1b2c3d4.jpg") == "a1b2c3d4-back.jpg"

    def test_handles_uuid_filename(self):
        """Works with a full UUID4 filename."""
        front = "550e8400-e29b-41d4-a716-446655440000.png"
        expected = "550e8400-e29b-41d4-a716-446655440000-back.png"
        assert get_back_image_path(front) == expected

    def test_handles_no_extension(self):
        """If no extension, appends suffix to end."""
        assert get_back_image_path("noext") == "noext-back"

    def test_handles_multiple_dots(self):
        """Only splits on the last dot."""
        assert get_back_image_path("file.name.jpg") == "file.name-back.jpg"


class TestGetPreflightImagePath:
    """Tests for get_preflight_image_path helper."""

    def test_inserts_preflight_suffix(self):
        """Inserts '-preflight' before the file extension."""
        assert get_preflight_image_path("a1b2c3d4.jpg") == "a1b2c3d4-preflight.jpg"

    def test_handles_uuid_filename(self):
        """Works with a full UUID4 filename."""
        front = "550e8400-e29b-41d4-a716-446655440000.png"
        expected = "550e8400-e29b-41d4-a716-446655440000-preflight.png"
        assert get_preflight_image_path(front) == expected

    def test_handles_no_extension(self):
        """If no extension, appends suffix to end."""
        assert get_preflight_image_path("noext") == "noext-preflight"

    def test_handles_multiple_dots(self):
        """Only splits on the last dot."""
        assert get_preflight_image_path("file.name.jpg") == "file.name-preflight.jpg"


class TestSaveBackImage:
    """Tests for save_back_image function."""

    def test_saves_with_back_suffix(self, tmp_path: Path):
        """Saves bytes to a file with '-back' suffix before extension."""
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        front = "abc123.png"
        filename = save_back_image(front, content, tmp_path)

        assert filename == "abc123-back.png"
        assert (tmp_path / filename).read_bytes() == content

    def test_byte_fidelity(self, tmp_path: Path):
        """Saved file is byte-for-byte identical to input."""
        content = bytes(range(256)) * 4
        filename = save_back_image("test.jpg", content, tmp_path)
        assert (tmp_path / filename).read_bytes() == content

    def test_raises_if_directory_does_not_exist(self, tmp_path: Path):
        """Raises ImageStorageError if store directory doesn't exist."""
        nonexistent = tmp_path / "no_such_dir"
        with pytest.raises(ImageStorageError, match="does not exist"):
            save_back_image("front.jpg", b"data", nonexistent)

    def test_raises_if_path_is_not_directory(self, tmp_path: Path):
        """Raises ImageStorageError if store_path is a file."""
        file_path = tmp_path / "afile.txt"
        file_path.write_text("not a dir")
        with pytest.raises(ImageStorageError, match="not a directory"):
            save_back_image("front.jpg", b"data", file_path)

    def test_raises_on_write_failure(self, tmp_path: Path):
        """Raises ImageStorageError if the write operation fails."""
        with patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            with pytest.raises(ImageStorageError, match="Failed to write"):
                save_back_image("front.jpg", b"data", tmp_path)


class TestSavePreflightImage:
    """Tests for save_preflight_image function."""

    def test_saves_with_preflight_suffix(self, tmp_path: Path):
        """Saves bytes to a file with '-preflight' suffix before extension."""
        content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        front = "abc123.png"
        filename = save_preflight_image(front, content, tmp_path)

        assert filename == "abc123-preflight.png"
        assert (tmp_path / filename).read_bytes() == content

    def test_byte_fidelity(self, tmp_path: Path):
        """Saved file is byte-for-byte identical to input."""
        content = bytes(range(256)) * 4
        filename = save_preflight_image("test.jpg", content, tmp_path)
        assert (tmp_path / filename).read_bytes() == content

    def test_raises_if_directory_does_not_exist(self, tmp_path: Path):
        """Raises ImageStorageError if store directory doesn't exist."""
        nonexistent = tmp_path / "no_such_dir"
        with pytest.raises(ImageStorageError, match="does not exist"):
            save_preflight_image("front.jpg", b"data", nonexistent)

    def test_raises_if_path_is_not_directory(self, tmp_path: Path):
        """Raises ImageStorageError if store_path is a file."""
        file_path = tmp_path / "afile.txt"
        file_path.write_text("not a dir")
        with pytest.raises(ImageStorageError, match="not a directory"):
            save_preflight_image("front.jpg", b"data", file_path)

    def test_raises_on_write_failure(self, tmp_path: Path):
        """Raises ImageStorageError if the write operation fails."""
        with patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            with pytest.raises(ImageStorageError, match="Failed to write"):
                save_preflight_image("front.jpg", b"data", tmp_path)
