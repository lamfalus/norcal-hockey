"""Crawl orchestration.

A run proceeds in four stages:

1. **Discover seasons.** Read the season dropdown. New seasons (S32 for 2026-27)
   appear here on their own; nothing is hardcoded.
2. **Scan teams.** For each active season, read the season index for divisions,
   teams and standings, then each team page for its game list and the site's
   published season totals.
3. **Fetch scoresheets.** Only for games that need one: newly final, never
   parsed, changed since last seen, or recently played (to catch scorekeeper
   corrections). This is what keeps a nightly run cheap.
4. **Derive.** Rebuild player identities and per-game stat lines from stored
   rows -- no network access involved.

Stage 4 can be re-run alone at any time, and with an archived raw copy of every
page, stages 2-3 can be re-parsed offline after a parser change.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from . import clubs as clubs_mod, identity, names as N, review
from .config import Config
from .db import Run, get_meta, now, set_meta, upsert
from .fetch import Fetcher, FetchError, RateLimited, RequestCeilingReached
from .sources import timetoscore as tts

log = logging.getLogger(__name__)


@dataclass
class Stats:
    seasons: int = 0
    teams: int = 0
    games_seen: int = 0
    games_new: int = 0
    scoresheets: int = 0
    skipped: int = 0
    errors: int = 0
    #: Scoresheets that fetched fine but contained no roster. A property of
    #: the data, not a failure of the run.
    empty_sheets: int = 0
    #: Scorecards (PDFs) with at least one reconciling goalie side stored.
    scorecards: int = 0
    #: Scorecards that fetched but reconciled nothing worth storing.
    scorecards_empty: int = 0
    #: Set when the request ceiling stopped the run before it finished.
    stopped_early: bool = False

    def summary(self) -> str:
        return (f"{self.seasons} seasons, {self.teams} teams, "
                f"{self.games_seen} games ({self.games_new} new), "
                f"{self.scoresheets} scoresheets, {self.errors} errors"
                + (f", {self.empty_sheets} with no roster" if self.empty_sheets else "")
                + (f", {self.scorecards} scorecards" if self.scorecards else ""))


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, config: Config, fetcher: Fetcher) -> None:
        self.conn = conn
        self.config = config
        self.fetcher = fetcher
        self.stats = Stats()

    # ------------------------------------------------------------ seasons
    @property
    def leagues(self) -> list[int]:
        """Configured leagues, or every one already known to the database."""
        if self.config.leagues:
            return self.config.leagues
        known = [
            row["league_id"] for row in self.conn.execute(
                "SELECT league_id FROM leagues ORDER BY priority, league_id")
        ]
        return known or [3]

    def leagues_for(self, season_id: int) -> list[int]:
        """Season-long leagues that carried teams in this season, in priority order.

        Weekend tournaments and high-school leagues are skipped, and a newly
        discovered league is skipped until it has been classified -- collecting
        it silently would be a decision made on the user's behalf.
        """
        if self.config.leagues:
            return self.config.leagues
        rows = self.conn.execute("""
            SELECT ls.league_id
              FROM league_seasons ls
              JOIN leagues l ON l.league_id = ls.league_id
             WHERE ls.season_id = ? AND ls.teams > 0 AND l.kind = 'season'
             ORDER BY l.priority, ls.league_id
        """, (season_id,)).fetchall()
        return [row["league_id"] for row in rows]

    def discover_leagues(self, season_id: int, *, force: bool = False) -> list[int]:
        """Find every league carrying teams in a season, by probing its ids.

        There is no index of leagues on the site, and a single competition is
        split across several: CAHA runs preseason, weekend, main-league and
        playoff ids that appear and disappear as the season progresses. So the
        id range is probed rather than assumed, and re-probed for the current
        season on every run so a league that switches on mid-season is picked up
        the same night.

        Past seasons are probed once and remembered; ``force`` re-probes them.
        """
        if self.config.leagues and not force:
            # An explicit list is the answer; probing sixty ids to rediscover
            # leagues the caller already named is wasted requests.
            return self.config.leagues
        if not self.config.discover_leagues and not force:
            return self.leagues_for(season_id)

        marker = f"leagues_probed:{season_id}"
        newest = _scalar(self.conn, "SELECT MAX(season_id) FROM seasons") or season_id
        already = get_meta(self.conn, marker)
        # The current season keeps changing; older ones do not.
        if already and season_id < newest and not force:
            return self.leagues_for(season_id)

        found: list[int] = []
        for league_id in range(1, self.config.max_league_id + 1):
            try:
                page = self.fetcher.get(
                    tts.season_index_path(league_id, season_id),
                    key=f"s{season_id}/l{league_id}/index",
                    use_cache=self.fetcher.offline,
                )
            except FetchError:
                continue  # an id that does not exist is not an error
            teams = tts.parse_season_index(page.html)
            if not teams:
                continue

            name = tts.parse_league_name(page.html)
            self._record_league(league_id, name, _default_priority(league_id))
            upsert(
                self.conn, "league_seasons",
                {"league_id": league_id, "season_id": season_id,
                 "teams": len(teams), "discovered_at": now()},
                keys=["league_id", "season_id"],
            )
            found.append(league_id)
            if not already:
                log.info("  S%d league %-3d %-24s %d teams",
                         season_id, league_id, name or "(unnamed)", len(teams))
            self._classify_league(league_id, season_id, teams)

        set_meta(self.conn, marker, now())
        self.conn.commit()
        collected = self.leagues_for(season_id)
        log.info("S%d: %d leagues carry teams, %d of them season-long",
                 season_id, len(found), len(collected))
        return collected

    def _classify_league(
        self, league_id: int, season_id: int, teams: list[tts.TeamRef]
    ) -> None:
        """Decide whether a newly discovered league is worth collecting.

        A season-long competition runs for months; a tournament is over in a
        weekend. One team's schedule is enough to tell them apart, so a single
        extra request settles it -- and the answer is recorded as a review item
        either way, because the guess is the collector's, not the user's.
        """
        row = self.conn.execute(
            "SELECT kind, name FROM leagues WHERE league_id = ?", (league_id,)
        ).fetchone()
        if row is None or row["kind"] != "unknown":
            return  # already decided, by seed or by hand

        name = row["name"] or f"league {league_id}"
        span, labels = self._probe_schedule(league_id, season_id, teams)

        if _CHAMPIONSHIP_NAMES.search(name):
            # Left 'unknown', which means skipped: the collector will not decide
            # this one on its own. It is re-measured and re-asked every run
            # until somebody answers with --include or --exclude.
            kind, why = "unknown", (
                "name reads as a championship above the league, whose field is "
                "drawn from outside California -- not collected without a decision")
        elif _PLAYOFF_NAMES.search(name):
            kind, why = "season", "name looks like a playoff or championship round"
        elif labels and labels.most_common(1)[0][0].lower() == "tournament":
            # The schedule labels each game with its competition. A league whose
            # games all say "Tournament" is a bucket for one-off events, however
            # long it runs -- tournaments happen all season, so its date span
            # looks exactly like a real league's.
            kind, why = "event", "its games are labelled 'Tournament'"
        elif span is not None and span >= SEASON_SPAN_DAYS:
            kind, why = "season", f"games run for {span} days"
        else:
            kind, why = "event", (
                f"games run for only {span} days" if span is not None
                else "no dated games found")

        self.conn.execute(
            "UPDATE leagues SET kind = ?, span_days = ? WHERE league_id = ?",
            (kind, span, league_id),
        )

        measured = f"{span} days between first and last game" if span is not None \
            else "no dated games found"
        review.record(self.conn, [review.Item(
            kind="new_league",
            subject=f"league {league_id} ({name}): {measured}",
            evidence={"names": [], "detail": [
                f"{len(teams)} teams in S{season_id}",
                measured,
                why,
                f"treated as {'a season-long league' if kind == 'season' else 'a single event'}",
            ]},
            suggestion=(
                "leave as is, or change with: norcalstats leagues "
                f"--{'exclude' if kind == 'season' else 'include'} {league_id}"
            ),
            applied=(
                f"kind = {kind} "
                + ("(collected)" if kind == "season"
                   else "(skipped, awaiting a decision)" if kind == "unknown"
                   else "(skipped)")
            ),
            # An undecided championship is the one case the collector is openly
            # unsure about, so it is not filed at the same confidence as a guess
            # it is prepared to act on.
            confidence=0.2 if kind == "unknown" else 0.5,
            parts=(str(league_id),),
        )])

    def _probe_schedule(
        self, league_id: int, season_id: int, teams: list[tts.TeamRef]
    ) -> tuple[Optional[int], Counter]:
        """Look at a few schedules: how long the league runs, and what it calls itself.

        Several teams are sampled and the **longest** span taken. A single team
        can have a nearly empty schedule -- the Pacific Girls league looked like
        a two-day tournament when judged on its first team alone, which would
        have quietly dropped a tier league.
        """
        longest: Optional[int] = None
        labels: Counter = Counter()

        for team in teams[:_CLASSIFY_SAMPLE]:
            try:
                page = self.fetcher.get(
                    tts.team_path(league_id, season_id, team.team_id),
                    key=f"s{season_id}/l{league_id}/team/{team.team_id}",
                    use_cache=self.fetcher.offline,
                )
            except FetchError:
                continue
            games = tts.parse_team_page(page.html).games
            labels.update(g.league for g in games if g.league)

            dates = sorted(
                d for d in (
                    tts.schedule_date_to_iso(g.date_text, _REFERENCE_YEAR)
                    for g in games
                ) if d
            )
            if len(dates) >= 2:
                span = abs((date.fromisoformat(dates[-1])
                            - date.fromisoformat(dates[0])).days)
                longest = span if longest is None else max(longest, span)
        return longest, labels

    def discover_seasons(self) -> list[tts.SeasonRef]:
        """Read the site's season list and record any seasons we hadn't seen."""
        # Any season page carries the full dropdown; season=0 is "Current".
        # use_cache lets this work offline from the archive too.
        try:
            page = self.fetcher.get(
                tts.season_index_path(self.leagues[0], 0),
                key="season-list",
                use_cache=self.fetcher.offline,
            )
        except FetchError as exc:
            log.warning("could not read the season list: %s", exc)
            return []

        seasons = tts.parse_season_list(page.html)
        if not seasons:
            log.warning("could not read the season list; falling back to configured seasons")

        # A new season goes live before the dropdown lists it -- the 2026-27
        # season was reachable as S33 while the dropdown still ended at S31 --
        # so the "Current" page is asked what season it actually is.
        current = tts.parse_current_season(page.html)
        if current and all(s.season_id != current for s in seasons):
            # Season numbers are not year-based and the site skips them (S31
            # was followed by S33), so the year is taken from the calendar
            # rather than extrapolated. It is confirmed against real game dates
            # later, once any scoresheet has been parsed.
            year = _current_season_start_year()
            log.info("current season S%d is live but not yet in the dropdown "
                     "(assuming it starts in %d)", current, year)
            seasons.append(tts.SeasonRef(
                season_id=current, label=f"Fall {year}", start_year=year,
            ))

        if seasons:
            self.record_seasons(seasons)
        return seasons

    def record_seasons(self, seasons: Iterable[tts.SeasonRef]) -> None:
        """Store season numbers and labels, announcing ones we hadn't seen."""
        known = {row["season_id"] for row in self.conn.execute("SELECT season_id FROM seasons")}
        for season in seasons:
            if season.season_id not in known:
                log.info("new season on site: S%d (%s)", season.season_id, season.label)
            upsert(
                self.conn, "seasons",
                {
                    "season_id": season.season_id,
                    "label": season.label,
                    "start_year": season.start_year,
                    "first_seen_at": now(),
                },
                keys=["season_id"],
                update=["label", "start_year"],   # never overwrite first_seen_at
            )
        self.conn.commit()

    def target_seasons(self, requested: Optional[Iterable[int]] = None) -> list[int]:
        """Which seasons this run should crawl."""
        available = [
            row["season_id"] for row in
            self.conn.execute("SELECT season_id FROM seasons ORDER BY season_id")
        ]
        if requested:
            # Explicitly requested seasons are always processed, even if the
            # season list is unavailable (offline rebuilds from the archive
            # start with an empty database). scan_season() fills in the label
            # and start year from the season page itself.
            return sorted(set(requested))
        if self.config.seasons:
            return [s for s in self.config.seasons if s in available]
        return self._recent_seasons(available)

    def _recent_seasons(self, available: list[int]) -> list[int]:
        """Trim to the configured window of recent seasons.

        Filtered on start year rather than season number, because the numbers
        are irregular -- the site skips them, and the gap between S24 and S27 is
        two seasons, not three.
        """
        if not self.config.seasons_back:
            return available
        years = {
            row["season_id"]: row["start_year"]
            for row in self.conn.execute("SELECT season_id, start_year FROM seasons")
        }
        newest = max((y for y in years.values() if y), default=None)
        if newest is None:
            return available
        cutoff = newest - self.config.seasons_back
        # A season with no known start year is kept: it is about to be scanned,
        # which is what establishes the year.
        return [s for s in available if years.get(s) is None or years[s] >= cutoff]

    # -------------------------------------------------------------- teams
    def scan_season(
        self,
        season_id: int,
        *,
        use_cache: bool = False,
        only_teams: Optional[set[int]] = None,
    ) -> None:
        """Scan every league that carried teams in this season."""
        leagues = self.discover_leagues(season_id)
        for priority, league_id in enumerate(leagues):
            try:
                self.scan_league(
                    season_id, league_id, priority=priority,
                    use_cache=use_cache, only_teams=only_teams,
                )
            except RequestCeilingReached:
                self.stats.stopped_early = True
                raise
            except FetchError as exc:
                log.error("S%d league %s: %s", season_id, league_id, exc)
                self.stats.errors += 1

        self.conn.execute(
            "UPDATE seasons SET last_scanned_at = ? WHERE season_id = ?",
            (now(), season_id),
        )
        self.conn.commit()

    def scan_league(
        self,
        season_id: int,
        league_id: int,
        *,
        priority: int = 0,
        use_cache: bool = False,
        only_teams: Optional[set[int]] = None,
    ) -> None:
        """Divisions, teams, standings, schedules and published totals."""
        page = self.fetcher.get(
            tts.season_index_path(league_id, season_id),
            key=f"s{season_id}/l{league_id}/index",
            use_cache=use_cache,
        )
        # Every season page carries the full season dropdown, so this also
        # seeds an empty database during an offline rebuild.
        self.record_seasons(tts.parse_season_list(page.html))
        self._record_league(league_id, tts.parse_league_name(page.html), priority)

        teams = tts.parse_season_index(page.html)
        if not teams:
            log.info("S%d league %s: nothing published", season_id, league_id)
            return
        log.info("scanning S%d league %s (%s): %d teams",
                 season_id, league_id, tts.parse_league_name(page.html) or "?", len(teams))

        row = self.conn.execute(
            "SELECT start_year FROM seasons WHERE season_id = ?", (season_id,)
        ).fetchone()
        start_year = row["start_year"] if row else None
        if start_year is None:
            log.warning("S%d: unknown start year; game dates will be incomplete", season_id)

        division_ids = self._store_divisions(season_id, league_id, teams)
        self._store_teams(season_id, league_id, priority, teams, division_ids)
        self.conn.commit()

        if only_teams is not None:
            teams = [t for t in teams if t.team_id in only_teams]
        self.stats.teams += len(teams)

        for team in teams:
            try:
                self._scan_team(season_id, league_id, team, start_year,
                                division_ids, use_cache)
            except RequestCeilingReached:
                self.stats.stopped_early = True
                raise
            except FetchError as exc:
                log.error("S%d L%s team %s: %s",
                          season_id, league_id, team.team_id, exc)
                self.stats.errors += 1
            self.conn.commit()

        self.conn.execute(
            "UPDATE leagues SET last_scanned_at = ? WHERE league_id = ?",
            (now(), league_id),
        )
        self.conn.commit()

    def _record_league(self, league_id: int, name: str, priority: int) -> None:
        # Seeded priorities are deliberate orderings; do not let a discovery
        # pass overwrite them with a default.
        row = self.conn.execute(
            "SELECT priority, name FROM leagues WHERE league_id = ?", (league_id,)
        ).fetchone()
        keep_priority = row["priority"] if row else priority
        upsert(
            self.conn, "leagues",
            {"league_id": league_id,
             "name": name or (row["name"] if row else None) or f"league {league_id}",
             "priority": keep_priority, "first_seen_at": now()},
            keys=["league_id"],
            update=["name", "priority"],
        )

    def _store_divisions(
        self, season_id: int, league_id: int, teams: list[tts.TeamRef]
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        for team in teams:
            division = team.division
            if not division or division.name in ids:
                continue
            upsert(
                self.conn, "divisions",
                {
                    "season_id": season_id, "league_id": league_id,
                    "name": division.name,
                    "level": division.level, "conf": division.conf,
                    "sort_order": division.sort_order,
                    "gender": N.division_gender(division.name),
                },
                keys=["season_id", "league_id", "name"],
            )
            row = self.conn.execute(
                "SELECT division_id FROM divisions "
                "WHERE season_id = ? AND league_id = ? AND name = ?",
                (season_id, league_id, division.name),
            ).fetchone()
            ids[division.name] = row["division_id"]
        return ids

    def _store_teams(
        self,
        season_id: int,
        league_id: int,
        priority: int,
        teams: list[tts.TeamRef],
        division_ids: dict[str, int],
    ) -> None:
        # Two teams from one club can share a division; number them so they can
        # be told apart in exports.
        seen_clubs: dict[tuple[str, Optional[int]], int] = {}
        for team in teams:
            division_id = division_ids.get(team.division.name) if team.division else None
            gender = N.division_gender(
                team.division.name if team.division else "", team.name)
            club = clubs_mod.canonical_name(team.name, gender)
            slot = (club, division_id)
            seen_clubs[slot] = seen_clubs.get(slot, 0) + 1

            # Team ids are global, so the same team turns up in tournament
            # leagues too. The highest-priority league owns the identity
            # columns; every appearance is recorded in team_leagues.
            upsert(
                self.conn, "team_leagues",
                {"team_id": team.team_id, "season_id": season_id,
                 "league_id": league_id, "division_id": division_id,
                 "name": team.name},
                keys=["team_id", "season_id", "league_id"],
            )

            if not self._owns_team(team.team_id, season_id, priority):
                continue

            upsert(
                self.conn, "teams",
                {
                    "team_id": team.team_id, "season_id": season_id,
                    "name": team.name, "club": club, "division_id": division_id,
                    "club_seq": seen_clubs[slot], "league_id": league_id,
                    # A girls team can sit inside a co-ed division, so the
                    # team's own name matters as much as the division's.
                    "gender": gender,
                    "first_seen_at": now(),
                },
                keys=["team_id", "season_id"],
                update=["name", "club", "division_id", "club_seq", "gender", "league_id"],
            )
            if team.standings and any(v is not None for v in team.standings.values()):
                upsert(
                    self.conn, "standings",
                    {"season_id": season_id, "team_id": team.team_id,
                     **team.standings, "updated_at": now()},
                    keys=["team_id", "season_id"],
                )

    def _owns_team(self, team_id: int, season_id: int, priority: int) -> bool:
        """True when the league being scanned should own this team's row."""
        row = self.conn.execute("""
            SELECT COALESCE(l.priority, 999) AS priority
              FROM teams t LEFT JOIN leagues l ON l.league_id = t.league_id
             WHERE t.team_id = ? AND t.season_id = ?
        """, (team_id, season_id)).fetchone()
        return row is None or priority <= row["priority"]

    def _scan_team(
        self,
        season_id: int,
        league_id: int,
        team: tts.TeamRef,
        start_year: Optional[int],
        division_ids: dict[str, int],
        use_cache: bool,
    ) -> None:
        page = self.fetcher.get(
            tts.team_path(league_id, season_id, team.team_id),
            key=f"s{season_id}/l{league_id}/team/{team.team_id}",
            use_cache=use_cache,
        )
        parsed = tts.parse_team_page(page.html)
        division_id = division_ids.get(team.division.name) if team.division else None

        for game in parsed.games:
            self._store_game(season_id, league_id, game, team, start_year,
                             division_id, division_ids)

        for row in parsed.stat_rows:
            upsert(
                self.conn, "team_stat_rows",
                {
                    "season_id": season_id, "team_id": team.team_id,
                    "kind": row.kind, "row_index": row.row_index,
                    "name": row.name, "jersey": row.jersey, "gp": row.gp,
                    "data_json": _json(row.data), "updated_at": now(),
                },
                keys=["season_id", "team_id", "kind", "row_index"],
            )

    def _store_game(
        self,
        season_id: int,
        league_id: int,
        game: tts.ScheduleGame,
        team: tts.TeamRef,
        start_year: Optional[int],
        team_division_id: Optional[int],
        division_ids: dict[str, int],
    ) -> None:
        """Record a schedule row.

        Each game appears on both teams' pages. Rather than guess sides from
        names alone -- two teams in one division can share a club name -- the
        side is resolved from whichever team's page we are reading, so crawling
        both pages fills in both ids.
        """
        self.stats.games_seen += 1

        existing = self.conn.execute(
            "SELECT game_id, schedule_hash, home_team_id, away_team_id "
            "FROM games WHERE game_id = ?", (game.game_id,)
        ).fetchone()
        if existing is None:
            self.stats.games_new += 1

        home_id = existing["home_team_id"] if existing else None
        away_id = existing["away_team_id"] if existing else None
        if game.home_name == game.away_name:
            # A club can enter two teams in one division, and the schedule row
            # then names both sides identically -- this page cannot say which
            # one we are. resolve_ambiguous_sides() settles it from the
            # scoresheet rosters once they have been fetched.
            pass
        elif team.name == game.home_name:
            home_id = team.team_id
        elif team.name == game.away_name:
            away_id = team.team_id
        needs_review = int(home_id is None or away_id is None)

        division_id = division_ids.get(game.level, team_division_id)
        row = {
            "game_id": game.game_id,
            "season_id": season_id,
            "league_id": league_id,
            "division_id": division_id,
            "date_text": game.date_text,
            "date_iso": tts.schedule_date_to_iso(game.date_text, start_year),
            "time_text": game.time_text,
            "rink": game.rink,
            "league": game.league,
            "level": game.level,
            "game_type": game.game_type,
            "game_class": tts.classify_game_type(game.game_type),
            "away_team_id": away_id,
            "home_team_id": home_id,
            "away_name": game.away_name,
            "home_name": game.home_name,
            "away_goals": game.away_goals,
            "home_goals": game.home_goals,
            "status": "final" if game.is_final else "scheduled",
            "has_scoresheet": int(game.has_scoresheet),
            "schedule_hash": _hash_game(game),
            "needs_review": needs_review,
            "updated_at": now(),
        }
        upsert(
            self.conn, "games", row, keys=["game_id"],
            update=[c for c in row if c != "game_id"],
        )

    # -------------------------------------------------------- scoresheets
    def pending_scoresheets(self, seasons: Iterable[int], *, force: bool = False) -> list[int]:
        """Games whose scoresheet should be fetched, newest first.

        A game qualifies when it is final, has a sheet, and either has never
        been parsed, was parsed by an older parser version, changed since it was
        last read, or was played recently enough that the scorekeeper may still
        correct it.
        """
        season_list = list(seasons)
        if not season_list:
            return []
        placeholders = ",".join("?" for _ in season_list)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.config.recheck_days)).date().isoformat()

        if force:
            condition = "1 = 1"
            params: list = list(season_list)
        else:
            condition = """(
                   g.scoresheet_at IS NULL
                OR g.parse_version IS NULL
                OR g.parse_version < ?
                OR (g.date_iso IS NOT NULL AND g.date_iso >= ?
                    AND g.scoresheet_at < ?)
            )"""
            params = [
                *season_list, tts.PARSE_VERSION, cutoff,
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),
            ]

        sql = f"""
            SELECT g.game_id FROM games g
             WHERE g.season_id IN ({placeholders})
               AND g.status = 'final'
               AND g.has_scoresheet = 1
               AND {condition}
             ORDER BY g.date_iso DESC, g.game_id DESC
        """
        return [row["game_id"] for row in self.conn.execute(sql, params)]

    def reparsable_scoresheets(self, seasons: Iterable[int]) -> set[int]:
        """Games that need reprocessing but not re-downloading.

        A ``PARSE_VERSION`` bump marks every stored scoresheet for another look.
        The page itself has not changed, though, and the archive already holds
        it -- so these are re-read from disk. Without this, improving the parser
        would cost thousands of requests to a volunteer-run site for pages we
        already have.
        """
        season_list = list(seasons)
        if not season_list:
            return set()
        placeholders = ",".join("?" for _ in season_list)
        recent = (datetime.now(timezone.utc)
                  - timedelta(days=self.config.recheck_days)).date().isoformat()
        rows = self.conn.execute(f"""
            SELECT g.game_id FROM games g
             WHERE g.season_id IN ({placeholders})
               AND g.status = 'final' AND g.has_scoresheet = 1
               AND g.scoresheet_at IS NOT NULL
               AND (g.parse_version IS NULL OR g.parse_version < ?)
               -- A recently played game may have been corrected since, so it
               -- is fetched afresh rather than re-read.
               AND (g.date_iso IS NULL OR g.date_iso < ?)
        """, [*season_list, tts.PARSE_VERSION, recent]).fetchall()
        return {row["game_id"] for row in rows}

    def pending_scorecards(self, seasons: Iterable[int], *, force: bool = False) -> list[int]:
        """Played games whose PDF scorecard should be fetched, newest first.

        Only games with a final score, since the Goaltender Records table is
        checked against it. A game qualifies when it has never had a scorecard
        read, was read by an older scorecard parser, or was played recently
        enough that the sheet may still be corrected -- the same freshness rule
        the scoresheets use, so the two stay in step.
        """
        season_list = list(seasons)
        if not season_list:
            return []
        placeholders = ",".join("?" for _ in season_list)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.config.recheck_days)).date().isoformat()

        if force:
            condition = "1 = 1"
            params: list = list(season_list)
        else:
            condition = """(
                   g.scorecard_at IS NULL
                OR g.scorecard_parse_version IS NULL
                OR g.scorecard_parse_version < ?
                OR (g.date_iso IS NOT NULL AND g.date_iso >= ?
                    AND g.scorecard_at < ?)
            )"""
            params = [
                *season_list, tts.SCORECARD_PARSE_VERSION, cutoff,
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),
            ]

        sql = f"""
            SELECT g.game_id FROM games g
             WHERE g.season_id IN ({placeholders})
               AND g.status = 'final'
               AND g.has_scoresheet = 1
               AND g.home_goals IS NOT NULL AND g.away_goals IS NOT NULL
               AND {condition}
             ORDER BY g.date_iso DESC, g.game_id DESC
        """
        return [row["game_id"] for row in self.conn.execute(sql, params)]

    def reparsable_scorecards(self, seasons: Iterable[int]) -> set[int]:
        """Scorecards to re-read from the archive rather than re-download."""
        season_list = list(seasons)
        if not season_list:
            return set()
        placeholders = ",".join("?" for _ in season_list)
        recent = (datetime.now(timezone.utc)
                  - timedelta(days=self.config.recheck_days)).date().isoformat()
        rows = self.conn.execute(f"""
            SELECT g.game_id FROM games g
             WHERE g.season_id IN ({placeholders})
               AND g.status = 'final' AND g.has_scoresheet = 1
               AND g.scorecard_at IS NOT NULL
               AND (g.scorecard_parse_version IS NULL
                    OR g.scorecard_parse_version < ?)
               AND (g.date_iso IS NULL OR g.date_iso < ?)
        """, [*season_list, tts.SCORECARD_PARSE_VERSION, recent]).fetchall()
        return {row["game_id"] for row in rows}

    def fetch_scoresheets(
        self, game_ids: Iterable[int], *, use_cache: bool = False, limit: Optional[int] = None
    ) -> None:
        game_ids = list(game_ids)
        if limit:
            game_ids = game_ids[:limit]
        total = len(game_ids)
        log.info("fetching %d scoresheet(s)", total)

        for i, game_id in enumerate(game_ids, 1):
            row = self.conn.execute(
                "SELECT season_id FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
            season_id = row["season_id"] if row else 0
            try:
                page = self.fetcher.get(
                    tts.scoresheet_path(game_id),
                    key=f"s{season_id}/game/{game_id}",
                    use_cache=use_cache,
                )
            except (RequestCeilingReached, RateLimited) as exc:
                # Not a problem with this game: stop, keeping what was already
                # collected. Re-running continues from here.
                log.warning("%s", exc)
                log.warning("stopped after %d of %d scoresheet(s); "
                            "re-run the same command to continue", i - 1, total)
                self.conn.commit()
                self.stats.stopped_early = True
                return
            except FetchError as exc:
                log.error("game %s: %s", game_id, exc)
                self.conn.execute(
                    "UPDATE games SET parse_error = ?, updated_at = ? WHERE game_id = ?",
                    (str(exc)[:300], now(), game_id),
                )
                self.stats.errors += 1
                self.conn.commit()
                continue

            self.store_scoresheet(game_id, page.html, page.sha256)
            self.stats.scoresheets += 1
            if i % 25 == 0 or i == total:
                log.info("  %d/%d scoresheets", i, total)
                self.conn.commit()
        self.conn.commit()

    def store_scoresheet(self, game_id: int, html: str, sha: str) -> None:
        """Parse one scoresheet and replace all stored detail for that game."""
        sheet = tts.parse_scoresheet(html, game_id)

        # A sheet whose date is not the fixture's date is not the fixture's
        # sheet. That happens when the game id was wrong: asking for game 1
        # returns the site's real game 1, played in 2010, whose roster then
        # lands on a 2024 tournament fixture -- 87 players who never existed
        # here, and a season year dragged back fourteen years by the date.
        # Rejecting it leaves the fixture intact and unparsed, which is what an
        # unreadable sheet should look like.
        if sheet.is_usable and sheet.date_iso:
            scheduled = self.conn.execute(
                "SELECT date_text FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
            expected = tts.schedule_day_month(scheduled["date_text"]) if scheduled else None
            if expected and expected != (int(sheet.date_iso[5:7]), int(sheet.date_iso[8:10])):
                log.warning(
                    "game %s: scoresheet is dated %s but the schedule says %r; "
                    "refusing it as another game's sheet",
                    game_id, sheet.date_iso, scheduled["date_text"])
                self.conn.execute(
                    "UPDATE games SET parse_error = ?, scoresheet_sha = ?, "
                    "scoresheet_at = ?, parse_version = ?, updated_at = ? "
                    "WHERE game_id = ?",
                    (f"scoresheet dated {sheet.date_iso} does not match the schedule",
                     sha, now(), tts.PARSE_VERSION, now(), game_id),
                )
                self.stats.empty_sheets += 1
                return

        if not sheet.is_usable:
            # Record the attempt, so a sheet that genuinely has no roster is not
            # refetched every night for the rest of the season. Bumping
            # PARSE_VERSION is what makes an improved parser try again.
            self.conn.execute(
                "UPDATE games SET parse_error = ?, scoresheet_sha = ?, "
                "scoresheet_at = ?, parse_version = ?, updated_at = ? "
                "WHERE game_id = ?",
                ("scoresheet contained no roster", sha, now(),
                 tts.PARSE_VERSION, now(), game_id),
            )
            self.stats.empty_sheets += 1
            return

        # Detail tables cascade from a single delete, keeping re-parsing clean.
        for table in ("game_rosters", "goals", "penalties", "goalie_stints",
                      "shot_marks", "period_scores"):
            self.conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))

        for side_name, side in (("home", sheet.home), ("away", sheet.away)):
            self.conn.executemany(
                "INSERT INTO game_rosters(game_id, side, slot, jersey, position, name, role) "
                "VALUES (?,?,?,?,?,?,?)",
                [(game_id, side_name, e.slot, e.jersey, e.position,
                  N.clean_name(e.name), e.role) for e in side.roster],
            )
            self.conn.executemany(
                "INSERT INTO goals(game_id, side, seq, period, time_text, time_sec, "
                "strength, scorer_jersey, assist1_jersey, assist2_jersey) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(game_id, side_name, e.seq, e.period, e.time_text, e.time_sec,
                  e.strength, e.scorer, e.assist1, e.assist2) for e in side.goals],
            )
            self.conn.executemany(
                "INSERT INTO penalties(game_id, side, seq, period, jersey, infraction, "
                "minutes, off_ice, start_time, end_time, on_ice) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(game_id, side_name, e.seq, e.period, e.jersey, e.infraction, e.minutes,
                  e.off_ice, e.start_time, e.end_time, e.on_ice) for e in side.penalties],
            )
            self.conn.executemany(
                "INSERT INTO goalie_stints(game_id, side, seq, name, note) VALUES (?,?,?,?,?)",
                [(game_id, side_name, e.seq, N.clean_name(e.name), e.note)
                 for e in side.goalies],
            )
            self.conn.executemany(
                "INSERT INTO period_scores(game_id, side, period, goals) VALUES (?,?,?,?)",
                [(game_id, side_name, period, goals)
                 for period, goals in side.period_goals.items()],
            )
            if side.shot_header is not None or side.shot_marked is not None:
                # Marked shots are only trustworthy when the marked goals match
                # the goals actually scored against that side.
                conceded = (sheet.away.final if side_name == "home" else sheet.home.final)
                reliable = int(
                    side.shot_goals is not None and conceded is not None
                    and side.shot_goals == conceded
                )
                self.conn.execute(
                    "INSERT INTO shot_marks(game_id, side, header_saves, marked, "
                    "goals_marked, reliable) VALUES (?,?,?,?,?,?)",
                    (game_id, side_name, side.shot_header, side.shot_marked,
                     side.shot_goals, reliable),
                )

        updates = {
            "scoresheet_sha": sha,
            "scoresheet_at": now(),
            "parse_version": tts.PARSE_VERSION,
            "parse_error": "; ".join(sheet.warnings)[:300] if sheet.warnings else None,
            "updated_at": now(),
        }
        if sheet.date_iso:
            updates["date_iso"] = sheet.date_iso
        if sheet.level:
            updates["level"] = sheet.level
        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.conn.execute(
            f"UPDATE games SET {assignments} WHERE game_id = ?",
            [*updates.values(), game_id],
        )

        if sheet.warnings:
            self.conn.executemany(
                "INSERT INTO anomalies(kind, game_id, detail, found_at) VALUES (?,?,?,?)",
                [("scoresheet", game_id, w, now()) for w in sheet.warnings],
            )

    def store_scorecard(self, game_id: int, payload: bytes, sha: str) -> None:
        """Parse one PDF scorecard's Goaltender Records and store what reconciles.

        Only a side whose goalie goals-against sum to the score is kept. A blank
        saves column or an inconsistent table is worse than the derived fallback
        it would replace, so it is recorded as an error and the fallback stands.
        """
        import json

        card = tts.parse_scorecard(payload, game_id)
        row = self.conn.execute(
            "SELECT home_goals, away_goals FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        home_goals = row["home_goals"] if row else None
        away_goals = row["away_goals"] if row else None
        ok = tts.reconcile_scorecard(card, home_goals, away_goals)

        self.conn.execute("DELETE FROM goalie_records WHERE game_id = ?", (game_id,))
        kept = 0
        problems = list(card.warnings)
        for side_name, records in (("home", card.home), ("away", card.away)):
            if not records:
                continue
            if not ok[side_name]:
                problems.append(f"{side_name} goalie records do not reconcile with the score")
                continue
            for rec in records:
                by_period = {
                    p: {"shots": rec.shots.get(p), "saves": rec.saves.get(p)}
                    for p in sorted(set(rec.shots) | set(rec.saves))
                    if p != "Total"
                }
                self.conn.execute(
                    "INSERT INTO goalie_records(game_id, side, seq, jersey, shots, "
                    "saves, goals_against, by_period) VALUES (?,?,?,?,?,?,?,?)",
                    (game_id, side_name, rec.seq, rec.jersey, rec.total_shots,
                     rec.total_saves, rec.goals_against, json.dumps(by_period)),
                )
                kept += 1

        self.conn.execute(
            "UPDATE games SET scorecard_sha = ?, scorecard_at = ?, "
            "scorecard_parse_version = ?, scorecard_error = ?, updated_at = ? "
            "WHERE game_id = ?",
            (sha, now(), tts.SCORECARD_PARSE_VERSION,
             "; ".join(problems)[:300] if problems else None, now(), game_id),
        )
        if kept:
            self.stats.scorecards += 1
        else:
            self.stats.scorecards_empty += 1

    def fetch_scorecards(
        self,
        game_ids: Iterable[int],
        *,
        use_cache: bool = False,
        limit: Optional[int] = None,
    ) -> None:
        """Fetch and store the PDF scorecard for each game."""
        game_ids = list(game_ids)
        if limit:
            game_ids = game_ids[:limit]
        total = len(game_ids)
        log.info("fetching %d scorecard(s)", total)

        for i, game_id in enumerate(game_ids, 1):
            row = self.conn.execute(
                "SELECT season_id FROM games WHERE game_id = ?", (game_id,)
            ).fetchone()
            season_id = row["season_id"] if row else 0
            try:
                page = self.fetcher.get_bytes(
                    tts.scorecard_path(game_id),
                    key=f"s{season_id}/scorecard/{game_id}",
                    ext="pdf",
                    use_cache=use_cache,
                )
            except (RequestCeilingReached, RateLimited) as exc:
                # Both mean stop, not fail. The site is asking us to back off (or
                # we hit our own ceiling); the games not yet reached stay
                # never-attempted, so the next run resumes this season rather
                # than skipping past what it never fetched.
                log.warning("%s", exc)
                log.warning("stopped after %d of %d scorecard(s); "
                            "re-run to continue", i - 1, total)
                self.conn.commit()
                self.stats.stopped_early = True
                return
            except FetchError as exc:
                log.error("game %s scorecard: %s", game_id, exc)
                self.conn.execute(
                    "UPDATE games SET scorecard_error = ?, updated_at = ? WHERE game_id = ?",
                    (str(exc)[:300], now(), game_id),
                )
                self.stats.errors += 1
                self.conn.commit()
                continue

            self.store_scorecard(game_id, page.payload, page.sha256)
            if i % 25 == 0 or i == total:
                log.info("  %d/%d scorecards", i, total)
                self.conn.commit()
        self.conn.commit()

    # ------------------------------------------------------------- derive
    def derive(self) -> dict[str, int]:
        """Rebuild identities and per-game stat lines. No network access."""
        refine_season_years(self.conn)
        resolved = resolve_ambiguous_sides(self.conn)
        if resolved:
            log.info("resolved %d ambiguous team side(s) by roster match", resolved)
        clubs_built = rebuild_clubs(self.conn)
        log.info("  %d club(s) from team names", clubs_built)
        log.info("resolving player identities")
        result = identity.rebuild(self.conn)
        log.info("  %(players)d players from %(names)d spellings", result)
        rebuild_player_game_stats(self.conn)
        audit(self.conn)
        raise_team_questions(self.conn)
        raise_oversized_rosters(self.conn)
        self.conn.commit()
        if result.get("questions"):
            log.info("  %d name question(s) for review "
                     "(run: norcalstats review list)", result["questions"])
        return result


# --------------------------------------------------------------- derivation


def rebuild_clubs(conn) -> int:
    """Recompute every team's club and refill the clubs table.

    Run from ``derive`` rather than at fetch time so that changing a naming
    rule fixes the whole history without re-fetching anything: the team names
    are already stored, and the club is only ever a reading of them.
    """
    rows = list(conn.execute(
        "SELECT t.team_id, t.season_id, t.name, t.gender, t.league_id,"
        "       d.name AS division"
        "  FROM teams t LEFT JOIN divisions d ON d.division_id = t.division_id"))

    leagues: dict[str, set[int]] = {}
    # A club is a high school only if it has never played anywhere else, so
    # this tracks whether every one of its team-seasons was in one.
    only_hs: dict[str, bool] = {}
    assigned: list[tuple[str, str, int, int]] = []
    club_of: dict[tuple[int, int], str] = {}
    for row in rows:
        # Gender is read again rather than trusted, for the same reason the club
        # is: it was decided by a rule, and the rule can be wrong. A girls team
        # in a co-ed division is recognised only by its own name, so widening
        # that rule has to reach the seasons already stored.
        gender = N.division_gender(row["division"] or "", row["name"])
        club = clubs_mod.canonical_name(row["name"], gender)
        assigned.append((club, gender, row["team_id"], row["season_id"]))
        club_of[(row["team_id"], row["season_id"])] = club
        leagues.setdefault(club, set()).add(row["league_id"])
        only_hs[club] = (only_hs.get(club, True)
                         and clubs_mod.is_high_school_division(row["division"] or ""))

    conn.executemany(
        "UPDATE teams SET club = ?, gender = ? WHERE team_id = ? AND season_id = ?",
        assigned)

    # club_seq only means anything relative to the club, so recanonicalising the
    # clubs invalidates it: teams that used to sit under three spellings now
    # share one, and each spelling had started counting at one. Renumber by
    # team id, which is stable across runs.
    seq_rows = conn.execute(
        "SELECT team_id, season_id, club, division_id FROM teams"
        " ORDER BY season_id, club, division_id, team_id")
    counter: dict[tuple, int] = {}
    renumbered = []
    for row in seq_rows:
        slot = (row["season_id"], row["club"], row["division_id"])
        counter[slot] = counter.get(slot, 0) + 1
        renumbered.append((counter[slot], row["team_id"], row["season_id"]))
    conn.executemany(
        "UPDATE teams SET club_seq = ? WHERE team_id = ? AND season_id = ?",
        renumbered)

    # A team id is global, so one team turns up in tournament leagues too.
    # Every appearance counts towards whether the club is one of ours.
    for row in conn.execute("SELECT team_id, season_id, league_id FROM team_leagues"):
        club = club_of.get((row["team_id"], row["season_id"]))
        if club:
            leagues[club].add(row["league_id"])

    conn.execute("DELETE FROM clubs")
    conn.executemany(
        "INSERT INTO clubs(name, short_name, kind) VALUES (?, ?, ?)",
        [(name, clubs_mod.short_name(name),
          clubs_mod.classify(name, seen, only_high_school=only_hs.get(name, False)))
         for name, seen in sorted(leagues.items())],
    )
    conn.commit()
    return len(leagues)


#: A roster match needs this many shared names, and this much of a lead over
#: the runner-up, before it is trusted to identify a team.
_MATCH_MIN = 3
_MATCH_MARGIN = 2


def resolve_ambiguous_sides(conn: sqlite3.Connection) -> int:
    """Identify teams for games the schedule pages could not resolve.

    A club often enters several teams in one division, all printed with the
    same name ("San Jose Jr Sharks" twice in 10U A). When such teams meet, the
    schedule row is identical on both teams' pages and neither side can be
    identified from it.

    The scoresheet can tell them apart: each side's roster is compared against
    the rosters the site publishes per team, and a side is assigned only on a
    clear win. Ambiguous games are left unresolved and flagged rather than
    guessed at.
    """
    candidates = conn.execute("""
        SELECT g.game_id, g.season_id, g.division_id, g.home_name, g.away_name,
               g.home_team_id, g.away_team_id
          FROM games g
         WHERE (g.home_team_id IS NULL OR g.away_team_id IS NULL)
           AND g.scoresheet_at IS NOT NULL
    """).fetchall()
    if not candidates:
        return 0

    # Published roster per team, used as the reference to match against.
    known: dict[tuple[int, int], set[str]] = {}
    for row in conn.execute(
        "SELECT season_id, team_id, name FROM team_stat_rows WHERE name <> ''"
    ):
        known.setdefault((row["season_id"], row["team_id"]), set()).add(
            N.clean_name(row["name"])
        )

    resolved = 0
    for game in candidates:
        rosters: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT side, name FROM game_rosters WHERE game_id = ? AND role = 'player'",
            (game["game_id"],),
        ):
            rosters.setdefault(row["side"], set()).add(row["name"])

        assigned = {
            "home": game["home_team_id"],
            "away": game["away_team_id"],
        }
        for side in ("home", "away"):
            if assigned[side] is not None or side not in rosters:
                continue
            name = game[f"{side}_name"]
            taken = {v for v in assigned.values() if v is not None}

            scores: list[tuple[int, int]] = []
            for row in conn.execute(
                "SELECT team_id, division_id FROM teams "
                "WHERE season_id = ? AND name = ?",
                (game["season_id"], name),
            ):
                team_id = row["team_id"]
                if team_id in taken:
                    continue
                if (game["division_id"] and row["division_id"]
                        and row["division_id"] != game["division_id"]):
                    continue
                overlap = len(rosters[side] & known.get((game["season_id"], team_id), set()))
                scores.append((overlap, team_id))

            if not scores:
                continue
            scores.sort(reverse=True)
            best, best_id = scores[0]
            runner_up = scores[1][0] if len(scores) > 1 else 0
            if best >= _MATCH_MIN and best - runner_up >= _MATCH_MARGIN:
                assigned[side] = best_id

        updates = {
            side: assigned[side] for side in ("home", "away")
            if assigned[side] != game[f"{side}_team_id"]
        }
        if updates:
            sets = ", ".join(f"{side}_team_id = ?" for side in updates)
            conn.execute(
                f"UPDATE games SET {sets}, needs_review = ?, updated_at = ? WHERE game_id = ?",
                [*updates.values(),
                 int(assigned["home"] is None or assigned["away"] is None),
                 now(), game["game_id"]],
            )
            resolved += len(updates)

    conn.commit()
    return resolved


