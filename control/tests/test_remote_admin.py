from io import BytesIO
from pathlib import Path
import subprocess
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from starlette.websockets import WebSocketDisconnect

from control import auth
from control.app import (
    LOGIN_BODY_LIMIT,
    PUBLIC_HTTP_ROUTES,
    SESSION_COOKIE,
    create_app,
)
from control.mock_conductor import MockConductor
from control.remote_config import RemoteSettings


VALID_HASH = (
    "scrypt$n=131072,r=8,p=1$"
    "AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def protected_settings(tmp_path: Path, *, require_https: bool = True) -> RemoteSettings:
    return RemoteSettings(
        conductor_mode="mock",
        password_hash=VALID_HASH,
        allowed_origins=frozenset({"https://control.example.test"}),
        allow_network_changes=False,
        require_https=require_https,
        data_dir=tmp_path,
    )


def protected_app(tmp_path: Path, *, require_https: bool = True):
    manager = auth.AuthManager.from_encoded_hash(VALID_HASH)
    app = create_app(
        MockConductor(),
        auth_manager=manager,
        settings=protected_settings(tmp_path, require_https=require_https),
    )
    return app, manager


def login(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "_verify_password_bytes", lambda *_args: True)
    response = client.post(
        "/api/auth/login",
        json={"password": "correct horse battery"},
        headers={"Origin": "https://control.example.test"},
    )
    assert response.status_code == 200
    return response


def test_default_deny_boundary_and_security_headers(tmp_path: Path) -> None:
    app, _manager = protected_app(tmp_path)
    with TestClient(app, base_url="https://control.example.test") as client:
        session = client.get("/api/auth/session")
        health = client.get("/api/health")
        login_page = client.get("/login")
        api = client.get("/api/state")
        page = client.get("/", follow_redirects=False)
        openapi = client.get("/openapi.json", follow_redirects=False)
        ordinary_asset = client.get("/static/app.js", follow_redirects=False)

    assert session.status_code == 200
    assert session.json() == {"authenticated": False}
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert login_page.status_code == 200
    assert api.status_code == 401
    assert api.json() == {"detail": "authentication required"}
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert openapi.status_code == 303 and openapi.headers["location"] == "/login"
    assert ordinary_asset.status_code == 303
    for response in (session, health, login_page, api, page, openapi, ordinary_asset):
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_registered_public_route_inventory_is_exact() -> None:
    assert PUBLIC_HTTP_ROUTES == {
        ("GET", "/login"),
        ("GET", "/static/login.js"),
        ("GET", "/static/login.css"),
        ("GET", "/api/auth/session"),
        ("GET", "/api/health"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/internal/provisioning/reserve-id"),
    }


def test_browser_assets_follow_detached_ota_and_auth_contract() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    login_js = (Path(__file__).parents[1] / "static" / "login.js").read_text(
        encoding="utf-8"
    )

    assert "error.status = response.status" in app_js
    assert "if (error.status === 423)" in app_js
    assert "Firmware installation is finishing." in app_js
    assert "async function pollOtaInstallUntilTerminal()" in app_js
    assert "pollOtaInstallWhile" not in app_js
    assert 'const ack = await api("/api/operations/ota-install"' in app_js
    assert "await pollOtaInstallUntilTerminal()" in app_js
    assert "wifi.allow_changes !== false" in app_js
    assert 'releaseInfo = await api("/api/releases")' in app_js
    assert "await applyLiveState(data.state)" in app_js
    assert "function renderReleases()" in app_js
    index_html = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert "Web control plane" in index_html
    assert "Field firmware" in index_html
    assert "field-release-pending-changes" in index_html
    assert "Full release changelog" in index_html
    assert "event.code === 4401" in app_js
    assert 'await api("/api/auth/logout", { method: "POST" })' in app_js
    assert "JSON.stringify({password: passwordInput.value})" in login_js


def test_live_state_refreshes_release_status_in_executed_javascript() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
    refresh_start = app_js.index("async function refreshReleaseInfo()")
    refresh_end = app_js.index("\n}\n\nasync function applyLiveState", refresh_start) + 2
    apply_start = app_js.index("async function applyLiveState")
    apply_end = app_js.index("\n}\n\nasync function refreshSavedPatterns", apply_start) + 2
    script = f"""
let state = null;
let releaseInfo = null;
let rendered = 0;
let releaseRendered = 0;
async function api(path) {{
  if (path !== "/api/releases") throw new Error(path);
  return {{control: {{version: "0.4.0"}}, firmware: {{version: "0.3.0"}}, history: []}};
}}
function render() {{ rendered += 1; }}
function renderReleases() {{ releaseRendered += 1; }}
{app_js[refresh_start:refresh_end]}
{app_js[apply_start:apply_end]}
await applyLiveState({{conductor: {{firmware: {{version: "0.3.0"}}}}}});
if (state === null || releaseInfo.control.version !== "0.4.0") process.exit(1);
if (rendered !== 1 || releaseRendered !== 1) process.exit(2);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_ota_control_lock_is_reversible_in_executed_javascript() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function setOtaControlDisabled")
    end = app_js.index("\n}\n\nfunction renderOta()", start) + 2
    function_source = app_js[start:end]
    script = f"""
{function_source}
const enabled = {{disabled: false, dataset: {{}}}};
const disabled = {{disabled: true, dataset: {{}}}};
setOtaControlDisabled(enabled, true);
setOtaControlDisabled(disabled, true);
if (!enabled.disabled || !disabled.disabled) process.exit(1);
setOtaControlDisabled(enabled, false);
setOtaControlDisabled(disabled, false);
if (enabled.disabled || !disabled.disabled) process.exit(2);
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_fresh_page_retries_terminal_ota_reservation_gap_in_javascript() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("async function refresh()")
    end = app_js.index("\n}\n\nasync function refreshSavedPatterns", start) + 2
    function_source = app_js[start:end]
    script = f"""
let state = null;
let otaInstall = null;
let savedPatterns = [];
let otaArtifact = null;
let calibrationFrames = [];
let releaseInfo = null;
let provisioning = null;
let stateCalls = 0;
let delays = 0;
async function api(path) {{
  if (path === "/api/operations/ota-install") {{
    return {{install: {{running: false, complete: true}}}};
  }}
  if (path === "/api/state") {{
    stateCalls += 1;
    if (stateCalls === 1) {{
      const error = new Error("locked");
      error.status = 423;
      throw error;
    }}
    return {{conductor: {{}}, summary: {{}}, pattern: {{}}}};
  }}
  if (path === "/api/patterns") return {{patterns: []}};
  if (path === "/api/releases") return {{control: {{}}, firmware: {{}}, history: []}};
  if (path === "/api/operations/ota-artifact") return {{artifact: null}};
  if (path === "/api/calibration/frames") return {{frames: []}};
  throw new Error(`unexpected path ${{path}}`);
}}
async function delay() {{ delays += 1; }}
async function refreshWifiStatus() {{}}
async function pollOtaInstallUntilTerminal() {{
  throw new Error("terminal state must not poll a running job");
}}
function toast() {{}}
function render() {{}}
function renderReleases() {{}}
{function_source}
await refresh();
if (stateCalls !== 2 || delays !== 1 || state === null) process.exit(1);
"""

    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_login_sets_exact_secure_cookie_and_logout_revokes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = protected_app(tmp_path)
    with TestClient(app, base_url="https://control.example.test") as client:
        login_response = login(client, monkeypatch)
        cookie = client.cookies.get(SESSION_COOKIE)
        authenticated_session = client.get("/api/auth/session")
        protected_state = client.get("/api/state")
        set_cookie = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://control.example.test"},
        )
        session = client.get("/api/auth/session")

    assert cookie is not None
    assert manager.lookup_session(cookie) is None
    assert authenticated_session.json() == {"authenticated": True}
    assert protected_state.status_code == 200
    login_header = login_response.headers["set-cookie"]
    assert login_header.startswith(f"{SESSION_COOKIE}=")
    assert "Domain=" not in login_header
    assert "HttpOnly" in login_header
    assert "Max-Age=43200" in login_header
    assert "Path=/" in login_header
    assert "SameSite=strict" in login_header
    assert "Secure" in login_header
    assert set_cookie.status_code == 204
    header = set_cookie.headers["set-cookie"]
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "Domain=" not in header
    assert "HttpOnly" in header
    assert "Path=/" in header
    assert "SameSite=strict" in header
    assert "Secure" in header
    assert session.json() == {"authenticated": False}


@pytest.mark.parametrize(
    "body,headers,expected",
    [
        (b"{", {"Content-Type": "application/json"}, 401),
        (b'{"password":"x","extra":true}', {"Content-Type": "application/json"}, 401),
        (
            b'{"password":"' + b"x" * LOGIN_BODY_LIMIT + b'"}',
            {"Content-Type": "application/json", "Content-Length": "1"},
            413,
        ),
    ],
)
def test_rejected_login_bodies_count_without_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    headers: dict[str, str],
    expected: int,
) -> None:
    app, manager = protected_app(tmp_path)
    calls = []
    monkeypatch.setattr(
        auth,
        "_verify_password_bytes",
        lambda *_args: calls.append(True) or True,
    )
    with TestClient(app, base_url="https://control.example.test") as client:
        response = client.post(
            "/api/auth/login",
            content=body,
            headers={
                "Origin": "https://control.example.test",
                **headers,
            },
        )

    assert response.status_code == expected
    assert manager.failed_attempt_count("127.0.0.1") == 1
    assert calls == []


