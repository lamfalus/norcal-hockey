"""SQLite access: connection setup, schema install, and small helpers."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now() -> str:
    """UTC timestamp string used for every ``*_at`` column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the database, creating and migrating it when needed."""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        try:
            # WAL keeps the nightly writer from blocking a concurrent read.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            init(conn)
        except Exception:
            # Never leave the file open on a failed open; the caller has no
            # handle to close and the database would stay locked.
            conn.close()
            raise
    return conn


#: Columns added after a version shipped. ``CREATE TABLE IF NOT EXISTS`` will
#: not alter an existing table, so new columns are applied here instead --
#: letting a mid-season database be upgraded in place without a rebuild.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, definition)
    ("games", "game_class", "TEXT"),
    ("divisions", "gender", "TEXT"),
    ("teams", "gender", "TEXT"),
    ("divisions", "league_id", "INTEGER NOT NULL DEFAULT 3"),
    ("teams", "league_id", "INTEGER DEFAULT 3"),
    ("games", "league_id", "INTEGER DEFAULT 3"),
    ("leagues", "kind", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("leagues", "span_days", "INTEGER"),
    ("leagues", "note", "TEXT"),
]


def init(conn: sqlite3.Connection) -> None:
    """Install the schema and record its version. Safe to call repeatedly."""
    # Read the stored version before the schema script runs: on a brand-new
    # database the meta table does not exist yet, and on an existing one the
    # script would otherwise tell us nothing about what version it was.
    existing = _stored_version(conn)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _add_missing_columns(conn)

    if existing is not None and existing > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{existing} is newer than this code (v{SCHEMA_VERSION}); "
            "upgrade norcalstats before running"
        )
    if existing is not None and existing < SCHEMA_VERSION:
        log.info("upgrading database schema v%d -> v%d", existing, SCHEMA_VERSION)
        _migrate(conn, existing)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def _stored_version(conn: sqlite3.Connection) -> Optional[int]:
    """The schema version recorded in the database, or None if it is new."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no meta table yet: brand-new database
    try:
        return int(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Upgrade an existing database in place.

    Review decisions and manual name overrides live in the database, so it is
    never acceptable to tell someone to delete and rebuild. Column additions are
    handled by ADDED_COLUMNS; anything that changes a constraint is done here.
    """
    if from_version < 2:
        _migrate_v2_league_scoped_divisions(conn)


def _migrate_v2_league_scoped_divisions(conn: sqlite3.Connection) -> None:
    """v1 -> v2: divisions become unique per (season, league, name).

    Norcal and SCAHA both run a division called "12U A", so the old
    ``UNIQUE(season_id, name)`` would merge them. SQLite cannot alter a
    constraint, so the table is rebuilt and its rows copied across.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'divisions'"
    ).fetchone()
    if not row or "league_id" in (row["sql"] or "") and "season_id, league_id, name" in (row["sql"] or ""):
        return  # already league-scoped

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("""
            CREATE TABLE divisions_v2 (
                division_id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id   INTEGER NOT NULL REFERENCES seasons(season_id),
                league_id   INTEGER NOT NULL DEFAULT 3 REFERENCES leagues(league_id),
                name        TEXT NOT NULL,
                level       INTEGER,
                conf        INTEGER,
                sort_order  INTEGER,
                gender      TEXT,
                UNIQUE (season_id, league_id, name)
            )
        """)
        # division_id values are preserved, so every games.division_id and
        # teams.division_id reference stays valid.
        conn.execute("""
            INSERT INTO divisions_v2
                (division_id, season_id, league_id, name, level, conf, sort_order, gender)
            SELECT division_id, season_id, COALESCE(league_id, 3), name,
                   level, conf, sort_order, gender
              FROM divisions
        """)
        conn.execute("DROP TABLE divisions")
        conn.execute("ALTER TABLE divisions_v2 RENAME TO divisions")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in ADDED_COLUMNS:
        try:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert(
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    *,
    keys: Sequence[str],
    update: Optional[Sequence[str]] = None,
) -> None:
    """INSERT ... ON CONFLICT(keys) DO UPDATE for a single row.

    ``update`` defaults to every non-key column. Pass an explicit list to keep
    columns that should only ever be written once (e.g. ``first_seen_at``).
    """
    cols = list(row)
    updatable = list(update) if update is not None else [c for c in cols if c not in keys]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    if updatable:
        assignments = ", ".join(f"{c} = excluded.{c}" for c in updatable)
        sql += f" ON CONFLICT({', '.join(keys)}) DO UPDATE SET {assignments}"
    else:
        sql += f" ON CONFLICT({', '.join(keys)}) DO NOTHING"
    conn.execute(sql, [row[c] for c in cols])


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence[Any]]) -> None:
    rows = list(rows)
    if rows:
        conn.executemany(sql, rows)


def scalar(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for the main tables -- used by ``status`` and run summaries."""
    tables = [
        "seasons", "divisions", "teams", "games", "game_rosters",
        "goals", "penalties", "players", "player_game_stats", "anomalies",
    ]
    out: dict[str, int] = {}
    for table in tables:
        try:
            out[table] = scalar(conn, f"SELECT COUNT(*) FROM {table}") or 0
        except sqlite3.Error:
            out[table] = 0
    return out


class Run:
    """Context manager recording one pipeline run in the ``runs`` table."""

    def __init__(self, conn: sqlite3.Connection, mode: str, seasons: str = "") -> None:
        self.conn = conn
        self.mode = mode
        self.seasons = seasons
        self.run_id: Optional[int] = None
        self.pages = 0
        self.games_seen = 0
        self.games_parsed = 0
        self.errors = 0
        self.note = ""

    def __enter__(self) -> "Run":
        cur = self.conn.execute(
            "INSERT INTO runs(mode, started_at, seasons) VALUES (?, ?, ?)",
            (self.mode, now(), self.seasons),
        )
        self.run_id = cur.lastrowid
        self.conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.errors += 1
            self.note = (self.note + f" | aborted: {exc}").strip(" |")
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, pages = ?, games_seen = ?, "
            "games_parsed = ?, errors = ?, note = ? WHERE run_id = ?",
            (now(), self.pages, self.games_seen, self.games_parsed,
             self.errors, self.note[:500], self.run_id),
        )
        self.conn.commit()
