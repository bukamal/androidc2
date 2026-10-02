import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("C2_SECRET", "change_this_in_production_9f2a")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "C2_DB_URI",
        f"sqlite:///{os.path.join(BASE_DIR, 'c2.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    MAX_CONTENT_LENGTH = 1024 * 1024 * 1024  # 1 GB per upload
    AGENT_HEARTBEAT_TIMEOUT = 60
    C2_HOST = "0.0.0.0"
    C2_PORT = int(os.environ.get("C2_PORT", 5000))
    API_KEY = os.environ.get("C2_API_KEY", "bukamal")
