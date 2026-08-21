"""The data the web app reads, split by granularity rather than by season.

The obvious split -- one file per season -- breaks the two views that exist to
span seasons: the club-by-club player flow, and a club's teams across the years.
So the split follows *granularity* instead:

``core.json``
    Everything cross-season and aggregate: players with their per-season
    summaries, teams, divisions, clubs, leagues, standings. Loaded once, and
    enough on its own for every view the app has today.

``logs/p<NN>.json``
    Per-game lines for a player, bucketed by player id. Opening a player fetches
    one bucket, around 100 KB gzipped, and it covers every season they played.

``games/s<SEASON>.json``
    Goals, penalties and period scores, for box scores. Opening a game fetches
    one season's events.

Every drill-down is scoped to a single player, game or team, so no shard ever
needs a cross-season join -- which is exactly why sharding here costs nothing
that season-sharding would have cost.

Sizes on six seasons: 61 MB of JSON, about 5.8 MB gzipped, which GitHub Pages
serves compressed. Transfer was never the constraint; parse time and memory on a
phone at a rink is, and that is what loading detail on demand solves -- core.json
is 7.6 MB of that, and nothing else is read until somebody clicks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .export import _human, _now, write_json

log = logging.getLogger(__name__)

#: Player game logs are spread over this many files. Chosen so each is a small
#: fetch (tens of KB) while keeping the file count manageable.
SHARD_COUNT = 32

#: Strength codes the scoresheet sets automatically when a goal is scored during
#: a penalty. They are recorded by the system rather than typed by hand, so they
#: are trustworthy: on five seasons, 12.6% PP, 4.5% SH, 1.1% empty net.
SPECIAL_STRENGTHS = ("PP", "SH", "EN", "5/3", "4/4", "PS")


def shard_for(player_id: int) -> int:
    return player_id % SHARD_COUNT


# --------------------------------------------------------------------- core


def build_core(conn: sqlite3.Connection) -> dict:
    """Everything the app needs before anyone clicks into a player or game."""
    seasons = [
        {"id": r["season_id"], "label": r["label"], "startYear": r["start_year"]}
        for r in conn.execute(
            "SELECT season_id, label, start_year FROM seasons "
            " WHERE season_id IN (SELECT DISTINCT season_id FROM teams)"
            " ORDER BY season_id")
    ]
    leagues = [
        {"id": r["league_id"], "name": r["name"]}
        for r in conn.execute(
            "SELECT league_id, name FROM leagues WHERE kind = 'season' ORDER BY priority")
    ]
    divisions = [
        {"id": r["division_id"], "season": r["season_id"], "league": r["league_id"],
         "name": r["name"], "gender": r["gender"]}
        for r in conn.execute(
            "SELECT division_id, season_id, league_id, name, gender FROM divisions"
            " ORDER BY season_id, league_id, sort_order")
    ]
    # A team id is only unique within its season -- the table is keyed by both --
    # so anything reading these keys on (season, id), never on the id alone.
    # ``seq`` is the club's own numbering, which is the only thing telling two
    # teams of one club in one division apart when the site does not suffix
    # their names.
    teams = [
        {"id": r["team_id"], "season": r["season_id"], "name": r["name"],
         "club": r["club"], "division": r["division_id"], "league": r["league_id"],
         "gender": r["gender"], "seq": r["club_seq"]}
        for r in conn.execute(
            "SELECT team_id, season_id, name, club, division_id, league_id, gender,"
            "       club_seq"
            "  FROM teams ORDER BY season_id, name")
    ]
    # Clubs carry the name to group by, the shorter one to show, and what the
    # thing is. Half the names the site prints are bracket slots, high schools
    # or visiting teams: they still need naming on a schedule, so they are
    # classified here rather than dropped, and the app shows only ``club``.
    clubs = [
        {"name": r["name"], "short": r["short_name"], "kind": r["kind"]}
        for r in conn.execute(
            "SELECT name, short_name, kind FROM clubs ORDER BY name")
    ]
    standings = [
        {"season": r["season_id"], "team": r["team_id"], "gp": r["gp"], "w": r["w"],
         "l": r["l"], "t": r["t"], "otl": r["otl"], "gf": r["gf"], "ga": r["ga"],
         "pts": r["pts"]}
        for r in conn.execute(
            "SELECT season_id, team_id, gp, w, l, t, otl, gf, ga, pts FROM standings")
    ]

    aliases: dict[int, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT player_id, name FROM player_names ORDER BY name"):
        aliases[r["player_id"]].append(r["name"])

    # Per-season, per-team, per-game-class summaries: the numbers every list and
    # table in the app is built from.
    #
    # Grouped by is_goalie as well, so a player who skates out and also takes a
    # turn in net gets one row per role. Folding the two together would count
    # their skating games in the goalie GP -- 618 of 37,009 splits mix the two,
    # and a two-way player is exactly the one somebody looks up.
    splits: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("""
        SELECT s.player_id, s.season_id, s.team_id,
               COALESCE(g.game_class, 'other') AS class, s.is_goalie,
               COUNT(*) AS gp, SUM(s.goals) AS g, SUM(s.assists) AS a,
               SUM(s.points) AS pts, SUM(s.pim) AS pim,
               SUM(s.ppg) AS ppg, SUM(s.shg) AS shg,
               SUM(s.goals_against) AS ga,
               MAX(s.jersey) AS jersey
          FROM player_game_stats s
          JOIN games g ON g.game_id = s.game_id
         WHERE s.team_id IS NOT NULL
         GROUP BY s.player_id, s.season_id, s.team_id, class, s.is_goalie
    """):
        splits[r["player_id"]].append({
            "season": r["season_id"], "team": r["team_id"], "class": r["class"],
            "jersey": r["jersey"] or "", "gp": r["gp"], "g": r["g"] or 0,
            "a": r["a"] or 0, "pts": r["pts"] or 0, "pim": r["pim"] or 0,
            "ppg": r["ppg"] or 0, "shg": r["shg"] or 0,
            "goalie": bool(r["is_goalie"]), "ga": r["ga"],
        })

    # The league's own published season totals, kept alongside so the app can
    # show them where scoresheets are missing or disagree.
    published: dict[tuple[int, int, str], dict] = {}
    for r in conn.execute("""
        SELECT t.season_id, t.team_id, t.kind, t.name, t.gp, t.data_json,
               COALESCE(m.player_id, n.player_id) AS player_id
          FROM team_stat_rows t
          LEFT JOIN player_name_map m
                 ON m.name = t.name AND m.season_id = t.season_id
                AND (m.team_id IS NULL OR m.team_id = t.team_id)
          LEFT JOIN player_names n ON n.name = t.name
    """):
        if r["player_id"] is None:
            continue
        data = json.loads(r["data_json"])
        published[(r["player_id"], r["season_id"], r["kind"])] = {
            "gp": r["gp"], "g": _int(data.get("Goals")), "a": _int(data.get("Ass.")),
            "pts": _int(data.get("Pts")), "shots": _int(data.get("Shots")),
            "ga": _int(data.get("GA")), "gaa": data.get("GAA"),
            "savePct": data.get("Save %"), "so": _int(data.get("SO")),
        }

    players = []
    for r in conn.execute(
        "SELECT player_id, display_name, birth_year, birth_year_min, birth_year_max"
        "  FROM players ORDER BY display_name"
    ):
        pid = r["player_id"]
        entry: dict[str, Any] = {
            "id": pid,
            "name": r["display_name"],
            "shard": shard_for(pid),
            "seasons": splits.get(pid, []),
        }
        if r["birth_year"]:
            entry["born"] = r["birth_year"]
        elif r["birth_year_min"]:
            entry["bornRange"] = [r["birth_year_min"], r["birth_year_max"]]
        extra = [a for a in aliases.get(pid, []) if a != r["display_name"]]
        if extra:
            entry["aliases"] = extra
        official = {
            f"{season}:{kind}": value
            for (p, season, kind), value in published.items() if p == pid
        }
        if official:
            entry["official"] = official
        players.append(entry)

    return {
        "metadata": {
            "generated": _now(),
            "schema": "norcal-hockey/app-1",
            "source": "stats.caha.timetoscore.com",
            "shardCount": SHARD_COUNT,
            "gameClasses": ["regular", "playoff", "preseason", "exhibition", "other"],
            "strengths": list(SPECIAL_STRENGTHS),
            "counts": {
                "players": len(players), "teams": len(teams),
                "seasons": len(seasons), "leagues": len(leagues),
                "clubs": sum(1 for c in clubs if c["kind"] == "club"),
            },
        },
        "seasons": seasons,
        "leagues": leagues,
        "divisions": divisions,
        "clubs": clubs,
        "teams": teams,
        "standings": standings,
        "players": players,
    }


# ---------------------------------------------------------------- game logs


def build_player_logs(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per-game lines for every player, grouped into shards by player id."""
    shards: dict[int, dict[str, list]] = {i: {} for i in range(SHARD_COUNT)}

    for r in conn.execute("""
        SELECT s.player_id, s.game_id, s.team_id, s.goals, s.assists, s.points,
               s.pim, s.ppg, s.shg, s.is_goalie, s.goals_against,
               g.season_id, g.date_iso, g.game_class, g.league_id,
               g.home_team_id, g.away_team_id, g.home_goals, g.away_goals, s.side
          FROM player_game_stats s
          JOIN games g ON g.game_id = s.game_id
         ORDER BY g.date_iso, s.game_id
    """):
        home = r["side"] == "home"
        line = {
            "game": r["game_id"], "season": r["season_id"], "date": r["date_iso"],
            "team": r["team_id"],
            "opp": r["away_team_id"] if home else r["home_team_id"],
            "home": home, "class": r["game_class"], "league": r["league_id"],
            "for": r["home_goals"] if home else r["away_goals"],
            "against": r["away_goals"] if home else r["home_goals"],
            "g": r["goals"], "a": r["assists"], "pts": r["points"],
            "pim": r["pim"], "ppg": r["ppg"], "shg": r["shg"],
        }
        if r["is_goalie"]:
            line["goalie"] = True
            line["ga"] = r["goals_against"]
        shards[shard_for(r["player_id"])].setdefault(str(r["player_id"]), []).append(line)

    return {
        index: {
            "metadata": {"generated": _now(), "shard": index,
                         "shardCount": SHARD_COUNT},
            "players": players,
        }
        for index, players in shards.items()
    }


