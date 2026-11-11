"""E2E fixtures: a real uvicorn subprocess per test, isolated data dir and Postgres DB."""

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Page, sync_playwright

from tests.e2e.toss_stub import STUB_CLIENT, STUB_SECRET

PROJECT_ROOT = Path(__file__).resolve().parents[2]
READY_TIMEOUT_S = 15
POLL_INTERVAL_S = 0.2
TEST_JWT_SECRET = "e2e-test-jwt-secret-not-for-production"
# Fernet key (urlsafe base64 of 32 bytes) so personal fields are encrypted in the DB.
TEST_PII_KEY = "c2hvcGxlZ2FjeS10ZXN0LXBpaS1rZXktMzJieXRlcyE="


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])


def _stop(proc: subprocess.Popen[bytes]) -> str:
    """Stop the server and return whatever it wrote to stderr."""
    if proc.poll() is None:
        proc.terminate()
    try:
        _, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return (err or b"").decode(errors="replace")


def _remaining_env(tmp_path: Path) -> dict[str, str]:
    """Worker, billing, feedback and audio-store settings shared by every E2E server."""
    return {
        "SHOPLEGACY_STT_WORKER": "thread",
        "SHOPLEGACY_BILLING_PROVIDER": "fake",
        "SHOPLEGACY_FEEDBACK_SECRET": "e2e-feedback-secret",
        "SHOPLEGACY_AUDIO_STORE": f"file://{tmp_path}/audio",
    }


@pytest.fixture
def jwt_secret() -> str:
    """Overrides the root fixture so minted tokens match the E2E server."""
    return TEST_JWT_SECRET


@pytest.fixture
def auth_headers(reviewer_headers: dict[str, str]) -> dict[str, str]:
    """Bearer + X-Org-Id for a reviewer: can create shops and publish cards."""
    return reviewer_headers


@pytest.fixture
def server_url(tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest) -> Iterator[str]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPLEGACY_")}
    env["SHOPLEGACY_DATA_DIR"] = str(tmp_path / "data")
    env["SHOPLEGACY_DATABASE_URL"] = app_dsn  # fresh clone of the migrated template DB
    env["SHOPLEGACY_JWT_SECRET"] = TEST_JWT_SECRET
    env["SHOPLEGACY_REQUIRE_CONSENT"] = (
        "false"  # pre-consent flows; server_url_strict covers the gate
    )
    env["SHOPLEGACY_PROVIDER"] = "fake"
    env["SHOPLEGACY_STT_PROVIDER"] = "fake"
    env["SHOPLEGACY_PII_KEYS"] = TEST_PII_KEY
    env["SHOPLEGACY_PUBLIC_BASE_URL"] = f"http://localhost:{port}"
    env.update(_remaining_env(tmp_path))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "shoplegacy.main:create_app",
            "--factory",
            "--host",
            "localhost",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://localhost:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(POLL_INTERVAL_S)
    if not ready:
        pytest.fail(f"server did not become ready on {url}\n{_stop(proc)}")
    try:
        yield url
    finally:
        stderr = _stop(proc)
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print(f"\n--- server stderr ({url}) ---\n{stderr}")