def raise_team_questions(conn: sqlite3.Connection) -> None:
    """Add review items for games whose teams could not be identified.

    Only games where both sides print the same club name are worth asking
    about; a merely unscanned opponent resolves itself on the next full run.
    """
    items = []
    for row in conn.execute("""
        SELECT g.game_id, g.season_id, g.date_iso, g.home_name, g.level,
               g.home_team_id, g.away_team_id,
               COUNT(r.slot) AS roster_rows
          FROM games g
          LEFT JOIN game_rosters r ON r.game_id = g.game_id
         WHERE g.home_name = g.away_name
           AND (g.home_team_id IS NULL OR g.away_team_id IS NULL)
           AND g.status = 'final'
         GROUP BY g.game_id
         ORDER BY g.date_iso
    """):
        unknown = [
            side for side, value in
            (("home", row["home_team_id"]), ("away", row["away_team_id"]))
            if value is None
        ]
        known = [
            f"{side} = team {value}" for side, value in
            (("home", row["home_team_id"]), ("away", row["away_team_id"]))
            if value is not None
        ]
        # Only teams in this game's own division are plausible candidates.
        candidates = [
            f"team {t['team_id']} ({t['division']})" for t in conn.execute("""
                SELECT t.team_id, COALESCE(d.name, '?') AS division
                  FROM teams t LEFT JOIN divisions d ON d.division_id = t.division_id
                 WHERE t.season_id = ? AND t.name = ?
                   AND (d.name = ? OR ? IS NULL)
                 ORDER BY t.team_id
            """, (row["season_id"], row["home_name"], row["level"], row["level"]))
        ]
        items.append(review.Item(
            kind="ambiguous_team",
            subject=(f"S{row['season_id']} {row['date_iso'] or '?'}: "
                     f"{row['home_name']} vs {row['home_name']} ({row['level']}) "
                     f"- {' and '.join(unknown)} side not identified"),
            evidence={"names": [], "detail": [
                f"candidate teams: {', '.join(candidates) or 'none found'}",
                f"already identified: {', '.join(known) or 'neither side'}",
                f"roster rows parsed: {row['roster_rows']}",
                "the club entered several teams under one name",
            ]},
            suggestion=(f"set it by hand, or 'dismiss' to stop asking: "
                        f"UPDATE games SET {unknown[0]}_team_id=<id> "
                        f"WHERE game_id={row['game_id']}"),
            applied=(f"{' and '.join(unknown)} side left unassigned, so this game "
                     "is excluded from that team's totals"),
            confidence=0.2,
            parts=(str(row["game_id"]),),
        ))
    # Always recorded, even when empty: a game whose teams were identified on a
    # later run should stop being asked about.
    review.record(conn, items, sweep=("ambiguous_team",))


