"""SQLite database for persisting multi-org calendar data."""

import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "dominion.db"))


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if they don't exist and run migrations."""
    conn = get_db()

    # -- Organizations table --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password TEXT NOT NULL DEFAULT ''
        )
    """)

    # -- Members table (replaces founders) --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY (org_slug) REFERENCES organizations(slug)
        )
    """)

    # -- Calendars table --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT 'Main',
            ical_url TEXT NOT NULL,
            FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
        )
    """)

    # -- Migrate from old founders table if it exists --
    try:
        old_founders = conn.execute("SELECT id, name FROM founders ORDER BY id").fetchall()
        if old_founders:
            # Ensure dominion org exists first
            conn.execute(
                "INSERT OR IGNORE INTO organizations (slug, name, password) VALUES (?, ?, ?)",
                ("dominion", "Dominion", "domcal"),
            )
            for f in old_founders:
                # Check if already migrated
                existing = conn.execute(
                    "SELECT id FROM members WHERE org_slug = 'dominion' AND name = ?",
                    (f["name"],),
                ).fetchone()
                if not existing:
                    cur = conn.execute(
                        "INSERT INTO members (org_slug, name) VALUES ('dominion', ?)",
                        (f["name"],),
                    )
                    new_member_id = cur.lastrowid
                    # Migrate calendars for this founder
                    old_cals = conn.execute(
                        "SELECT label, ical_url FROM calendars WHERE founder_id = ?",
                        (f["id"],),
                    ).fetchall()
                    for cal in old_cals:
                        conn.execute(
                            "INSERT INTO calendars (member_id, label, ical_url) VALUES (?, ?, ?)",
                            (new_member_id, cal["label"], cal["ical_url"]),
                        )
            # Drop old tables after migration
            conn.execute("DROP TABLE IF EXISTS calendars_old_backup")
            # We'll leave old tables for safety, they just won't be used
    except sqlite3.OperationalError:
        pass  # Old tables don't exist, no migration needed

    # If calendars table still has founder_id column, we need to recreate it
    # Check if calendars table has the new schema (member_id)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(calendars)").fetchall()]
    if "founder_id" in cols and "member_id" not in cols:
        # Old schema — rebuild
        conn.execute("ALTER TABLE calendars RENAME TO calendars_old")
        conn.execute("""
            CREATE TABLE calendars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT 'Main',
                ical_url TEXT NOT NULL,
                FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
            )
        """)
        # Migrate calendar data using the member mapping
        rows = conn.execute("SELECT id, founder_id, label, ical_url FROM calendars_old").fetchall()
        for row in rows:
            # Find the member that was migrated from this founder
            old_founder = conn.execute(
                "SELECT name FROM founders WHERE id = ?", (row["founder_id"],)
            ).fetchone()
            if old_founder:
                member = conn.execute(
                    "SELECT id FROM members WHERE org_slug = 'dominion' AND name = ?",
                    (old_founder["name"],),
                ).fetchone()
                if member:
                    conn.execute(
                        "INSERT INTO calendars (member_id, label, ical_url) VALUES (?, ?, ?)",
                        (member["id"], row["label"], row["ical_url"]),
                    )
        conn.execute("DROP TABLE calendars_old")

    # -- Seed organizations --
    orgs = [
        ("dominion", "Dominion", "domcal"),
        ("cross-formed-kids", "Cross Formed Kids", ""),
        ("bully-pulpit", "The Bully Pulpit", ""),
    ]
    for slug, name, password in orgs:
        conn.execute(
            "INSERT OR IGNORE INTO organizations (slug, name, password) VALUES (?, ?, ?)",
            (slug, name, password),
        )

    # Seed default members for dominion if none exist
    existing = conn.execute(
        "SELECT COUNT(*) as cnt FROM members WHERE org_slug = 'dominion'"
    ).fetchone()
    if existing["cnt"] == 0:
        for name in ["Erik", "Brandon", "Coat"]:
            conn.execute(
                "INSERT INTO members (org_slug, name) VALUES ('dominion', ?)",
                (name,),
            )

    conn.commit()
    conn.close()


# ── Organization queries ──

def get_all_orgs():
    """Return all organizations."""
    conn = get_db()
    rows = conn.execute("SELECT slug, name FROM organizations ORDER BY slug").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_org_by_slug(slug):
    """Return a single org by slug, or None."""
    conn = get_db()
    row = conn.execute("SELECT slug, name, password FROM organizations WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Member queries ──

def get_members(org_slug):
    """Return all members for an org with their calendars."""
    conn = get_db()
    members = conn.execute(
        "SELECT id, name FROM members WHERE org_slug = ? ORDER BY id",
        (org_slug,),
    ).fetchall()
    result = []
    for m in members:
        cals = conn.execute(
            "SELECT id, label, ical_url FROM calendars WHERE member_id = ? ORDER BY id",
            (m["id"],),
        ).fetchall()
        result.append({
            "id": m["id"],
            "name": m["name"],
            "calendars": [dict(c) for c in cals],
        })
    conn.close()
    return result


def get_member_ids(org_slug):
    """Return list of member IDs for an org."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id FROM members WHERE org_slug = ? ORDER BY id",
        (org_slug,),
    ).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def add_member(org_slug, name):
    """Add a new member to an org. Returns the new member ID."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO members (org_slug, name) VALUES (?, ?)",
        (org_slug, name),
    )
    member_id = cur.lastrowid
    conn.commit()
    conn.close()
    return member_id


def remove_member(member_id, org_slug):
    """Remove a member and their calendars from an org."""
    conn = get_db()
    # Calendars cascade-delete via FK, but just in case:
    conn.execute("DELETE FROM calendars WHERE member_id = ?", (member_id,))
    conn.execute(
        "DELETE FROM members WHERE id = ? AND org_slug = ?",
        (member_id, org_slug),
    )
    conn.commit()
    conn.close()


def save_member_name(member_id, org_slug, name):
    """Update a member's display name."""
    conn = get_db()
    conn.execute(
        "UPDATE members SET name = ? WHERE id = ? AND org_slug = ?",
        (name, member_id, org_slug),
    )
    conn.commit()
    conn.close()


def member_belongs_to_org(member_id, org_slug):
    """Check if a member ID belongs to the given org."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM members WHERE id = ? AND org_slug = ?",
        (member_id, org_slug),
    ).fetchone()
    conn.close()
    return row is not None


# ── Calendar queries ──

def get_all_ical_urls(org_slug):
    """Return a flat list of all iCal URLs for an org."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.ical_url
        FROM calendars c
        JOIN members m ON c.member_id = m.id
        WHERE m.org_slug = ?
        ORDER BY m.id, c.id
    """, (org_slug,)).fetchall()
    conn.close()
    return [r["ical_url"] for r in rows]


def add_calendar(member_id, label, ical_url):
    """Add a calendar URL for a member."""
    conn = get_db()
    conn.execute(
        "INSERT INTO calendars (member_id, label, ical_url) VALUES (?, ?, ?)",
        (member_id, label, ical_url),
    )
    conn.commit()
    conn.close()


def remove_calendar(calendar_id, member_id):
    """Remove a calendar by ID (scoped to member for safety)."""
    conn = get_db()
    conn.execute(
        "DELETE FROM calendars WHERE id = ? AND member_id = ?",
        (calendar_id, member_id),
    )
    conn.commit()
    conn.close()


# Initialize on import
init_db()
