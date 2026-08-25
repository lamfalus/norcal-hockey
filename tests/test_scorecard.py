"""The PDF scorecard: parsing the Goaltender Records, and the fix it drives.

Reads real scorecards saved as fixtures. Three games cover the cases that
matter: one goalie per side, two goalies splitting one side, home printed in
the right-hand column, and a side whose saves column the scorekeeper left blank.
Everything runs offline against the saved PDFs.
"""

import pathlib
import tempfile
import unittest

from norcalstats import db, pdf, pipeline
from norcalstats.config import Config
from norcalstats.sources import timetoscore as tts

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: (home_goals, away_goals) for each fixture, as the site recorded them.
SCORES = {56739: (4, 6), 56801: (6, 2), 56743: (14, 0)}


def card_bytes(game_id: int) -> bytes:
    return (FIXTURES / f"scorecard_{game_id}.pdf").read_bytes()


class TestPdfReader(unittest.TestCase):
    def test_positioned_text_is_extracted(self):
        items = pdf.text_items(card_bytes(56739))
        self.assertTrue(items)
        texts = {it.text for it in items}
        self.assertIn("Goaltender Records", " ".join(sorted(texts)) or "",
                      )  # the section title is present somewhere
        self.assertTrue(any("Goaltender" in it.text for it in items))

    def test_rows_group_top_of_page_first(self):
        rows = pdf.rows(pdf.text_items(card_bytes(56739)))
        ys = [r[0].y for r in rows if r]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_two_different_pdf_versions_both_read(self):
        # 56739 is PDF 1.3, 56801 is PDF 1.4; both must yield the table.
        for gid in (56739, 56801):
            rows = pdf.rows(pdf.text_items(card_bytes(gid)))
            self.assertTrue(any([t.text for t in r].count("Goalie") == 2 for r in rows),
                            f"{gid} has no goaltender header")


class TestScorecardParse(unittest.TestCase):
    def test_single_goalie_each_side(self):
        card = tts.parse_scorecard(card_bytes(56801), 56801)
        self.assertEqual(len(card.home), 1)
        self.assertEqual(len(card.away), 1)
        self.assertEqual(card.home[0].goals_against, 2)
        self.assertEqual(card.away[0].goals_against, 6)

    def test_two_goalies_split_one_side(self):
        # The bug that started this: one side used two goalies. Each gets their
        # own line, not the side's whole goals-against.
        card = tts.parse_scorecard(card_bytes(56739), 56739)
        self.assertEqual(len(card.home), 2)
        self.assertEqual({g.jersey for g in card.home}, {"1", "72"})
        self.assertEqual(sorted(g.goals_against for g in card.home), [3, 3])
        self.assertEqual(len(card.away), 1)
        self.assertEqual(card.away[0].jersey, "90")
        self.assertEqual(card.away[0].goals_against, 4)

    def test_home_column_order_is_read_not_assumed(self):
        # 56739 prints home on the left, 56801 prints home on the right. The
        # On Home/On Away labels decide it; getting this wrong swaps the sides.
        home_left = tts.parse_scorecard(card_bytes(56739), 56739)
        home_right = tts.parse_scorecard(card_bytes(56801), 56801)
        # In both, the home total GA equals the away team's goals.
        self.assertEqual(sum(g.goals_against for g in home_left.home), SCORES[56739][1])
        self.assertEqual(sum(g.goals_against for g in home_right.home), SCORES[56801][1])

    def test_per_period_shots_and_saves(self):
        card = tts.parse_scorecard(card_bytes(56739), 56739)
        haenlein = next(g for g in card.home if g.jersey == "1")
        self.assertEqual(haenlein.shots, {"1": 10, "2": 8, "Total": 18})
        self.assertEqual(haenlein.saves, {"1": 8, "2": 7, "Total": 15})

    def test_reconciles_with_the_score(self):
        for gid in (56739, 56801):
            card = tts.parse_scorecard(card_bytes(gid), gid)
            ok = tts.reconcile_scorecard(card, *SCORES[gid])
            self.assertTrue(ok["home"] and ok["away"], f"{gid} should reconcile")

    def test_blank_saves_column_does_not_reconcile(self):
        # A scorekeeper left the saves column empty; the GA cannot be computed,
        # so neither side reconciles and the caller keeps the derived fallback.
        card = tts.parse_scorecard(card_bytes(56743), 56743)
        ok = tts.reconcile_scorecard(card, *SCORES[56743])
        self.assertFalse(ok["home"])
        self.assertFalse(ok["away"])

    def test_a_non_pdf_is_handled_not_crashed(self):
        card = tts.parse_scorecard(b"not a pdf at all", 1)
        self.assertFalse(card.is_usable)
        self.assertTrue(card.warnings)


