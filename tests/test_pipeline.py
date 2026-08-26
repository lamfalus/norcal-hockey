"""End-to-end tests: fixtures -> database -> derived stats -> export.

Everything here runs offline against saved pages, so the suite never touches
the network.
"""

import json
import pathlib
import sqlite3
import tempfile
import unittest

from norcalstats import db, export, identity, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class PipelineTestCase(unittest.TestCase):
    """Builds a small database from the fixtures."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.pipeline = pipeline.Pipeline(
            self.conn, self.config, Fetcher(self.config.base_url, offline=True)
        )
        self.conn.execute(
            "INSERT INTO seasons(season_id, label, start_year, first_seen_at) "
            "VALUES (31, 'Fall 2025', 2025, '2026-01-01')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    #: Norcal, the league the fixtures come from.
    LEAGUE = 3

    def seed_season_and_team(self) -> None:
        from norcalstats.sources import timetoscore as tts

        teams = tts.parse_season_index(load("ts31.html"))
        self.pipeline._record_league(self.LEAGUE, "Norcal", 0)
        division_ids = self.pipeline._store_divisions(31, self.LEAGUE, teams)
        self.pipeline._store_teams(31, self.LEAGUE, 0, teams, division_ids)
        team = next(t for t in teams if t.team_id == 58)
        page = tts.parse_team_page(load("team58.html"))
        for game in page.games:
            self.pipeline._store_game(
                31, self.LEAGUE, game, team, 2025,
                division_ids.get(team.division.name), division_ids,
            )
        self.conn.commit()


class TestStorage(PipelineTestCase):
    def test_season_index_populates_divisions_and_teams(self):
        self.seed_season_and_team()
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM teams"), 130)
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM divisions"), 13)
        row = self.conn.execute(
            "SELECT name, club FROM teams WHERE team_id = 58 AND season_id = 31"
        ).fetchone()
        self.assertEqual(row["club"], "San Jose Jr Sharks")

    def test_games_stored_with_class_and_iso_date(self):
        self.seed_season_and_team()
        row = self.conn.execute(
            "SELECT date_iso, game_class, away_team_id, status FROM games WHERE game_id = 50647"
        ).fetchone()
        self.assertEqual(row["date_iso"], "2025-08-29")
        self.assertEqual(row["game_class"], "preseason")
        self.assertEqual(row["away_team_id"], 58)
        self.assertEqual(row["status"], "final")

    def test_regular_games_are_classified(self):
        self.seed_season_and_team()
        count = db.scalar(
            self.conn, "SELECT COUNT(*) FROM games WHERE game_class = 'regular'"
        )
        self.assertEqual(count, 15, "team 58 plays a 15-game regular season")

    def test_same_named_opponents_are_flagged_not_guessed(self):
        self.seed_season_and_team()
        rows = self.conn.execute(
            "SELECT game_id FROM games WHERE home_name = away_name"
        ).fetchall()
        self.assertTrue(rows, "fixture should contain Sharks-vs-Sharks games")
        for row in rows:
            game = self.conn.execute(
                "SELECT home_team_id, away_team_id, needs_review FROM games WHERE game_id = ?",
                (row["game_id"],),
            ).fetchone()
            self.assertIsNone(game["home_team_id"])
            self.assertIsNone(game["away_team_id"])
            self.assertEqual(game["needs_review"], 1)


class TestSeasonTargeting(unittest.TestCase):
    """An offline rebuild starts from an empty database and must still work."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.pipeline = pipeline.Pipeline(
            self.conn, self.config, Fetcher(self.config.base_url, offline=True)
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_requested_seasons_survive_an_empty_database(self):
        self.assertEqual(self.pipeline.target_seasons([31]), [31])

    def test_no_request_and_no_seasons_yields_nothing(self):
        self.assertEqual(self.pipeline.target_seasons(None), [])

    def test_season_page_seeds_the_seasons_table(self):
        from norcalstats.sources import timetoscore as tts

        self.pipeline.record_seasons(tts.parse_season_list(load("ts31.html")))
        row = self.conn.execute(
            "SELECT label, start_year FROM seasons WHERE season_id = 31"
        ).fetchone()
        self.assertEqual(row["label"], "Fall 2025")
        self.assertEqual(row["start_year"], 2025)

    def test_recording_seasons_twice_keeps_first_seen(self):
        from norcalstats.sources import timetoscore as tts

        seasons = tts.parse_season_list(load("ts31.html"))
        self.pipeline.record_seasons(seasons)
        first = db.scalar(self.conn, "SELECT first_seen_at FROM seasons WHERE season_id = 31")
        self.pipeline.record_seasons(seasons)
        self.assertEqual(
            db.scalar(self.conn, "SELECT first_seen_at FROM seasons WHERE season_id = 31"),
            first,
        )


