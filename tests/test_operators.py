"""Named operators, hashed credentials, and bearer-token machine auth.

The shared-password design could only ever write "operator" into the audit
trail. These tests pin the replacement: real names, scrypt hashes, tokens
stored as digests, and no way for a header to impersonate an operator.
"""

import pytest

from conftest import OPERATOR_NAME, OPERATOR_PASSWORD, login
from c2 import operators as ops
from c2.auth import bearer_token, is_authenticated, token_operator
from database import db
from models import Operator, OperatorAction


def make(app, name, password="a-long-enough-password", role="admin"):
    with app.app_context():
        return ops.create_operator(name, password, role=role)


def token_for(app, name):
    with app.app_context():
        _, token = ops.issue_token(name)
        return token


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("name", ["", None, "1abc", "has space", "a" * 80,
                                  "semi;colon", "sla/sh", "<script>"])
def test_invalid_names_are_refused(app, name):
    with app.app_context():
        with pytest.raises(ops.AuthError):
            ops.create_operator(name, "a-long-enough-password")


@pytest.mark.parametrize("name", ["kanha", "op.1", "a_b-c", "Z9"])
def test_valid_names_are_accepted(app, name):
    row, token = make(app, name)
    assert row.name == name
    assert token


@pytest.mark.parametrize("password", ["", None, "short", "x" * 11])
def test_short_passwords_are_refused(app, password):
    with app.app_context():
        with pytest.raises(ops.AuthError):
            ops.create_operator("someone", password)


def test_twelve_characters_is_accepted(app):
    row, _ = make(app, "twelvechars", password="x" * 12)
    assert row.password_hash


def test_unknown_role_is_refused(app):
    with app.app_context():
        with pytest.raises(ops.AuthError):
            ops.create_operator("rolecheck", "a-long-enough-password",
                                role="superuser")


def test_duplicate_name_is_refused(app):
    make(app, "dupe")
    with app.app_context():
        with pytest.raises(ops.AuthError):
            ops.create_operator("dupe", "a-long-enough-password")


# -------------------------------------------------------------- hashing

def test_password_is_never_stored_in_the_clear(app):
    row, _ = make(app, "hashcheck", password="plaintext-password-123")
    with app.app_context():
        stored = db.session.get(Operator, row.id).password_hash
    assert "plaintext-password-123" not in stored
    assert stored.startswith(("scrypt:", "pbkdf2:", "argon2"))


def test_two_operators_with_the_same_password_get_different_hashes(app):
    a, _ = make(app, "samepw", password="identical-password-99")
    b, _ = make(app, "otherpw", password="identical-password-99")
    assert a.password_hash != b.password_hash, "hashes must be salted"


def test_token_is_never_stored_in_the_clear(app):
    row, token = make(app, "tokcheck")
    with app.app_context():
        stored = db.session.get(Operator, row.id).token_hash
    assert token not in stored
    assert stored == ops.hash_token(token)
    assert len(stored) == 64, "sha256 hex digest"


# -------------------------------------------------------------- verify

def test_correct_credentials_verify(app):
    make(app, "goodop", password="correct-password-1")
    with app.app_context():
        assert ops.verify_password("goodop", "correct-password-1").name == "goodop"


def test_wrong_password_raises(app):
    make(app, "goodop", password="correct-password-1")
    with app.app_context():
        with pytest.raises(ops.AuthError):
            ops.verify_password("goodop", "wrong-password-1")


def test_unknown_name_raises_the_same_error(app):
    """Same message and comparable timing, so the form cannot be used to
    enumerate operator names."""
    make(app, "goodop", password="correct-password-1")
    with app.app_context():
        with pytest.raises(ops.AuthError) as unknown:
            ops.verify_password("nobody", "correct-password-1")
        with pytest.raises(ops.AuthError) as wrong_pw:
            ops.verify_password("goodop", "wrong-password-1")
    assert str(unknown.value) == str(wrong_pw.value)


def test_disabled_operator_cannot_log_in(app):
    make(app, "suspended", password="correct-password-1")
    with app.app_context():
        ops.disable("suspended", True)
        with pytest.raises(ops.AuthError):
            ops.verify_password("suspended", "correct-password-1")