def squad_clusters(conn: sqlite3.Connection, season_id: int, team_id: int,
                   threshold: float = 0.3) -> list[dict]:
    """Partition a team-season's games into squads by roster overlap.

    Single-linkage on the Jaccard overlap of each game's roster: games of one
    squad share most of their players and chain together, while a game belonging
    to a different squad shares few and falls out on its own. So a team that is
    really one squad plus a stray mis-filed game comes back as a big cluster and
    a small one. Returned largest first, each carrying its games and the union of
    their players.
    """
    rosters: dict[int, set[int]] = {}
    for r in conn.execute(
        "SELECT s.game_id, s.player_id FROM player_game_stats s "
        "  JOIN games g ON g.game_id = s.game_id "
        " WHERE g.season_id = ? AND s.team_id = ?", (season_id, team_id)
    ):
        rosters.setdefault(r["game_id"], set()).add(r["player_id"])
    games = list(rosters)
    parent = {g: g for g in games}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(games)):
        for j in range(i + 1, len(games)):
            a, b = rosters[games[i]], rosters[games[j]]
            union = len(a | b)
            if union and len(a & b) / union >= threshold:
                parent[find(games[i])] = find(games[j])

    grouped: dict[int, list[int]] = {}
    for g in games:
        grouped.setdefault(find(g), []).append(g)
    clusters = [{"games": gs, "players": set().union(*(rosters[g] for g in gs))}
                for gs in grouped.values()]
    clusters.sort(key=lambda c: len(c["games"]), reverse=True)
    return clusters


