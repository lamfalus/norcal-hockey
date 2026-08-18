"""Export the database to JSON.

Two exports are produced:

``legacy``
    The exact shape the existing single-file viewer expects, so
    ``norcal_hockey_viewer.html`` keeps working untouched. Season totals come
    from the site's own published tables (which the old scraper read directly),
    with names resolved through the identity map so spelling variants merge.

``rich``
    Everything the game-level database can offer that the old format could not:
    standings, per-season splits by game class, career aggregates, and -- when
    asked for -- per-game logs.

Both are written atomically, so a reader (or a half-finished ``git add``) never
sees a partial file.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Skater columns in the site's published table -> legacy JSON field names.
_SKATER_FIELDS = {"G": "Goals", "A": "Ass.", "Hat": "Hat", "Pts": "Pts"}
#: Goalie columns likewise.
_GOALIE_FIELDS = {
    "Shots": "Shots", "GA": "GA", "GAA": "GAA", "Save%": "Save %", "SO": "SO",
}


def write_json(path: Path, payload: Any, *, indent: Optional[int] = None) -> int:
    """Write JSON atomically. Returns the byte size written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=indent,
                      separators=(",", ":") if indent is None else None)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return len(text.encode("utf-8"))


# ------------------------------------------------------------------ legacy


def build_legacy(conn: sqlite3.Connection) -> dict:
    """Build the viewer-compatible payload.

    Entries are keyed by resolved player, one per team per season per role --
    matching the original format, where a player who changed teams mid-season
    has two entries and a two-way player has both a skater and a goalie entry.
    """
    seasons = {
        row["season_id"]: row for row in
        conn.execute("SELECT season_id, label, start_year FROM seasons")
    }
    division_names = _division_labels(conn)
    teams = {
        (row["team_id"], row["season_id"]): row
        for row in conn.execute("""
            SELECT t.team_id, t.season_id, t.name, t.club, t.gender,
                   t.division_id, d.name AS division, l.name AS league
              FROM teams t LEFT JOIN divisions d ON d.division_id = t.division_id
                   LEFT JOIN leagues l ON l.league_id = t.league_id
        """)
    }
    # Two different children can share a spelling. They are separate players
    # here, so the export keys them apart by birth year -- otherwise their
    # entries would silently pile up under one name again.
    display_names = _unique_display_names(conn)

    # Derived stat lines, split by game class. The league publishes totals for
    # the regular season only; the game-level data covers preseason, playoff
    # and exhibition games too, and those are what the entries below report.
    derived = _derived_lines(conn)

    players: dict[str, list[dict]] = defaultdict(list)
    entry_count = 0

    # Resolve each published row to a specific child: the season/team-aware map
    # first (so two children sharing a spelling stay apart), then the coarse
    # alias map as a fallback.
    rows = conn.execute("""
        SELECT r.season_id, r.team_id, r.kind, r.name, r.jersey, r.gp, r.data_json,
               p.player_id, p.display_name, p.birth_year
          FROM team_stat_rows r
          LEFT JOIN players p ON p.player_id = COALESCE(
                (SELECT m.player_id FROM player_name_map m
                  WHERE m.name = r.name AND m.season_id = r.season_id
                    AND (m.team_id IS NULL OR m.team_id = r.team_id)
                  ORDER BY (m.team_id IS NULL) LIMIT 1),
                (SELECT n.player_id FROM player_names n WHERE n.name = r.name)
          )
         ORDER BY r.season_id, r.team_id, r.kind, r.row_index
    """)

    seen: set[tuple[int, int, int, str]] = set()

    for row in rows:
        team = teams.get((row["team_id"], row["season_id"]))
        if team is None:
            continue
        data = json.loads(row["data_json"])
        name = display_names.get(row["player_id"]) or row["display_name"] or row["name"]
        kind = "skater" if row["kind"] == "skater" else "goalie"
        key = (row["player_id"], row["season_id"], row["team_id"], kind)
        seen.add(key)
        line = derived.get(key)

        entry = _legacy_entry(
            season=row["season_id"], team=team, kind=kind,
            jersey=row["jersey"] or "", division=division_names,
            published=data, published_gp=row["gp"], line=line,
        )
        players[name].append(entry)
        entry_count += 1

    # Players who only appear on scoresheets -- in a preseason or playoff game,
    # or on a team the league never published totals for -- would otherwise be
    # missing entirely.
    for key, line in sorted(derived.items(), key=lambda kv: kv[0][1:]):
        if key in seen:
            continue
        player_id, season_id, team_id, kind = key
        team = teams.get((team_id, season_id))
        if team is None or player_id is None:
            continue
        name = display_names.get(player_id)
        if not name:
            continue
        players[name].append(_legacy_entry(
            season=season_id, team=team, kind=kind,
            jersey=line.get("jersey", ""), division=division_names,
            published={}, published_gp=None, line=line,
        ))
        entry_count += 1

    season_ids = sorted(seasons)
    return {
        "metadata": {
            "generated": _now(),
            "seasons": season_ids,
            "seasonLabels": {str(s): seasons[s]["label"] for s in season_ids},
            "playerCount": len(players),
            "entryCount": entry_count,
            "teamCount": len(teams),
            "source": "stats.caha.timetoscore.com",
            "leagues": {
                str(r["league_id"]): r["name"] for r in
                conn.execute("SELECT league_id, name FROM leagues ORDER BY priority")
            },
            "gameClasses": ["regular", "preseason", "playoff", "exhibition", "other"],
            "note": (
                "Totals cover every game played -- preseason, regular season and "
                "playoffs -- derived from scoresheets. 'byClass' breaks each line "
                "down, and 'byClass.regular' is what the league publishes."
            ),
        },
        "players": dict(sorted(players.items())),
    }


