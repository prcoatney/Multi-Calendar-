"""SQLite database for persisting multi-org calendar data."""

import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "dominion.db"))

# Log the actual path being used on startup
print(f"[DB] Using database at: {DB_PATH}")
print(f"[DB] File exists: {os.path.exists(DB_PATH)}")
if os.path.exists(DB_PATH):
    print(f"[DB] File size: {os.path.getsize(DB_PATH)} bytes")


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _backup_to_json(snapshot_tag: str | None = None):
    """Backup all data to a JSON file alongside the db.

    If snapshot_tag is provided, also writes an additional timestamped snapshot
    (e.g. dominion.db.snapshot.20260421T120000.pre-delete.json) that is never
    overwritten — a safety net for destructive ops.
    """
    import json
    from datetime import datetime as _dt
    try:
        conn = get_db()
        orgs = conn.execute("SELECT slug, name, password FROM organizations").fetchall()
        members = conn.execute("SELECT id, org_slug, name, booking_slug, booking_enabled FROM members").fetchall()
        calendars = conn.execute("SELECT id, member_id, label, ical_url FROM calendars").fetchall()
        conn.close()
        backup = {
            "organizations": [dict(o) for o in orgs],
            "members": [dict(m) for m in members],
            "calendars": [dict(c) for c in calendars],
        }
        backup_path = DB_PATH + ".backup.json"
        with open(backup_path, "w") as f:
            json.dump(backup, f, indent=2)
        print(f"[DB] Backup: {len(orgs)} orgs, {len(members)} members, {len(calendars)} calendars")

        if snapshot_tag:
            stamp = _dt.utcnow().strftime("%Y%m%dT%H%M%S")
            # Sanitize the tag — only letters, digits, and dashes.
            safe_tag = ''.join(c if c.isalnum() or c == '-' else '-' for c in snapshot_tag)
            snap_path = f"{DB_PATH}.snapshot.{stamp}.{safe_tag}.json"
            with open(snap_path, "w") as f:
                json.dump(backup, f, indent=2)
            print(f"[DB] Snapshot written: {snap_path}")
    except Exception as e:
        print(f"[DB] Backup failed: {e}")


def _restore_from_json():
    """Restore data from JSON backup if db is empty but backup exists."""
    import json
    backup_path = DB_PATH + ".backup.json"
    if not os.path.exists(backup_path):
        return
    try:
        conn = get_db()
        cal_count = conn.execute("SELECT COUNT(*) FROM calendars").fetchone()[0]
        if cal_count > 0:
            conn.close()
            return  # DB has data, no restore needed

        with open(backup_path) as f:
            backup = json.load(f)

        for o in backup.get("organizations", []):
            conn.execute(
                "INSERT OR IGNORE INTO organizations (slug, name, password) VALUES (?, ?, ?)",
                (o["slug"], o["name"], o.get("password", "")),
            )
        for m in backup.get("members", []):
            conn.execute(
                "INSERT OR REPLACE INTO members (id, org_slug, name, booking_slug, booking_enabled) VALUES (?, ?, ?, ?, ?)",
                (
                    m["id"],
                    m["org_slug"],
                    m["name"],
                    m.get("booking_slug"),
                    m.get("booking_enabled", 0) or 0,
                ),
            )
        for c in backup.get("calendars", []):
            conn.execute(
                "INSERT INTO calendars (member_id, label, ical_url) VALUES (?, ?, ?)",
                (c["member_id"], c["label"], c["ical_url"]),
            )
        conn.commit()
        conn.close()
        print(f"[DB] Restored from backup: {len(backup.get('members', []))} members, {len(backup.get('calendars', []))} calendars")
    except Exception as e:
        print(f"[DB] Restore failed: {e}")