def _outlier_home(conn: sqlite3.Connection, season_id: int, players: set,
                  exclude_team: int):
    """The team these players most often play for other than ``exclude_team`` --
    where an outlier game most likely belongs."""
    if not players:
        return None
    marks = ",".join("?" * len(players))
    row = conn.execute(
        f"SELECT s.team_id, t.name, COUNT(*) AS n "
        f"  FROM player_game_stats s "
        f"  JOIN games g ON g.game_id = s.game_id "
        f"  JOIN teams t ON t.team_id = s.team_id AND t.season_id = g.season_id "
        f" WHERE g.season_id = ? AND s.team_id <> ? "
        f"   AND s.player_id IN ({marks}) "
        f" GROUP BY s.team_id ORDER BY n DESC LIMIT 1",
        (season_id, exclude_team, *players)).fetchone()
    return (row["team_id"], row["name"]) if row else None


#: A USA Hockey roster tops out at 22 players. A team-season carrying more than
#: that is almost always two squads the source filed under one team id -- as CAHA
#: did with Golden State Elite's two 14U AA teams in 2022-23, whose 38 players
#: landed on one team. Counting the roster is a cheap, early tell that a merge
#: has happened, long before anyone reads the names.
MAX_ROSTER = 22


def raise_oversized_rosters(conn: sqlite3.Connection) -> None:
    """Flag team-seasons whose player count exceeds a legal roster.

    Read from the materialized stat lines, so it runs after
    ``rebuild_player_game_stats``. Self-heals like the other team questions: a
    team split or dismissed on a later run drops off the queue.
    """
    items = []
    for row in conn.execute("""
        SELECT g.season_id, s.team_id, t.name,
               COALESCE(d.name, '?') AS division,
               COUNT(DISTINCT s.player_id) AS players
          FROM player_game_stats s
          JOIN games g ON g.game_id = s.game_id
          JOIN teams t ON t.team_id = s.team_id AND t.season_id = g.season_id
          LEFT JOIN divisions d ON d.division_id = t.division_id
          LEFT JOIN clubs c ON c.name = t.club
         WHERE s.team_id IS NOT NULL
           -- High school rosters are allowed to be larger, so the cap does not
           -- apply to them.
           AND (c.kind IS NULL OR c.kind != 'high_school')
         GROUP BY g.season_id, s.team_id
        HAVING players > ?
         ORDER BY players DESC
    """, (MAX_ROSTER,)):
        detail = [
            f"{row['players']} distinct players, over the {MAX_ROSTER}-player "
            "USA Hockey maximum",
        ]
        # Break the games into squads by roster overlap. A clean one-squad team
        # with heavy call-ups stays one cluster; a merge splits, and a small
        # off-cluster is usually games mis-filed from another team.
        clusters = squad_clusters(conn, row["season_id"], row["team_id"])
        if len(clusters) > 1:
            total = sum(len(c["games"]) for c in clusters)
            sizes = " + ".join(str(len(c["games"])) for c in clusters)
            detail.append(f"its {total} games split into {sizes} by roster overlap")
            for c in clusters[1:]:
                home = _outlier_home(conn, row["season_id"], c["players"],
                                     row["team_id"])
                where = (f"; those players otherwise skate for {home[1]} "
                         f"(team {home[0]})") if home else ""
                games = ", ".join(str(g) for g in sorted(c["games"])[:6])
                detail.append(
                    f"{len(c['games'])} game(s) look like a different squad "
                    f"({games}){where}")
        else:
            detail.append("usually two squads the source filed under a single team id")
        detail.append("a season of heavy call-ups can reach this honestly -- "
                      "'dismiss' if that is what it is")
        items.append(review.Item(
            kind="oversized_roster",
            subject=(f"S{row['season_id']} {row['name']} ({row['division']}): "
                     f"{row['players']} players on one team"),
            evidence={"names": [], "detail": detail},
            suggestion="split the team into its squads, or 'dismiss' to accept the count",
            applied="left as one team; its totals combine every squad that played",
            confidence=0.3,
            parts=(str(row["season_id"]), str(row["team_id"])),
        ))
    review.record(conn, items, sweep=("oversized_roster",))


