"""Password verification and process-local browser sessions.

This module deliberately has no web-framework dependencies.  The application is
responsible for request-size limits, cookies, and mapping ``AuthStatus`` values
to HTTP responses.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
from dataclasses import dataclass
from enum import Enum
import getpass
import hashlib
import hmac
import ipaddress
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable


SCRYPT_N = 131_072
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 268_435_456
SALT_BYTES = 16
MAX_PASSWORD_BYTES = 1_024
MIN_GENERATED_PASSWORD_CHARS = 12
SESSION_TOKEN_BYTES = 32
DEFAULT_SESSION_LIFETIME_S = 12 * 60 * 60
DEFAULT_FAILURE_WINDOW_S = 5 * 60
DEFAULT_MAX_FAILURES = 5
MAX_TRACKED_CLIENT_IPS = 4096

_HASH_PREFIX = "scrypt$n=131072,r=8,p=1"


class PasswordHashError(ValueError):
    """The configured password hash is missing or not canonical."""


class PasswordPolicyError(ValueError):
    """A new password does not meet the generation policy."""


@dataclass(frozen=True)
class PasswordHash:
    salt: bytes
    digest: bytes

    def encoded(self) -> str:
        if len(self.salt) != SALT_BYTES or len(self.digest) != SCRYPT_DKLEN:
            raise PasswordHashError("password hash has invalid component lengths")
        return (
            f"{_HASH_PREFIX}$"
            f"{_base64url_encode(self.salt)}$"
            f"{_base64url_encode(self.digest)}"
        )


class AuthStatus(Enum):
    SUCCESS = "success"
    INVALID_CREDENTIALS = "invalid_credentials"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"
    DISABLED = "disabled"


@dataclass(frozen=True)
class AuthOutcome:
    status: AuthStatus
    token: str | None = None
    expires_at: float | None = None

    @property
    def authenticated(self) -> bool:
        return self.status is AuthStatus.SUCCESS


@dataclass(frozen=True)
class Session:
    token: str
    created_at: float
    expires_at: float


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode_canonical(value: str, expected_bytes: int) -> bytes:
    if not value or "=" in value:
        raise PasswordHashError("password hash uses invalid base64url")
    if any(
        not (
            "A" <= character <= "Z"
            or "a" <= character <= "z"
            or "0" <= character <= "9"
            or character in "_-"
        )
        for character in value
    ):
        raise PasswordHashError("password hash uses invalid base64url")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise PasswordHashError("password hash uses invalid base64url") from exc
    if len(decoded) != expected_bytes or _base64url_encode(decoded) != value:
        raise PasswordHashError("password hash has invalid component length")
    return decoded


def parse_password_hash(encoded: str | None) -> PasswordHash:
    """Parse the one accepted deployment hash format.

    Parsing is intentionally stricter than ``hashlib``: parameters, ordering,
    component lengths, alphabet, and lack of base64 padding are all part of the
    deployment contract.
    """

    if not isinstance(encoded, str):
        raise PasswordHashError("password hash is required")
    parts = encoded.split("$")
    if len(parts) != 4 or "$".join(parts[:2]) != _HASH_PREFIX:
        raise PasswordHashError("password hash format is invalid")
    salt = _base64url_decode_canonical(parts[2], SALT_BYTES)
    digest = _base64url_decode_canonical(parts[3], SCRYPT_DKLEN)
    return PasswordHash(salt=salt, digest=digest)


def _password_bytes(password: object) -> bytes | None:
    if not isinstance(password, str):
        return None
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_PASSWORD_BYTES:
        return None
    return encoded


def _derive_password(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def _verify_password_bytes(password: bytes, password_hash: PasswordHash) -> bool:
    actual = _derive_password(password, password_hash.salt)
    return hmac.compare_digest(actual, password_hash.digest)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create the canonical deployment encoding for a newly chosen password."""

    if not isinstance(password, str) or len(password) < MIN_GENERATED_PASSWORD_CHARS:
        raise PasswordPolicyError(
            f"password must contain at least {MIN_GENERATED_PASSWORD_CHARS} characters"
        )
    encoded_password = _password_bytes(password)
    if encoded_password is None:
        raise PasswordPolicyError(
            f"password must encode to at most {MAX_PASSWORD_BYTES} UTF-8 bytes"
        )
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    if not isinstance(salt, bytes) or len(salt) != SALT_BYTES:
        raise ValueError(f"salt must contain exactly {SALT_BYTES} bytes")
    digest = _derive_password(encoded_password, salt)
    return PasswordHash(salt=salt, digest=digest).encoded()