def test_chunked_oversized_login_is_rejected_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = protected_app(tmp_path)
    calls = []
    monkeypatch.setattr(
        auth,
        "_verify_password_bytes",
        lambda *_args: calls.append(True) or True,
    )
    chunks = iter([b'{"password":"', b"x" * LOGIN_BODY_LIMIT, b'"}'])
    with TestClient(app, base_url="https://control.example.test") as client:
        response = client.post(
            "/api/auth/login",
            content=chunks,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://control.example.test",
            },
        )

    assert response.status_code == 413
    assert manager.failed_attempt_count("127.0.0.1") == 1
    assert calls == []


def test_proxy_headers_are_trusted_only_from_the_loopback_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = protected_app(tmp_path)
    seen = []

    async def authenticate(client_ip: str, _password: object):
        seen.append(client_ip)
        return auth.AuthOutcome(auth.AuthStatus.INVALID_CREDENTIALS)

    monkeypatch.setattr(manager, "authenticate", authenticate)
    with TestClient(app, base_url="http://internal") as client:
        response = client.post(
            "/api/auth/login",
            json={"password": "wrong password"},
            headers={
                "CF-Connecting-IP": "2001:0db8::0001",
                "X-Forwarded-Proto": "https",
                "Origin": "https://control.example.test",
            },
        )

    assert response.status_code == 401
    assert seen == ["2001:db8::1"]