def rebuild_player_game_stats(conn: sqlite3.Connection) -> None:
    """Materialize per-player, per-game stat lines from the event tables.

    One player can hold several roster rows in a single game: listed twice on
    the sheet, or -- before placeholders were recognised -- sharing a name with
    everyone on an unsubmitted roster. Those rows are collapsed to one first.
    Inserting them directly violates the primary key, and that used to abort the
    whole derive stage, leaving the database with no derived stats at all.
    """
    conn.execute("DELETE FROM player_game_stats")
    conn.execute("""
        INSERT INTO player_game_stats(
            game_id, player_id, season_id, team_id, side, jersey,
            goals, assists, points, pim, penalties, ppg, shg, is_goalie, goals_against)
        SELECT r.game_id,
               r.player_id,
               g.season_id,
               CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END,
               r.side,
               r.jersey,
               COALESCE(sc.goals, 0),
               COALESCE(a1.assists, 0) + COALESCE(a2.assists, 0),
               COALESCE(sc.goals, 0) + COALESCE(a1.assists, 0) + COALESCE(a2.assists, 0),
               COALESCE(pen.pim, 0),
               COALESCE(pen.count, 0),
               COALESCE(sc.ppg, 0),
               COALESCE(sc.shg, 0),
               r.is_goalie,
               CASE WHEN r.is_goalie = 1
                    THEN (SELECT COUNT(*) FROM goals og
                           WHERE og.game_id = r.game_id AND og.side <> r.side)
                    ELSE NULL END
          FROM (
                -- One row per player per game, whatever the sheet listed.
                SELECT game_id, player_id,
                       MIN(side)   AS side,
                       MIN(jersey) AS jersey,
                       MAX(CASE WHEN UPPER(COALESCE(position, '')) = 'G'
                                THEN 1 ELSE 0 END) AS is_goalie
                  FROM game_rosters
                 WHERE player_id IS NOT NULL AND role = 'player'
                 GROUP BY game_id, player_id
          ) r
          JOIN games g ON g.game_id = r.game_id
          LEFT JOIN (
                SELECT game_id, scorer_player_id AS pid, COUNT(*) AS goals,
                       SUM(CASE WHEN UPPER(strength) = 'PP' THEN 1 ELSE 0 END) AS ppg,
                       SUM(CASE WHEN UPPER(strength) = 'SH' THEN 1 ELSE 0 END) AS shg
                  FROM goals WHERE scorer_player_id IS NOT NULL
                 GROUP BY game_id, scorer_player_id
          ) sc ON sc.game_id = r.game_id AND sc.pid = r.player_id
          LEFT JOIN (
                SELECT game_id, assist1_player_id AS pid, COUNT(*) AS assists
                  FROM goals WHERE assist1_player_id IS NOT NULL
                 GROUP BY game_id, assist1_player_id
          ) a1 ON a1.game_id = r.game_id AND a1.pid = r.player_id
          LEFT JOIN (
                SELECT game_id, assist2_player_id AS pid, COUNT(*) AS assists
                  FROM goals WHERE assist2_player_id IS NOT NULL
                 GROUP BY game_id, assist2_player_id
          ) a2 ON a2.game_id = r.game_id AND a2.pid = r.player_id
          LEFT JOIN (
                SELECT game_id, player_id AS pid, COUNT(*) AS count,
                       SUM(COALESCE(minutes, 0)) AS pim
                  FROM penalties WHERE player_id IS NOT NULL
                 GROUP BY game_id, player_id
          ) pen ON pen.game_id = r.game_id AND pen.pid = r.player_id
    """)
    _apply_goalie_records(conn)


