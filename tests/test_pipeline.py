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


class TestRequestCeiling(unittest.TestCase):
    """Hitting the ceiling must stop the run, not fail every remaining item."""

    def test_the_ceiling_raises_its_own_error(self):
        from norcalstats.fetch import FetchError, RequestCeilingReached

        fetcher = Fetcher("https://example.invalid", max_requests=0)
        with self.assertRaises(RequestCeilingReached):
            fetcher.get("/anything")
        # Still a FetchError, so existing handlers keep working.
        self.assertTrue(issubclass(RequestCeilingReached, FetchError))

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