class TestScorecardStorage(unittest.TestCase):
    """store_scorecard -> derive, on a database with the three games."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.pipe = pipeline.Pipeline(self.conn, self.config, None)
        self.conn.execute(
            "INSERT INTO seasons(season_id, label, start_year, first_seen_at) "
            "VALUES (33, 'Fall 2026', 2026, '2026-08-01')")
        # Two rostered goalies per side per game, so the split and the phantom
        # backup are both exercised. Jerseys match the fixtures.
        rosters = {
            56739: {"home": [("1", "Grady R Haenlein"), ("72", "Selma F Tossavainen")],
                    "away": [("90", "Connor Hervey"), ("30", "Ethan Chiu")]},
        }
        for gid, (hg, ag) in SCORES.items():
            self.conn.execute(
                "INSERT INTO games(game_id, season_id, status, has_scoresheet, "
                "home_goals, away_goals, date_iso) VALUES (?,33,'final',1,?,?,'2026-08-22')",
                (gid, hg, ag))
            # Goal events, so the derived goals-against fallback has something
            # to count -- that fallback is COUNT(goals against the side).
            for seq in range(hg):
                self.conn.execute(
                    "INSERT INTO goals(game_id, side, seq, period, time_text) "
                    "VALUES (?, 'home', ?, '1', '0:00')", (gid, seq))
            for seq in range(ag):
                self.conn.execute(
                    "INSERT INTO goals(game_id, side, seq, period, time_text) "
                    "VALUES (?, 'away', ?, '1', '0:00')", (gid, seq))
        for gid, sides in rosters.items():
            for side, goalies in sides.items():
                for slot, (jersey, name) in enumerate(goalies):
                    pid = 1000 + gid % 100 + slot + (0 if side == "home" else 50)
                    self.conn.execute(
                        "INSERT INTO players(player_id, display_name, canonical_name, "
                        "created_at) VALUES (?,?,?,?)",
                        (pid, name, name.lower(), "2026-08-22"))
                    self.conn.execute(
                        "INSERT INTO game_rosters(game_id, side, slot, jersey, position, "
                        "name, role, player_id) VALUES (?,?,?,?,'G',?, 'player', ?)",
                        (gid, side, slot, jersey, name, pid))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_reconciling_records_are_stored(self):
        self.pipe.store_scorecard(56739, card_bytes(56739), "sha")
        rows = self.conn.execute(
            "SELECT side, jersey, goals_against, shots, saves FROM goalie_records "
            "WHERE game_id=56739 ORDER BY side, jersey").fetchall()
        got = {(r["side"], r["jersey"]): (r["goals_against"], r["shots"], r["saves"])
               for r in rows}
        self.assertEqual(got[("home", "1")], (3, 18, 15))
        self.assertEqual(got[("home", "72")], (3, 23, 20))
        self.assertEqual(got[("away", "90")], (4, 23, 19))

    def test_non_reconciling_side_is_rejected_with_a_reason(self):
        self.pipe.store_scorecard(56743, card_bytes(56743), "sha")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM goalie_records WHERE game_id=56743")
            .fetchone()[0], 0)
        err = self.conn.execute(
            "SELECT scorecard_error FROM games WHERE game_id=56743").fetchone()[0]
        self.assertIn("reconcile", err)

    def test_derive_gives_each_goalie_their_own_goals_against(self):
        self.pipe.store_scorecard(56739, card_bytes(56739), "sha")
        self.conn.commit()
        self.pipe.derive()
        lines = {
            (r["jersey"]): (r["goals_against"], r["shots_faced"], r["saves"])
            for r in self.conn.execute(
                "SELECT jersey, goals_against, shots_faced, saves "
                "FROM player_game_stats WHERE game_id=56739 AND is_goalie=1")
        }
        # The two who split the game, each with their real line.
        self.assertEqual(lines["1"], (3, 18, 15))
        self.assertEqual(lines["72"], (3, 23, 20))
        self.assertEqual(lines["90"], (4, 23, 19))
        # The backup who never took the net: zeroed, not the side's whole GA.
        self.assertEqual(lines["30"], (0, 0, 0))

    def test_without_a_scorecard_the_derived_fallback_stands(self):
        # No store_scorecard call: the goalie GA is the old side-total count,
        # and shots/saves stay unknown.
        self.pipe.derive()
        rows = {
            r["jersey"]: (r["goals_against"], r["shots_faced"])
            for r in self.conn.execute(
                "SELECT jersey, goals_against, shots_faced FROM player_game_stats "
                "WHERE game_id=56739 AND is_goalie=1")
        }
        hg, ag = SCORES[56739]
        # No scorecard: shots stay unknown, and every rostered goalie carries
        # the side's whole goals-against -- home goalies the away goals, and so
        # on. This is the known limitation the scorecard exists to fix, and it
        # is asserted here so a future change to the fallback is deliberate.
        self.assertEqual(rows["1"], (ag, None))
        self.assertEqual(rows["72"], (ag, None))   # both home goalies get all 6
        self.assertEqual(rows["90"], (hg, None))
        self.assertEqual(rows["30"], (hg, None))   # both away goalies get all 4


if __name__ == "__main__":
    unittest.main()
