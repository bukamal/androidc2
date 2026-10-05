"""The deploy/ artefacts must stay valid.

These files are what actually runs in production, so they get checked here
rather than discovered broken during a deploy.
"""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

REQUIRED = [
    "gunicorn.conf.py",
    "Caddyfile",
    "androidc2.service",
    "caddy.service",
    "install.sh",
]


@pytest.fixture(autouse=True)
def _require_deploy_dir():
    if not DEPLOY.is_dir():
        pytest.skip("deploy/ not present")


@pytest.mark.parametrize("name", REQUIRED)
def test_deploy_file_exists(name):
    path = DEPLOY / name
    assert path.is_file(), f"deploy/{name} missing"
    assert path.stat().st_size > 0


def test_gunicorn_config_is_valid_python():
    ast.parse((DEPLOY / "gunicorn.conf.py").read_text())


def test_install_script_is_valid_bash():
    if not shutil.which("bash"):
        pytest.skip("bash unavailable")
    r = subprocess.run(["bash", "-n", str(DEPLOY / "install.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gunicorn_uses_single_worker():
    """The agent registry is per-process; more than one worker breaks routing."""
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert re.search(r"^workers\s*=\s*1\s*$", src, re.M), \
        "workers must stay 1 until the registry moves to Redis"


def test_gunicorn_avoids_gevent():
    """gevent has no cp313 wheels and its source build fails on modern
    Cython, so it must not appear in requirements or the worker class."""
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert not re.search(r'worker_class\s*=\s*"gevent', src), \
        "gevent worker needs a compiled dependency; use gthread"

    reqs = (DEPLOY.parent / "requirements.txt").read_text()
    for banned in ("gevent", "gevent-websocket"):
        assert not re.search(rf"^{re.escape(banned)}==", reqs, re.M), \
            f"{banned} must not be pinned: no cp313 wheel, source build fails"


def test_gunicorn_uses_threaded_worker():
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert re.search(r'worker_class\s*=\s*"gthread"', src)
    assert re.search(r"^threads\s*=", src, re.M), \
        "threads are what allow concurrent WebSockets without gevent"


def test_simple_websocket_is_pinned():
    """The gthread worker gets WebSocket support from simple-websocket only."""
    reqs = (DEPLOY.parent / "requirements.txt").read_text()
    assert re.search(r"^simple-websocket==", reqs, re.M), \
        "simple-websocket is what makes WebSocket work on plain threads"


def test_gunicorn_binds_loopback_by_default():
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert 'C2_BIND", "127.0.0.1' in src, \
        "default bind must be loopback; TLS terminates at Caddy"


def test_gunicorn_timeouts_exceed_socket_ping_interval():
    """A proxy or worker timeout shorter than the ping interval kills idle
    agent sockets that are doing nothing, which looks like flakiness."""
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    timeout = int(re.search(r"^timeout\s*=\s*(\d+)", src, re.M).group(1))
    keepalive = int(re.search(r"^keepalive\s*=\s*(\d+)", src, re.M).group(1))
    assert keepalive < timeout, \
        f"keepalive {keepalive}s must be under worker timeout {timeout}s"


def test_caddyfile_proxies_to_the_gunicorn_port():
    src = (DEPLOY / "Caddyfile").read_text()
    assert "reverse_proxy 127.0.0.1:5000" in src
    # WebSocket support must not be broken by an aggressive flush interval.
    assert "flush_interval -1" in src, \
        "buffered flushing will delay socket frames"
    assert "max_size 1GB" in src, "captures are large; do not 413 them"


def test_caddyfile_sets_security_headers():
    src = (DEPLOY / "Caddyfile").read_text()
    for header in ("Strict-Transport-Security", "X-Content-Type-Options",
                   "X-Frame-Options", "Content-Security-Policy"):
        assert header in src, f"{header} missing from Caddyfile"
    csp = re.search(r'Content-Security-Policy\s+"([^"]+)"', src).group(1)
    assert "default-src 'self'" in csp
    assert "'unsafe-eval'" not in csp, "three.js does not need unsafe-eval"


def test_caddyfile_rate_limits_login():
    src = (DEPLOY / "Caddyfile").read_text()
    assert "rate_limit" in src and "path /login" in src, \
        "login must be rate limited before it reaches Python"


def test_systemd_unit_hardening():
    src = (DEPLOY / "androidc2.service").read_text()
    for directive in ("NoNewPrivileges=yes", "PrivateTmp=yes",
                      "ProtectSystem=strict", "ProtectHome=yes",
                      "RestrictSUIDSGID=yes", "LockPersonality=yes"):
        assert directive in src, f"{directive} missing"
    assert "Restart=always" in src
    assert "EnvironmentFile=" in src, "secrets must not live in the unit file"
    # No secret may be committed inside the unit.
    assert "C2_API_KEY=" not in src


def test_systemd_unit_pins_single_worker():
    src = (DEPLOY / "androidc2.service").read_text()
    assert "-w " not in src and "--workers" not in src, \
        "do not add workers here; the registry is per-process"
    assert "gunicorn" in src


def test_install_script_never_writes_a_secret_into_the_repo():
    src = (DEPLOY / "install.sh").read_text()
    assert "/etc/androidc2/env" in src
    # The secret file is chmod'd inside the embedded python helper.
    assert "0o600" in src, "env file must be created mode 0600"
    # Secrets must never be echoed in full.
    assert "echo \"$C2_API_KEY\"" not in src
    assert "echo \"$C2_SECRET\"" not in src

def test_app_factory_is_a_valid_wsgi_callable():
    """gunicorn is invoked as `app:create_app()` — that string form must
    produce something that satisfies the WSGI protocol."""
    import importlib

    os.environ.setdefault("C2_OPERATOR_PASSWORD", "deploy-test-pass")
    module = importlib.import_module("app")

    # `gunicorn 'app:create_app()'` calls this with no arguments.
    application = module.create_app(start_background=False)

    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers
        return lambda data: None

    body = b"".join(application({
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/login",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.input": None,
        "wsgi.errors": None,
        "wsgi.version": (1, 0),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "wsgi.url_scheme": "http",
        "SCRIPT_NAME": "",
    }, start_response))

    assert captured.get("status", "").startswith("200")
    assert b"<form" in body, "login page should render through the WSGI app"


def test_gunicorn_worker_class_is_importable_when_installed():
    """Skip when gunicorn is absent — the requirement is what matters."""
    gunicorn = pytest.importorskip("gunicorn.workers.gthread")
    assert gunicorn.ThreadWorker is not None


def test_gunicorn_does_not_duplicate_the_access_log():
    """c2/logs.py already emits one line per request with status, duration,
    ip and request id. A second access log adds nothing but noise."""
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert re.search(r"^accesslog\s*=\s*None", src, re.M), \
        "disable gunicorn access logging; ours is structured"


def test_gunicorn_log_level_matches_the_app_setting():
    src = (DEPLOY / "gunicorn.conf.py").read_text()
    assert "C2_LOG_LEVEL" in src, \
        "gunicorn must respect the same level variable as c2.logs"


def test_logging_settings_are_documented():
    env = (DEPLOY.parent / ".env.example").read_text()
    assert "C2_LOG_FORMAT" in env
    assert "C2_LOG_LEVEL" in env
    assert "C2_TRUST_PROXY_REQUEST_ID" in env