def test_touch_login_records_ip_and_time(app):
    row, _ = make(app, "lastseen", password="correct-password-1")
    assert row.last_login is None
    with app.app_context():
        ops.touch_login(ops.require("lastseen"), "10.0.0.5")
        again = ops.require("lastseen")
        assert again.last_login is not None
        assert again.last_login_ip == "10.0.0.5"


# ---------------------------------------------------------------- tokens

def test_token_authenticates(app, client):
    token = token_for(app, OPERATOR_NAME)
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    assert client.get("/api/devices").status_code == 200


def test_token_reports_its_owner(app, client):
    token = token_for(app, OPERATOR_NAME)
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    body = client.get("/api/operators/me").get_json()
    assert body["name"] == OPERATOR_NAME
    assert body["auth"] == "token"


def test_session_reports_its_owner(client):
    login(client)
    body = client.get("/api/operators/me").get_json()
    assert body["name"] == OPERATOR_NAME
    assert body["auth"] == "session"


def test_bad_token_is_rejected(app, client):
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer not-a-real-token-value"
    assert client.get("/api/devices").status_code == 401


def test_non_bearer_authorization_header_is_ignored(app, client):
    """Only `Bearer` is honoured; Basic/token/etc must not authenticate."""
    token = token_for(app, OPERATOR_NAME)

    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    assert client.get("/api/devices").status_code == 200

    for header in ("Basic abc", "token xyz", "Bearer", "Bearer   ", "bearer"):
        client.environ_base["HTTP_AUTHORIZATION"] = header
        assert client.get("/api/devices").status_code == 401, header


def test_rotating_a_token_invalidates_the_old_one(app, client):
    old = token_for(app, OPERATOR_NAME)
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {old}"
    assert client.get("/api/devices").status_code == 200

    new = token_for(app, OPERATOR_NAME)
    assert new != old

    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {old}"
    assert client.get("/api/devices").status_code == 401, \
        "the previous token must stop working immediately"

    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {new}"
    assert client.get("/api/devices").status_code == 200


def test_revoking_a_token_disables_token_auth(app, client):
    token = token_for(app, OPERATOR_NAME)
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    assert client.get("/api/devices").status_code == 200

    with app.app_context():
        ops.revoke_token(OPERATOR_NAME)

    assert client.get("/api/devices").status_code == 401


def test_revoking_a_token_keeps_password_login(app, client):
    with app.app_context():
        ops.revoke_token(OPERATOR_NAME)
    assert client.post("/login", data={"name": OPERATOR_NAME,
                                       "password": OPERATOR_PASSWORD}
                       ).status_code == 302


def test_disabling_an_operator_kills_their_token(app, client):
    token = token_for(app, OPERATOR_NAME)
    with app.app_context():
        ops.disable(OPERATOR_NAME, True)
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    assert client.get("/api/devices").status_code == 401


def test_no_header_can_claim_an_identity(app, client):
    """A caller must not be able to say who they are in a header."""
    client.environ_base["HTTP_X_OPERATOR_NAME"] = "someone-else"
    client.environ_base["HTTP_X_OPERATOR"] = "admin"
    client.environ_base["HTTP_X_USER"] = "root"
    assert client.get("/api/devices").status_code == 401


def test_api_operators_never_returns_a_token(app, client):
    login(client)
    for entry in client.get("/api/operators").get_json():
        assert "token" not in entry
        assert "password_hash" not in entry
        assert "token_hash" not in entry
        assert entry["token_fp"] is None or len(entry["token_fp"]) == 8


# ------------------------------------------------------------ login flow

def test_login_requires_both_fields(client):
    assert client.post("/login", data={"name": OPERATOR_NAME}).status_code == 401
    assert client.post("/login", data={"password": OPERATOR_PASSWORD}).status_code == 401


def test_login_rejects_unknown_operator(client):
    r = client.post("/login", data={"name": "ghost",
                                    "password": OPERATOR_PASSWORD})
    assert r.status_code == 401
    assert b"invalid name or password" in r.data


def test_login_form_carries_the_name_back_on_failure(client):
    r = client.post("/login", data={"name": "kanha", "password": "wrong-one"})
    assert b'value="kanha"' in r.data


def test_login_records_the_operator_name_in_the_audit(client):
    login(client)
    with client.application.app_context():
        entry = (OperatorAction.query
                 .filter_by(action="auth.login_ok").first())
        assert entry.actor == OPERATOR_NAME


