import asyncio
import base64
import threading

import pytest

from control import auth


VALID_HASH = (
    "scrypt$n=131072,r=8,p=1$"
    "AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


class FakeClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def decode_token(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def run(coroutine):
    return asyncio.run(coroutine)


def test_hash_password_uses_the_only_accepted_encoding():
    encoded = auth.hash_password("correct horse battery", salt=bytes(range(16)))

    parsed = auth.parse_password_hash(encoded)

    assert encoded.startswith("scrypt$n=131072,r=8,p=1$")
    _algorithm, _parameters, salt, digest = encoded.split("$")
    assert "=" not in salt
    assert "=" not in digest
    assert parsed.salt == bytes(range(16))
    assert len(parsed.digest) == 32
    assert auth.verify_password("correct horse battery", parsed)
    assert not auth.verify_password("incorrect horse battery", parsed)


def test_hash_password_uses_exact_scrypt_cost_and_random_16_byte_salt(monkeypatch):
    calls = []

    def fake_scrypt(password, **parameters):
        calls.append((password, parameters))
        return b"d" * 32

    monkeypatch.setattr(auth.hashlib, "scrypt", fake_scrypt)
    monkeypatch.setattr(
        auth.secrets,
        "token_bytes",
        lambda size: b"s" * size,
    )

    parsed = auth.parse_password_hash(auth.hash_password("twelve chars!"))

    assert parsed.salt == b"s" * 16
    assert parsed.digest == b"d" * 32
    assert calls == [
        (
            b"twelve chars!",
            {
                "salt": b"s" * 16,
                "n": 131_072,
                "r": 8,
                "p": 1,
                "dklen": 32,
                "maxmem": 268_435_456,
            },
        )
    ]


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "pbkdf2$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131071,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=9,p=1$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=2$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1,dklen=32$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA=$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAA+$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "scrypt$n=131072,r=8,p=1$AAAAAAAAAAAAAAAAAAAAAA$"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA$extra",
    ],
)
def test_parse_password_hash_rejects_every_noncanonical_encoding(encoded):
    with pytest.raises(auth.PasswordHashError):
        auth.parse_password_hash(encoded)


def test_from_encoded_hash_rejects_missing_and_malformed_configuration():
    with pytest.raises(auth.PasswordHashError):
        auth.AuthManager.from_encoded_hash(None)
    with pytest.raises(auth.PasswordHashError):
        auth.AuthManager.from_encoded_hash("not-a-hash")


def test_password_generation_bounds_characters_and_utf8_bytes(monkeypatch):
    monkeypatch.setattr(auth.hashlib, "scrypt", lambda *_args, **_kwargs: b"x" * 32)

    with pytest.raises(auth.PasswordPolicyError, match="12 characters"):
        auth.hash_password("short")
    with pytest.raises(auth.PasswordPolicyError, match="1024 UTF-8 bytes"):
        auth.hash_password("é" * 513)

    encoded = auth.hash_password("é" * 512, salt=b"s" * 16)
    assert auth.parse_password_hash(encoded).digest == b"x" * 32
    assert not auth.verify_password("\ud800", encoded)


def test_verification_uses_compare_digest(monkeypatch):
    comparisons = []
    monkeypatch.setattr(auth, "_derive_password", lambda _password, _salt: b"d" * 32)
    monkeypatch.setattr(
        auth.hmac,
        "compare_digest",
        lambda actual, expected: comparisons.append((actual, expected)) or True,
    )
    parsed = auth.PasswordHash(salt=b"s" * 16, digest=b"d" * 32)

    assert auth.verify_password("valid utf8", parsed)
    assert comparisons == [(b"d" * 32, b"d" * 32)]


def test_canonicalize_client_ip_normalizes_ipv4_and_ipv6():
    assert auth.canonicalize_client_ip("192.0.2.10") == "192.0.2.10"
    assert auth.canonicalize_client_ip("2001:0db8::0001") == "2001:db8::1"
    with pytest.raises(ValueError):
        auth.canonicalize_client_ip("client.example")


def test_success_creates_a_256_bit_absolute_lifetime_session(monkeypatch):
    clock = FakeClock()
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH, clock=clock)
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)

    outcome = run(manager.authenticate("2001:db8::1", "right password"))

    assert outcome.status is auth.AuthStatus.SUCCESS
    assert outcome.token is not None
    assert len(decode_token(outcome.token)) == 32
    assert outcome.expires_at == clock.now + 12 * 60 * 60
    session = manager.lookup_session(outcome.token)
    assert session is not None
    assert session.token == outcome.token
    assert session.created_at == clock.now
    assert manager.session_expires_at(outcome.token) == outcome.expires_at

    clock.advance(12 * 60 * 60 - 1)
    assert manager.lookup_session(outcome.token) is not None
    clock.advance(1)
    assert manager.lookup_session(outcome.token) is None


