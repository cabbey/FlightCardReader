"""Tests for the EventManager class.

Covers: discovery, slug computation, open/close lifecycle, lazy opening,
idle timeout, refresh_events, list_events, and shutdown.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from flight_card_scanner.config import ServerConfig
from flight_card_scanner.event_manager import EventInfo, EventManager, EventSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_event_config(path: Path, event_name: str = "Test Event") -> None:
    """Write a minimal valid event config.json at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "event_name": event_name,
        "event_date_range": {"start": "2026-05-01", "end": "2026-05-03"},
        "extraction_mode": "deferred",
        "extraction_endpoints": [
            {"url": "http://localhost:11434", "concurrency": 1}
        ],
    }
    path.write_text(json.dumps(config), encoding="utf-8")


def _make_server_config(events_dir: Path, idle_timeout: int = 60) -> ServerConfig:
    """Create a ServerConfig pointing at the given events_dir."""
    return ServerConfig(
        events_dir=events_dir,
        event_idle_timeout_minutes=idle_timeout,
    )


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverEvents:
    """Tests for EventManager.discover_events()."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        """No config.json files means no events discovered."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()
        assert result == {}
        assert mgr.events == {}

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """Non-existent events_dir returns empty dict without crashing."""
        events_dir = tmp_path / "does_not_exist"
        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()
        assert result == {}

    def test_single_event(self, tmp_path: Path) -> None:
        """A single config.json is discovered with correct slug."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "2026" / "nxrs" / "config.json", "NXRS 2026")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert len(result) == 1
        assert "2026/nxrs" in result
        info = result["2026/nxrs"]
        assert info.slug == "2026/nxrs"
        assert info.event_config.event_name == "NXRS 2026"
        assert info.is_open is False

    def test_multiple_events(self, tmp_path: Path) -> None:
        """Multiple config.json files yield multiple events with correct slugs."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "2026" / "nxrs" / "config.json", "NXRS 2026")
        _write_event_config(events_dir / "2025" / "balls" / "config.json", "BALLS 2025")
        _write_event_config(events_dir / "demo" / "config.json", "Demo Event")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert len(result) == 3
        assert "2026/nxrs" in result
        assert "2025/balls" in result
        assert "demo" in result

    def test_nested_deep_path(self, tmp_path: Path) -> None:
        """Deeply nested config.json produces a multi-segment slug."""
        events_dir = tmp_path / "events"
        _write_event_config(
            events_dir / "region" / "west" / "2026" / "config.json", "West 2026"
        )

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert "region/west/2026" in result

    def test_config_at_events_root(self, tmp_path: Path) -> None:
        """config.json directly in events_dir gets an empty-string slug."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "config.json", "Root Event")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert "" in result
        assert result[""].event_config.event_name == "Root Event"

    def test_invalid_config_skipped(self, tmp_path: Path) -> None:
        """An invalid config.json is skipped without crashing."""
        events_dir = tmp_path / "events"
        # Valid event
        _write_event_config(events_dir / "good" / "config.json", "Good Event")
        # Invalid event (bad JSON)
        bad_path = events_dir / "bad" / "config.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("NOT JSON", encoding="utf-8")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert len(result) == 1
        assert "good" in result


# ---------------------------------------------------------------------------
# Open/close lifecycle tests
# ---------------------------------------------------------------------------


class TestOpenCloseEvent:
    """Tests for open_event and close_event."""

    @pytest.mark.asyncio
    async def test_open_event_sets_state(self, tmp_path: Path) -> None:
        """open_event initializes engine, session_factory, services and marks is_open."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "2026" / "nxrs" / "config.json", "NXRS")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with (
            patch(
                "flight_card_scanner.event_manager.create_async_engine"
            ) as mock_engine_fn,
            patch(
                "flight_card_scanner.event_manager.create_all", new_callable=AsyncMock
            ) as mock_create_all,
            patch(
                "flight_card_scanner.event_manager.MotorLookupService"
            ) as mock_mls_cls,
            patch(
                "flight_card_scanner.event_manager.ExtractionService"
            ) as mock_es_cls,
        ):
            mock_engine = MagicMock()
            mock_engine_fn.return_value = mock_engine
            mock_session_factory = MagicMock()
            with patch(
                "flight_card_scanner.event_manager.async_sessionmaker",
                return_value=mock_session_factory,
            ):
                mock_mls = MagicMock()
                mock_mls.startup = AsyncMock()
                mock_mls_cls.return_value = mock_mls

                mock_es = MagicMock()
                mock_es.start = AsyncMock()
                mock_es_cls.return_value = mock_es

                info = await mgr.open_event("2026/nxrs")

        assert info.is_open is True
        assert info.engine is mock_engine
        assert info.session_factory is mock_session_factory
        assert info.extraction_service is mock_es
        assert info.motor_lookup_service is mock_mls
        mock_create_all.assert_called_once_with(mock_engine)
        mock_mls.startup.assert_called_once()
        mock_es.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_event_not_found(self, tmp_path: Path) -> None:
        """open_event raises KeyError for unknown slug."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with pytest.raises(KeyError, match="not_there"):
            await mgr.open_event("not_there")

    @pytest.mark.asyncio
    async def test_open_already_open_is_noop(self, tmp_path: Path) -> None:
        """Opening an already-open event just updates last_accessed."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with (
            patch(
                "flight_card_scanner.event_manager.create_async_engine"
            ) as mock_engine_fn,
            patch(
                "flight_card_scanner.event_manager.create_all", new_callable=AsyncMock
            ),
            patch(
                "flight_card_scanner.event_manager.MotorLookupService"
            ) as mock_mls_cls,
            patch(
                "flight_card_scanner.event_manager.ExtractionService"
            ) as mock_es_cls,
            patch(
                "flight_card_scanner.event_manager.async_sessionmaker",
                return_value=MagicMock(),
            ),
        ):
            mock_engine_fn.return_value = MagicMock()
            mock_mls = MagicMock()
            mock_mls.startup = AsyncMock()
            mock_mls_cls.return_value = mock_mls
            mock_es = MagicMock()
            mock_es.start = AsyncMock()
            mock_es_cls.return_value = mock_es

            await mgr.open_event("ev")
            first_accessed = mgr.events["ev"].last_accessed

            # Call again - should not re-create engine
            mock_engine_fn.reset_mock()
            await mgr.open_event("ev")

            mock_engine_fn.assert_not_called()
            assert mgr.events["ev"].last_accessed >= first_accessed

    @pytest.mark.asyncio
    async def test_close_event(self, tmp_path: Path) -> None:
        """close_event stops services, disposes engine, clears state."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        mock_es = MagicMock()
        mock_es.stop = AsyncMock()

        # Manually set the event as open
        info = mgr.events["ev"]
        info.is_open = True
        info.engine = mock_engine
        info.session_factory = MagicMock()
        info.extraction_service = mock_es
        info.motor_lookup_service = MagicMock()
        info.flier_match_service = MagicMock()

        await mgr.close_event("ev")

        assert info.is_open is False
        assert info.engine is None
        assert info.session_factory is None
        assert info.extraction_service is None
        assert info.motor_lookup_service is None
        assert info.flier_match_service is None
        mock_es.stop.assert_called_once()
        mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_event_not_open_is_noop(self, tmp_path: Path) -> None:
        """Closing an already-closed event does nothing."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        # Should not raise
        await mgr.close_event("ev")

    @pytest.mark.asyncio
    async def test_close_event_not_found(self, tmp_path: Path) -> None:
        """close_event raises KeyError for unknown slug."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with pytest.raises(KeyError, match="unknown"):
            await mgr.close_event("unknown")


# ---------------------------------------------------------------------------
# get_event (lazy open) tests
# ---------------------------------------------------------------------------


class TestGetEvent:
    """Tests for get_event lazy-open behavior."""

    @pytest.mark.asyncio
    async def test_get_event_opens_if_closed(self, tmp_path: Path) -> None:
        """get_event opens the event if it is not already open."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with (
            patch(
                "flight_card_scanner.event_manager.create_async_engine",
                return_value=MagicMock(),
            ),
            patch(
                "flight_card_scanner.event_manager.create_all", new_callable=AsyncMock
            ),
            patch(
                "flight_card_scanner.event_manager.MotorLookupService"
            ) as mock_mls_cls,
            patch(
                "flight_card_scanner.event_manager.ExtractionService"
            ) as mock_es_cls,
            patch(
                "flight_card_scanner.event_manager.async_sessionmaker",
                return_value=MagicMock(),
            ),
        ):
            mock_mls = MagicMock()
            mock_mls.startup = AsyncMock()
            mock_mls_cls.return_value = mock_mls
            mock_es = MagicMock()
            mock_es.start = AsyncMock()
            mock_es_cls.return_value = mock_es

            info = await mgr.get_event("ev")

        assert info.is_open is True

    @pytest.mark.asyncio
    async def test_get_event_updates_last_accessed(self, tmp_path: Path) -> None:
        """get_event updates last_accessed if already open."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        # Manually mark as open with an old timestamp
        info = mgr.events["ev"]
        info.is_open = True
        info.engine = MagicMock()
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        info.last_accessed = old_time

        result = await mgr.get_event("ev")
        assert result.last_accessed > old_time

    @pytest.mark.asyncio
    async def test_get_event_not_found(self, tmp_path: Path) -> None:
        """get_event raises KeyError for unknown slug."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        with pytest.raises(KeyError):
            await mgr.get_event("nope")


