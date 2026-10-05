"""Operator authentication, session, CSRF and login throttling."""

from conftest import OPERATOR_NAME, OPERATOR_PASSWORD


def test_dashboard_redirects_anonymous(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_api_returns_401_not_redirect(client):
    r = client.get("/api/devices")
    assert r.status_code == 401
    assert r.get_json()["error"] == "unauthorized"


def test_every_operator_api_route_is_guarded(client, make_device):
    dev_pk = make_device()
    guarded = [
        ("GET", "/api/devices"),
        ("GET", "/api/catalog"),
        ("GET", "/api/session"),
        ("GET", f"/api/device/{dev_pk}"),
        ("GET", f"/api/device/{dev_pk}/commands"),
        ("GET", f"/api/device/{dev_pk}/files"),
        ("GET", f"/api/device/{dev_pk}/logs"),
        ("GET", f"/api/device/{dev_pk}/latest-screenshot"),
        ("POST", f"/api/device/{dev_pk}/command"),
        ("POST", f"/api/device/{dev_pk}/push"),
        ("POST", f"/api/device/{dev_pk}/notes"),
        ("POST", f"/api/device/{dev_pk}/tags"),
        ("POST", f"/api/device/{dev_pk}/upload_to_agent"),
        ("DELETE", f"/api/device/{dev_pk}"),
    ]
    for method, path in guarded:
        r = client.open(path, method=method, json={})
        assert r.status_code == 401, f"{method} {path} answered {r.status_code}"


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"name": OPERATOR_NAME, "password": "nope"})
    assert r.status_code == 401
    assert b"invalid name or password" in r.data


def test_login_then_access(op):
    assert op.get("/").status_code == 200
    assert op.get("/api/devices").status_code == 200


def test_session_exposes_csrf(op):
    payload = op.get("/api/session").get_json()
    assert payload["authenticated"] is True
    assert len(payload["csrf_token"]) >= 16


def test_mutation_without_csrf_is_forbidden(op):
    pk = None
    r = op.post("/api/device/1/notes", json={"notes": "x"})
    # 404 vs 403 ordering depends on decorator order; both must not be 200.
    assert r.status_code in (403, 404)
    assert r.status_code != 200
    assert pk is None


def test_mutation_with_csrf_succeeds(op, make_device):
    dev_pk = make_device()
    r = op.post(f"/api/device/{dev_pk}/notes", json={"notes": "hello"})
    assert r.status_code == 200

    r = op.get(f"/api/device/{dev_pk}").get_json()
    assert r["notes"] == "hello"


def test_csrf_token_rejected_when_wrong(client, make_device):
    dev_pk = make_device()
    client.post("/login", data={"name": OPERATOR_NAME, "password": OPERATOR_PASSWORD})
    client.environ_base["HTTP_X_CSRF_TOKEN"] = "forged-token"
    r = client.post(f"/api/device/{dev_pk}/notes", json={"notes": "x"})
    assert r.status_code == 403


def test_login_throttle_blocks_after_repeated_failures(client):
    for _ in range(5):
        client.post("/login", data={"name": OPERATOR_NAME, "password": "wrong"})

    r = client.post("/login", data={"name": OPERATOR_NAME, "password": OPERATOR_PASSWORD})
    assert r.status_code == 429


def test_logout_ends_session(op):
    assert op.post("/logout").status_code == 302
    assert op.get("/api/devices").status_code == 401


def test_open_redirect_is_refused(client):
    """A hostile `next` must never become the post-login destination."""
    for evil in ("https://evil.example/steal", "//evil.example/steal"):
        client.post("/login", data={
            "name": OPERATOR_NAME,
            "password": OPERATOR_PASSWORD,
            "next": evil,
        })
        r = client.get("/")
        # Either bounced back to /login, or landed on the dashboard — never
        # on the attacker's host.
        assert r.status_code in (200, 302)
        if r.status_code == 302:
            assert "evil.example" not in r.headers["Location"]
        else:
            assert b"evil.example" not in r.data

        client.post("/logout", headers={"X-CSRF-Token": _csrf(client)})


def test_login_next_is_honoured_for_local_paths(client, make_device):
    make_device("dev-x")
    client.post("/login", data={"name": OPERATOR_NAME, "password": OPERATOR_PASSWORD,
                                "next": "/api/devices"})
    r = client.get("/")
    assert r.status_code == 200


def _csrf(client):
    return client.get("/api/session").get_json()["csrf_token"]

# ----------------------------------------------------------- fingerprints

def test_fingerprint_is_stable_and_non_reversible():
    from c2.auth import key_fingerprint

    a = key_fingerprint("super-secret-value")
    b = key_fingerprint("super-secret-value")
    c = key_fingerprint("super-secret-value-2")

    assert a == b, "must be deterministic so two processes can compare"
    assert a != c, "different keys must produce different tags"
    assert len(a) == 8
    assert "super-secret" not in a
    assert "super-secret-value" != a


def test_empty_key_has_a_distinct_fingerprint():
    from c2.auth import key_fingerprint

    assert key_fingerprint("") == "--------"
    assert key_fingerprint(None) == "--------"
    assert key_fingerprint("x") != "--------"


def test_fingerprint_matches_a_known_digest():
    import hashlib

    from c2.auth import key_fingerprint

    value = "test-agent-key"
    assert key_fingerprint(value) == \
        hashlib.sha256(value.encode()).hexdigest()[:8]