def test_failed_attempt_limit_is_per_client_and_rolling(monkeypatch):
    clock = FakeClock()
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH, clock=clock)
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: False)

    for _ in range(5):
        outcome = run(manager.authenticate("192.0.2.1", "wrong password"))
        assert outcome.status is auth.AuthStatus.INVALID_CREDENTIALS

    assert manager.is_client_rate_limited("192.0.2.1")
    assert run(
        manager.authenticate("192.0.2.1", "right password")
    ).status is auth.AuthStatus.RATE_LIMITED
    assert run(
        manager.authenticate("192.0.2.2", "wrong password")
    ).status is auth.AuthStatus.INVALID_CREDENTIALS

    clock.advance(299.9)
    assert manager.is_client_rate_limited("192.0.2.1")
    clock.advance(0.1)
    assert not manager.is_client_rate_limited("192.0.2.1")


def test_failed_attempt_tracking_is_bounded_and_stale_entries_are_swept(monkeypatch):
    clock = FakeClock()
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH, clock=clock)
    monkeypatch.setattr(auth, "MAX_TRACKED_CLIENT_IPS", 2)

    manager.record_failed_attempt("192.0.2.1")
    manager.record_failed_attempt("192.0.2.2")
    manager.record_failed_attempt("192.0.2.3")

    assert set(manager._failures) == {"192.0.2.2", "192.0.2.3"}
    clock.advance(300)
    manager.reap_stale_failures()
    assert manager._failures == {}


def test_failure_tracking_capacity_never_globally_locks_out_new_clients(monkeypatch):
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    monkeypatch.setattr(auth, "MAX_TRACKED_CLIENT_IPS", 2)
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)
    manager.record_failed_attempt("192.0.2.1")
    manager.record_failed_attempt("192.0.2.2")

    outcome = run(manager.authenticate("192.0.2.3", "correct password"))

    assert outcome.status is auth.AuthStatus.SUCCESS


def test_expired_lookup_does_not_hide_token_from_reaper(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)
    manager = auth.AuthManager.from_encoded_hash(
        VALID_HASH, clock=clock, session_lifetime_s=2
    )
    token = run(manager.authenticate("192.0.2.1", "password one")).token
    assert token is not None

    clock.advance(2)

    assert manager.lookup_session(token) is None
    assert manager.reap_expired_sessions() == [token]


def test_success_clears_prior_failures(monkeypatch):
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    results = iter([False, True])
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: next(results))

    assert run(
        manager.authenticate("192.0.2.1", "first try")
    ).status is auth.AuthStatus.INVALID_CREDENTIALS
    assert run(
        manager.authenticate("192.0.2.1", "second try")
    ).status is auth.AuthStatus.SUCCESS
    assert not manager.is_client_rate_limited("192.0.2.1")
    assert manager.failed_attempt_count("192.0.2.1") == 0


def test_oversized_or_invalid_password_counts_without_hashing(monkeypatch):
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    calls = []
    monkeypatch.setattr(
        auth, "_verify_password_bytes", lambda *_args: calls.append(True) or False
    )

    oversized = run(manager.authenticate("192.0.2.1", "é" * 513))
    invalid_utf8 = run(manager.authenticate("192.0.2.1", "\ud800"))

    assert oversized.status is auth.AuthStatus.INVALID_CREDENTIALS
    assert invalid_utf8.status is auth.AuthStatus.INVALID_CREDENTIALS
    assert manager.failed_attempt_count("192.0.2.1") == 2
    assert calls == []


def test_only_one_password_verification_can_run(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_verification(*_args):
        started.set()
        assert release.wait(timeout=2)
        return False

    monkeypatch.setattr(auth, "_verify_password_bytes", blocking_verification)
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)

    async def exercise_concurrency():
        first = asyncio.create_task(
            manager.authenticate("192.0.2.1", "password one")
        )
        assert await asyncio.to_thread(started.wait, 1)
        assert manager.failed_attempt_count("192.0.2.1") == 1
        second = await manager.authenticate("192.0.2.2", "password two")
        release.set()
        return second, await first

    second, first_result = run(exercise_concurrency())

    assert second.status is auth.AuthStatus.BUSY
    assert first_result.status is auth.AuthStatus.INVALID_CREDENTIALS
    assert manager.failed_attempt_count("192.0.2.2") == 0