def _legacy_entry(
    *,
    season: int,
    team: sqlite3.Row,
    kind: str,
    jersey: str,
    division: dict[int, str],
    published: dict,
    published_gp: Optional[int],
    line: Optional[dict],
) -> dict[str, Any]:
    """One legacy stat line, preferring derived (all-games) numbers.

    Derived numbers only win when they are at least as complete as the league's
    published regular-season line. Half-way through a backfill -- or for a
    season whose scoresheets were never published -- the derived total would
    otherwise be an undercount, and quietly replacing a complete published
    figure with a partial one is worse than not using it at all.

    The published table is also still the source for the goalie columns the
    game-level data cannot supply (shots faced, GAA, save percentage), since the
    scoresheet's shot grid is unreliable.
    """
    entry: dict[str, Any] = {
        "season": season,
        "division": division.get(team["division_id"], team["division"] or ""),
        "team": team["name"],
        "type": kind,
        "jersey": jersey,
        "GP": "",
    }

    totals = (line or {}).get("all", {})
    complete = _derived_is_complete(line, published_gp)
    if kind == "skater":
        if line and complete:
            games = totals.get("gp", 0)
            points = totals.get("pts", 0)
            entry.update({
                "GP": str(games),
                "G": str(totals.get("g", 0)),
                "A": str(totals.get("a", 0)),
                "Hat": str(published.get("Hat", "") or ""),
                "PIM": _fmt(totals.get("pim", 0)) if totals.get("pim") else "",
                "PtsPerGame": f"{points / games:.2f}" if games else "0.00",
                "Pts": str(points),
            })
        else:
            games = published_gp or 0
            points = _int(published.get("Pts"))
            entry.update({
                "GP": str(published_gp if published_gp is not None else ""),
                "G": str(published.get("Goals", "") or ""),
                "A": str(published.get("Ass.", "") or ""),
                "Hat": str(published.get("Hat", "") or ""),
                "PIM": "",
                "PtsPerGame": f"{points / games:.2f}" if games else "0.00",
                "Pts": str(points),
            })
        entry = {
            "season": entry["season"], "division": entry["division"],
            "team": entry["team"], "type": entry["type"], "jersey": entry["jersey"],
            "GP": entry["GP"], "G": entry["G"], "A": entry["A"], "Hat": entry["Hat"],
            "PIM": entry["PIM"], "PtsPerGame": entry["PtsPerGame"], "Pts": entry["Pts"],
        }
    else:
        if line and complete:
            entry["GP"] = str(totals.get("gp", ""))
        else:
            entry["GP"] = str(published_gp if published_gp is not None else "")
        for out_key, src in _GOALIE_FIELDS.items():
            entry[out_key] = str(published.get(src, "") or "")
        if line and complete and totals.get("ga") is not None and not entry["GA"]:
            entry["GA"] = str(totals["ga"])

    # Extra keys the original viewer ignores: which league the line belongs to,
    # the same numbers broken out per game class, and where the totals came
    # from so a partial backfill is never mistaken for a complete one.
    entry["league"] = team["league"] if "league" in team.keys() else None
    entry["source"] = "games" if (line and complete) else "published"
    if line and line.get("byClass"):
        entry["byClass"] = line["byClass"]
    return entry