# ---------------------------------------------------------------------------
# Idle timeout tests
# ---------------------------------------------------------------------------


class TestCheckIdleEvents:
    """Tests for check_idle_events."""

    @pytest.mark.asyncio
    async def test_idle_event_is_closed(self, tmp_path: Path) -> None:
        """An event idle beyond the timeout is closed."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir, idle_timeout=30))
        mgr.discover_events()

        # Simulate an open event that was last accessed 31 minutes ago
        info = mgr.events["ev"]
        info.is_open = True
        info.engine = MagicMock()
        info.engine.dispose = AsyncMock()
        info.extraction_service = MagicMock()
        info.extraction_service.stop = AsyncMock()
        info.last_accessed = datetime.now(timezone.utc) - timedelta(minutes=31)

        closed = await mgr.check_idle_events()

        assert "ev" in closed
        assert info.is_open is False

    @pytest.mark.asyncio
    async def test_recent_event_not_closed(self, tmp_path: Path) -> None:
        """An event accessed recently is not closed."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir, idle_timeout=60))
        mgr.discover_events()

        info = mgr.events["ev"]
        info.is_open = True
        info.engine = MagicMock()
        info.last_accessed = datetime.now(timezone.utc) - timedelta(minutes=5)

        closed = await mgr.check_idle_events()

        assert closed == []
        assert info.is_open is True

    @pytest.mark.asyncio
    async def test_closed_events_not_considered(self, tmp_path: Path) -> None:
        """Already-closed events are not considered for idle timeout."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir, idle_timeout=1))
        mgr.discover_events()

        # Event is not open
        info = mgr.events["ev"]
        info.last_accessed = datetime.now(timezone.utc) - timedelta(hours=2)

        closed = await mgr.check_idle_events()
        assert closed == []


# ---------------------------------------------------------------------------
# Refresh events tests
# ---------------------------------------------------------------------------


class TestRefreshEvents:
    """Tests for refresh_events."""

    def test_new_events_added(self, tmp_path: Path) -> None:
        """New config.json files are picked up on refresh."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "a" / "config.json", "A")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()
        assert len(mgr.events) == 1

        # Add a new event
        _write_event_config(events_dir / "b" / "config.json", "B")
        mgr.refresh_events()

        assert len(mgr.events) == 2
        assert "b" in mgr.events

    def test_open_events_preserved(self, tmp_path: Path) -> None:
        """Open events are preserved even if their config changes on disk."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Original")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        # Mark as open
        mgr.events["ev"].is_open = True
        mgr.events["ev"].engine = MagicMock()

        # Change config on disk
        _write_event_config(events_dir / "ev" / "config.json", "Changed")

        mgr.refresh_events()

        # The open event should still have the original engine reference
        assert mgr.events["ev"].is_open is True
        assert mgr.events["ev"].engine is not None
        # Name should NOT be updated because event is open
        assert mgr.events["ev"].event_config.event_name == "Original"

    def test_removed_events_cleaned_if_closed(self, tmp_path: Path) -> None:
        """Events whose config.json is removed are dropped if not open."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "a" / "config.json", "A")
        _write_event_config(events_dir / "b" / "config.json", "B")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()
        assert len(mgr.events) == 2

        # Remove event b from disk
        (events_dir / "b" / "config.json").unlink()

        mgr.refresh_events()
        assert "a" in mgr.events
        assert "b" not in mgr.events

    def test_removed_open_event_preserved(self, tmp_path: Path) -> None:
        """An open event whose config.json was removed is preserved."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()
        mgr.events["ev"].is_open = True
        mgr.events["ev"].engine = MagicMock()

        # Remove config from disk
        (events_dir / "ev" / "config.json").unlink()

        mgr.refresh_events()

        # Event should still be there because it's open
        assert "ev" in mgr.events
        assert mgr.events["ev"].is_open is True


# ---------------------------------------------------------------------------
# list_events tests
# ---------------------------------------------------------------------------


class TestListEvents:
    """Tests for list_events."""

    def test_returns_summaries(self, tmp_path: Path) -> None:
        """list_events returns EventSummary objects with correct data."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "2026" / "nxrs" / "config.json", "NXRS 2026")
        _write_event_config(events_dir / "2025" / "balls" / "config.json", "BALLS 2025")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        summaries = mgr.list_events()

        assert len(summaries) == 2
        assert all(isinstance(s, EventSummary) for s in summaries)

        # Should be sorted by slug
        assert summaries[0].slug == "2025/balls"
        assert summaries[0].event_name == "BALLS 2025"
        assert summaries[0].is_open is False
        assert summaries[1].slug == "2026/nxrs"
        assert summaries[1].event_name == "NXRS 2026"

    def test_open_status_reflected(self, tmp_path: Path) -> None:
        """list_events correctly shows is_open status."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()
        mgr.events["ev"].is_open = True

        summaries = mgr.list_events()
        assert summaries[0].is_open is True

    def test_empty_events(self, tmp_path: Path) -> None:
        """list_events returns empty list when no events discovered."""
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        assert mgr.list_events() == []


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


class TestShutdown:
    """Tests for shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_open(self, tmp_path: Path) -> None:
        """shutdown() closes all open events."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "a" / "config.json", "A")
        _write_event_config(events_dir / "b" / "config.json", "B")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        # Mark both as open
        for slug in ["a", "b"]:
            info = mgr.events[slug]
            info.is_open = True
            info.engine = MagicMock()
            info.engine.dispose = AsyncMock()
            info.extraction_service = MagicMock()
            info.extraction_service.stop = AsyncMock()

        await mgr.shutdown()

        assert mgr.events["a"].is_open is False
        assert mgr.events["b"].is_open is False

    @pytest.mark.asyncio
    async def test_shutdown_with_no_open_events(self, tmp_path: Path) -> None:
        """shutdown() with no open events does nothing."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "ev" / "config.json", "Ev")

        mgr = EventManager(_make_server_config(events_dir))
        mgr.discover_events()

        # Should not raise
        await mgr.shutdown()


# ---------------------------------------------------------------------------
# Slug computation edge cases
# ---------------------------------------------------------------------------


class TestSlugComputation:
    """Tests for slug computation edge cases."""

    def test_slug_uses_forward_slashes(self, tmp_path: Path) -> None:
        """Slugs always use forward slashes regardless of OS."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "a" / "b" / "c" / "config.json", "ABC")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert "a/b/c" in result
        # No backslashes
        for slug in result:
            assert "\\" not in slug

    def test_single_level_slug(self, tmp_path: Path) -> None:
        """A config.json one level deep produces a single-segment slug."""
        events_dir = tmp_path / "events"
        _write_event_config(events_dir / "myevent" / "config.json", "My Event")

        mgr = EventManager(_make_server_config(events_dir))
        result = mgr.discover_events()

        assert "myevent" in result