def test_https_and_exact_origin_are_required(tmp_path: Path) -> None:
    app, _manager = protected_app(tmp_path)
    with TestClient(app, base_url="http://control.example.test") as client:
        insecure = client.get("/login")
    with TestClient(app, base_url="https://control.example.test") as client:
        wrong_origin = client.post(
            "/api/auth/login",
            json={"password": "anything"},
            headers={"Origin": "https://evil.example"},
        )

    assert insecure.status_code == 400
    assert insecure.json() == {"detail": "HTTPS is required"}
    assert wrong_origin.status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "origin",
    ["null", "", "https://evil.example", "https://control.example.test.evil"],
)
def test_every_mutating_method_rejects_nonexact_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    origin: str,
) -> None:
    app, _manager = protected_app(tmp_path)
    with TestClient(app, base_url="https://control.example.test") as client:
        login(client, monkeypatch)
        response = client.request(
            method,
            "/api/state",
            headers={"Origin": origin},
        )

    assert response.status_code == 403


def test_websocket_requires_session_and_exact_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _manager = protected_app(tmp_path)
    with TestClient(app, base_url="https://control.example.test") as client:
        with pytest.raises(WebSocketDisconnect) as unauthenticated:
            with client.websocket_connect(
                "/ws",
                headers={
                    "Origin": "https://control.example.test",
                    "X-Forwarded-Proto": "https",
                },
            ):
                pass
        assert unauthenticated.value.code == 4401
        login(client, monkeypatch)
        session_cookie = client.cookies.get(SESSION_COOKIE)
        assert session_cookie is not None
        with pytest.raises(WebSocketDisconnect) as foreign_origin:
            with client.websocket_connect(
                "/ws",
                headers={
                    "Origin": "https://evil.example",
                    "X-Forwarded-Proto": "https",
                    "Cookie": f"{SESSION_COOKIE}={session_cookie}",
                },
            ):
                pass
        assert foreign_origin.value.code == 4403
        with client.websocket_connect(
            "/ws",
            headers={
                "Origin": "https://control.example.test",
                "X-Forwarded-Proto": "https",
                "Cookie": f"{SESSION_COOKIE}={session_cookie}",
            },
        ) as websocket:
            assert websocket.receive_json()["type"] == "state"