def test_failed_login_records_the_attempted_name(client):
    client.post("/login", data={"name": "kanha", "password": "wrong-one"})
    with client.application.app_context():
        entry = (OperatorAction.query
                 .filter_by(action="auth.login_fail").first())
        assert entry.detail()["name"] == "kanha"
        assert entry.outcome == "denied"


def test_two_operators_get_distinct_audit_attribution(app, client, make_device):
    make(app, "second", password="second-password-1")
    dev_id = make_device("attribution-dev")

    login(client)
    client.post(f"/api/device/{dev_id}/notes", json={"notes": "by tester"})

    client.post("/logout", headers={"X-CSRF-Token":
                                   client.get("/api/session").get_json()["csrf_token"]})
    client.post("/login", data={"name": "second", "password": "second-password-1"})
    client.environ_base["HTTP_X_CSRF_TOKEN"] = \
        client.get("/api/session").get_json()["csrf_token"]
    client.post(f"/api/device/{dev_id}/notes", json={"notes": "by second"})

    with app.app_context():
        actors = {e.actor for e in OperatorAction.query
                  .filter_by(action="device.notes").all()}
    assert actors == {OPERATOR_NAME, "second"}, \
        "two people must be distinguishable in the audit trail"


# ------------------------------------------------------------- throttle

def test_throttle_is_bucketed_per_operator_name(app, client):
    """Five wrong attempts for one account must not lock out another."""
    from c2 import auth
    make(app, "noisy", password="noisy-password-1")
    auth._FAILURES.clear()
    for _ in range(5):
        client.post("/login", data={"name": "noisy", "password": "wrong-one"})

    r = client.post("/login", data={"name": OPERATOR_NAME,
                                    "password": OPERATOR_PASSWORD})
    assert r.status_code == 302, "a different operator must stay usable"


def test_throttle_covers_a_legitimate_operator(client):
    for _ in range(5):
        client.post("/login", data={"name": OPERATOR_NAME, "password": "wrong-one"})
    r = client.post("/login", data={"name": OPERATOR_NAME,
                                    "password": OPERATOR_PASSWORD})
    assert r.status_code == 429


def test_ip_bucket_has_a_larger_budget(client):
    """Spraying many different names from one host must eventually block the
    host itself, not just each account."""
    from c2 import auth

    for i in range(auth.IP_MAX_ATTEMPTS + 1):
        client.post("/login", data={"name": f"sprayed-{i}",
                                    "password": "wrong-one"})
    r = client.post("/login", data={"name": OPERATOR_NAME,
                                    "password": OPERATOR_PASSWORD})
    assert r.status_code == 429


def test_name_bucket_trips_before_the_ip_bucket(client):
    """One account mistyped repeatedly is blocked long before the host is."""
    from c2 import auth

    assert auth.NAME_MAX_ATTEMPTS < auth.IP_MAX_ATTEMPTS


# ---------------------------------------------------------- management

def test_token_rotation_endpoint(app, client, make_device):
    make_device("rotate-dev")
    login(client)
    body = client.post(f"/api/operators/{OPERATOR_NAME}/token").get_json()
    assert body["token"]
    assert body["token_fp"]

    # The old token now fails, the new one works.
    fresh = client.post("/login", data={"name": OPERATOR_NAME,
                                        "password": OPERATOR_PASSWORD})
    assert fresh.status_code == 302


def test_revoke_endpoint(app, client):
    login(client)
    body = client.delete(f"/api/operators/{OPERATOR_NAME}/token").get_json()
    assert body["has_token"] is False


def test_operator_management_is_audited(app, client, make_device):
    make_device("mgmt-dev")
    login(client)
    client.post("/api/operators", json={"name": "created-via-api",
                                        "password": "api-password-12345"})
    with app.app_context():
        assert OperatorAction.query.filter_by(action="operator.create").first()
    client.post("/api/operators/created-via-api/token")
    with app.app_context():
        assert OperatorAction.query.filter_by(action="operator.token_rotate").first()


def test_creating_an_operator_needs_auth(client):
    r = client.post("/api/operators", json={"name": "sneaky",
                                            "password": "sneaky-password-1"})
    assert r.status_code == 401


def test_creating_an_operator_needs_csrf(client):
    login(client, csrf=False)
    r = client.post("/api/operators", json={"name": "nocorsf",
                                            "password": "nocorsf-password-1"})
    assert r.status_code == 403