def _apply_goalie_records(conn: sqlite3.Connection) -> None:
    """Override goalie goals-against with the scorecard's real per-goalie figure.

    Without the scorecard, ``rebuild_player_game_stats`` gives every goalie who
    dressed the *side's whole* goals-against -- right when one goalie played,
    doubled when two split the game, and a phantom line for a backup who never
    took the net. The scorecard's Goaltender Records table says exactly how many
    shots each faced and saved, and only reconciling records were stored, so
    where one exists it is the truth and replaces the derived count. Where none
    exists -- most of history, until the scorecards are backfilled -- the
    derived value stands untouched.
    """
    # Resolve each record to a player through the roster's jersey on that side.
    conn.execute("UPDATE goalie_records SET player_id = NULL")
    conn.execute("""
        UPDATE goalie_records
           SET player_id = (
                SELECT r.player_id FROM game_rosters r
                 WHERE r.game_id = goalie_records.game_id
                   AND r.side = goalie_records.side
                   AND r.jersey = goalie_records.jersey
                   AND r.player_id IS NOT NULL
                 LIMIT 1)
    """)

    # A backup who never took the net has a record with 0 shots (or none at
    # all); the reconciling side still lists only goalies who actually played,
    # so a stat line with no matching record but is_goalie=1 keeps its derived
    # value. Override only the lines we have a record for.
    conn.execute("""
        UPDATE player_game_stats
           SET goals_against = (
                SELECT gr.goals_against FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.player_id = player_game_stats.player_id),
               shots_faced = (
                SELECT gr.shots FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.player_id = player_game_stats.player_id),
               saves = (
                SELECT gr.saves FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.player_id = player_game_stats.player_id)
         WHERE is_goalie = 1
           AND EXISTS (
                SELECT 1 FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.player_id = player_game_stats.player_id)
    """)

    # A goalie the scorecard lists who was NOT credited a stat line -- a backup
    # dressed but the derive dropped them for zero appearances -- is already
    # absent, which is correct. But a goalie who dressed, got a derived line
    # with the side's full GA, and did NOT play (no reconciling record naming
    # them) must not keep that phantom GA when a co-goalie's record proves the
    # side's goals were the other goalie's. Zero those out.
    #
    # Only when the side's records were actually attributed to players, though.
    # An old scorecard with blank jerseys stores records that match no roster
    # row (player_id NULL); zeroing every goalie against those would wrongly
    # wipe a real goalie's line, so such a side keeps the derived fallback.
    conn.execute("""
        UPDATE player_game_stats
           SET goals_against = 0, shots_faced = 0, saves = 0
         WHERE is_goalie = 1
           AND goals_against IS NOT NULL
           AND NOT EXISTS (
                SELECT 1 FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.player_id = player_game_stats.player_id)
           AND EXISTS (
                SELECT 1 FROM goalie_records gr
                 WHERE gr.game_id = player_game_stats.game_id
                   AND gr.side = player_game_stats.side
                   AND gr.player_id IS NOT NULL)
    """)