def test_logout_closes_an_existing_authenticated_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _manager = protected_app(tmp_path)
    with TestClient(app, base_url="https://control.example.test") as client:
        login(client, monkeypatch)
        token = client.cookies.get(SESSION_COOKIE)
        assert token is not None
        headers = {
            "Origin": "https://control.example.test",
            "X-Forwarded-Proto": "https",
            "Cookie": f"{SESSION_COOKIE}={token}",
        }
        with client.websocket_connect("/ws", headers=headers) as websocket:
            assert websocket.receive_json()["type"] == "state"
            logout = client.post(
                "/api/auth/logout",
                headers={"Origin": "https://control.example.test"},
            )
            assert logout.status_code == 204
            with pytest.raises(WebSocketDisconnect) as disconnected:
                websocket.receive_json()
            assert disconnected.value.code == 4401


def test_expiry_reaper_closes_socket_at_absolute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    manager = auth.AuthManager.from_encoded_hash(
        VALID_HASH,
        clock=clock,
        session_lifetime_s=2,
    )
    app = create_app(
        MockConductor(),
        auth_manager=manager,
        settings=protected_settings(tmp_path),
    )
    with TestClient(app, base_url="https://control.example.test") as client:
        login(client, monkeypatch)
        token = client.cookies.get(SESSION_COOKIE)
        assert token is not None
        headers = {
            "Origin": "https://control.example.test",
            "X-Forwarded-Proto": "https",
            "Cookie": f"{SESSION_COOKIE}={token}",
        }
        with client.websocket_connect("/ws", headers=headers) as websocket:
            assert websocket.receive_json()["type"] == "state"
            clock.now += 2
            assert client.get("/api/auth/session").json() == {"authenticated": False}
            time.sleep(1.1)
            with pytest.raises(WebSocketDisconnect) as disconnected:
                websocket.receive_json()
            assert disconnected.value.code == 4401


def test_login_http_maps_rate_limit_without_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = protected_app(tmp_path)
    calls = []
    monkeypatch.setattr(
        auth,
        "_verify_password_bytes",
        lambda *_args: calls.append(True) or True,
    )
    for _ in range(5):
        manager.record_failed_attempt("127.0.0.1")

    with TestClient(app, base_url="https://control.example.test") as client:
        response = client.post(
            "/api/auth/login",
            json={"password": "correct horse battery"},
            headers={"Origin": "https://control.example.test"},
        )

    assert response.status_code == 429
    assert calls == []


