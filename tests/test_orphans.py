"""Orphan-scoresheet probe: games that exist only as a scoresheet, with no
schedule row (e.g. the CAHA inter-regional girls games scored via the live
system). Runs offline: the fetcher is stubbed with a saved scoresheet.
"""

import pathlib
import re
import tempfile
import types
import unittest

from norcalstats import db, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher, FetchError

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class ProbeOrphansTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.pipe = pipeline.Pipeline(
            self.conn, self.config, Fetcher(self.config.base_url, offline=True)
        )
        self.conn.execute(
            "INSERT INTO seasons(season_id, label, start_year, first_seen_at) "
            "VALUES (33, 'Fall 2026', 2026, '2026-01-01')"
        )
        self.conn.execute(
            "INSERT INTO divisions(season_id, league_id, name, gender) "
            "VALUES (33, 5, 'Girls 12AAA', 'girls')"
        )
        self.conn.commit()

        html = (FIXTURES / "scoresheet_girls12aaa_62137.html").read_text(
            encoding="utf-8", errors="replace")

        def fake_get(path, key=None, use_cache=False, force=False):
            m = re.search(r"game_id=(\d+)", path)
            gid = int(m.group(1)) if m else None
            if gid == 62137:  # the one orphan our stub "knows"
                return types.SimpleNamespace(html=html, sha256="deadbeef", url=path)
            raise FetchError(f"no fixture for game {gid}")

        self.pipe.fetcher.get = fake_get

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_ingests_orphan_with_score_and_detail(self):
        res = self.pipe.probe_orphan_scoresheets(33, start_id=62135, end_id=62139, cap=10)
        self.assertEqual(res["found"], 1)
        row = self.conn.execute(
            "SELECT away_name, home_name, away_goals, home_goals, level, league, "
            "status, has_scoresheet FROM games WHERE game_id = 62137"
        ).fetchone()
        self.assertIsNotNone(row, "the orphan game should have been created")
        self.assertEqual(row["level"], "Girls 12AAA")
        self.assertEqual(row["league"], "CAHA")
        self.assertEqual(row["status"], "final")
        self.assertEqual(row["has_scoresheet"], 1)
        # Score read straight from the sheet (LA Lions 3, SJ Jr Sharks Girls 0).
        self.assertEqual({row["away_goals"], row["home_goals"]}, {0, 3})
        both = f"{row['away_name']} {row['home_name']}"
        self.assertIn("Lions", both)
        self.assertIn("Sharks", both)
        # store_scoresheet ran -> roster detail landed.
        self.assertGreater(
            db.scalar(self.conn, "SELECT COUNT(*) FROM game_rosters WHERE game_id = 62137"),
            0, "scoresheet detail should have been parsed and stored")

    def test_already_known_game_is_skipped(self):
        self.pipe.probe_orphan_scoresheets(33, start_id=62137, end_id=62137, cap=10)
        again = self.pipe.probe_orphan_scoresheets(33, start_id=62137, end_id=62137, cap=10)
        self.assertEqual(again["found"], 0, "a game already in the DB must not be re-ingested")

    def test_last_id_reaches_end_when_range_completes(self):
        res = self.pipe.probe_orphan_scoresheets(33, start_id=62135, end_id=62139, cap=50)
        self.assertEqual(res["last_id"], 62139, "covered the whole range")

    def test_last_id_stops_where_cap_bit(self):
        # Every id but 62137 raises FetchError (still 'covered'); cap counts
        # fetches, so cap=2 stops after covering 62135 and 62136.
        res = self.pipe.probe_orphan_scoresheets(33, start_id=62135, end_id=62139, cap=2)
        self.assertEqual(res["fetched"], 2)
        self.assertEqual(res["last_id"], 62136, "resume point is the last covered id")


class HighWaterMarkTest(unittest.TestCase):
    """The CLI advances a persistent mark so no id is ever permanently skipped."""

    def setUp(self):
        from norcalstats import cli
        from norcalstats.fetch import FetchError
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False,
                             orphan_recent_tail=50, orphan_frontier_ahead=10)
        self.conn = db.connect(self.config.db_path)
        self.conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                          "VALUES (33,'x',2026,'t')")
        # a single known game sets the frontier at 5000
        self.conn.execute("INSERT INTO games(game_id,season_id,status) VALUES (5000,33,'final')")
        self.conn.commit()

        class Stub:
            requests_made = 0
            offline = False
            def get(self, *a, **k):
                raise FetchError("no fixture")
        self._orig = cli._fetcher
        cli._fetcher = lambda config, **kw: Stub()

    def tearDown(self):
        self.cli._fetcher = self._orig
        self.conn.close(); self.tmp.cleanup()

    def _args(self, **kw):
        base = dict(season=33, from_id=None, to_id=None, cap=10000)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_default_run_sets_and_advances_the_mark(self):
        self.cli._cmd_probe_orphans(self.conn, self.config, self._args())
        mark = int(db.get_meta(self.conn, "orphan_probed_to:33"))
        self.assertEqual(mark, 5010, "advanced to the frontier + ahead")
        # a second run now starts at the tail (max_id - recent_tail), not from 1
        self.cli._cmd_probe_orphans(self.conn, self.config, self._args())
        self.assertGreaterEqual(int(db.get_meta(self.conn, "orphan_probed_to:33")), 5010)

    def test_frontier_advances_even_when_the_cap_is_tiny(self):
        # Guard the bug this fixed: a dense tail (mostly other-league ids) must
        # not consume the whole cap and stall the mark. Frontier-first means new
        # ids are always covered first, so the mark advances every run.
        db.set_meta(self.conn, "orphan_probed_to:33", "4800"); self.conn.commit()
        self.cli._cmd_probe_orphans(self.conn, self.config, self._args(cap=5))
        mark = int(db.get_meta(self.conn, "orphan_probed_to:33"))
        self.assertGreater(mark, 4800, "the mark advanced despite the tiny cap")

    def test_manual_range_leaves_the_mark_untouched(self):
        db.set_meta(self.conn, "orphan_probed_to:33", "4000"); self.conn.commit()
        self.cli._cmd_probe_orphans(self.conn, self.config, self._args(from_id=100, to_id=120))
        self.assertEqual(db.get_meta(self.conn, "orphan_probed_to:33"), "4000",
                         "a manual sweep must not move the mark")


if __name__ == "__main__":
    unittest.main()