def _start_server(
    tmp_path: Path,
    app_dsn: str,
    request: pytest.FixtureRequest,
    *,
    require_consent: bool,
    extra_env: dict[str, str] | None = None,
) -> Iterator[str]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPLEGACY_")}
    env["SHOPLEGACY_DATA_DIR"] = str(tmp_path / "data")
    env["SHOPLEGACY_DATABASE_URL"] = app_dsn
    env["SHOPLEGACY_JWT_SECRET"] = TEST_JWT_SECRET
    env["SHOPLEGACY_REQUIRE_CONSENT"] = "true" if require_consent else "false"
    env["SHOPLEGACY_PROVIDER"] = "fake"
    env["SHOPLEGACY_STT_PROVIDER"] = "fake"
    env["SHOPLEGACY_PII_KEYS"] = TEST_PII_KEY
    env["SHOPLEGACY_PUBLIC_BASE_URL"] = f"http://localhost:{port}"
    env.update(_remaining_env(tmp_path))
    env.update(extra_env or {})
    cmd = [sys.executable, "-m", "uvicorn", "shoplegacy.main:create_app", "--factory"]
    cmd += ["--host", "localhost", "--port", str(port)]
    proc = subprocess.Popen(
        cmd, cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    url = f"http://localhost:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(POLL_INTERVAL_S)
    if not ready:
        pytest.fail(f"server did not become ready on {url}\n{_stop(proc)}")
    try:
        yield url
    finally:
        stderr = _stop(proc)
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print(f"\n--- server stderr ({url}) ---\n{stderr}")


@pytest.fixture
def server_url_strict(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest
) -> Iterator[str]:
    """Server with SHOPLEGACY_REQUIRE_CONSENT=true (publication needs consent)."""
    yield from _start_server(tmp_path, app_dsn, request, require_consent=True)


@pytest.fixture
def server_url_proxy(tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest) -> Iterator[str]:
    """Server behind a trusted proxy (loopback) with a 3 per minute rate limit."""
    loopback = f"{socket.gethostbyname('localhost')},::1"
    extra = {"SHOPLEGACY_TRUSTED_PROXIES": loopback, "SHOPLEGACY_RATE_LIMIT": "3/minute"}
    yield from _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)


