from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Make SQLite survive concurrent access.

    The app runs Flask-SocketIO in threading mode with a commit per socket
    event plus multi-hundred-megabyte uploads streaming through. Under the
    default rollback journal that deadlocks on "database is locked". WAL lets
    readers run while a writer holds the lock, and busy_timeout makes the
    writers queue instead of raising immediately.
    """
    if type(dbapi_connection).__module__.split(".")[0] != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()