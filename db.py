"""SQLite database for persisting founder iCal URLs."""

import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "dominion.db"))


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS founders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            founder_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'Main',
            ical_url TEXT NOT NULL,
            FOREIGN KEY (founder_id) REFERENCES founders(id)
        )
    """)
    # Migrate: if old ical_url column exists on founders, move data to calendars
    try:
        rows = conn.execute("SELECT id, ical_url FROM founders WHERE ical_url != ''").fetchall()
        for row in rows:
            existing = conn.execute(
                "SELECT 1 FROM calendars WHERE founder_id = ? AND ical_url = ?",
                (row["id"], row["ical_url"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO calendars (founder_id, label, ical_url) VALUES (?, 'Main', ?)",
                    (row["id"], row["ical_url"]),
                )
    except sqlite3.OperationalError:
        pass  # Column doesn't exist, no migration needed

    # Seed the 3 founders if they don't exist
    defaults = [
        ("founder1", "Erik"),
        ("founder2", "Brandon"),
        ("founder3", "Coat"),
    ]
    for fid, name in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO founders (id, name) VALUES (?, ?)",
            (fid, name),
        )
    conn.commit()
    conn.close()


def get_founders():
    """Return all founders with their calendars."""
    conn = get_db()
    founders = conn.execute("SELECT id, name FROM founders ORDER BY id").fetchall()
    result = []
    for f in founders:
        cals = conn.execute(
            "SELECT id, label, ical_url FROM calendars WHERE founder_id = ? ORDER BY id",
            (f["id"],),
        ).fetchall()
        result.append({
            "id": f["id"],
            "name": f["name"],
            "calendars": [dict(c) for c in cals],
        })
    conn.close()
    return result


def get_all_ical_urls():
    """Return a flat list of all iCal URLs across all founders."""
    conn = get_db()
    rows = conn.execute("SELECT ical_url FROM calendars ORDER BY founder_id, id").fetchall()
    conn.close()
    return [r["ical_url"] for r in rows]


def add_calendar(founder_id, label, ical_url):
    """Add a calendar URL for a founder."""
    conn = get_db()
    conn.execute(
        "INSERT INTO calendars (founder_id, label, ical_url) VALUES (?, ?, ?)",
        (founder_id, label, ical_url),
    )
    conn.commit()
    conn.close()


def remove_calendar(calendar_id, founder_id):
    """Remove a calendar by ID (scoped to founder for safety)."""
    conn = get_db()
    conn.execute(
        "DELETE FROM calendars WHERE id = ? AND founder_id = ?",
        (calendar_id, founder_id),
    )
    conn.commit()
    conn.close()


def save_founder_name(founder_id, name):
    """Update a founder's display name."""
    conn = get_db()
    conn.execute(
        "UPDATE founders SET name = ? WHERE id = ?",
        (name, founder_id),
    )
    conn.commit()
    conn.close()


# Initialize on import
init_db()