def _derived_is_complete(line: Optional[dict], published_gp: Optional[int]) -> bool:
    """True when the parsed games cover at least the league's published season.

    With no published line to compare against (a preseason-only team, or a
    league that publishes no totals), any derived data is the best there is.
    """
    if not line:
        return False
    if published_gp is None:
        return True
    regular = line.get("byClass", {}).get("regular", {}).get("GP", 0)
    return regular >= published_gp


def _derived_lines(conn: sqlite3.Connection) -> dict[tuple[int, int, int, str], dict]:
    """Per (player, season, team, role) stat lines, split by game class."""
    lines: dict[tuple[int, int, int, str], dict] = {}
    for row in conn.execute("""
        SELECT s.player_id, s.season_id, s.team_id,
               CASE WHEN s.is_goalie = 1 THEN 'goalie' ELSE 'skater' END AS kind,
               COALESCE(g.game_class, 'other') AS class,
               MAX(s.jersey) AS jersey,
               COUNT(*) AS gp, SUM(s.goals) AS g, SUM(s.assists) AS a,
               SUM(s.points) AS pts, SUM(s.pim) AS pim,
               SUM(s.goals_against) AS ga
          FROM player_game_stats s
          JOIN games g ON g.game_id = s.game_id
         WHERE s.team_id IS NOT NULL
         GROUP BY s.player_id, s.season_id, s.team_id, kind, class
    """):
        key = (row["player_id"], row["season_id"], row["team_id"], row["kind"])
        line = lines.setdefault(key, {"jersey": row["jersey"] or "", "byClass": {},
                                      "all": {"gp": 0, "g": 0, "a": 0, "pts": 0,
                                              "pim": 0, "ga": 0}})
        stats = {
            "GP": row["gp"], "G": row["g"] or 0, "A": row["a"] or 0,
            "Pts": row["pts"] or 0, "PIM": row["pim"] or 0,
        }
        if row["ga"] is not None:
            stats["GA"] = row["ga"]
        line["byClass"][row["class"]] = stats

        total = line["all"]
        total["gp"] += row["gp"]
        total["g"] += row["g"] or 0
        total["a"] += row["a"] or 0
        total["pts"] += row["pts"] or 0
        total["pim"] += row["pim"] or 0
        total["ga"] += row["ga"] or 0
    return lines


# -------------------------------------------------------------------- rich