@pytest.fixture
def toss_stub() -> Iterator[str]:
    """Base URL of a fresh Toss API stub process."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.toss_stub", str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://localhost:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{url}/__stub/health", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(POLL_INTERVAL_S)
    if not ready:
        pytest.fail(f"toss stub did not become ready on {url}\n{_stop(proc)}")
    try:
        yield url
    finally:
        _stop(proc)


@pytest.fixture
def server_url_toss(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest, toss_stub: str
) -> Iterator[str]:
    """Server whose billing provider is Toss, pointed at the local stub."""
    extra = {
        "SHOPLEGACY_BILLING_PROVIDER": "toss",
        "SHOPLEGACY_TOSS_CLIENT_KEY": STUB_CLIENT,
        "SHOPLEGACY_TOSS_SECRET_KEY": STUB_SECRET,
        "SHOPLEGACY_TOSS_API_BASE": toss_stub,
    }
    yield from _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)


@pytest.fixture
def s3_bucket() -> Iterator[tuple[str, Any]]:
    """``(bucket, S3FileSystem)`` of a throwaway bucket on the S3 endpoint under test."""
    endpoint = os.environ.get("SHOPLEGACY_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("SHOPLEGACY_TEST_S3_ENDPOINT not set")
    import s3fs

    fs = s3fs.S3FileSystem(endpoint_url=endpoint)
    prefix = os.environ.get("SHOPLEGACY_TEST_DB_PREFIX", "sl_test").replace("_", "-")
    bucket = f"sl-e2e-{prefix}-{uuid.uuid4().hex[:6]}"
    fs.mkdir(bucket)
    try:
        yield bucket, fs
    finally:
        fs.rm(bucket, recursive=True)
        with contextlib.suppress(FileNotFoundError):  # some servers drop the bucket with rm
            fs.rmdir(bucket)


@pytest.fixture
def server_url_s3(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest, s3_bucket: tuple[str, Any]
) -> Iterator[str]:
    """Server storing audio in the S3 bucket from ``s3_bucket``."""
    extra = {
        "SHOPLEGACY_AUDIO_STORE": f"s3://{s3_bucket[0]}/audio",
        "SHOPLEGACY_AUDIO_S3_ENDPOINT": os.environ["SHOPLEGACY_TEST_S3_ENDPOINT"],
    }
    yield from _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)


@pytest.fixture
def server_url_no_worker(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest
) -> Iterator[str]:
    """Server that queues transcription jobs but never runs them."""
    yield from _start_server(
        tmp_path,
        app_dsn,
        request,
        require_consent=False,
        extra_env={"SHOPLEGACY_STT_WORKER": "off"},
    )


@pytest.fixture
def maintenance(pg_dsn: str) -> Callable[[list[str]], str]:
    """``maintenance(args) -> stdout`` of the operator CLI, run as the database owner."""

    def _run(args: list[str]) -> str:
        env = {
            **os.environ,
            "SHOPLEGACY_DATABASE_URL": pg_dsn,
            "SHOPLEGACY_JWT_SECRET": TEST_JWT_SECRET,
            "SHOPLEGACY_PII_KEYS": TEST_PII_KEY,
            "SHOPLEGACY_BILLING_PROVIDER": "fake",
        }
        done = subprocess.run(
            [sys.executable, "-m", "shoplegacy.maintenance", *args],
            check=False,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    return _run


@pytest.fixture
def second_org(pg_dsn: str) -> dict[str, str]:
    """Another org (not the one behind ``auth_headers``) with its own reviewer token."""
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    email = f"reviewer-b-{org_id[:8]}@example.test"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (user_id, email))
        conn.execute(
            "INSERT INTO public.orgs (org_id, name, created_by) VALUES (%s, %s, %s)",
            (org_id, "second org", user_id),
        )
        conn.execute(
            "INSERT INTO public.org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, user_id, "reviewer"),
        )
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    token = jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


@pytest.fixture
def interviewer_headers(org_fixture: tuple[str, str, str, str]) -> dict[str, str]:
    org_id, _, _, token = org_fixture
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


@pytest.fixture
def stt_small_server_url(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest
) -> Iterator[str]:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPLEGACY_")}
    env["SHOPLEGACY_DATA_DIR"] = str(tmp_path / "data")
    env["SHOPLEGACY_DATABASE_URL"] = app_dsn  # fresh clone of the migrated template DB
    env["SHOPLEGACY_JWT_SECRET"] = TEST_JWT_SECRET
    env["SHOPLEGACY_PROVIDER"] = "fake"
    env["SHOPLEGACY_STT_PROVIDER"] = "fake"
    env["SHOPLEGACY_PII_KEYS"] = TEST_PII_KEY
    env["SHOPLEGACY_PUBLIC_BASE_URL"] = f"http://localhost:{port}"
    env["SHOPLEGACY_STT_MAX_BYTES"] = "1024"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "shoplegacy.main:create_app",
            "--factory",
            "--host",
            "localhost",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://localhost:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                ready = True
                break
        except httpx.TransportError:
            pass
        time.sleep(POLL_INTERVAL_S)
    if not ready:
        pytest.fail(f"server did not become ready on {url}\n{_stop(proc)}")
    try:
        yield url
    finally:
        stderr = _stop(proc)
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print(f"\n--- server stderr ({url}) ---\n{stderr}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> object:
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


@pytest.fixture
def shot_dir() -> Path:
    path = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def page(auth_headers: dict[str, str]) -> Iterator[Page]:
    token = auth_headers["Authorization"].removeprefix("Bearer ")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1000, "height": 900})
        context.add_init_script(
            f"window.localStorage.setItem('sl_access_token', {json.dumps(token)});"
            f"window.localStorage.setItem('sl_org_id', {json.dumps(auth_headers['X-Org-Id'])});"
        )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


@pytest.fixture
def owner_headers(org_fixture: tuple[str, str, str, str]) -> dict[str, str]:
    """Bearer + X-Org-Id for the org owner (admin screens, plan, members)."""
    org_id, owner_token, _, _ = org_fixture
    return {"Authorization": f"Bearer {owner_token}", "X-Org-Id": org_id}


@pytest.fixture
def admin_headers(owner_headers: dict[str, str]) -> dict[str, str]:
    """Headers of a user allowed to run admin actions (the org owner)."""
    return owner_headers


@contextmanager
def _browser_page(
    headers: dict[str, str] | None, launch_args: list[str] | None = None
) -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=launch_args or [])
        context = browser.new_context(
            viewport={"width": 1000, "height": 900},
            permissions=["microphone"] if launch_args else [],
        )
        if headers is not None:
            token = headers["Authorization"].removeprefix("Bearer ")
            context.add_init_script(
                f"window.localStorage.setItem('sl_access_token', {json.dumps(token)});"
                f"window.localStorage.setItem('sl_org_id', {json.dumps(headers['X-Org-Id'])});"
            )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


@pytest.fixture
def owner_page(owner_headers: dict[str, str]) -> Iterator[Page]:
    """Browser page logged in as the org owner."""
    with _browser_page(owner_headers) as pg:
        yield pg


@pytest.fixture
def mic_page(auth_headers: dict[str, str]) -> Iterator[Page]:
    """Browser page logged in as a reviewer, with a fake microphone that is always allowed."""
    args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
    with _browser_page(auth_headers, args) as pg:
        yield pg


@pytest.fixture
def anon_page() -> Iterator[Page]:
    """Browser page with no login."""
    with _browser_page(None) as pg:
        yield pg


@pytest.fixture
def axe_check() -> Callable[[Page], list[dict[str, Any]]]:
    """``axe_check(page)``: critical or serious accessibility violations on the page."""

    def _check(pg: Page) -> list[dict[str, Any]]:
        results = Axe().run(pg)
        violations: list[dict[str, Any]] = results.response["violations"]
        return [v for v in violations if v["impact"] in ("critical", "serious")]

    return _check


@pytest.fixture
def published_card(
    make_card: Callable[..., tuple[str, str]],
) -> Callable[[str, dict[str, str]], str]:
    """``published_card(base, headers) -> card_id`` of a freshly published card."""

    def _publish(base: str, headers: dict[str, str]) -> str:
        _, card_id = make_card(base, headers)
        res = httpx.post(f"{base}/api/cards/{card_id}/publish", timeout=10, headers=headers)
        assert res.status_code == 200, res.text
        return card_id

    return _publish


@pytest.fixture
def make_card() -> Callable[..., tuple[str, str]]:
    """``make_card(base, headers, **shop_fields) -> (shop_id, card_id)``: register, interview, end."""

    def _make(base: str, headers: dict[str, str], **shop_fields: Any) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "name": "가나다식당",
            "owner_name": "홍길동",
            "trade": "restaurant",
            "area_type": "residential",
            "years": 18,
            "closed_year": 2025,
            "supplier_names": ["라마바정육"],
        }
        payload.update(shop_fields)
        shop = httpx.post(
            f"{base}/api/shops",
            json=payload,
            timeout=10,
            headers=headers,
        )
        assert shop.status_code == 201, shop.text
        shop_id = str(shop.json()["shop_id"])
        started = httpx.post(
            f"{base}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=headers
        )
        assert started.status_code == 201, started.text
        session_id = started.json()["session_id"]
        for _ in range(40):
            res = httpx.post(
                f"{base}/api/sessions/{session_id}/answer",
                json={"text": "손님들이 편하게 드시도록 늘 같은 방식으로 준비했어요."},
                timeout=10,
                headers=headers,
            )
            assert res.status_code == 200, res.text
            if res.json()["next_question"] is None:
                break
        else:
            raise AssertionError("interview never ended")
        end = httpx.post(f"{base}/api/sessions/{session_id}/end", timeout=30, headers=headers)
        assert end.status_code == 200, end.text
        return shop_id, str(end.json()["card_id"])

    return _make


@pytest.fixture
def open_session() -> Callable[[str, dict[str, str]], str]:
    """``open_session(base, headers) -> session_id`` of a fresh shop's running interview."""

    def _open(base: str, headers: dict[str, str]) -> str:
        shop = httpx.post(
            f"{base}/api/shops",
            json={
                "name": "가나다식당",
                "owner_name": "홍길동",
                "trade": "restaurant",
                "area_type": "residential",
                "years": 18,
                "closed_year": 2025,
            },
            timeout=10,
            headers=headers,
        )
        assert shop.status_code == 201, shop.text
        started = httpx.post(
            f"{base}/api/sessions",
            json={"shop_id": shop.json()["shop_id"]},
            timeout=10,
            headers=headers,
        )
        assert started.status_code == 201, started.text
        return str(started.json()["session_id"])

    return _open
