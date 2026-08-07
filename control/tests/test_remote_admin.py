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


def test_local_serial_mode_accepts_only_direct_loopback_requests(tmp_path: Path) -> None:
    settings = RemoteSettings(
        conductor_mode="local-serial",
        password_hash=None,
        allowed_origins=frozenset(),
        allow_network_changes=False,
        require_https=False,
        data_dir=tmp_path,
    )
    app = create_app(MockConductor(), settings=settings)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        direct = client.get("/api/state")
        public_host = client.get("/api/state", headers={"Host": "control.example.test"})
        forwarded = client.get(
            "/api/state",
            headers={"X-Forwarded-Proto": "https"},
        )

    assert direct.status_code == 200
    assert public_host.status_code == 403
    assert forwarded.status_code == 403


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
    assert 'const ack = await api("/api/operations/ota-activate"' in app_js
    assert 'api("/api/operations/ota-stage"' not in app_js
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
    assert "Performers online" in index_html
    assert 'id="online-performer-count"' in index_html
    assert "Placed lights" in index_html
    assert 'src="/static/app.js?v=15"' in index_html
    assert 'href="/static/styles.css?v=4"' in index_html
    assert 'data-view="power"' in index_html
    assert 'id="view-power"' in index_html
    assert index_html.index('data-view="ops"') < index_html.index('data-view="flash"')
    assert '<button data-view="flash">Firmware</button>' in index_html
    flash_view = index_html[index_html.index('id="view-flash"'):index_html.index('id="view-power"')]
    assert 'id="ota-mode"' in flash_view
    assert "OTA firmware update" in flash_view
    assert "Upload to control plane" not in flash_view
    assert "Update field" in flash_view
    assert "Automatic updates: On" in flash_view
    assert "Development / recovery" in flash_view
    assert 'id="ota-release-select"' in flash_view
    assert 'data-action="upload-ota-artifact"' in flash_view
    assert flash_view.index("Update field") < flash_view.index('id="ota-nodes"')
    assert "USB Flashing Station" in flash_view
    assert "Simultaneous flashes" in flash_view
    assert "Enable auto-update" in flash_view
    assert "Disable auto-update" in flash_view
    assert "Start station" not in flash_view
    assert "Start for new boards" not in flash_view
    assert ".filter((job) => job.connected)" in app_js
    assert "function provisioningUpdateLabel(job)" in app_js
    assert 'update_needed: "Update needed"' in app_js
    assert 'unknown: "Unknown"' in app_js
    assert "Install firmware" in app_js
    assert "/api/provisioning/auto-update" in app_js
    assert "job.firmware_version && job.firmware_build" in app_js
    assert "Target v${escapeHtml(artifact.version)}" in app_js
    assert "Battery SOC" in index_html
    assert index_html.index("Battery SOC") < index_html.index('<nav class="tabs"')
    assert "Average draw per performer" in index_html
    assert "Estimated field draw" not in index_html
    assert "monitor.average_performer_draw_w" in app_js
    assert "node_offsets" in app_js
    assert 'verified ? "✓" : ""' in app_js
    assert "fullImage" in app_js
    assert "crcMatches" in app_js
    assert 'complete: "Installed"' in app_js
    assert "Blackout all groups" in index_html
    assert "Restore all groups" in index_html
    assert 'data-action="turn-off-group"' in index_html
    assert 'data-action="save-group-name"' in index_html
    assert "USB power-bank keepalive" not in index_html
    assert 'api(`/api/groups/${selectedGroup}`' in app_js
    assert "/api/operations/keepalive" not in app_js
    assert "event.code === 4401" in app_js
    assert 'await api("/api/auth/logout", { method: "POST" })' in app_js
    assert "JSON.stringify({password: passwordInput.value})" in login_js