def build_rich(conn: sqlite3.Connection, *, include_game_logs: bool = False) -> dict:
    """Build the richer export that the game-level database makes possible."""
    payload: dict[str, Any] = {
        "metadata": {
            "generated": _now(),
            "schema": "norcal-hockey/2",
            "source": "stats.caha.timetoscore.com",
            "includesGameLogs": include_game_logs,
        },
        "seasons": [], "divisions": [], "teams": [], "standings": [],
        "players": [], "games": [],
    }

    payload["seasons"] = [
        dict(r) for r in conn.execute(
            "SELECT season_id, label, start_year FROM seasons "
            "WHERE season_id IN (SELECT DISTINCT season_id FROM teams) ORDER BY season_id"
        )
    ]
    payload["divisions"] = [
        dict(r) for r in conn.execute(
            "SELECT d.division_id, d.season_id, d.league_id, d.name, d.level, d.sort_order, d.gender, l.name AS league FROM divisions d LEFT JOIN leagues l ON l.league_id = d.league_id "
            "ORDER BY d.season_id, d.league_id, d.sort_order"
        )
    ]
    payload["teams"] = [
        dict(r) for r in conn.execute("""
            SELECT t.team_id, t.season_id, t.name, t.club, t.club_seq, t.gender,
                   d.name AS division, t.league_id, l.name AS league
              FROM teams t LEFT JOIN divisions d ON d.division_id = t.division_id
                  LEFT JOIN leagues l ON l.league_id = t.league_id
             ORDER BY t.season_id, t.name
        """)
    ]
    payload["standings"] = [
        dict(r) for r in conn.execute(
            "SELECT season_id, team_id, gp, w, l, t, otl, gf, ga, diff, pts "
            "FROM standings ORDER BY season_id, team_id"
        )
    ]
    payload["games"] = [
        dict(r) for r in conn.execute("""
            SELECT game_id, season_id, league_id, date_iso, level, game_type, game_class,
                   away_team_id, home_team_id, away_name, home_name,
                   away_goals, home_goals, rink, status
              FROM games
             WHERE status = 'final'
             ORDER BY date_iso, game_id
        """)
    ]

    # Per-player season splits, separated by game class so regular-season
    # numbers stay comparable with the league's published totals.
    splits: dict[int, dict] = {}
    # Grouped by team as well as season, so a player who was double-rostered --
    # a girls team and a co-ed team in one season -- gets a separate stat line
    # for each, tagged with that team's division and gender.
    for row in conn.execute("""
        SELECT s.player_id, s.season_id, s.team_id, g.game_class,
               COUNT(*) AS gp, SUM(s.goals) AS g, SUM(s.assists) AS a,
               SUM(s.points) AS pts, SUM(s.pim) AS pim,
               SUM(s.ppg) AS ppg, SUM(s.shg) AS shg,
               MAX(s.is_goalie) AS is_goalie, SUM(s.goals_against) AS ga,
               t.name AS team, t.gender AS gender, d.name AS division
          FROM player_game_stats s
          JOIN games g ON g.game_id = s.game_id
          LEFT JOIN teams t ON t.team_id = s.team_id AND t.season_id = s.season_id
          LEFT JOIN divisions d ON d.division_id = t.division_id
         GROUP BY s.player_id, s.season_id, s.team_id, g.game_class
    """):
        entry = splits.setdefault(row["player_id"], {"seasons": []})
        entry["seasons"].append({
            "season": row["season_id"], "teamId": row["team_id"],
            "team": row["team"], "division": row["division"],
            "gender": row["gender"],
            "class": row["game_class"], "GP": row["gp"], "G": row["g"],
            "A": row["a"], "Pts": row["pts"], "PIM": row["pim"],
            "PPG": row["ppg"], "SHG": row["shg"],
            "goalie": bool(row["is_goalie"]), "GA": row["ga"],
        })

    logs: dict[int, list] = defaultdict(list)
    if include_game_logs:
        for row in conn.execute("""
            SELECT s.player_id, s.game_id, g.date_iso, s.team_id, s.goals, s.assists,
                   s.points, s.pim, s.is_goalie, s.goals_against
              FROM player_game_stats s JOIN games g ON g.game_id = s.game_id
             ORDER BY g.date_iso
        """):
            logs[row["player_id"]].append({
                "gameId": row["game_id"], "date": row["date_iso"],
                "teamId": row["team_id"], "G": row["goals"], "A": row["assists"],
                "Pts": row["points"], "PIM": row["pim"],
                "goalie": bool(row["is_goalie"]), "GA": row["goals_against"],
            })

    names: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT player_id, name FROM player_names ORDER BY name"):
        names[row["player_id"]].append(row["name"])

    for row in conn.execute(
        "SELECT player_id, display_name, birth_year, birth_year_min, birth_year_max "
        "FROM players ORDER BY display_name"
    ):
        pid = row["player_id"]
        player = {
            "id": pid,
            "name": row["display_name"],
            "birthYear": row["birth_year"],
            "birthYearRange": [row["birth_year_min"], row["birth_year_max"]],
            "aliases": [n for n in names.get(pid, []) if n != row["display_name"]],
            "seasons": splits.get(pid, {}).get("seasons", []),
        }
        if include_game_logs:
            player["games"] = logs.get(pid, [])
        payload["players"].append(player)

    payload["metadata"].update({
        "playerCount": len(payload["players"]),
        "gameCount": len(payload["games"]),
        "teamCount": len(payload["teams"]),
    })
    return payload