def init_db():
    """Create tables if they don't exist and run migrations."""
    # Safety: backup existing data before any migration
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        try:
            _backup_to_json()
            print("[DB] Pre-migration backup complete")
        except Exception as e:
            print(f"[DB] Pre-migration backup skipped: {e}")

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
            booking_slug TEXT,
            booking_enabled INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (org_slug) REFERENCES organizations(slug)
        )
    """)

    # Migration: add booking_slug + booking_enabled if table pre-dated them.
    member_cols = [row[1] for row in conn.execute("PRAGMA table_info(members)").fetchall()]
    if "booking_slug" not in member_cols:
        conn.execute("ALTER TABLE members ADD COLUMN booking_slug TEXT")
    if "booking_enabled" not in member_cols:
        conn.execute("ALTER TABLE members ADD COLUMN booking_enabled INTEGER NOT NULL DEFAULT 0")

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
    # Step 1: Migrate founder records to members (don't touch calendars yet)
    try:
        old_founders = conn.execute("SELECT id, name FROM founders ORDER BY id").fetchall()
        if old_founders:
            conn.execute(
                "INSERT OR IGNORE INTO organizations (slug, name, password) VALUES (?, ?, ?)",
                ("dominion", "Dominion", "domcal"),
            )
            for f in old_founders:
                existing = conn.execute(
                    "SELECT id FROM members WHERE org_slug = 'dominion' AND name = ?",
                    (f["name"],),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO members (org_slug, name) VALUES ('dominion', ?)",
                        (f["name"],),
                    )
    except sqlite3.OperationalError:
        pass  # Old founders table doesn't exist, no migration needed

    # Step 2: If calendars table has old schema (founder_id), rebuild it
    cols = [row[1] for row in conn.execute("PRAGMA table_info(calendars)").fetchall()]
    if "founder_id" in cols and "member_id" not in cols:
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
        # Map old founder calendars to new member IDs
        try:
            rows = conn.execute("SELECT founder_id, label, ical_url FROM calendars_old").fetchall()
            for row in rows:
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
        except sqlite3.OperationalError:
            pass
        conn.execute("DROP TABLE IF EXISTS calendars_old")

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

    # Seed a bookable "Coat" member in CFK if the CFK org has nobody yet.
    cfk_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM members WHERE org_slug = 'cross-formed-kids'"
    ).fetchone()
    if cfk_count["cnt"] == 0:
        conn.execute(
            "INSERT INTO members (org_slug, name, booking_slug, booking_enabled) VALUES (?, ?, ?, 1)",
            ("cross-formed-kids", "Coat", "coat"),
        )

    conn.commit()
    conn.close()

    # Restore from backup if db was empty (volume got wiped)
    _restore_from_json()

    # Integrity check — log what's in the DB after init
    try:
        conn = get_db()
        org_count = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        member_count = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        cal_count = conn.execute("SELECT COUNT(*) FROM calendars").fetchone()[0]
        conn.close()
        print(f"[DB] Ready: {org_count} orgs, {member_count} members, {cal_count} calendars")
    except Exception as e:
        print(f"[DB] Integrity check failed: {e}")


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
        "SELECT id, name, booking_slug, booking_enabled FROM members WHERE org_slug = ? ORDER BY id",
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
            "booking_slug": m["booking_slug"],
            "booking_enabled": bool(m["booking_enabled"]),
            "calendars": [dict(c) for c in cals],
        })
    conn.close()
    return result


def get_member(member_id, org_slug):
    """Return one member (by id+org), or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, booking_slug, booking_enabled FROM members WHERE id = ? AND org_slug = ?",
        (member_id, org_slug),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "booking_slug": row["booking_slug"],
        "booking_enabled": bool(row["booking_enabled"]),
    }


def get_bookable_member(org_slug, booking_slug):
    """Return the member whose booking_slug matches and who is booking_enabled. None otherwise."""
    conn = get_db()
    row = conn.execute(
        """SELECT id, name, booking_slug, booking_enabled FROM members
           WHERE org_slug = ? AND booking_slug = ? AND booking_enabled = 1""",
        (org_slug, booking_slug),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "booking_slug": row["booking_slug"],
        "booking_enabled": bool(row["booking_enabled"]),
    }