def test_cli_group_is_registered(app):
    assert "operators" in app.cli.commands


def test_bootstrap_is_idempotent(app):
    with app.app_context():
        before = Operator.query.count()
        action, row = ops.ensure_bootstrap("another", "another-password-1")
        assert action == "existing"
        assert row is None
        assert Operator.query.count() == before


def test_bootstrap_skips_without_a_password(app):
    with app.app_context():
        for row in Operator.query.all():
            db.session.delete(row)
        db.session.commit()

        action, row = ops.ensure_bootstrap("nobody", None)
        assert action == "skipped"
        assert Operator.query.count() == 0


def test_environment_never_overrides_an_existing_operator(app):
    """Rotating C2_OPERATOR_PASSWORD must not silently change a live account."""
    with app.app_context():
        original = db.session.get(Operator, ops.require(OPERATOR_NAME).id)
        before = original.password_hash

        action, _ = ops.ensure_bootstrap(OPERATOR_NAME, "a-completely-new-password")
        assert action == "existing"
        assert db.session.get(Operator, original.id).password_hash == before


def test_password_change_takes_effect(app, client):
    with app.app_context():
        ops.set_password(OPERATOR_NAME, "brand-new-password-9")
    assert client.post("/login", data={"name": OPERATOR_NAME,
                                       "password": OPERATOR_PASSWORD}).status_code == 401
    assert client.post("/login", data={"name": OPERATOR_NAME,
                                       "password": "brand-new-password-9"}).status_code == 302
    with app.app_context():
        ops.set_password(OPERATOR_NAME, OPERATOR_PASSWORD)


def test_deleting_an_operator_keeps_audit_history(app, client, make_device):
    make(app, "temporary", password="temporary-password-1")
    dev_id = make_device("audit-history-dev")
    login(client)
    client.post(f"/api/device/{dev_id}/notes", json={"notes": "before deletion"})

    with app.app_context():
        ops.delete("temporary")
        assert Operator.query.filter_by(name="temporary").first() is None
        assert OperatorAction.query.filter_by(action="device.notes").count() >= 1


def test_token_caller_can_mutate_without_csrf(app, client, make_device):
    """A bearer token is not an ambient credential, so CSRF does not apply.
    The Telegram bridge queues commands, so this has to work."""
    dev_id = make_device("token-mutation")
    token = token_for(app, OPERATOR_NAME)

    machine = app.test_client()
    machine.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"

    r = machine.post(f"/api/device/{dev_id}/command",
                     json={"type": "device_info"})
    assert r.status_code == 200, "token callers must be able to queue commands"

    r = machine.post(f"/api/device/{dev_id}/notes", json={"notes": "via token"})
    assert r.status_code == 200


def test_token_caller_is_audited_under_its_own_name(app, client, make_device):
    dev_id = make_device("token-attribution")
    token = token_for(app, OPERATOR_NAME)

    machine = app.test_client()
    machine.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    machine.post(f"/api/device/{dev_id}/notes", json={"notes": "robot was here"})

    with app.app_context():
        entries = OperatorAction.query.filter_by(action="device.notes").all()
        assert len(entries) == 1
        assert entries[0].actor == OPERATOR_NAME, \
            "a token caller is still a named operator"


def test_token_auth_does_not_open_a_csrf_hole(app, client, make_device):
    """A cross-site form cannot set an Authorization header, so there is
    nothing for an attacker to ride. Confirm the token is genuinely required."""
    dev_id = make_device("no-token")
    anonymous = app.test_client()
    anonymous.environ_base["HTTP_AUTHORIZATION"] = ""     # as a browser would send
    r = anonymous.post(f"/api/device/{dev_id}/notes", json={"notes": "x"})
    assert r.status_code == 401


def test_two_tokens_are_audited_separately(app, make_device):
    """A person and a machine on the same account are distinguishable."""
    dev_id = make_device("mixed-clients")
    _, bridge_token = ops.issue_token(OPERATOR_NAME)

    with app.app_context():
        row = ops.require(OPERATOR_NAME)
        assert row.name == OPERATOR_NAME

    machine = app.test_client()
    machine.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {bridge_token}"
    machine.post(f"/api/device/{dev_id}/notes", json={"notes": "bot"})

    with app.app_context():
        assert OperatorAction.query.filter_by(
            action="device.notes").one().actor == OPERATOR_NAME