def audit(conn: sqlite3.Connection) -> None:
    """Record data-quality findings so problems surface instead of hiding."""
    conn.execute("DELETE FROM anomalies WHERE kind <> 'scoresheet'")
    timestamp = now()

    # Goals credited to a jersey that is not on that side's roster.
    conn.execute("""
        INSERT INTO anomalies(kind, game_id, detail, found_at)
        SELECT 'unmatched_scorer', game_id,
               'side ' || side || ' jersey ' || scorer_jersey, ?
          FROM goals
         WHERE scorer_player_id IS NULL AND TRIM(COALESCE(scorer_jersey,'')) <> ''
    """, (timestamp,))

    # Final games whose scoresheet never parsed.
    conn.execute("""
        INSERT INTO anomalies(kind, game_id, season_id, detail, found_at)
        SELECT 'missing_scoresheet', game_id, season_id,
               COALESCE(parse_error, 'never fetched'), ?
          FROM games
         WHERE status = 'final' AND has_scoresheet = 1 AND scoresheet_at IS NULL
    """, (timestamp,))

    # Games where neither side could be tied to a team id.
    conn.execute("""
        INSERT INTO anomalies(kind, game_id, season_id, detail, found_at)
        SELECT 'unresolved_teams', game_id, season_id,
               COALESCE(away_name,'?') || ' at ' || COALESCE(home_name,'?'), ?
          FROM games
         WHERE home_team_id IS NULL OR away_team_id IS NULL
    """, (timestamp,))

    # Derived totals that disagree with the site's published season totals.
    # Only regular-season games are compared: the published tables exclude
    # preseason, exhibition and playoff games.
    conn.execute("""
        INSERT INTO anomalies(kind, season_id, player_id, detail, found_at)
        SELECT 'totals_mismatch', d.season_id, d.player_id,
               'derived G=' || d.goals || ' vs site G=' || d.site_goals, ?
          FROM (
            SELECT s.season_id, pn.player_id,
                   SUM(p.goals) AS goals,
                   MAX(CAST(json_extract(s.data_json, '$.Goals') AS INTEGER)) AS site_goals
              FROM team_stat_rows s
              JOIN player_names pn ON pn.name = s.name
              JOIN player_game_stats p
                ON p.player_id = pn.player_id AND p.season_id = s.season_id
                                              AND p.team_id = s.team_id
              JOIN games g ON g.game_id = p.game_id AND g.game_class = 'regular'
             WHERE s.kind = 'skater'
             GROUP BY s.season_id, pn.player_id
          ) d
         WHERE d.site_goals IS NOT NULL AND d.goals <> d.site_goals
    """, (timestamp,))

    # Games still missing a season-total row we could reconcile against are not
    # errors, but a season whose regular-season games are largely unparsed is.
    conn.execute("""
        INSERT INTO anomalies(kind, season_id, detail, found_at)
        SELECT 'season_incomplete', season_id,
               CAST(SUM(CASE WHEN scoresheet_at IS NULL THEN 1 ELSE 0 END) AS TEXT)
               || ' of ' || CAST(COUNT(*) AS TEXT) || ' regular games unparsed', ?
          FROM games
         WHERE game_class = 'regular' AND status = 'final' AND has_scoresheet = 1
         GROUP BY season_id
        HAVING SUM(CASE WHEN scoresheet_at IS NULL THEN 1 ELSE 0 END) > 0
    """, (timestamp,))


