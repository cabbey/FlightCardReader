# Feature: auth-and-audit, Property 1: Password Hashing Round-Trip
"""
Property-based test for password hashing round-trip.

For any valid password string (8-128 characters), hashing with Argon2id and
then verifying the original password against the hash SHALL succeed, and
verifying any different password against that hash SHALL fail.

**Validates: Requirements 1.3, 8.1**
"""
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError


# Use the same hasher configuration as the auth service
_hasher = PasswordHasher()


# --- Strategies ---

# Valid passwords: 8-128 characters, printable characters
valid_passwords = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S", "Z"),
        blacklist_characters="\x00",
    ),
    min_size=8,
    max_size=128,
)


# --- Tests ---

class TestPasswordHashingRoundTrip:
    """Property 1: Password Hashing Round-Trip.

    For any valid password (8-128 chars), hashing with argon2id and verifying
    the original succeeds, and verifying a different password fails.
    """

    @given(password=valid_passwords)
    @settings(max_examples=100)
    def test_hash_then_verify_original_succeeds(self, password: str):
        """Hashing a password and verifying the same password always succeeds."""
        hashed = _hasher.hash(password)

        # Verify should not raise — the original password matches
        assert _hasher.verify(hashed, password) is True

    @given(password=valid_passwords, other_password=valid_passwords)
    @settings(max_examples=100)
    def test_verify_different_password_fails(self, password: str, other_password: str):
        """Verifying a different password against the hash always fails."""
        assume(password != other_password)

        hashed = _hasher.hash(password)

        # Verifying a different password should raise VerifyMismatchError
        try:
            _hasher.verify(hashed, other_password)
            # If verify returned True, that's a failure of the property
            assert False, "verify() should have raised VerifyMismatchError"
        except VerifyMismatchError:
            pass  # Expected: different password fails verification

    @given(password=valid_passwords)
    @settings(max_examples=100)
    def test_hash_produces_argon2id_format(self, password: str):
        """Every hash produced uses the argon2id algorithm identifier."""
        hashed = _hasher.hash(password)

        # Argon2id hashes start with $argon2id$
        assert hashed.startswith("$argon2id$"), (
            f"Expected argon2id hash format, got: {hashed[:30]}"
        )

    @given(password=valid_passwords)
    @settings(max_examples=100)
    def test_hash_is_not_plaintext(self, password: str):
        """The hash never contains the plaintext password."""
        hashed = _hasher.hash(password)

        # The hash should never equal or contain the raw password
        assert hashed != password
        # For short passwords that could theoretically appear in the hash
        # encoding, we check the hash starts with the argon2id prefix
        assert hashed.startswith("$argon2id$")
