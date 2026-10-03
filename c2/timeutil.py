"""UTC clock helper.

``datetime.utcnow()`` is deprecated since Python 3.12 and scheduled for
removal. It also returns a *naive* datetime, which silently breaks any
comparison against an aware value.

``utcnow()`` here is timezone-correct at the source and then converted to
naive UTC, because the SQLite ``DateTime`` columns already hold naive UTC
strings. Going aware in the DB would require a migration of every existing
row and would change the ISO format the dashboard JS consumes.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (what the DB columns expect)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with an explicit offset."""
    return datetime.now(timezone.utc).isoformat()