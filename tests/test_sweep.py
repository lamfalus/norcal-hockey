"""The same-day sweep: which games it considers due, how it reconciles a
result from the whole-league day-window, and that it makes no request when
nothing is due.

Offline throughout -- the one test that exercises a fetch uses a stub fetcher
that serves saved pages, so the suite never touches the network.
"""

import pathlib
import tempfile
import unittest
from datetime import datetime

from norcalstats import db, pipeline
from norcalstats.config import Config
from norcalstats.sources import timetoscore as tts

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class _Page:
    def __init__(self, html: str) -> None:
        self.html = html
        self.sha256 = "0" * 64


class _StubFetcher:
    """Serves saved pages by URL shape and counts requests. get_bytes is left
    unimplemented so a test that reaches for a PDF fails loudly rather than
    silently hitting the network."""

    offline = False

    def __init__(self, day_window: str = "", scoresheet: str = "") -> None:
        self._day_window = day_window
        self._scoresheet = scoresheet
        self.requests_made = 0
        self.paths: list[str] = []

    def get(self, path, *, key=None, use_cache=False):
        self.requests_made += 1
        self.paths.append(path)
        if "oss-scoresheet" in path:
            return _Page(self._scoresheet)
        if "display-schedule" in path:
            return _Page(self._day_window)
        raise AssertionError(f"unexpected fetch: {path}")


class SweepTestCase(unittest.TestCase):
    NOW = datetime(2026, 9, 5, 12, 0)   # a Saturday noon
    TODAY = "2026-09-05"
    SEASON = 31
    LEAGUE = 3

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.conn.execute(
            "INSERT INTO seasons(season_id, label, start_year, first_seen_at) "
            "VALUES (?, 'Fall 2025', 2025, '2026-01-01')", (self.SEASON,)
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def add_game(self, game_id, *, time_text="8:30 AM", date_iso=None,
                 status="scheduled", has_scoresheet=0, home_goals=None,
                 away_goals=None, scoresheet_at=None, scorecard_at=None,
                 away_name="Visitors", home_name="Home"):
        db.upsert(self.conn, "games", {
            "game_id": game_id, "season_id": self.SEASON, "league_id": self.LEAGUE,
            "date_iso": date_iso or self.TODAY, "time_text": time_text,
            "status": status, "has_scoresheet": has_scoresheet,
            "home_goals": home_goals, "away_goals": away_goals,
            "away_name": away_name, "home_name": home_name,
            "scoresheet_at": scoresheet_at, "scorecard_at": scorecard_at,
            "schedule_hash": "seed",
        }, keys=["game_id"])
        self.conn.commit()

    def pipe(self, fetcher):
        return pipeline.Pipeline(self.conn, self.config, fetcher)


class TestDueGames(SweepTestCase):
    def test_selects_only_games_within_the_window_and_still_wanting_data(self):
        # A: started 3h ago, no result yet -> due.
        self.add_game(1, time_text="9:00 AM")
        # B: started 30 min ago -> too soon (before the 100-minute first look).
        self.add_game(2, time_text="11:30 AM")
        # C: started 6h ago -> past the 5-hour give-up, left to the nightly.
        self.add_game(3, time_text="6:00 AM")
        # D: started 3h ago but fully collected -> nothing left to get.
        self.add_game(4, time_text="9:00 AM", status="final", has_scoresheet=1,
                      home_goals=3, away_goals=2,
                      scoresheet_at="x", scorecard_at="x")
        # E: yesterday -> outside today's filter.
        self.add_game(5, time_text="9:00 AM", date_iso="2026-09-04")

        due = {r["game_id"] for r in self.pipe(_StubFetcher()).due_games(self.NOW)}
        self.assertEqual(due, {1})

    def test_final_but_missing_the_pdf_is_still_due(self):
        self.add_game(10, time_text="9:00 AM", status="final", has_scoresheet=1,
                      home_goals=4, away_goals=1, scoresheet_at="x",
                      scorecard_at=None)
        due = {r["game_id"] for r in self.pipe(_StubFetcher()).due_games(self.NOW)}
        self.assertEqual(due, {10})

    def test_missing_time_is_skipped(self):
        self.add_game(20, time_text="")
        self.assertEqual(self.pipe(_StubFetcher()).due_games(self.NOW), [])

    def test_blank_side_stub_is_excluded(self):
        # A tournament bracket slot with no opponent can never go final; a real
        # game right beside it still sweeps.
        self.add_game(40, time_text="9:00 AM", away_name="", away_goals=None)
        self.add_game(41, time_text="9:00 AM", home_name=None)
        self.add_game(42, time_text="9:00 AM")
        due = {r["game_id"] for r in self.pipe(_StubFetcher()).due_games(self.NOW)}
        self.assertEqual(due, {42})


class TestApplyResult(SweepTestCase):
    def test_updates_result_columns_and_reports_change(self):
        self.add_game(30)
        game = tts.ScheduleGame(
            game_id=30, date_text="Sat Sep 5", time_text="8:30 AM", rink="X",
            league="Norcal", level="12U A", away_name="A", home_name="B",
            away_goals=2, home_goals=5, game_type="Regular 1", has_scoresheet=True)

        pipe = self.pipe(_StubFetcher())
        self.assertTrue(pipe.apply_schedule_result(game))
        row = self.conn.execute(
            "SELECT status, home_goals, has_scoresheet FROM games WHERE game_id = 30"
        ).fetchone()
        self.assertEqual(row["status"], "final")
        self.assertEqual(row["home_goals"], 5)
        self.assertEqual(row["has_scoresheet"], 1)
        # Re-applying the identical row changes nothing.
        self.assertFalse(pipe.apply_schedule_result(game))

    def test_unknown_game_is_left_for_the_nightly_scan(self):
        game = tts.ScheduleGame(
            game_id=999, date_text="", time_text="", rink="", league="", level="",
            away_name="A", home_name="B", away_goals=1, home_goals=0,
            game_type="", has_scoresheet=True)
        self.assertFalse(self.pipe(_StubFetcher()).apply_schedule_result(game))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM games WHERE game_id = 999").fetchone())


class TestSweepRun(SweepTestCase):
    def test_no_due_games_makes_no_request(self):
        self.add_game(1, time_text="11:45 AM")   # too soon to be due
        fetcher = _StubFetcher()
        info = self.pipe(fetcher).sweep(self.NOW)
        self.assertEqual(info["due"], 0)
        self.assertEqual(fetcher.requests_made, 0)

    def test_reconciles_a_due_game_and_fetches_its_sheet(self):
        # Game 50647 is a final game on team 58's page; seed it locally as still
        # scheduled and due, so the day-window flips it to final.
        self.add_game(50647, time_text="8:30 AM")
        self.config.collect_scorecards = False   # isolate to the scoresheet path
        fetcher = _StubFetcher(day_window=load("team58.html"),
                               scoresheet="<html><body>no roster</body></html>")

        info = self.pipe(fetcher).sweep(self.NOW)

        self.assertEqual(info["due"], 1)
        self.assertEqual(info["changed"], 1)
        self.assertEqual(info["scoresheets"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM games WHERE game_id = 50647").fetchone()["status"],
            "final")
        self.assertTrue(any("display-schedule" in p for p in fetcher.paths))
        self.assertTrue(any("oss-scoresheet" in p for p in fetcher.paths))


if __name__ == "__main__":
    unittest.main()