# -------------------------------------------------------------- game detail


def build_game_detail(conn: sqlite3.Connection) -> dict[int, dict]:
    """Every game of a season, with its events where it has been played.

    Scheduled games are included, not just finished ones. A team page is a
    schedule as much as a record, and dropping the unplayed games would empty
    it exactly where it matters most: a season that has started but finished
    nothing has 119 scheduled games and no final ones at all.
    """
    games: dict[int, dict[str, dict]] = defaultdict(dict)

    for r in conn.execute("""
        SELECT game_id, season_id, league_id, division_id, date_iso, time_text,
               rink, level, game_class, game_type, status,
               home_team_id, away_team_id, home_name, away_name,
               home_goals, away_goals
          FROM games ORDER BY date_iso
    """):
        entry = {
            "date": r["date_iso"], "time": r["time_text"], "rink": r["rink"],
            "level": r["level"], "class": r["game_class"], "type": r["game_type"],
            "league": r["league_id"], "division": r["division_id"],
            "home": r["home_team_id"], "away": r["away_team_id"],
            "homeName": r["home_name"], "awayName": r["away_name"],
            "periods": {}, "goals": [], "penalties": [],
        }
        if r["status"] == "final":
            entry["hg"] = r["home_goals"]
            entry["ag"] = r["away_goals"]
        else:
            # No score, and no events to attach: the app reads the absence of a
            # result as "not played yet" rather than as nil-nil.
            entry["status"] = r["status"]
        games[r["season_id"]][str(r["game_id"])] = entry

    seasons = {
        r["game_id"]: r["season_id"]
        for r in conn.execute("SELECT game_id, season_id FROM games")
    }

    def bucket(game_id: int) -> Optional[dict]:
        season = seasons.get(game_id)
        return games[season].get(str(game_id)) if season else None

    for r in conn.execute(
        "SELECT game_id, side, period, goals FROM period_scores"
    ):
        game = bucket(r["game_id"])
        if game is not None:
            game["periods"].setdefault(r["side"], {})[r["period"]] = r["goals"]

    for r in conn.execute("""
        SELECT game_id, side, period, time_text, time_sec, strength,
               scorer_player_id, assist1_player_id, assist2_player_id,
               scorer_jersey, assist1_jersey, assist2_jersey
          FROM goals ORDER BY game_id, side, seq
    """):
        game = bucket(r["game_id"])
        if game is None:
            continue
        goal = {
            "side": r["side"], "per": r["period"], "time": r["time_text"],
            "sec": r["time_sec"],
            "by": r["scorer_player_id"], "byNo": r["scorer_jersey"],
        }
        strength = (r["strength"] or "").strip().upper()
        if strength:
            goal["str"] = strength
        assists = [a for a in (r["assist1_player_id"], r["assist2_player_id"]) if a]
        if assists:
            goal["assists"] = assists
        game["goals"].append(goal)

    for r in conn.execute("""
        SELECT game_id, side, period, player_id, jersey, infraction, minutes,
               off_ice, start_time, end_time
          FROM penalties ORDER BY game_id, side, seq
    """):
        game = bucket(r["game_id"])
        if game is None:
            continue
        game["penalties"].append({
            "side": r["side"], "per": r["period"], "by": r["player_id"],
            "byNo": r["jersey"], "inf": r["infraction"], "min": r["minutes"],
            "off": r["off_ice"], "start": r["start_time"], "end": r["end_time"],
        })

    return {
        season: {"metadata": {"generated": _now(), "season": season},
                 "games": payload}
        for season, payload in games.items()
    }


# ------------------------------------------------------------------ writing


def write_app(conn: sqlite3.Connection, out_dir: Path) -> dict[str, int]:
    """Write the whole app dataset. Returns ``{path: bytes}``."""
    out_dir = Path(out_dir)
    written: dict[str, int] = {}

    written["core.json"] = write_json(out_dir / "core.json", build_core(conn))

    for index, payload in build_player_logs(conn).items():
        name = f"logs/p{index:02d}.json"
        written[name] = write_json(out_dir / name, payload)

    for season, payload in build_game_detail(conn).items():
        name = f"games/s{season}.json"
        written[name] = write_json(out_dir / name, payload)

    total = sum(written.values())
    log.info("app data: %d files, %s (core %s)",
             len(written), _human(total), _human(written["core.json"]))
    return written


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
