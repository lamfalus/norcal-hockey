"""SQLite access: connection setup, schema install, and small helpers."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import names as N

log = logging.getLogger(__name__)

SCHEMA_VERSION = 10
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
    ("leagues", "parent_id", "INTEGER"),
    ("leagues", "stage", "TEXT"),
    # The PDF scorecard: fetch/parse bookkeeping on the game, and real
    # per-goalie shots/saves on the derived stat line.
    ("games", "scorecard_sha", "TEXT"),
    ("games", "scorecard_at", "TEXT"),
    ("games", "scorecard_error", "TEXT"),
    ("games", "scorecard_parse_version", "INTEGER"),
    ("player_game_stats", "shots_faced", "INTEGER"),
    ("player_game_stats", "saves", "INTEGER"),
    # When a completed game was announced to the Telegram channel, so it is
    # announced exactly once.
    ("games", "notified_at", "TEXT"),
]


def init(conn: sqlite3.Connection) -> None:
    """Install the schema and record its version. Safe to call repeatedly."""
    # Read the stored version before the schema script runs: on a brand-new
    # database the meta table does not exist yet, and on an existing one the
    # script would otherwise tell us nothing about what version it was.
    existing = _stored_version(conn)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _add_missing_columns(conn)
    # After the columns, never inside the schema script: parent_id and stage
    # arrive through ADDED_COLUMNS, so a database made before they existed
    # cannot be updated until _add_missing_columns has run.
    _seed_league_rollup(conn)

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


#: Which league each round of a multi-part competition is shown as, and what
#: to call the round. CAHA runs its tier competition under four ids on the
#: site; they are collected separately because that is how the pages are
#: fetched, and presented as one because that is what they are.
LEAGUE_ROLLUP: dict[int, tuple[int, str]] = {
    16: (5, "Preseason"),
    17: (5, "Weekends"),
    24: (5, "Playoffs"),
}


def _seed_league_rollup(conn: sqlite3.Connection) -> None:
    """Point each round at its parent. Idempotent, and safe on a new database."""
    for league_id, (parent_id, stage) in LEAGUE_ROLLUP.items():
        conn.execute(
            "UPDATE leagues SET parent_id = ?, stage = ? WHERE league_id = ?",
            (parent_id, stage, league_id))


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
    if from_version < 3:
        _migrate_v3_relabel_placeholder_rosters(conn)
    if from_version < 4:
        _migrate_v4_rebuild_clubs(conn)
    if from_version < 5:
        _migrate_v5_drop_foreign_scoresheets(conn)
    if from_version < 6:
        _migrate_v6_drop_out_of_state_championships(conn)
    # v7 adds the goalie_records table and scorecard bookkeeping columns, all
    # additive -- CREATE TABLE IF NOT EXISTS and ADDED_COLUMNS handle it, so
    # there is nothing to do here beyond record the version.
    if from_version < 8:
        _migrate_v8_rekey_goalie_records(conn)
    if from_version < 9:
        _migrate_v9_collect_festival(conn)
    if from_version < 10:
        _migrate_v10_mark_existing_notified(conn)


#: Leagues dropped in v6, and why. Both are end-of-season championships drawn
#: from a national field: 47 of the clubs playing in them are Alaska, Boston,
#: Buffalo, Chicago, Cleveland, Colorado and the like. A California team turning
#: up in one is incidental, and there is no season-long record to keep.
DROPPED_LEAGUES = (37, 38)


def purge_leagues(conn: sqlite3.Connection, league_ids: Sequence[int]) -> dict[str, int]:
    """Remove every trace of a league. Returns what went, by table.

    Deleting the games is the easy half: rosters, goals, penalties, period
    scores, goalie stints, shot marks and per-game stats all cascade from
    ``games``. Four things do not, and are the reason this is a function rather
    than one DELETE:

    ``teams``
        A team that also plays a real season keeps its row and is re-pointed at
        the best league it has left. Only a team whose entire record here was
        the purged competition is removed, along with its standings row and the
        site's published stat rows, neither of which cascades.

    ``player_team_seasons``
        Keyed on team, not on game, so it survives the cascade.

    ``players``
        Nothing else in this codebase ever deletes a player, which is why the
        database still carries orphans from earlier work. Leaving 1,258 more
        would be the same mistake, so the players who were only ever here for
        this competition go too -- identified before the delete, then confirmed
        to have nothing left after it. A player named in a hand-made override
        or split is kept regardless: that decision is not ours to discard.

    ``review_items``
        Left alone. They are keyed by an opaque fingerprint and represent
        questions that were genuinely asked; a stale one is answerable, an
        auto-deleted one is not.
    """
    if not league_ids:
        return {}
    conn.execute("PRAGMA foreign_keys = ON")
    marks = ",".join("?" for _ in league_ids)
    ids = tuple(league_ids)
    gone: dict[str, int] = {}

    # Everyone attached to this competition, asked before any of it disappears.
    #
    # Three ways in, and the third is not optional: a player whose games were
    # never scoresheeted has no stat line and no roster row, and is tied to the
    # league only by the team they were listed on.
    candidates = {
        r[0] for r in conn.execute(f"""
            SELECT DISTINCT s.player_id
              FROM player_game_stats s JOIN games g ON g.game_id = s.game_id
             WHERE g.league_id IN ({marks})
             UNION
            SELECT DISTINCT r.player_id
              FROM game_rosters r JOIN games g ON g.game_id = r.game_id
             WHERE g.league_id IN ({marks}) AND r.player_id IS NOT NULL
             UNION
            SELECT DISTINCT pts.player_id
              FROM player_team_seasons pts
              JOIN teams t ON t.team_id = pts.team_id
                          AND t.season_id = pts.season_id
             WHERE t.league_id IN ({marks})
        """, ids + ids + ids)
    }

    gone["games"] = conn.execute(
        f"SELECT count(*) FROM games WHERE league_id IN ({marks})", ids).fetchone()[0]
    conn.execute(f"DELETE FROM games WHERE league_id IN ({marks})", ids)

    # Teams: re-point the ones with a season elsewhere, remove the rest.
    removed_teams = 0
    for row in conn.execute(
            f"SELECT team_id, season_id FROM teams WHERE league_id IN ({marks})",
            ids).fetchall():
        keep = conn.execute(f"""
            SELECT tl.league_id, tl.division_id, tl.name
              FROM team_leagues tl JOIN leagues l ON l.league_id = tl.league_id
             WHERE tl.team_id = ? AND tl.season_id = ?
               AND tl.league_id NOT IN ({marks}) AND l.kind = 'season'
             ORDER BY l.priority LIMIT 1
        """, (row["team_id"], row["season_id"], *ids)).fetchone()
        if keep:
            conn.execute(
                "UPDATE teams SET league_id = ?, division_id = ?, name = ?"
                " WHERE team_id = ? AND season_id = ?",
                (keep["league_id"], keep["division_id"], keep["name"],
                 row["team_id"], row["season_id"]))
            continue
        for table in ("standings", "team_stat_rows", "player_team_seasons", "teams"):
            conn.execute(
                f"DELETE FROM {table} WHERE team_id = ? AND season_id = ?",
                (row["team_id"], row["season_id"]))
        removed_teams += 1
    gone["teams"] = removed_teams

    for table in ("team_leagues", "league_seasons", "divisions"):
        gone[table] = conn.execute(
            f"SELECT count(*) FROM {table} WHERE league_id IN ({marks})", ids).fetchone()[0]
        conn.execute(f"DELETE FROM {table} WHERE league_id IN ({marks})", ids)

    # Players with nothing left anywhere, and no hand-made decision naming them.
    # An override names a player_id outright, so it needs no translation. A
    # split names the *key* it was decided under -- "ryan smith" -- while the
    # children it produced carry the person inside their canonical name,
    # "ryan smith#a". Comparing those two directly never matches, which is how
    # the promise above came to be broken for exactly the half that a reviewer
    # had to think hardest about.
    protected = {
        r[0] for r in conn.execute(
            "SELECT player_id FROM player_overrides WHERE player_id IS NOT NULL")
    }
    split_keys = {r[0] for r in conn.execute("SELECT DISTINCT name FROM player_splits")}
    if split_keys:
        for r in conn.execute("SELECT player_id, canonical_name FROM players"):
            if N.canonical_person(r["canonical_name"])[0] in split_keys:
                protected.add(r["player_id"])
    removed_players = 0
    for player_id in candidates:
        if player_id is None or player_id in protected:
            continue
        still_here = conn.execute("""
            SELECT EXISTS(SELECT 1 FROM player_game_stats WHERE player_id = ?)
                OR EXISTS(SELECT 1 FROM player_team_seasons WHERE player_id = ?)
                OR EXISTS(SELECT 1 FROM game_rosters WHERE player_id = ?)
        """, (player_id, player_id, player_id)).fetchone()[0]
        if still_here:
            continue
        conn.execute("DELETE FROM player_name_map WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM player_names WHERE player_id = ?", (player_id,))
        conn.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
        removed_players += 1
    gone["players"] = removed_players

    conn.commit()
    return gone


def _migrate_v6_drop_out_of_state_championships(conn: sqlite3.Connection) -> None:
    """v5 -> v6: remove Pacific District and USAH Nationals, and roll CAHA up.

    Two unrelated corrections that both live in the leagues table.

    The championships go entirely. They are the playoff progression for tier
    teams, which is why they were collected, but the field is drawn from the
    whole country -- 47 of the clubs in them are Alaska, Boston, Buffalo,
    Chicago, Cleveland, Colorado and the like -- so a California team reaching
    one is incidental and there is no season-long record here to keep. Since
    they will not be collected again, the rows they left behind are not history
    worth carrying: ``purge_leagues`` takes them out completely, players and
    all.

    The CAHA roll-up changes nothing about what is collected. The site runs the
    tier competition under four ids and they stay four ids here, because that is
    how the pages are fetched -- but the three rounds now point at the main
    league, so a reader picking a league picks CAHA once, and which round a game
    belonged to rides on the game as a label.
    """
    _seed_league_rollup(conn)

    marks = ",".join("?" for _ in DROPPED_LEAGUES)
    conn.execute(
        f"UPDATE leagues SET kind = 'excluded', priority = 207 + league_id - 37"
        f" WHERE league_id IN ({marks})", DROPPED_LEAGUES)
    conn.commit()

    gone = purge_leagues(conn, DROPPED_LEAGUES)
    if gone.get("games"):
        log.info("dropped leagues %s: %s",
                 ", ".join(str(l) for l in DROPPED_LEAGUES),
                 ", ".join(f"{n} {table}" for table, n in gone.items() if n))


def _migrate_v8_rekey_goalie_records(conn: sqlite3.Connection) -> None:
    """v7 -> v8: re-key goalie_records on the goalie's order, not jersey.

    The v7 table keyed on (game_id, side, jersey), but an old scorecard often
    prints no jersey number, so two goalies who split a game both arrive with a
    blank jersey and collide. The table is rebuilt keyed on (game_id, side, seq)
    and emptied; setting ``scorecard_parse_version`` back to NULL marks every
    scorecard already fetched for a re-parse from the archive -- no refetch --
    so the table repopulates with the new keys on the next run.
    """
    conn.execute("DROP TABLE IF EXISTS goalie_records")
    conn.execute("""
        CREATE TABLE goalie_records (
            game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
            side      TEXT NOT NULL,
            seq       INTEGER NOT NULL,
            jersey    TEXT,
            shots     INTEGER,
            saves     INTEGER,
            goals_against INTEGER,
            by_period TEXT,
            player_id INTEGER REFERENCES players(player_id),
            PRIMARY KEY (game_id, side, seq)
        )
    """)
    conn.execute(
        "UPDATE games SET scorecard_parse_version = NULL WHERE scorecard_at IS NOT NULL")
    conn.commit()


def _migrate_v9_collect_festival(conn: sqlite3.Connection) -> None:
    """v8 -> v9: collect the California Dreamin' Labor Day Festival (league 41).

    A SoCal preseason tournament whose field is almost all California clubs
    already tracked in SCAHA and CAHA. The automatic classifier reads a
    tournament as an 'event' and skips it, and the ``INSERT OR IGNORE`` seed in
    schema.sql cannot change a row an existing database already discovered -- so
    the decision is applied here for a database that met the league before this
    shipped. Priority 230 keeps it below every season league, so it never owns a
    team's name or division, and it is left out of ``clubs.HOME_LEAGUES`` so an
    out-of-state entrant stays a visitor. A row somebody has since excluded by
    hand is left excluded.
    """
    changed = conn.execute(
        "UPDATE leagues SET kind = 'season', priority = 230 "
        "WHERE league_id = 41 AND kind <> 'excluded'").rowcount
    if not changed:
        # Not discovered yet (or its row is missing): seed it so the next crawl
        # collects it. An existing 'excluded' row is left untouched.
        conn.execute(
            "INSERT OR IGNORE INTO leagues(league_id, name, priority, kind, note) "
            "VALUES (41, 'California Dreamin Labor Day Festival', 230, 'season', "
            "'SoCal Labor Day tournament, collected by hand')")
    conn.commit()


def _migrate_v10_mark_existing_notified(conn: sqlite3.Connection) -> None:
    """v9 -> v10: stamp every already-final game as already announced.

    The Telegram notifier fires for completed games whose ``notified_at`` is
    still NULL. Without this, the first run after the feature ships would replay
    the entire back catalogue of finished games to the channel. Marking the
    existing finals here means only games that reach 'final' from now on are
    announced. The column itself is added by ADDED_COLUMNS, before this runs.
    """
    conn.execute(
        "UPDATE games SET notified_at = ? "
        "WHERE status = 'final' AND notified_at IS NULL",
        (now(),),
    )
    conn.commit()


def _migrate_v5_drop_foreign_scoresheets(conn: sqlite3.Connection) -> None:
    """v4 -> v5: remove games carrying another game's scoresheet.

    An earlier parser invented a game id when it could not read one. Asking the
    site for game 1 returns the site's real game 1 -- played in 2010, different
    teams, different children -- and that roster was then stored against a 2024
    tournament fixture. Thirteen games, 87 players who never played here, and a
    season year dragged back fourteen years because the dates came with them.

    The tell is that the scoresheet's date is not the date the schedule printed
    for that fixture. Nothing else in 8,490 games trips it.
    """
    from .sources.timetoscore import schedule_day_month

    try:
        rows = conn.execute(
            "SELECT game_id, date_text, date_iso FROM games"
            " WHERE has_scoresheet = 1 AND date_text IS NOT NULL AND date_iso IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return

    doomed = []
    for row in rows:
        expected = schedule_day_month(row["date_text"])
        if not expected:
            continue
        if expected != (int(row["date_iso"][5:7]), int(row["date_iso"][8:10])):
            doomed.append(row["game_id"])
    if not doomed:
        return

    log.info("dropping %d game(s) carrying another game's scoresheet", len(doomed))
    conn.execute("PRAGMA foreign_keys = ON")
    marks = ",".join("?" for _ in doomed)
    conn.execute(f"DELETE FROM games WHERE game_id IN ({marks})", doomed)
    # The players those sheets invented are left parentless; identity.rebuild
    # on the next derive clears them out of the way.
    conn.commit()


def _migrate_v4_rebuild_clubs(conn: sqlite3.Connection) -> None:
    """v3 -> v4: clubs become a table, and their names get canonicalised.

    Every club was previously whatever suffix-stripping made of the first team
    name that mentioned it, so one club could hold a dozen identities -- 202
    names for 123 actual clubs. The team names needed to fix that are already
    stored, so this reads them again rather than re-fetching.
    """
    from .pipeline import rebuild_clubs

    try:
        rebuild_clubs(conn)
    except sqlite3.Error as err:  # a partially built database: derive will redo it
        log.warning("could not rebuild clubs during migration: %s", err)


def _migrate_v3_relabel_placeholder_rosters(conn: sqlite3.Connection) -> None:
    """v2 -> v3: stop treating roster placeholders as people.

    "Not Signed In" is what the site prints when a roster was never submitted.
    It was stored as a player, so an entire roster collapsed into one identity
    -- 1,588 roster rows across 81 teams in one database -- and the duplicate
    (game, player) rows that produced aborted every derive with a UNIQUE
    constraint failure, leaving no derived stats at all.

    Relabels the stored rows so the next run is correct without re-fetching or
    re-parsing anything.
    """
    from .names import is_placeholder

    try:
        names = [
            row["name"] for row in conn.execute(
                "SELECT DISTINCT name FROM game_rosters WHERE role = 'player'")
        ]
    except sqlite3.Error:
        return

    bogus = [name for name in names if is_placeholder(name)]
    if not bogus:
        return

    marks = ",".join("?" for _ in bogus)
    victims = [
        row["player_id"] for row in conn.execute(
            f"SELECT DISTINCT player_id FROM player_names WHERE name IN ({marks})",
            bogus)
    ]

    conn.commit()
    # The identities these placeholders created are referenced from the event
    # tables, so those references are cleared before the rows go. Foreign keys
    # are relaxed for the rewrite, as in the v2 migration.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        rows = conn.execute(
            f"UPDATE game_rosters SET role = 'placeholder', player_id = NULL "
            f"WHERE name IN ({marks})", bogus).rowcount

        if victims:
            ids = ",".join("?" for _ in victims)
            for table, column in (
                ("goals", "scorer_player_id"),
                ("goals", "assist1_player_id"),
                ("goals", "assist2_player_id"),
                ("penalties", "player_id"),
                ("goalie_stints", "player_id"),
                ("game_rosters", "player_id"),
            ):
                conn.execute(
                    f"UPDATE {table} SET {column} = NULL "
                    f"WHERE {column} IN ({ids})", victims)
            for table in ("player_team_seasons", "player_game_stats",
                          "player_names", "players"):
                conn.execute(
                    f"DELETE FROM {table} WHERE player_id IN ({ids})", victims)
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    log.info("relabelled %d placeholder roster row(s) and removed %d bogus "
             "player(s): %s", rows, len(victims), ", ".join(sorted(bogus)[:5]))


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