def test_performer_lists_sort_by_numeric_id() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function performerId(item)")
    end = app_js.index("\n}\n\nfunction patternForGroup", start) + 2
    function_source = app_js[start:end]
    script = f"""
let state = {{lanterns: [
  {{node_id: 17, label: "#17", mac: "D"}},
  {{label: "#4", mac: "C"}},
  {{node_id: 2, label: "#2", mac: "B"}},
  {{label: "unassigned", mac: "A"}},
]}};
{function_source}
const labels = lanterns().map((item) => item.label).join(",");
if (labels !== "#2,#4,#17,unassigned") process.exit(1);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_online_performer_count_uses_fresh_roster_status() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function onlinePerformerCount(items)")
    end = app_js.index("\n}\n\nfunction render()", start) + 2
    function_source = app_js[start:end]
    script = f"""
{function_source}
const lanterns = [
  {{status: "alive"}},
  {{status: "missing"}},
  {{status: "alive"}},
];
if (onlinePerformerCount(lanterns) !== 2) process.exit(1);
if (onlinePerformerCount(null) !== 0) process.exit(2);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_group_performer_counts_include_missing_members_but_not_retired() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function groupPerformerCounts(items)")
    end = app_js.index("\n}\n\nfunction lanternDisplayName", start) + 2
    counts_source = app_js[start:end]
    options_start = app_js.index("function groupOptions(selectedGroupId)")
    options_end = app_js.index("\n}\n\nfunction updateGroupNameDirtyState", options_start) + 2
    options_source = app_js[options_start:options_end]
    script = f"""
const GROUP_COUNT = 8;
const items = [
  {{group_id: 0, status: "alive"}},
  {{group_id: 0, status: "missing"}},
  {{group_id: 1, status: "alive"}},
  {{group_id: 1, status: "retired"}},
  {{group_id: 99, status: "alive"}},
];
function lanterns() {{ return items; }}
function groupLabel(groupId) {{ return `Group ${{groupId + 1}}`; }}
function escapeHtml(value) {{ return value; }}
{counts_source}
{options_source}
const counts = groupPerformerCounts(items);
if (JSON.stringify(counts[0]) !== JSON.stringify({{online: 1, total: 2}})) process.exit(1);
if (JSON.stringify(counts[1]) !== JSON.stringify({{online: 1, total: 1}})) process.exit(2);
if (groupPerformerCounts(null).some((count) => count.online || count.total)) process.exit(3);
const options = groupOptions(0);
if (!options.includes("Group 1 (1 online / 2)")) process.exit(4);
if (!options.includes("Group 2 (1 online / 1)")) process.exit(5);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_control_plane_assets_revalidate_in_browser() -> None:
    app = create_app(MockConductor())
    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/static/app.js?v=3")

    assert page.status_code == 200
    assert script.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"


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


def test_ota_ui_keeps_field_controls_live_and_locks_only_firmware_inputs() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function renderOta()")
    end = app_js.index("\n}\n\nfunction otaReadyForInstall", start) + 2
    function_source = app_js[start:end]

    assert "setOtaControlDisabled" not in app_js
    assert "[data-pattern]" not in function_source
    assert "fileInput.disabled = installing" in function_source
    assert "stage-ota-artifact" not in function_source
    assert "install-ota" in function_source
    assert "stage-ota" not in function_source
    assert "activate-ota" not in function_source
    assert "pause-ota" in function_source


def test_ota_node_rows_show_performer_percent_and_verified_checkmark() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("function renderOtaNodes()")
    end = app_js.index("\n}\n\nfunction calibrationSettings", start) + 2
    function_source = app_js[start:end]
    sort_start = app_js.index("function performerId(item)")
    sort_end = app_js.index("\n}\n\nfunction lanterns", sort_start) + 2
    sort_source = app_js[sort_start:sort_end]
    script = f"""
const box = {{ hidden: true, innerHTML: "" }};
const state = {{ ota: {{ nodes: [
  {{mac: "AA", phase: "staged", offset: 250, crc32: 111}},
  {{mac: "BB", phase: "staged", offset: 1000, crc32: 123}},
] }} }};
const otaInstall = {{
  running: true,
  size: 1000,
  crc32: 123,
  target_macs: ["AA", "BB"],
  node_offsets: {{AA: 250, BB: 1000}},
  staged_macs: ["AA", "BB"],
  activated_macs: ["BB"],
  nodes: [],
}};
function $(selector) {{ return selector === "#ota-nodes" ? box : null; }}
function lanterns() {{ return [{{mac: "AA", label: "#1"}}, {{mac: "BB", label: "#2"}}]; }}
function escapeHtml(value) {{ return String(value); }}
function formatBytes(value) {{ return `${{value}} B`; }}
{sort_source}
{function_source}
renderOtaNodes();
if (box.hidden) process.exit(1);
if (!box.innerHTML.includes("Uploading · 25%")) process.exit(2);
if (!box.innerHTML.includes("250 B / 1000 B")) process.exit(3);
if (!box.innerHTML.includes("Installed · 100%")) process.exit(4);
if ((box.innerHTML.match(/aria-label="verified">✓/g) || []).length !== 1) process.exit(4);
if ((box.innerHTML.match(/ota-node-row /g) || []).length !== 2) process.exit(5);
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_ota_dashboard_background_poll_detects_automatic_updates() -> None:
    app_js = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    start = app_js.index("async function refreshOtaInstall()")
    end = app_js.index("\nasync function refreshWifiStatus", start)
    function_source = app_js[start:end]
    script = f"""
let otaInstall = {{running: false}};
let otaInstallRefreshPromise = null;
let otaInstallPollTimer = null;
let scheduled = null;
let requests = 0;
const responses = [{{running: true}}, {{running: false, complete: true}}];
const window = {{
  setTimeout(callback, delay) {{
    scheduled = {{callback, delay}};
    return requests + 1;
  }},
}};
async function api(path) {{
  if (path !== "/api/operations/ota-install") process.exit(1);
  const install = responses[requests++];
  return {{install}};
}}
function renderOta() {{}}
{function_source}
startOtaInstallPolling();
if (!scheduled || scheduled.delay !== 3000) process.exit(2);
let callback = scheduled.callback;
scheduled = null;
await callback();
if (requests !== 1 || !otaInstall.running) process.exit(3);
if (!scheduled || scheduled.delay !== 750) process.exit(4);
callback = scheduled.callback;
scheduled = null;
await callback();
if (requests !== 2 || otaInstall.running || !otaInstall.complete) process.exit(5);
if (!scheduled || scheduled.delay !== 3000) process.exit(6);
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
    assert app.state.group_store.root == tmp_path / "groups"
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
    group = first.state.group_store.update(1, "Lotus lanterns")
    image_data = BytesIO()
    Image.new("RGB", (2, 2), "black").save(image_data, format="PNG")
    frame = first.state.calibration_store.add_image(
        "frame.png",
        image_data.getvalue(),
    )

    restarted = create_app(MockConductor(), settings=settings)

    assert restarted.state.ota_store.current()["sha256"] == artifact["sha256"]
    assert restarted.state.pattern_store.get(pattern["id"])["name"] == "Saved glow"
    assert restarted.state.group_store.list()[1] == group
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