# ------------------------------------------------------------------ driver


def export_all(
    conn: sqlite3.Connection,
    *,
    export_dir: Path,
    legacy_name: str,
    rich_name: Optional[str] = None,
    include_game_logs: bool = False,
) -> dict[str, int]:
    """Write both exports. Returns ``{filename: bytes}``."""
    export_dir = Path(export_dir)
    written: dict[str, int] = {}

    legacy = build_legacy(conn)
    written[legacy_name] = write_json(export_dir / legacy_name, legacy)
    log.info("wrote %s (%d players, %s)", legacy_name,
             legacy["metadata"]["playerCount"], _human(written[legacy_name]))

    if rich_name:
        rich = build_rich(conn, include_game_logs=include_game_logs)
        written[rich_name] = write_json(export_dir / rich_name, rich)
        log.info("wrote %s (%d games, %s)", rich_name,
                 rich["metadata"]["gameCount"], _human(written[rich_name]))

    return written


# ------------------------------------------------------------------ helpers


def _division_labels(conn: sqlite3.Connection) -> dict[int, str]:
    """Division id -> a label that is unambiguous across leagues.

    Norcal and SCAHA both run a division called "12U A". The legacy format
    carries divisions as bare strings, so a name shared by two leagues in one
    season is qualified with the league -- ``"12U A (SCAHA)"`` -- while names
    that are already unique are left exactly as the league prints them.
    """
    rows = conn.execute("""
        SELECT d.division_id, d.season_id, d.name,
               COALESCE(l.name, 'league ' || d.league_id) AS league
          FROM divisions d LEFT JOIN leagues l ON l.league_id = d.league_id
    """).fetchall()

    clashes: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in rows:
        clashes[(row["season_id"], row["name"])].add(row["league"])

    labels: dict[int, str] = {}
    for row in rows:
        others = clashes[(row["season_id"], row["name"])]
        labels[row["division_id"]] = (
            f"{row['name']} ({row['league']})" if len(others) > 1 else row["name"]
        )
    return labels


def _unique_display_names(conn: sqlite3.Connection) -> dict[int, str]:
    """Player id -> a display name unique across the export.

    When two children genuinely share a spelling, their birth years tell them
    apart (``Ryan Smith '13`` / ``Ryan Smith '07``) -- the same convention the
    viewer already uses for its birth-year badges. Falls back to a numeric
    suffix when even the birth years are unknown.
    """
    rows = conn.execute(
        "SELECT player_id, display_name, "
        "       COALESCE(birth_year, birth_year_min) AS birth_year "
        "  FROM players "
        " ORDER BY display_name, birth_year IS NULL, birth_year, player_id"
    ).fetchall()

    by_name: dict[str, list] = defaultdict(list)
    for row in rows:
        by_name[row["display_name"]].append(row)

    names: dict[int, str] = {}
    for name, group in by_name.items():
        if len(group) == 1:
            names[group[0]["player_id"]] = name
            continue
        used: set[str] = set()
        for i, row in enumerate(group, 1):
            if row["birth_year"]:
                candidate = f"{name} '{str(row['birth_year'])[-2:]}"
            else:
                candidate = f"{name} ({i})"
            while candidate in used:
                candidate = f"{candidate}*"
            used.add(candidate)
            names[row["player_id"]] = candidate
    return names


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int(text: Any) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return 0


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size}B"
