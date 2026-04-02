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
            name TEXT NOT NULL,
            ical_url TEXT DEFAULT ''
        )
    """)
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
    """Return all founders with their iCal URLs."""
    conn = get_db()
    rows = conn.execute("SELECT id, name, ical_url FROM founders ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_founder_url(founder_id, ical_url):
    """Save or update a founder's iCal URL."""
    conn = get_db()
    conn.execute(
        "UPDATE founders SET ical_url = ? WHERE id = ?",
        (ical_url, founder_id),
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