def test_login_http_stays_responsive_while_scrypt_slot_is_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _manager = protected_app(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking_verification(*_args):
        started.set()
        assert release.wait(timeout=3)
        return False

    monkeypatch.setattr(auth, "_verify_password_bytes", blocking_verification)
    with TestClient(app, base_url="https://control.example.test") as client:
        first_result = {}

        def first_login() -> None:
            first_result["response"] = client.post(
                "/api/auth/login",
                json={"password": "first wrong password"},
                headers={"Origin": "https://control.example.test"},
            )

        thread = threading.Thread(target=first_login)
        thread.start()
        assert started.wait(timeout=2)
        started_at = time.monotonic()
        session = client.get("/api/auth/session")
        busy = client.post(
            "/api/auth/login",
            json={"password": "second wrong password"},
            headers={"Origin": "https://control.example.test"},
        )
        elapsed = time.monotonic() - started_at
        release.set()
        thread.join(timeout=3)

    assert session.status_code == 200
    assert busy.status_code == 429
    assert busy.json() == {"detail": "try again later"}
    assert elapsed < 0.5
    assert first_result["response"].status_code == 401


def test_data_directory_and_network_change_policy(tmp_path: Path) -> None:
    settings = protected_settings(tmp_path)
    app = create_app(
        MockConductor(),
        auth_manager=auth.AuthManager.disabled(),
        settings=RemoteSettings(
            conductor_mode="mock",
            password_hash=None,
            allowed_origins=settings.allowed_origins,
            allow_network_changes=False,
            require_https=False,
            data_dir=tmp_path,
        ),
    )
    with TestClient(app) as client:
        wifi = client.get("/api/network/wifi")
        join = client.post("/api/network/wifi", json={"ssid": "Starlink", "password": ""})
        hotspot = client.post("/api/network/hotspot")

    assert app.state.ota_store.root == tmp_path / "ota"
    assert app.state.pattern_store.root == tmp_path / "patterns"
    assert app.state.calibration_store.root == tmp_path / "calibration"
    assert wifi.json()["wifi"]["allow_changes"] is False
    assert join.status_code == 403
    assert hotspot.status_code == 403


def test_data_directory_stores_survive_app_restart(tmp_path: Path) -> None:
    settings = RemoteSettings(
        conductor_mode="mock",
        password_hash=None,
        allowed_origins=frozenset(),
        allow_network_changes=True,
        require_https=False,
        data_dir=tmp_path,
    )
    first = create_app(MockConductor(), settings=settings)
    artifact = first.state.ota_store.stage("firmware.bin", b"\xe9\x00")
    pattern = first.state.pattern_store.create(
        "Saved glow",
        "Glow",
        48,
        {"hue": 40},
    )
    image_data = BytesIO()
    Image.new("RGB", (2, 2), "black").save(image_data, format="PNG")
    frame = first.state.calibration_store.add_image(
        "frame.png",
        image_data.getvalue(),
    )

    restarted = create_app(MockConductor(), settings=settings)

    assert restarted.state.ota_store.current()["sha256"] == artifact["sha256"]
    assert restarted.state.pattern_store.get(pattern["id"])["name"] == "Saved glow"
    assert restarted.state.calibration_store.frame(frame["frame_id"]) is not None


def test_injected_conductor_cannot_disable_serial_mode_authentication(
    tmp_path: Path,
) -> None:
    settings = RemoteSettings(
        conductor_mode="serial",
        password_hash=VALID_HASH,
        allowed_origins=frozenset({"https://control.example.test"}),
        allow_network_changes=False,
        require_https=True,
        data_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="cannot be disabled"):
        create_app(
            MockConductor(),
            auth_manager=auth.AuthManager.disabled(),
            settings=settings,
        )

    app = create_app(MockConductor(), settings=settings)
    assert app.state.auth_manager.enabled is True