def test_cancelled_request_keeps_slot_and_reserved_attempt_until_scrypt_exits(
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_verification(*_args):
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return False

    monkeypatch.setattr(auth, "_verify_password_bytes", blocking_verification)
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)

    async def exercise_cancellation():
        first = asyncio.create_task(
            manager.authenticate("192.0.2.1", "password one")
        )
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        busy = await manager.authenticate("192.0.2.2", "password two")
        assert busy.status is auth.AuthStatus.BUSY
        assert manager.failed_attempt_count("192.0.2.1") == 1

        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await manager.authenticate("192.0.2.2", "password two")

    assert run(exercise_cancellation()).status is auth.AuthStatus.INVALID_CREDENTIALS


def test_external_failed_attempts_share_the_same_limit(monkeypatch):
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    calls = []
    monkeypatch.setattr(
        auth, "_verify_password_bytes", lambda *_args: calls.append(True) or True
    )

    for _ in range(5):
        manager.record_failed_attempt("192.0.2.9")

    outcome = run(manager.authenticate("192.0.2.9", "valid password"))
    assert outcome.status is auth.AuthStatus.RATE_LIMITED
    assert calls == []


def test_logout_reaper_and_restart_invalidate_sessions(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)
    manager = auth.AuthManager.from_encoded_hash(
        VALID_HASH, clock=clock, session_lifetime_s=10
    )
    token_1 = run(manager.authenticate("192.0.2.1", "password one")).token
    clock.advance(2)
    token_2 = run(manager.authenticate("192.0.2.2", "password two")).token
    assert token_1 is not None and token_2 is not None
    assert manager.next_session_expiry() == 1_010.0

    assert manager.revoke_session(token_1)
    assert not manager.revoke_session(token_1)
    assert manager.lookup_session(token_1) is None

    restarted = auth.AuthManager.from_encoded_hash(VALID_HASH, clock=clock)
    assert restarted.lookup_session(token_2) is None

    clock.advance(10)
    assert manager.reap_expired_sessions() == [token_2]
    assert manager.next_session_expiry() is None


def test_revoke_all_returns_tokens_for_websocket_cleanup(monkeypatch):
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    token_1 = run(manager.authenticate("192.0.2.1", "password one")).token
    token_2 = run(manager.authenticate("192.0.2.2", "password two")).token

    assert set(manager.revoke_all_sessions()) == {token_1, token_2}
    assert manager.lookup_session(token_1) is None
    assert manager.lookup_session(token_2) is None


def test_disabled_manager_is_explicit_and_never_creates_sessions():
    manager = auth.AuthManager.disabled()

    outcome = run(manager.authenticate("192.0.2.1", "any password"))

    assert not manager.enabled
    assert outcome.status is auth.AuthStatus.DISABLED
    assert outcome.token is None
    assert manager.lookup_session("anything") is None
    assert manager.reap_expired_sessions() == []


def test_hash_password_cli_prompts_twice_and_prints_only_hash(monkeypatch, capsys):
    prompts = iter(["correct horse battery", "correct horse battery"])
    monkeypatch.setattr(auth, "_has_controlling_terminal", lambda: True)
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: next(prompts))
    monkeypatch.setattr(
        auth,
        "hash_password",
        lambda password: VALID_HASH if password == "correct horse battery" else "",
    )

    assert auth.main(["hash-password"]) == 0
    captured = capsys.readouterr()
    assert captured.out == VALID_HASH + "\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("passwords", "message"),
    [
        (["does not match", "different value"], "do not match"),
        (["too short", "too short"], "at least 12 characters"),
    ],
)
def test_hash_password_cli_rejects_mismatch_and_short_password(
    monkeypatch, capsys, passwords, message
):
    prompts = iter(passwords)
    monkeypatch.setattr(auth, "_has_controlling_terminal", lambda: True)
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: next(prompts))

    assert auth.main(["hash-password"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    for password in passwords:
        assert password not in captured.err


def test_hash_password_cli_does_not_accept_password_argument():
    with pytest.raises(SystemExit):
        auth.main(["hash-password", "password-on-argv"])


def test_hash_password_cli_refuses_stdin_fallback(monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr(auth, "_has_controlling_terminal", lambda: False)
    monkeypatch.setattr(
        auth.getpass, "getpass", lambda prompt: prompts.append(prompt) or "not used"
    )

    assert auth.main(["hash-password"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "never read from stdin" in captured.err
    assert prompts == []