# ------------------------------------------------------------------ helpers


#: A league whose games span at least this many days is a season, not an event.
#: Tournaments finish inside a long weekend; the shortest real competition round
#: still runs for weeks.
SEASON_SPAN_DAYS = 30

#: Playoffs conclude a league that has already been followed all season, so they
#: are wanted even though they are short -- the span rule alone would throw them
#: out along with the weekend invitationals. Matched against the league name.
_PLAYOFF_NAMES = re.compile(
    r"\b(playoff|championship|final|district|regional|national|state)s?\b", re.I)

#: The subset of those names that describes a championship *above* the league
#: rather than the end of one. Pacific District and USAH Nationals both read as
#: playoffs and were collected on that basis, and both turned out to be drawn
#: from a national field -- 47 out-of-state clubs between them, with California
#: teams appearing by chance. So a new league naming itself this way is not
#: collected on the strength of its name; it is measured, skipped, and asked
#: about. "Playoff" and "final" are deliberately not here: those are how a
#: league ends its own season, which is wanted.
_CHAMPIONSHIP_NAMES = re.compile(
    r"\b(district|regional|national)s?\b", re.I)

#: Any year works for measuring a span -- only the difference between two dates
#: matters, and a schedule never crosses more than one new year.
_REFERENCE_YEAR = 2000

#: How many teams to sample when classifying a league. One is not enough: a
#: team with an almost empty schedule makes a real league look like a weekend.
_CLASSIFY_SAMPLE = 5


def _default_priority(league_id: int) -> int:
    """Ordering for a league discovered by probing.

    Seeded leagues keep their explicit priority; anything else sorts after them
    by id, so a newly discovered league never takes ownership of a team away
    from an established one.
    """
    return 100 + league_id


def _scalar(conn: sqlite3.Connection, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


#: Seasons run autumn to spring, so anything from this month onward belongs to
#: a season starting in the current calendar year.
_SEASON_START_MONTH = 6


def _current_season_start_year(today: Optional[date] = None) -> int:
    """The calendar year the season now underway started in."""
    today = today or date.today()
    return today.year if today.month >= _SEASON_START_MONTH else today.year - 1


def refine_season_years(conn: sqlite3.Connection) -> int:
    """Correct season start years from real game dates.

    Scoresheets carry absolute dates (``08-29-25``), unlike the schedule pages,
    which print only "Fri Aug 29". Once any scoresheet in a season has been
    parsed, its dates settle the start year definitively -- so a season whose
    year had to be guessed is corrected as soon as the first game is played.

    The year every game agrees on wins, not the earliest one. Taking the minimum
    means a single bad date decides the season: nine games carrying a 2010 date,
    out of 1,647, moved S29 to "Fall 2009" and dragged 797 scheduled games back
    with it. Since the start year is what birth years are inferred from, that
    then split real children into two and three people apiece and raised
    thousands of review questions. A vote cannot be swung by nine rows.
    """
    corrected = 0
    rows = conn.execute("""
        SELECT g.season_id,
               s.start_year AS start_year,
               CASE WHEN CAST(substr(g.date_iso, 6, 2) AS INTEGER) >= ?
                    THEN CAST(substr(g.date_iso, 1, 4) AS INTEGER)
                    ELSE CAST(substr(g.date_iso, 1, 4) AS INTEGER) - 1
               END AS implied,
               COUNT(*) AS games
          FROM games g JOIN seasons s ON s.season_id = g.season_id
         WHERE g.scoresheet_at IS NOT NULL AND g.date_iso IS NOT NULL
         GROUP BY g.season_id, implied
    """, (_SEASON_START_MONTH,)).fetchall()

    votes: dict[int, Counter] = {}
    stored: dict[int, Optional[int]] = {}
    for row in rows:
        votes.setdefault(row["season_id"], Counter())[row["implied"]] = row["games"]
        stored[row["season_id"]] = row["start_year"]

    for season_id, tally in votes.items():
        actual, backing = tally.most_common(1)[0]
        outvoted = sum(tally.values()) - backing
        if outvoted:
            log.debug("S%d: %d game(s) disagree with %d", season_id, outvoted, actual)
        if stored[season_id] == actual:
            continue
        log.info("S%d actually starts in %d (was %s, %d of %d games agree); correcting",
                 season_id, actual, stored[season_id], backing, sum(tally.values()))
        conn.execute(
            "UPDATE seasons SET start_year = ?, label = ? WHERE season_id = ?",
            (actual, f"Fall {actual}", season_id),
        )
        _recompute_schedule_dates(conn, season_id, actual)
        corrected += 1
    return corrected


def _recompute_schedule_dates(conn: sqlite3.Connection, season_id: int, start_year: int) -> None:
    """Redo the schedule-page date conversions that used the old start year.

    Games whose scoresheet has been parsed already hold an absolute date and are
    left alone.
    """
    updates = []
    for row in conn.execute(
        "SELECT game_id, date_text FROM games "
        " WHERE season_id = ? AND scoresheet_at IS NULL AND date_text IS NOT NULL",
        (season_id,),
    ):
        iso = tts.schedule_date_to_iso(row["date_text"], start_year)
        if iso:
            updates.append((iso, row["game_id"]))
    if updates:
        conn.executemany("UPDATE games SET date_iso = ? WHERE game_id = ?", updates)


def _json(data: dict) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _hash_game(game: tts.ScheduleGame) -> str:
    import hashlib
    payload = "|".join(str(x) for x in (
        game.date_text, game.time_text, game.rink, game.level,
        game.away_name, game.away_goals, game.home_name, game.home_goals,
        game.game_type, game.has_scoresheet,
    ))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------- entrypoint


def run(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: str = "update",
    seasons: Optional[Iterable[int]] = None,
    use_cache: bool = False,
    offline: bool = False,
    force_scoresheets: bool = False,
    limit: Optional[int] = None,
    skip_scan: bool = False,
    only_teams: Optional[set[int]] = None,
) -> Stats:
    """Run the crawl and derivation stages."""
    fetcher = Fetcher(
        config.base_url,
        delay=config.delay,
        timeout=config.timeout,
        retries=config.retries,
        backoff=config.retry_backoff,
        user_agent=config.user_agent,
        raw_dir=config.raw_dir,
        keep_raw=config.keep_raw,
        max_requests=config.max_requests,
        offline=offline,
    )
    pipeline = Pipeline(conn, config, fetcher)

    with Run(conn, mode, ",".join(str(s) for s in seasons or [])) as record:
        if not offline:
            pipeline.discover_seasons()
        targets = pipeline.target_seasons(seasons)
        if not targets:
            log.warning("no seasons to process")
        pipeline.stats.seasons = len(targets)

        if not skip_scan:
            for season_id in targets:
                pipeline.scan_season(
                    season_id, use_cache=use_cache or offline, only_teams=only_teams
                )

        pending = pipeline.pending_scoresheets(targets, force=force_scoresheets)
        # Split those that only need re-parsing from those that need fetching,
        # so an improved parser costs no requests for pages already archived.
        from_archive = (set() if (use_cache or offline or force_scoresheets)
                        else pipeline.reparsable_scoresheets(targets))
        to_fetch = [g for g in pending if g not in from_archive]
        ordered = [g for g in pending if g in from_archive]

        if ordered:
            log.info("%d scoresheet(s) re-parsed from the archive (no requests)",
                     len(ordered))
            pipeline.fetch_scoresheets(ordered, use_cache=True, limit=limit)
        log.info("%d scoresheet(s) to fetch", len(to_fetch))
        pipeline.fetch_scoresheets(to_fetch, use_cache=use_cache or offline, limit=limit)

        # Scorecards (the PDF Goaltender Records) are collected for the current
        # season only for now: the table is the one source of real per-goalie
        # goals-against, but it is a second request per game, so older seasons
        # are a deliberate, separate backfill rather than part of the nightly.
        if config.collect_scorecards and targets:
            card_seasons = [max(targets)]
            card_pending = pipeline.pending_scorecards(card_seasons, force=force_scoresheets)
            card_archive = (set() if (use_cache or offline or force_scoresheets)
                            else pipeline.reparsable_scorecards(card_seasons))
            card_fetch = [g for g in card_pending if g not in card_archive]
            card_reparse = [g for g in card_pending if g in card_archive]
            if card_reparse:
                log.info("%d scorecard(s) re-parsed from the archive (no requests)",
                         len(card_reparse))
                pipeline.fetch_scorecards(card_reparse, use_cache=True, limit=limit)
            log.info("%d scorecard(s) to fetch", len(card_fetch))
            pipeline.fetch_scorecards(card_fetch, use_cache=use_cache or offline, limit=limit)

        pipeline.derive()

        record.pages = fetcher.requests_made
        record.games_seen = pipeline.stats.games_seen
        record.games_parsed = pipeline.stats.scoresheets
        record.errors = pipeline.stats.errors
        record.note = pipeline.stats.summary()

    log.info("done: %s", pipeline.stats.summary())
    return pipeline.stats