def set_booking_config(member_id, org_slug, booking_slug, booking_enabled):
    """Update a member's booking configuration. booking_slug may be None to clear."""
    slug = (booking_slug or "").strip().lower() or None
    if slug:
        # Normalize to a URL-safe slug.
        slug = ''.join(c if c.isalnum() else '-' for c in slug)
        while '--' in slug:
            slug = slug.replace('--', '-')
        slug = slug.strip('-') or None
    conn = get_db()
    # Ensure the new slug is unique within the org (excluding self).
    if slug:
        conflict = conn.execute(
            """SELECT id FROM members
               WHERE org_slug = ? AND booking_slug = ? AND id != ?""",
            (org_slug, slug, member_id),
        ).fetchone()
        if conflict:
            conn.close()
            raise ValueError(f"Another member in this org already uses booking slug '{slug}'.")
    conn.execute(
        "UPDATE members SET booking_slug = ?, booking_enabled = ? WHERE id = ? AND org_slug = ?",
        (slug, 1 if booking_enabled else 0, member_id, org_slug),
    )
    conn.commit()
    conn.close()
    _backup_to_json()
    return slug


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
    _backup_to_json()
    return member_id


def remove_member(member_id, org_slug, confirm_name):
    """Remove a member and their calendars from an org.

    Safety: confirm_name must match the member's current name exactly.
    Returns True on success, False if the confirmation didn't match.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM members WHERE id = ? AND org_slug = ?",
        (member_id, org_slug),
    ).fetchone()
    if not row:
        conn.close()
        return False
    if (confirm_name or "").strip() != row["name"]:
        conn.close()
        return False
    # Safety: write a timestamped snapshot *before* the destructive delete.
    # That snapshot is never overwritten, so even if someone mistypes later,
    # the pre-delete state survives on disk alongside the db.
    conn.close()
    _backup_to_json(snapshot_tag=f"pre-remove-member-{member_id}")
    conn = get_db()
    conn.execute("DELETE FROM calendars WHERE member_id = ?", (member_id,))
    conn.execute(
        "DELETE FROM members WHERE id = ? AND org_slug = ?",
        (member_id, org_slug),
    )
    conn.commit()
    conn.close()
    _backup_to_json()
    return True


def save_member_name(member_id, org_slug, name):
    """Update a member's display name."""
    conn = get_db()
    conn.execute(
        "UPDATE members SET name = ? WHERE id = ? AND org_slug = ?",
        (name, member_id, org_slug),
    )
    conn.commit()
    conn.close()
    _backup_to_json()


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
    _backup_to_json()


def remove_calendar(calendar_id, member_id, confirm_label):
    """Remove a calendar by ID (scoped to member for safety).

    Safety: confirm_label must match the calendar's current label exactly.
    Returns True on success, False if the confirmation didn't match.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT label FROM calendars WHERE id = ? AND member_id = ?",
        (calendar_id, member_id),
    ).fetchone()
    if not row:
        conn.close()
        return False
    if (confirm_label or "").strip() != row["label"]:
        conn.close()
        return False
    conn.close()
    _backup_to_json(snapshot_tag=f"pre-remove-cal-{calendar_id}")
    conn = get_db()
    conn.execute(
        "DELETE FROM calendars WHERE id = ? AND member_id = ?",
        (calendar_id, member_id),
    )
    conn.commit()
    conn.close()
    _backup_to_json()
    return True


def create_org(name, slug=None, password=""):
    """Create a new organization dynamically. Returns the slug."""
    if not slug:
        slug = ''.join(c if c.isalnum() else '-' for c in name.lower().strip())
        while '--' in slug:
            slug = slug.replace('--', '-')
        slug = slug.strip('-')
    if not slug:
        raise ValueError("Could not generate a valid slug from the name")
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM organizations WHERE slug = ?", (slug,)).fetchone()
    if existing:
        conn.close()
        raise ValueError(f"Organization '{slug}' already exists")
    conn.execute(
        "INSERT INTO organizations (slug, name, password) VALUES (?, ?, ?)",
        (slug, name, password),
    )
    conn.commit()
    conn.close()
    _backup_to_json()
    return slug


def get_member_calendar_map(org_slug):
    """Return calendars with member association for fetch reporting."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.ical_url, m.name as member_name, c.label
        FROM calendars c
        JOIN members m ON c.member_id = m.id
        WHERE m.org_slug = ?
        ORDER BY m.id, c.id
    """, (org_slug,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Initialize on import
init_db()
