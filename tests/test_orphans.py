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


if __name__ == "__main__":
    unittest.main()