def verify_password(password: object, encoded: str | PasswordHash) -> bool:
    """Verify a bounded UTF-8 password with constant-time digest comparison."""

    password_hash = (
        encoded if isinstance(encoded, PasswordHash) else parse_password_hash(encoded)
    )
    encoded_password = _password_bytes(password)
    if encoded_password is None:
        return False
    return _verify_password_bytes(encoded_password, password_hash)


def canonicalize_client_ip(client_ip: str) -> str:
    """Return a stable textual key for an already selected socket/proxy IP."""

    if not isinstance(client_ip, str):
        raise ValueError("client IP must be a string")
    try:
        return ipaddress.ip_address(client_ip).compressed
    except ValueError as exc:
        raise ValueError("client IP is invalid") from exc


class AuthManager:
    """Own failed-login accounting and process-local absolute sessions."""

    def __init__(
        self,
        password_hash: PasswordHash | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        session_lifetime_s: float = DEFAULT_SESSION_LIFETIME_S,
        failure_window_s: float = DEFAULT_FAILURE_WINDOW_S,
        max_failures: int = DEFAULT_MAX_FAILURES,
    ):
        if session_lifetime_s <= 0:
            raise ValueError("session lifetime must be positive")
        if failure_window_s <= 0:
            raise ValueError("failure window must be positive")
        if max_failures <= 0:
            raise ValueError("maximum failures must be positive")
        self._password_hash = password_hash
        self._clock = clock
        self._session_lifetime_s = session_lifetime_s
        self._failure_window_s = failure_window_s
        self._max_failures = max_failures
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, deque[float]] = {}
        self._verification_active = False
        self._lock = threading.Lock()

    @classmethod
    def disabled(cls) -> AuthManager:
        return cls(None)

    @classmethod
    def from_encoded_hash(
        cls,
        encoded: str | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        session_lifetime_s: float = DEFAULT_SESSION_LIFETIME_S,
        failure_window_s: float = DEFAULT_FAILURE_WINDOW_S,
        max_failures: int = DEFAULT_MAX_FAILURES,
    ) -> AuthManager:
        return cls(
            parse_password_hash(encoded),
            clock=clock,
            session_lifetime_s=session_lifetime_s,
            failure_window_s=failure_window_s,
            max_failures=max_failures,
        )

    @property
    def enabled(self) -> bool:
        return self._password_hash is not None

    @property
    def session_lifetime_s(self) -> float:
        return self._session_lifetime_s

    def _prune_all_failures_locked(self, now: float) -> None:
        cutoff = now - self._failure_window_s
        stale = [
            client_ip
            for client_ip, failures in self._failures.items()
            if not failures or failures[-1] <= cutoff
        ]
        for client_ip in stale:
            self._failures.pop(client_ip, None)

    def _prune_failures_locked(self, client_ip: str, now: float) -> deque[float]:
        self._prune_all_failures_locked(now)
        failures = self._failures.get(client_ip)
        if failures is None:
            failures = deque()
        cutoff = now - self._failure_window_s
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(client_ip, None)
        return failures

    def _record_failure_locked(self, client_ip: str, now: float) -> None:
        failures = self._prune_failures_locked(client_ip, now)
        if client_ip not in self._failures:
            if len(self._failures) >= MAX_TRACKED_CLIENT_IPS:
                self._failures.pop(next(iter(self._failures)))
            self._failures[client_ip] = failures
        # Attempts rejected because the window is already full do not extend it.
        if len(failures) < self._max_failures:
            failures.append(now)

    def record_failed_attempt(self, client_ip: str) -> None:
        canonical_ip = canonicalize_client_ip(client_ip)
        with self._lock:
            self._record_failure_locked(canonical_ip, self._clock())

    def failed_attempt_count(self, client_ip: str) -> int:
        canonical_ip = canonicalize_client_ip(client_ip)
        with self._lock:
            return len(self._prune_failures_locked(canonical_ip, self._clock()))

    def is_client_rate_limited(self, client_ip: str) -> bool:
        return self.failed_attempt_count(client_ip) >= self._max_failures

    def _release_verification_slot(self) -> None:
        with self._lock:
            self._verification_active = False

    def _finish_abandoned_verification(self, worker: asyncio.Task[bool]) -> None:
        # Retrieve a worker exception so a disconnected request cannot produce
        # an unhandled-task warning.  The reserved failed attempt remains.
        if not worker.cancelled():
            worker.exception()
        self._release_verification_slot()

    async def authenticate(self, client_ip: str, password: object) -> AuthOutcome:
        """Verify once without blocking the event loop and create a session.

        ``BUSY`` and ``RATE_LIMITED`` never run scrypt.  Invalid password values
        (including values over the byte bound) count as failures without running
        scrypt.  The single verification slot is reserved before yielding.
        """

        if not self.enabled:
            return AuthOutcome(AuthStatus.DISABLED)
        canonical_ip = canonicalize_client_ip(client_ip)
        encoded_password = _password_bytes(password)
        if encoded_password is None:
            self.record_failed_attempt(canonical_ip)
            return AuthOutcome(AuthStatus.INVALID_CREDENTIALS)

        with self._lock:
            now = self._clock()
            failures = self._prune_failures_locked(canonical_ip, now)
            if len(failures) >= self._max_failures:
                return AuthOutcome(AuthStatus.RATE_LIMITED)
            if self._verification_active:
                return AuthOutcome(AuthStatus.BUSY)
            if (
                canonical_ip not in self._failures
                and len(self._failures) >= MAX_TRACKED_CLIENT_IPS
            ):
                self._failures.pop(next(iter(self._failures)))
            # Reserve the potential failure before yielding.  A disconnected
            # request cannot evade the rolling limit while its scrypt job runs.
            if canonical_ip not in self._failures:
                self._failures[canonical_ip] = failures
            failures.append(now)
            self._verification_active = True

        assert self._password_hash is not None
        worker = asyncio.create_task(
            asyncio.to_thread(
                _verify_password_bytes,
                encoded_password,
                self._password_hash,
            )
        )
        release_in_finally = True
        try:
            verified = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The thread cannot be canceled.  Retain the memory-bounding slot
            # until it really exits, even though its HTTP request disappeared.
            release_in_finally = False
            worker.add_done_callback(self._finish_abandoned_verification)
            raise
        finally:
            if release_in_finally:
                self._release_verification_slot()

        now = self._clock()
        if not verified:
            return AuthOutcome(AuthStatus.INVALID_CREDENTIALS)

        with self._lock:
            self._failures.pop(canonical_ip, None)
            while True:
                token = _base64url_encode(secrets.token_bytes(SESSION_TOKEN_BYTES))
                if token not in self._sessions:
                    break
            expires_at = now + self._session_lifetime_s
            self._sessions[token] = Session(
                token=token,
                created_at=now,
                expires_at=expires_at,
            )
        return AuthOutcome(AuthStatus.SUCCESS, token=token, expires_at=expires_at)

    def lookup_session(self, token: str | None) -> Session | None:
        if not self.enabled or not isinstance(token, str):
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if self._clock() >= session.expires_at:
                return None
            return session

    def session_expires_at(self, token: str | None) -> float | None:
        session = self.lookup_session(token)
        return None if session is None else session.expires_at

    def revoke_session(self, token: str | None) -> bool:
        if not isinstance(token, str):
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def revoke_all_sessions(self) -> list[str]:
        with self._lock:
            tokens = list(self._sessions)
            self._sessions.clear()
            return tokens

    def reap_expired_sessions(self) -> list[str]:
        now = self._clock()
        with self._lock:
            expired = [
                token
                for token, session in self._sessions.items()
                if now >= session.expires_at
            ]
            for token in expired:
                del self._sessions[token]
            return expired

    def reap_stale_failures(self) -> None:
        with self._lock:
            self._prune_all_failures_locked(self._clock())

    def next_session_expiry(self) -> float | None:
        with self._lock:
            if not self._sessions:
                return None
            return min(session.expires_at for session in self._sessions.values())


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m control.auth",
        description="Control-plane authentication utilities",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "hash-password",
        help="prompt for a deployment password and print its encoded hash",
    )
    return parser


def _has_controlling_terminal() -> bool:
    """Prove getpass will not fall back to reading the password from stdin."""

    flags = os.O_RDWR
    if hasattr(os, "O_NOCTTY"):
        flags |= os.O_NOCTTY
    try:
        fd = os.open("/dev/tty", flags)
    except OSError:
        return False
    os.close(fd)
    return True


def main(argv: list[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    if args.command != "hash-password":  # pragma: no cover - argparse enforces this
        return 2

    if not _has_controlling_terminal():
        print(
            "A controlling terminal is required; passwords are never read from stdin.",
            file=sys.stderr,
        )
        return 1
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < MIN_GENERATED_PASSWORD_CHARS:
        print(
            f"Password must contain at least {MIN_GENERATED_PASSWORD_CHARS} characters.",
            file=sys.stderr,
        )
        return 1
    try:
        encoded = hash_password(password)
    except PasswordPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
