# Feature: auth-and-audit, Property 8: Rate Limiting Enforcement
"""
Property-based test for rate limiting enforcement.

For any email address, after exactly 5 failed login attempts within a
15-minute sliding window, the next login attempt for that email SHALL be
rejected (check_rate_limit returns (True, seconds_remaining)). After a
successful login (when under the limit), the failed-attempt counter SHALL
reset to zero.

**Validates: Requirements 8.2, 8.3, 8.7**
"""
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from flight_card_scanner.services.auth_service import (
    AuthService,
    RATE_LIMIT_MAX_ATTEMPTS,
    RATE_LIMIT_WINDOW_SECONDS,
)


# --- Strategies ---

# Generate email-like strings (lowercase with @ and domain)
emails = st.emails()

# Number of failed attempts to simulate (1-10 range)
attempt_counts = st.integers(min_value=1, max_value=10)


# --- Helpers ---

def _make_auth_service() -> AuthService:
    """Create an AuthService with a dummy session_factory (rate limiting is in-memory only)."""
    return AuthService(
        session_factory=None,  # type: ignore[arg-type]
        session_secret="test_secret_not_used",
        timeout_hours=8.0,
    )


# --- Tests ---

class TestRateLimitingEnforcement:
    """Property 8: Rate Limiting Enforcement.

    Tests that:
    1. After exactly 5 failed attempts within 15 minutes, check_rate_limit
       returns (True, seconds_remaining)
    2. Before 5 attempts, check_rate_limit returns (False, 0)
    3. After reset_failed_attempts(), the counter goes back to 0
    """

    @given(email=emails, num_attempts=st.integers(min_value=1, max_value=4))
    @settings(max_examples=100)
    def test_below_threshold_not_limited(self, email: str, num_attempts: int):
        """Before reaching 5 failed attempts, the email is NOT rate-limited."""
        service = _make_auth_service()

        # Record fewer than 5 failed attempts
        for _ in range(num_attempts):
            service.record_failed_attempt(email)

        is_limited, seconds_remaining = service.check_rate_limit(email)
        assert is_limited is False
        assert seconds_remaining == 0

    @given(email=emails, num_attempts=st.integers(min_value=5, max_value=10))
    @settings(max_examples=100)
    def test_at_or_above_threshold_is_limited(self, email: str, num_attempts: int):
        """After 5 or more failed attempts within the window, the email IS rate-limited."""
        service = _make_auth_service()

        # Record attempts
        for _ in range(num_attempts):
            service.record_failed_attempt(email)

        is_limited, seconds_remaining = service.check_rate_limit(email)
        assert is_limited is True
        assert seconds_remaining > 0
        assert seconds_remaining <= RATE_LIMIT_WINDOW_SECONDS + 1

    @given(email=emails)
    @settings(max_examples=100)
    def test_exactly_at_threshold_is_limited(self, email: str):
        """After exactly RATE_LIMIT_MAX_ATTEMPTS (5) failed attempts, rate limit triggers."""
        service = _make_auth_service()

        # Record exactly 5 failed attempts
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            service.record_failed_attempt(email)

        is_limited, seconds_remaining = service.check_rate_limit(email)
        assert is_limited is True
        assert seconds_remaining > 0

    @given(email=emails, num_attempts=st.integers(min_value=1, max_value=10))
    @settings(max_examples=100)
    def test_reset_clears_counter(self, email: str, num_attempts: int):
        """After reset_failed_attempts(), the counter goes back to 0 (not limited)."""
        service = _make_auth_service()

        # Record some failed attempts
        for _ in range(num_attempts):
            service.record_failed_attempt(email)

        # Reset
        service.reset_failed_attempts(email)

        # Should no longer be limited regardless of how many attempts were recorded
        is_limited, seconds_remaining = service.check_rate_limit(email)
        assert is_limited is False
        assert seconds_remaining == 0

    @given(email=emails)
    @settings(max_examples=100)
    def test_case_insensitive_rate_limiting(self, email: str):
        """Rate limiting treats different cases of the same email as identical."""
        service = _make_auth_service()

        # Record attempts using different cases
        variants = [email.lower(), email.upper(), email.swapcase()]
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            service.record_failed_attempt(variants[i % len(variants)])

        # All case variants should be rate-limited
        is_limited, _ = service.check_rate_limit(email.lower())
        assert is_limited is True

        is_limited_upper, _ = service.check_rate_limit(email.upper())
        assert is_limited_upper is True

    @given(email=emails)
    @settings(max_examples=100)
    def test_reset_then_reaccumulate(self, email: str):
        """After reset, new failed attempts accumulate fresh toward the threshold."""
        service = _make_auth_service()

        # Hit the limit
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            service.record_failed_attempt(email)
        is_limited, _ = service.check_rate_limit(email)
        assert is_limited is True

        # Reset (simulates successful login)
        service.reset_failed_attempts(email)

        # Record fewer than threshold again
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS - 1):
            service.record_failed_attempt(email)

        is_limited, seconds_remaining = service.check_rate_limit(email)
        assert is_limited is False
        assert seconds_remaining == 0
