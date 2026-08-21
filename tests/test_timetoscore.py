"""Parser tests against real pages saved from the live site.

Fixtures cover both ends of the backfill range (2021 and 2025) so a format
change in either direction is caught.
"""

import pathlib
import unittest

from norcalstats.sources import timetoscore as tts

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class TestSeasonList(unittest.TestCase):
    def test_reads_seasons_from_the_dropdown(self):
        seasons = tts.parse_season_list(load("ts31.html"))
        by_id = {s.season_id: s for s in seasons}
        self.assertEqual(by_id[31].label, "Fall 2025")
        self.assertEqual(by_id[31].start_year, 2025)
        self.assertEqual(by_id[27].start_year, 2021)

    def test_season_number_is_not_year_minus_1994(self):
        # The old README's shortcut is wrong for older seasons; reading the
        # dropdown is the only reliable mapping.
        by_id = {s.season_id: s for s in tts.parse_season_list(load("ts31.html"))}
        self.assertEqual(by_id[24].start_year, 2019)   # not 2018
        self.assertEqual(by_id[18].start_year, 2016)   # not 2012

    def test_current_placeholder_is_skipped(self):
        ids = {s.season_id for s in tts.parse_season_list(load("ts31.html"))}
        self.assertNotIn(0, ids)


class TestSeasonIndex(unittest.TestCase):
    def test_every_team_gets_a_division(self):
        # The original scraper left divisions blank; nothing should be unassigned.
        for fixture in ("ts31.html", "ts27.html"):
            teams = tts.parse_season_index(load(fixture))
            self.assertTrue(teams)
            missing = [t for t in teams if t.division is None]
            self.assertEqual(missing, [], f"{fixture}: teams without a division")

    def test_team_names_and_ids(self):
        teams = {t.team_id: t for t in tts.parse_season_index(load("ts31.html"))}
        self.assertEqual(teams[58].name, "San Jose Jr Sharks")
        self.assertEqual(teams[58].division.name, "10U A")

    def test_standings_are_read_by_header_label(self):
        teams = {t.team_id: t for t in tts.parse_season_index(load("ts31.html"))}
        standing = teams[58].standings
        self.assertEqual(standing["gp"], 15)
        self.assertEqual(standing["w"], 14)
        self.assertEqual(standing["l"], 1)
        self.assertEqual(standing["gf"], 160)
        self.assertEqual(standing["ga"], 36)
        self.assertEqual(standing["pts"], 28)

    def test_same_club_can_appear_twice_in_one_division(self):
        teams = tts.parse_season_index(load("ts31.html"))
        tenu_a = [t for t in teams if t.division and t.division.name == "10U A"]
        names = [t.name for t in tenu_a]
        self.assertGreater(len(names), len(set(names)),
                           "expected a duplicated club name in 10U A")


class TestTeamPage(unittest.TestCase):
    def test_game_results(self):
        page = tts.parse_team_page(load("team58.html"))
        game = next(g for g in page.games if g.game_id == 50647)
        self.assertEqual(game.date_text, "Fri Aug 29")
        self.assertEqual(game.level, "10U A")
        self.assertEqual(game.away_name, "San Jose Jr Sharks")
        self.assertEqual(game.away_goals, 19)
        self.assertEqual(game.home_name, "Santa Clara Blackhawks")
        self.assertEqual(game.home_goals, 2)
        self.assertEqual(game.game_type, "Preseason")
        self.assertTrue(game.has_scoresheet)
        self.assertTrue(game.is_final)

    def test_game_without_a_scoresheet_is_still_recorded(self):
        # 26493 has a final score but no scoresheet link -- the result must not
        # be dropped just because no detail exists.
        page = tts.parse_team_page(load("team27.html"))
        game = next(g for g in page.games if g.game_id == 26493)
        self.assertFalse(game.has_scoresheet)
        self.assertTrue(game.is_final)
        self.assertEqual((game.away_goals, game.home_goals), (5, 2))

    def test_game_id_comes_from_the_link_not_the_label(self):
        """SCAHA prints a prefixed label where the id should be.

        The row reads "SCAHA-10000983*" but the game is really 28592. Reading
        the digits out of the label invented ids, stored 647 games under them,
        and then fetched scoresheets from URLs that had no roster on them.
        """
        page = tts.parse_team_page(load("team_scaha.html"))
        ids = {g.game_id for g in page.games}
        self.assertIn(28592, ids, "the id from the scoresheet link")
        self.assertNotIn(10000983, ids, "not the digits from the printed label")
        # Nothing implausible should survive: real ids are five digits here.
        self.assertTrue(all(g.game_id < 1_000_000 for g in page.games),
                        sorted(ids)[-3:])

    def test_a_row_without_a_link_still_yields_its_id(self):
        # Games with no scoresheet yet have no link; the label is all there is.
        page = tts.parse_team_page(load("team27.html"))
        game = next(g for g in page.games if g.game_id == 26493)
        self.assertFalse(game.has_scoresheet)

    def test_published_stat_tables(self):
        page = tts.parse_team_page(load("team58.html"))
        skaters = [r for r in page.stat_rows if r.kind == "skater"]
        goalies = [r for r in page.stat_rows if r.kind == "goalie"]
        self.assertTrue(skaters and goalies)
        goalie = goalies[0]
        self.assertEqual(goalie.name, "Sol Orlov")
        self.assertEqual(goalie.gp, 15)
        self.assertEqual(goalie.data["Shots"], "255")
        self.assertEqual(goalie.data["GA"], "35")
        # The old scraper read SO from the wrong column; check the label map.
        self.assertIn("SO", goalie.data)


