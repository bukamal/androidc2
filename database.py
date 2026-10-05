from flask_sqlalchemy import SQLAlchemy

from c2 import logs
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine

# expire_on_commit=False: after commit() the default is to expire every
# attribute, so any object handed back to a caller becomes unreadable outside
# its app context. Functions like c2.operators.create_operator return a row
# for the caller to inspect, so keep instances usable after the commit.
db = SQLAlchemy(session_options={"expire_on_commit": False})


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


# Columns added after the first release, with the DDL type to use when
# back-filling an existing database. `db.create_all()` never ALTERs an
# existing table, so without this a pre-existing c2.db would fail every query
# touching a new column. Kept in step with models.py.
_ADDED_COLUMNS = [
    ("devices", "capabilities", "TEXT"),
    ("devices", "agent_version", "VARCHAR(32)"),
]


def _ensure_columns(app) -> list[str]:
    """Additively migrate a database created by an older build.

    Only ever adds columns; never drops or retypes. Idempotent, so it is safe
    to run on every startup. Alembic is the real answer (see README) — this
    exists so a pull onto an existing c2.db does not crash on boot.
    """
    applied: list[str] = []

    with app.app_context():
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())

        for table, column, ddl_type in _ADDED_COLUMNS:
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column in existing:
                continue
            db.session.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
            )
            applied.append(f"{table}.{column}")

        if applied:
            db.session.commit()

    return applied


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        added = _ensure_columns(app)
        if added:
            logs.log(logs.get_logger("c2.db"), "info", "schema.column_added",
                     columns=added)