class TestScoresheetStorage(PipelineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_season_and_team()
        self.pipeline.store_scoresheet(50647, load("game.html"), "sha-test")
        self.conn.commit()

    def test_rosters_goals_and_penalties_stored(self):
        self.assertEqual(
            db.scalar(self.conn, "SELECT COUNT(*) FROM goals WHERE game_id = 50647"), 21
        )
        self.assertEqual(
            db.scalar(self.conn,
                      "SELECT COUNT(*) FROM goals WHERE game_id = 50647 AND side = 'away'"),
            19,
        )
        self.assertEqual(
            db.scalar(self.conn, "SELECT COUNT(*) FROM penalties WHERE game_id = 50647"), 4
        )
        self.assertTrue(
            db.scalar(self.conn,
                      "SELECT COUNT(*) FROM game_rosters WHERE game_id = 50647 "
                      "AND role = 'coach'") > 0
        )

    def test_parse_marked_and_no_error(self):
        row = self.conn.execute(
            "SELECT scoresheet_at, parse_version, parse_error FROM games WHERE game_id = 50647"
        ).fetchone()
        self.assertIsNotNone(row["scoresheet_at"])
        self.assertIsNotNone(row["parse_version"])
        self.assertIsNone(row["parse_error"])

    def test_reparsing_is_idempotent(self):
        before = db.scalar(self.conn, "SELECT COUNT(*) FROM goals")
        self.pipeline.store_scoresheet(50647, load("game.html"), "sha-test")
        self.conn.commit()
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM goals"), before)

    def test_shot_grid_marked_unreliable_when_it_disagrees(self):
        # The home goalie conceded 19 but only 2 goals are marked on the grid.
        row = self.conn.execute(
            "SELECT reliable, goals_marked FROM shot_marks "
            "WHERE game_id = 50647 AND side = 'home'"
        ).fetchone()
        self.assertEqual(row["goals_marked"], 2)
        self.assertEqual(row["reliable"], 0)

    def test_derived_stats_credit_the_right_players(self):
        identity.rebuild(self.conn)
        pipeline.rebuild_player_game_stats(self.conn)
        self.conn.commit()

        total_goals = db.scalar(
            self.conn, "SELECT SUM(goals) FROM player_game_stats WHERE game_id = 50647"
        )
        self.assertEqual(total_goals, 21, "every goal should be credited to a player")

        goalies = self.conn.execute("""
            SELECT p.display_name, s.goals_against
              FROM player_game_stats s JOIN players p ON p.player_id = s.player_id
             WHERE s.game_id = 50647 AND s.is_goalie = 1
             ORDER BY s.goals_against
        """).fetchall()
        self.assertEqual(len(goalies), 2)
        self.assertEqual(goalies[0]["goals_against"], 2)   # Sol Orlov
        self.assertEqual(goalies[1]["goals_against"], 19)  # Clayton J Harvey


class TestAmbiguousSideResolution(PipelineTestCase):
    def test_roster_match_resolves_a_same_named_matchup(self):
        self.seed_season_and_team()
        # Publish a roster for team 58 only, then present a Sharks-vs-Sharks
        # game whose away roster is team 58's players.
        names = ["Alexander Aksyonov", "Sol Orlov", "Kai Garrett", "Dov Jacobson"]
        for i, name in enumerate(names):
            self.conn.execute(
                "INSERT INTO team_stat_rows(season_id, team_id, kind, row_index, "
                "name, jersey, gp, data_json) VALUES (31, 58, 'skater', ?, ?, '', 1, '{}')",
                (i, name),
            )
        game_id = db.scalar(
            self.conn, "SELECT game_id FROM games WHERE home_name = away_name LIMIT 1"
        )
        self.conn.execute(
            "UPDATE games SET scoresheet_at = '2026-01-01' WHERE game_id = ?", (game_id,)
        )
        for i, name in enumerate(names):
            self.conn.execute(
                "INSERT INTO game_rosters(game_id, side, slot, jersey, position, name, role) "
                "VALUES (?, 'away', ?, '', '', ?, 'player')",
                (game_id, i, name),
            )
        self.conn.commit()

        resolved = pipeline.resolve_ambiguous_sides(self.conn)
        self.assertEqual(resolved, 1)
        row = self.conn.execute(
            "SELECT away_team_id, home_team_id FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        self.assertEqual(row["away_team_id"], 58)
        self.assertIsNone(row["home_team_id"], "the other side stays unknown")

    def test_weak_evidence_does_not_resolve(self):
        self.seed_season_and_team()
        game_id = db.scalar(
            self.conn, "SELECT game_id FROM games WHERE home_name = away_name LIMIT 1"
        )
        self.conn.execute(
            "UPDATE games SET scoresheet_at = '2026-01-01' WHERE game_id = ?", (game_id,)
        )
        self.conn.execute(
            "INSERT INTO game_rosters(game_id, side, slot, jersey, position, name, role) "
            "VALUES (?, 'away', 0, '', '', 'Nobody Known', 'player')", (game_id,)
        )
        self.conn.commit()
        self.assertEqual(pipeline.resolve_ambiguous_sides(self.conn), 0)


class TestExport(PipelineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed_season_and_team()
        self.pipeline.store_scoresheet(50647, load("game.html"), "sha")
        self.conn.execute(
            "INSERT INTO team_stat_rows(season_id, team_id, kind, row_index, name, "
            "jersey, gp, data_json) VALUES (31, 58, 'skater', 0, 'Kai Garrett', '27', 15, ?)",
            (json.dumps({"Goals": "10", "Ass.": "9", "Hat": "0", "Pts": "19"}),),
        )
        self.conn.execute(
            "INSERT INTO team_stat_rows(season_id, team_id, kind, row_index, name, "
            "jersey, gp, data_json) VALUES (31, 58, 'goalie', 0, 'Sol Orlov', '30', 15, ?)",
            (json.dumps({"Shots": "255", "GA": "35", "GAA": "2.33",
                         "Save %": "0.863", "SO": "1"}),),
        )
        self.conn.commit()
        identity.rebuild(self.conn)
        pipeline.rebuild_player_game_stats(self.conn)
        self.conn.commit()

    def test_legacy_entry_shape_matches_the_viewer(self):
        """The original keys must come first, in order, so the viewer still works.

        Extra keys are appended (league, source, byClass); the viewer reads
        fields by name and ignores what it does not know.
        """
        payload = export.build_legacy(self.conn)
        skater_keys = ["season", "division", "team", "type", "jersey",
                       "GP", "G", "A", "Hat", "PIM", "PtsPerGame", "Pts"]
        goalie_keys = ["season", "division", "team", "type", "jersey",
                       "GP", "Shots", "GA", "GAA", "Save%", "SO"]
        found = {"skater": False, "goalie": False}
        for entries in payload["players"].values():
            for entry in entries:
                expected = skater_keys if entry["type"] == "skater" else goalie_keys
                keys = list(entry.keys())
                self.assertEqual(keys[:len(expected)], expected)
                self.assertTrue(set(keys) - set(expected) <= {"league", "source", "byClass"})
                found[entry["type"]] = True
        self.assertTrue(all(found.values()))

    def test_legacy_values_and_division(self):
        payload = export.build_legacy(self.conn)
        entry = payload["players"]["Kai Garrett"][0]
        self.assertEqual(entry["division"], "10U A")
        self.assertEqual(entry["team"], "San Jose Jr Sharks")
        self.assertEqual((entry["G"], entry["A"], entry["Pts"]), ("10", "9", "19"))
        self.assertEqual(entry["PtsPerGame"], "1.27")

    def test_rich_export_has_games_and_players(self):
        payload = export.build_rich(self.conn)
        self.assertTrue(payload["games"])
        self.assertTrue(payload["players"])
        self.assertTrue(payload["standings"])
        self.assertEqual(payload["metadata"]["schema"], "norcal-hockey/2")

    def test_write_json_is_atomic_and_leaves_no_temp(self):
        path = pathlib.Path(self.tmp.name) / "out.json"
        export.write_json(path, {"a": 1})
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})


class TestRosterPlaceholders(PipelineTestCase):
    """A roster that was never submitted must not become a player.

    The site prints "Not Signed In" for every slot when no roster was filed.
    Treated as a name, one identity absorbed whole rosters -- 1,588 rows across
    81 teams -- and the duplicate (game, player) rows aborted every derive with
    a UNIQUE constraint failure, leaving no derived stats at all.
    """

    def setUp(self):
        super().setUp()
        self.seed_season_and_team()

    def _roster(self, game_id, names, side="home"):
        for slot, name in enumerate(names):
            self.conn.execute(
                "INSERT INTO game_rosters(game_id, side, slot, jersey, position, "
                "name, role) VALUES (?,?,?,?,'',?,?)",
                (game_id, side, slot, str(10 + slot), name,
                 "placeholder" if __import__("norcalstats.names", fromlist=["x"])
                 .is_placeholder(name) else "player"))
        self.conn.commit()

    def test_placeholder_names_are_recognised(self):
        from norcalstats import names as N
        for name in ("Not Signed In", "Home Unknown Goalie 1",
                     "Visitor Unknown Goalie", "unknown", "TBD"):
            self.assertTrue(N.is_placeholder(name), name)

    def test_real_names_are_not_mistaken_for_placeholders(self):
        from norcalstats import names as N
        for name in ("Sol Orlov", "Norman Player", "Unknown Smith",
                     "Gavin B Duganne"):
            self.assertFalse(N.is_placeholder(name), name)

    def test_the_parser_marks_them_placeholder(self):
        from norcalstats.sources import timetoscore as tts
        grid = ("<table><tr><th>#</th><th>P</th><th>Name</th></tr>"
                "<tr><td>95</td><td>G</td><td>Not Signed In</td></tr>"
                "<tr><td>44</td><td></td><td>Sol Orlov</td></tr></table>")
        entries = tts._parse_roster_grid(all_tables_first(grid))
        roles = {e.name: e.role for e in entries}
        self.assertEqual(roles["Not Signed In"], "placeholder")
        self.assertEqual(roles["Sol Orlov"], "player")

    def test_derive_survives_a_player_listed_twice(self):
        # A genuine duplicate: the same child on the sheet twice. This used to
        # abort the entire derive stage.
        self._roster(50647, ["Kai Garrett", "Kai Garrett", "Sol Orlov"])
        identity.rebuild(self.conn)
        pipeline.rebuild_player_game_stats(self.conn)   # must not raise
        self.conn.commit()

        rows = db.scalar(
            self.conn, "SELECT COUNT(*) FROM player_game_stats WHERE game_id = 50647")
        self.assertEqual(rows, 2, "one row per player, not one per roster line")

    def test_placeholders_never_reach_the_stats(self):
        self._roster(50684, ["Not Signed In", "Not Signed In", "Sol Orlov"])
        identity.rebuild(self.conn)
        pipeline.rebuild_player_game_stats(self.conn)
        self.conn.commit()

        names = [r["display_name"] for r in self.conn.execute(
            "SELECT display_name FROM players")]
        self.assertNotIn("Not Signed In", names)
        self.assertEqual(
            db.scalar(self.conn,
                      "SELECT COUNT(*) FROM player_game_stats WHERE game_id = 50684"),
            1, "only the real player counts")


def all_tables_first(html):
    from norcalstats.htmltable import all_tables
    return all_tables(html)[0]


class TestForeignScoresheets(PipelineTestCase):
    """A sheet dated differently from the fixture is another game's sheet."""

    def test_a_sheet_for_a_different_date_is_refused(self):
        # Asking for game 1 returns the site's real game 1, from 2010. Its
        # roster must not land on a 2025 fixture.
        self.seed_season_and_team()
        row = self.conn.execute(
            "SELECT game_id, date_text FROM games WHERE date_text IS NOT NULL"
            " AND has_scoresheet = 1 LIMIT 1").fetchone()
        game_id = row["game_id"]
        html = load("game.html").replace("08-29-25", "03-06-10")

        self.pipeline.store_scoresheet(game_id, html, "sha-foreign")
        self.conn.commit()

        stored = self.conn.execute(
            "SELECT date_iso, parse_error FROM games WHERE game_id = ?", (game_id,)
        ).fetchone()
        self.assertNotIn("2010", stored["date_iso"] or "",
                         "the foreign date must not overwrite the fixture's")
        self.assertIn("does not match", stored["parse_error"] or "")
        self.assertEqual(
            db.scalar(self.conn,
                      "SELECT COUNT(*) FROM game_rosters WHERE game_id = ?", (game_id,)),
            0, "no roster from another game's sheet")

    def test_the_matching_sheet_is_still_accepted(self):
        # The guard must not reject the sheets it exists to protect.
        self.seed_season_and_team()
        self.pipeline.store_scoresheet(50647, load("game.html"), "sha-ok")
        self.conn.commit()
        self.assertEqual(
            db.scalar(self.conn, "SELECT date_iso FROM games WHERE game_id = 50647"),
            "2025-08-29")
        self.assertTrue(
            db.scalar(self.conn,
                      "SELECT COUNT(*) FROM game_rosters WHERE game_id = 50647") > 0)


class TestSeasonYearFromGameDates(PipelineTestCase):
    """The year the games agree on decides the season, not the earliest one."""

    _next_id = 1

    def add_games(self, season, dates, *, scoresheet=True):
        for iso in dates:
            self.conn.execute(
                "INSERT INTO games(game_id, season_id, league_id, date_iso,"
                " status, scoresheet_at) VALUES (?,?,3,?,'final',?)",
                (TestSeasonYearFromGameDates._next_id, season, iso,
                 '2026-01-01' if scoresheet else None))
            TestSeasonYearFromGameDates._next_id += 1
        self.conn.commit()

    def start_year(self, season):
        return db.scalar(
            self.conn, "SELECT start_year FROM seasons WHERE season_id = ?", (season,))

    def test_a_handful_of_bad_dates_cannot_move_the_season(self):
        # Exactly what happened to S29: nine games carrying a decade-old date,
        # against sixteen hundred real ones. Taking the minimum moved the season
        # to Fall 2009, which then mis-inferred every birth year derived from it.
        self.conn.execute("UPDATE seasons SET start_year = 2023, label = 'Fall 2023'"
                          " WHERE season_id = 31")
        self.add_games(31, ["2010-03-06"] * 9 + ["2023-09-15"] * 200 + ["2024-02-01"] * 150)
        pipeline.refine_season_years(self.conn)
        self.assertEqual(self.start_year(31), 2023)

    def test_a_genuinely_wrong_year_is_still_corrected(self):
        # The guard must not stop the correction it exists for.
        self.add_games(31, ["2022-09-10"] * 40 + ["2023-01-20"] * 30)
        pipeline.refine_season_years(self.conn)
        self.assertEqual(self.start_year(31), 2022)

    def test_games_before_the_cutoff_month_belong_to_the_previous_season(self):
        # A February game belongs to the season that started the previous autumn.
        self.add_games(31, ["2026-02-14"] * 20)
        pipeline.refine_season_years(self.conn)
        self.assertEqual(self.start_year(31), 2025)

    def test_schedule_only_games_do_not_vote(self):
        # Their dates were guessed from the season year in the first place, so
        # letting them vote would just confirm whatever was already believed.
        self.add_games(31, ["2019-09-01"] * 500, scoresheet=False)
        self.add_games(31, ["2025-09-15"] * 10)
        pipeline.refine_season_years(self.conn)
        self.assertEqual(self.start_year(31), 2025)


class TestPlaceholderMigration(unittest.TestCase):
    """An existing database must heal itself without refetching."""

    def test_v2_database_is_repaired_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            conn = db.connect(path)
            conn.execute("INSERT INTO seasons(season_id,label,first_seen_at) "
                         "VALUES (31,'Fall 2025','x')")
            conn.execute("INSERT INTO games(game_id,season_id) VALUES (1,31)")
            for slot, name in enumerate(["Not Signed In", "Not Signed In", "Sol Orlov"]):
                conn.execute(
                    "INSERT INTO game_rosters(game_id,side,slot,name,role) "
                    "VALUES (1,'home',?,?,'player')", (slot, name))
            db.set_meta(conn, "schema_version", "2")
            conn.commit()
            conn.close()

            conn = db.connect(path)          # triggers the migration
            try:
                roles = dict(conn.execute(
                    "SELECT name, role FROM game_rosters GROUP BY name, role").fetchall())
                self.assertEqual(roles["Not Signed In"], "placeholder")
                self.assertEqual(roles["Sol Orlov"], "player")
                self.assertEqual(db.get_meta(conn, "schema_version"),
                                 str(db.SCHEMA_VERSION))
            finally:
                conn.close()


class TestRequestCeiling(unittest.TestCase):
    """Hitting the ceiling must stop the run, not fail every remaining item."""

    def test_the_ceiling_raises_its_own_error(self):
        from norcalstats.fetch import FetchError, RequestCeilingReached

        fetcher = Fetcher("https://example.invalid", max_requests=0)
        with self.assertRaises(RequestCeilingReached):
            fetcher.get("/anything")
        # Still a FetchError, so existing handlers keep working.
        self.assertTrue(issubclass(RequestCeilingReached, FetchError))

    def test_persistent_429_raises_rate_limited_not_a_plain_error(self):
        # A 429 that survives the retries must be distinguishable, so a fetch
        # loop can stop and resume rather than record the page as broken and
        # skip it. It stays a FetchError so existing handlers still catch it.
        import urllib.error
        from norcalstats.fetch import FetchError, RateLimited

        fetcher = Fetcher("https://example.invalid", retries=2, backoff=0)

        def always_429(url):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

        with self.assertRaises(RateLimited):
            fetcher._with_retries("https://example.invalid/x", always_429)
        self.assertTrue(issubclass(RateLimited, FetchError))

    def test_429_backs_off_far_harder_than_a_500(self):
        import urllib.error
        fetcher = Fetcher("https://example.invalid", backoff=4.0)
        five = urllib.error.HTTPError("u", 500, "err", {}, None)
        rate = urllib.error.HTTPError("u", 429, "slow down", {}, None)
        self.assertEqual(fetcher._retry_delay(1, five), 4.0)
        self.assertGreater(fetcher._retry_delay(1, rate), fetcher._retry_delay(1, five))

    def test_the_archive_is_still_served_at_the_ceiling(self):
        # A page already collected costs no request, so a resumed run makes
        # progress rather than stopping again immediately.
        with tempfile.TemporaryDirectory() as tmp:
            raw = pathlib.Path(tmp)
            fetcher = Fetcher("https://example.invalid", raw_dir=raw,
                              max_requests=99)
            fetcher._write_raw("k", "", "<html>cached</html>")

            starved = Fetcher("https://example.invalid", raw_dir=raw,
                              max_requests=0)
            page = starved.get("/anything", key="k", use_cache=True)
            self.assertTrue(page.from_cache)
            self.assertEqual(starved.requests_made, 0)

    def test_binary_archive_round_trips_and_serves_from_cache(self):
        # A PDF must survive the archive intact -- decoding it to text would
        # corrupt it -- and be served from disk without a request, the same as
        # the HTML pages.
        with tempfile.TemporaryDirectory() as tmp:
            raw = pathlib.Path(tmp)
            payload = b"%PDF-1.4\n\x00\x01\x02 binary \xff\xfe bytes"
            fetcher = Fetcher("https://example.invalid", raw_dir=raw, max_requests=99)
            fetcher._write_raw_bytes("s33/scorecard/1", "", payload, "pdf")
            self.assertEqual(fetcher.read_raw_bytes("s33/scorecard/1", "pdf"), payload)

            starved = Fetcher("https://example.invalid", raw_dir=raw, max_requests=0)
            page = starved.get_bytes("/x", key="s33/scorecard/1", ext="pdf",
                                     use_cache=True)
            self.assertTrue(page.from_cache)
            self.assertEqual(page.payload, payload)
            self.assertEqual(starved.requests_made, 0)

    def test_stats_report_an_early_stop(self):
        stats = pipeline.Stats()
        self.assertFalse(stats.stopped_early)
        stats.stopped_early = True
        self.assertTrue(stats.stopped_early)


class TestPublishGuard(unittest.TestCase):
    """Publishing must refuse to replace a full export with a thin one.

    A half-finished backfill produces a valid but tiny file; overwriting the
    published data with it is the worst outcome the collector can produce.
    """

    def setUp(self):
        import subprocess
        from norcalstats import publish as publish_mod

        self.publish = publish_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name)
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "Test"]):
            subprocess.run(["git", *args], cwd=self.repo, capture_output=True)
        self._write(500)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=self.repo,
                       capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, players: int) -> None:
        (self.repo / "export.json").write_text(json.dumps({
            "metadata": {}, "players": {f"Player {i}": [] for i in range(players)},
        }), encoding="utf-8")

    def test_a_full_export_publishes(self):
        self._write(500)
        self.assertEqual(self.publish.check_shrinkage(self.repo, ["export.json"]), [])

    def test_a_small_gain_is_fine(self):
        self._write(520)
        self.assertEqual(self.publish.check_shrinkage(self.repo, ["export.json"]), [])

    def test_a_slight_drop_is_tolerated(self):
        # Players do leave; a few percent is normal season-to-season churn.
        self._write(480)
        self.assertEqual(self.publish.check_shrinkage(self.repo, ["export.json"]), [])

    def test_a_collapse_is_refused(self):
        self._write(40)
        problems = self.publish.check_shrinkage(self.repo, ["export.json"])
        self.assertEqual(len(problems), 1)
        self.assertIn("40 players", problems[0])
        self.assertIn("500", problems[0])

    def test_publish_raises_rather_than_committing(self):
        self._write(40)
        with self.assertRaises(self.publish.PublishError) as caught:
            self.publish.publish(self.repo, ["export.json"], message="m", push=False)
        self.assertIn("lost most of its players", str(caught.exception))

    def test_force_overrides_the_guard(self):
        self._write(40)
        sha = self.publish.publish(
            self.repo, ["export.json"], message="m", push=False, force=True)
        self.assertIsNotNone(sha)

    def test_a_file_never_published_before_is_allowed(self):
        (self.repo / "new.json").write_text('{"players": {}}', encoding="utf-8")
        self.assertEqual(self.publish.check_shrinkage(self.repo, ["new.json"]), [])


class TestSchema(unittest.TestCase):
    def test_init_is_idempotent_and_adds_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.sqlite3"
            conn = db.connect(path)
            db.init(conn)  # second run must not fail
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(games)")}
            self.assertIn("game_class", columns)
            conn.close()

    def test_detail_rows_cascade_when_a_game_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(pathlib.Path(tmp) / "t.sqlite3")
            conn.execute("INSERT INTO seasons(season_id,label,first_seen_at) VALUES (31,'x','y')")
            conn.execute("INSERT INTO games(game_id,season_id) VALUES (1,31)")
            conn.execute("INSERT INTO goals(game_id,side,seq) VALUES (1,'home',0)")
            conn.execute("DELETE FROM games WHERE game_id = 1")
            conn.commit()
            self.assertEqual(db.scalar(conn, "SELECT COUNT(*) FROM goals"), 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