class TestGameClass(unittest.TestCase):
    def test_regular_season_games_are_numbered(self):
        self.assertEqual(tts.classify_game_type("Regular 12"), "regular")
        self.assertEqual(tts.classify_game_type("Regular 1"), "regular")

    def test_other_classes(self):
        self.assertEqual(tts.classify_game_type("Preseason"), "preseason")
        self.assertEqual(tts.classify_game_type("Exhibition"), "exhibition")
        self.assertEqual(tts.classify_game_type("Round Robin"), "playoff")
        self.assertEqual(tts.classify_game_type("Championship"), "playoff")
        self.assertEqual(tts.classify_game_type(""), "other")


class TestScoresheet(unittest.TestCase):
    def test_2025_sheet(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        self.assertTrue(sheet.is_usable)
        self.assertEqual(sheet.date_iso, "2025-08-29")
        self.assertEqual(sheet.level, "10U A")
        self.assertEqual(sheet.location, "San Jose Grey")
        self.assertEqual(sheet.away.team_name, "San Jose Jr Sharks")
        self.assertEqual(sheet.home.team_name, "Santa Clara Blackhawks")
        self.assertEqual(sheet.away.final, 19)
        self.assertEqual(sheet.home.final, 2)
        self.assertEqual(sheet.away.period_goals, {"1": 4, "2": 12, "3": 3})

    def test_2021_sheet_same_shape(self):
        sheet = tts.parse_scoresheet(load("game27.html"), 25592)
        self.assertTrue(sheet.is_usable)
        self.assertEqual(sheet.date_iso, "2021-09-19")
        self.assertEqual(sheet.away.final, 1)
        self.assertEqual(sheet.home.final, 13)

    def test_goal_count_matches_final_score(self):
        for fixture, game_id in (("game.html", 50647), ("game27.html", 25592)):
            sheet = tts.parse_scoresheet(load(fixture), game_id)
            for side in (sheet.home, sheet.away):
                self.assertEqual(len(side.goals), side.final, fixture)
            self.assertEqual(sheet.warnings, [], fixture)

    def test_scoring_tables_are_assigned_to_the_right_side(self):
        # The visitor's table renders first; a mix-up would swap 19 and 2.
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        self.assertEqual(len(sheet.away.goals), 19)
        self.assertEqual(len(sheet.home.goals), 2)

    def test_coaches_are_not_players(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        roster = sheet.away.roster
        coaches = [e for e in roster if e.role == "coach"]
        players = [e for e in roster if e.role == "player"]
        self.assertTrue(coaches, "expected HC/AC entries")
        self.assertTrue(all(e.jersey.upper() in ("HC", "AC") for e in coaches))
        self.assertTrue(all(e.jersey.upper() not in ("HC", "AC") for e in players))

    def test_goalies_identified_by_position(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        goalies = [e for e in sheet.away.roster if e.position == "G"]
        self.assertEqual([g.name for g in goalies], ["Sol Orlov"])

    def test_goalie_changes(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        self.assertEqual(sheet.home.goalies[0].name, "Clayton J Harvey")
        self.assertEqual(sheet.home.goalies[0].note, "Starting")
        self.assertEqual(sheet.away.goalies[0].name, "Sol Orlov")

    def test_goal_details(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        with_assists = [g for g in sheet.away.goals if g.assist1]
        self.assertTrue(with_assists)
        powerplay = [g for g in sheet.away.goals if g.strength == "PP"]
        shorthanded = [g for g in sheet.away.goals if g.strength == "SH"]
        self.assertTrue(powerplay, "expected a PP goal")
        self.assertTrue(shorthanded, "expected a SH goal")

    def test_penalties(self):
        sheet = tts.parse_scoresheet(load("game.html"), 50647)
        penalty = sheet.away.penalties[0]
        self.assertEqual(penalty.period, "1")
        self.assertEqual(penalty.jersey, "8")
        self.assertEqual(penalty.infraction, "Holding")
        self.assertEqual(penalty.minutes, 2.0)

    def test_jersey_with_leading_zero_preserved(self):
        # "06" must not become 6 -- the roster and the event tables disagree.
        sheet = tts.parse_scoresheet(load("game27.html"), 25592)
        assists = [g.assist1 for g in sheet.home.goals if g.assist1]
        self.assertIn("06", assists)

    def test_empty_document_is_not_usable(self):
        sheet = tts.parse_scoresheet("<html><body>Not found</body></html>", 1)
        self.assertFalse(sheet.is_usable)


class TestDatesAndClock(unittest.TestCase):
    def test_schedule_date_spans_the_new_year(self):
        self.assertEqual(tts.schedule_date_to_iso("Fri Aug 29", 2025), "2025-08-29")
        self.assertEqual(tts.schedule_date_to_iso("Sun Jan 4", 2025), "2026-01-04")
        self.assertEqual(tts.schedule_date_to_iso("Sat Mar 14", 2025), "2026-03-14")

    def test_schedule_date_without_a_year_is_none(self):
        self.assertIsNone(tts.schedule_date_to_iso("Fri Aug 29", None))

    def test_clock_parsing(self):
        self.assertEqual(tts._parse_clock("9:10"), 550)
        self.assertEqual(tts._parse_clock("48.6"), 48)  # under a minute
        self.assertIsNone(tts._parse_clock(""))

    def test_jersey_index_handles_leading_zeros(self):
        sheet = tts.parse_scoresheet(load("game27.html"), 25592)
        index = tts.iter_jerseys(sheet.home.roster)
        if "06" in index:
            self.assertIs(index["06"], index["6"])


if __name__ == "__main__":
    unittest.main()
