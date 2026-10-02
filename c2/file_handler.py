"""File storage: hashing, safe naming, category routing."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from datetime import datetime

from config import Config
from database import db
from models import CapturedFile, Device


def safe_write(stream, dest_dir: str, filename: str):
    """Write incoming file stream. Returns (path, size, sha256)."""
    os.makedirs(dest_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_{filename}"
    path = os.path.join(dest_dir, safe_name)

    h = hashlib.sha256()
    size = 0
    with open(path, "wb") as out:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return path, size, h.hexdigest()


def persist_upload(
    device: Device,
    file_stream,
    filename: str,
    category: str = "misc",
    command_id: int | None = None,
    mime_hint: str | None = None,
    metadata: dict | None = None,
) -> CapturedFile:
    cat_dir = os.path.join(Config.UPLOAD_FOLDER, category, device.device_id)
    path, size, sha = safe_write(file_stream, cat_dir, filename)

    mime = mime_hint or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    import json
    cf = CapturedFile(
        device_id=device.id,
        command_id=command_id,
        category=category,
        filename=filename,
        stored_path=path,
        sha256=sha,
        size_bytes=size,
        mime_type=mime,
        captured_at=datetime.utcnow(),
        metadata_json=json.dumps(metadata or {}),
    )
    db.session.add(cf)
    db.session.commit()
    return cf
