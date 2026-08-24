"""Multi-league collection, season discovery, and the v1 -> v2 migration.

timetoscore hosts Norcal travel (3), SCAHA (4), CAHA tier 1/tier 2 (5) and
several tournament leagues side by side. They share team ids and season
numbers but have separate divisions whose *names* collide.
"""

import pathlib
import sqlite3
import tempfile
import unittest
from datetime import date

from norcalstats import db, export, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher
from norcalstats.sources import timetoscore as tts

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class TestSeasonDiscovery(unittest.TestCase):
    def test_current_season_is_read_from_page_links(self):
        # The 2026-27 season was live as S33 while the dropdown ended at S31.
        self.assertEqual(tts.parse_current_season(load("current_l3.html")), 33)

    def test_a_normal_season_page_reports_its_own_season(self):
        self.assertEqual(tts.parse_current_season(load("ts31.html")), 31)

    def test_season_zero_is_never_returned(self):
        self.assertIsNone(tts.parse_current_season("<a href='?season=0'>x</a>"))

    def test_league_names_are_read_from_the_page(self):
        self.assertEqual(tts.parse_league_name(load("l3.html")), "Norcal")
        self.assertEqual(tts.parse_league_name(load("l4.html")), "SCAHA")
        self.assertEqual(tts.parse_league_name(load("l5.html")), "CAHA")

    def test_season_start_year_comes_from_the_calendar_not_the_number(self):
        # S31 -> S33 skips a number, so extrapolating would be a year out.
        self.assertEqual(pipeline._current_season_start_year(date(2026, 8, 17)), 2026)
        self.assertEqual(pipeline._current_season_start_year(date(2027, 2, 1)), 2026)
        self.assertEqual(pipeline._current_season_start_year(date(2026, 6, 1)), 2026)
        self.assertEqual(pipeline._current_season_start_year(date(2026, 5, 31)), 2025)


class LeagueDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False,
                             leagues=[3, 5])
        self.conn = db.connect(self.config.db_path)
        self.pipeline = pipeline.Pipeline(
            self.conn, self.config, Fetcher(self.config.base_url, offline=True))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestLeagueSeeding(LeagueDbTestCase):
    def test_known_leagues_are_present(self):
        rows = {r["league_id"]: r["name"] for r in
                self.conn.execute("SELECT league_id, name FROM leagues")}
        self.assertEqual(rows[3], "Norcal")
        self.assertEqual(rows[5], "CAHA")
        self.assertEqual(rows[4], "SCAHA")

    def test_season_long_leagues_outrank_preseason_and_playoffs(self):
        # CAHA runs four ids; the main league must own its teams.
        priority = {r["league_id"]: r["priority"] for r in
                    self.conn.execute("SELECT league_id, priority FROM leagues")}
        self.assertLess(priority[5], priority[16])   # CAHA vs CAHA Preseason
        self.assertLess(priority[5], priority[24])   # CAHA vs CAHA Playoffs
        self.assertLess(priority[3], priority[17])   # Norcal vs CAHA Weekends

    def test_a_discovered_league_never_outranks_a_collected_one(self):
        # A new league must not take ownership of a team away from Norcal/CAHA.
        self.assertGreater(
            pipeline._default_priority(19),
            db.scalar(self.conn,
                      "SELECT MAX(priority) FROM leagues WHERE kind = 'season'"))

    def test_default_league_id_resolves(self):
        # divisions.league_id defaults to 3, which must satisfy the foreign key.
        self.conn.execute(
            "INSERT INTO seasons(season_id,label,first_seen_at) VALUES (31,'x','y')")
        self.conn.execute("INSERT INTO divisions(season_id,name) VALUES (31,'10U A')")
        self.conn.commit()
        self.assertEqual(
            db.scalar(self.conn, "SELECT league_id FROM divisions"), 3)


class TestWhichLeaguesAreCollected(LeagueDbTestCase):
    """Season-long competitions and their playoffs; not weekend tournaments."""

    def setUp(self):
        super().setUp()
        # This suite is about the automatic policy, so drop the fixed list.
        self.config.leagues = []
        self.conn.execute(
            "INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
            "VALUES (31,'Fall 2025',2025,'x')")
        self.conn.commit()

    def _present(self, *league_ids):
        for league_id in league_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO leagues(league_id,name,priority) VALUES (?,?,?)",
                (league_id, f"league {league_id}", 100 + league_id))
            self.conn.execute(
                "INSERT INTO league_seasons(league_id,season_id,teams) VALUES (?,31,10)",
                (league_id,))
        self.conn.commit()

    #: Every league that should be collected, decided explicitly rather than
    #: left to the classifier.
    WANTED = {
        3:  "Norcal travel",
        4:  "SCAHA",
        5:  "CAHA tier 1 / tier 2",
        16: "CAHA preseason",
        17: "CAHA weekends",
        24: "CAHA playoffs",
        34: "PGHL, the girls tier league",
    }

    #: Dropped in v6. Both are end-of-season championships whose field is drawn
    #: from the whole country, so a California team in one is incidental.
    UNWANTED = {
        37: "Pacific District",
        38: "USAH Nationals",
    }

    def test_the_wanted_leagues_are_collected(self):
        self._present(*self.WANTED)
        collected = self.pipeline.leagues_for(31)
        self.assertEqual(set(collected), set(self.WANTED))
        # The four season-long leagues rank above the preseason and playoff
        # rounds, so a team is named and placed by the league it plays in.
        self.assertEqual(collected[:4], [3, 5, 4, 34])
        self.assertEqual(collected[4:], [16, 17, 24])

    def test_every_wanted_league_is_seeded_not_guessed(self):
        for league_id, what in self.WANTED.items():
            kind = db.scalar(
                self.conn, "SELECT kind FROM leagues WHERE league_id = ?", (league_id,))
            self.assertEqual(kind, "season", f"{league_id} ({what})")

    def test_the_out_of_state_championships_are_not_collected(self):
        # Excluded by id, not by the classifier: both names still read as
        # playoffs, which is how they were collected in the first place.
        self._present(*self.UNWANTED)
        self.assertEqual(self.pipeline.leagues_for(31), [])
        for league_id, name in self.UNWANTED.items():
            kind = db.scalar(
                self.conn, "SELECT kind FROM leagues WHERE league_id = ?", (league_id,))
            self.assertEqual(kind, "excluded", f"{league_id} ({name})")
            self.assertIsNotNone(pipeline._PLAYOFF_NAMES.search(name))

    def test_rolling_the_caha_family_up_does_not_change_what_is_collected(self):
        # The roll-up is a presentation decision. All four ids are still
        # crawled, because the site publishes them separately.
        self._present(5, 16, 17, 24)
        self.assertEqual(set(self.pipeline.leagues_for(31)), {5, 16, 17, 24})

    def test_playoffs_are_collected(self):
        self._present(24)
        self.assertIn(24, self.pipeline.leagues_for(31))

    def test_the_girls_tier_league_is_collected(self):
        # PGHL runs Girls 12/14/16/19 AA and AAA -- a tier league.
        self._present(34)
        self.assertIn(34, self.pipeline.leagues_for(31))

    def test_college_hockey_is_skipped(self):
        # ACHA and ACHA MD2: Stanford, Grand Canyon, Berkeley -- not youth.
        self._present(23, 36)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_the_out_of_area_tournament_bucket_is_skipped(self):
        # League 19 runs all season but holds only tournament games.
        self._present(19)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_a_mid_season_holiday_tournament_is_skipped(self):
        # League 40 is an MLK weekend event. Its bracket includes games typed
        # "Championship", which must not be mistaken for a league playoff.
        self._present(40)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_high_school_leagues_are_skipped(self):
        self._present(15, 27, 26, 28)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_weekend_tournaments_are_skipped(self):
        self._present(8, 18, 39, 13, 6, 10)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_an_unclassified_league_is_not_collected_silently(self):
        self._present(19)
        self.assertEqual(self.pipeline.leagues_for(31), [])

    def test_a_manual_include_takes_effect(self):
        self._present(19)
        self.conn.execute("UPDATE leagues SET kind='season' WHERE league_id=19")
        self.conn.commit()
        self.assertEqual(self.pipeline.leagues_for(31), [19])

    def test_an_explicit_config_list_overrides_the_policy(self):
        self._present(8)
        self.config.leagues = [8]
        self.assertEqual(self.pipeline.leagues_for(31), [8])


class TestTheCahaFamilyRollsUp(LeagueDbTestCase):
    """Four ids on the site, one competition to a reader.

    They stay four ids in the database, because that is how the pages are
    fetched. What changes is that three of them point at the fourth, so a
    league picker offers CAHA once, and the round is kept as a label.
    """

    ROUNDS = {16: "Preseason", 17: "Weekends", 24: "Playoffs"}

    def test_each_round_points_at_the_main_league(self):
        for league_id, stage in self.ROUNDS.items():
            row = self.conn.execute(
                "SELECT parent_id, stage FROM leagues WHERE league_id = ?",
                (league_id,)).fetchone()
            self.assertEqual(row["parent_id"], 5, league_id)
            self.assertEqual(row["stage"], stage)

    def test_the_main_league_is_nobody_s_child(self):
        for league_id in (3, 4, 5, 34):
            row = self.conn.execute(
                "SELECT parent_id FROM leagues WHERE league_id = ?",
                (league_id,)).fetchone()
            self.assertIsNone(row["parent_id"], league_id)


class TestDroppingTheChampionships(LeagueDbTestCase):
    """The v6 purge, on a database that already holds the leagues it removes."""

    def _seed_championship(self):
        conn = self.conn
        conn.execute("INSERT OR IGNORE INTO seasons(season_id,label,start_year,"
                     "first_seen_at) VALUES (31,'Fall 2025',2025,'x')")
        conn.execute("INSERT INTO divisions(season_id,league_id,name) "
                     "VALUES (31,37,'16U Tier I NHL')")
        district = db.scalar(conn, "SELECT division_id FROM divisions"
                                   " WHERE league_id = 37")
        conn.execute("INSERT INTO divisions(season_id,league_id,name) "
                     "VALUES (31,3,'16U AA')")
        norcal = db.scalar(conn, "SELECT division_id FROM divisions"
                                 " WHERE league_id = 3")

        # 700 only ever played the championship; 701 also plays a real season.
        for team_id, league_id, division_id in ((700, 37, district),
                                                (701, 37, district)):
            conn.execute(
                "INSERT INTO teams(team_id,season_id,name,club,division_id,"
                "league_id) VALUES (?,31,'Buffalo Regals','Buffalo Regals',?,?)",
                (team_id, division_id, league_id))
            conn.execute(
                "INSERT INTO team_leagues(team_id,season_id,league_id,"
                "division_id,name) VALUES (?,31,37,?,'Buffalo Regals')",
                (team_id, division_id))
            conn.execute("INSERT INTO standings(season_id,team_id,gp,updated_at)"
                         " VALUES (31,?,4,'x')", (team_id,))
        conn.execute(
            "INSERT INTO team_leagues(team_id,season_id,league_id,division_id,"
            "name) VALUES (701,31,3,?,'Tri Valley Bulls 16AA')", (norcal,))

        for game_id, league_id in ((900, 37), (901, 38), (902, 3)):
            conn.execute(
                "INSERT INTO games(game_id,season_id,league_id,division_id,"
                "date_iso,home_team_id,away_team_id,status,game_class) "
                "VALUES (?,31,?,?,'2026-03-20',700,701,'final','playoff')",
                (game_id, league_id, district if league_id != 3 else norcal))
            conn.execute("INSERT INTO goals(game_id,side,seq,period,"
                         "scorer_jersey) VALUES (?,'home',0,'1','9')", (game_id,))
        conn.commit()

    def _run_migration(self):
        db.set_meta(self.conn, "schema_version", "5")
        db.init(self.conn)

    def test_the_championship_games_go_and_the_rest_stay(self):
        self._seed_championship()
        self._run_migration()
        left = [r[0] for r in self.conn.execute(
            "SELECT game_id FROM games ORDER BY game_id")]
        self.assertEqual(left, [902], "only the Norcal game survives")

    def test_deleting_a_game_takes_its_events_with_it(self):
        self._seed_championship()
        self._run_migration()
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT game_id FROM goals")], [902])

    def test_a_team_that_only_played_the_championship_is_removed(self):
        self._seed_championship()
        self._run_migration()
        self.assertIsNone(db.scalar(
            self.conn, "SELECT team_id FROM teams WHERE team_id = 700"))
        self.assertIsNone(db.scalar(
            self.conn, "SELECT team_id FROM standings WHERE team_id = 700"))

    def test_a_team_that_also_plays_a_real_season_is_kept_and_repointed(self):
        self._seed_championship()
        self._run_migration()
        row = self.conn.execute(
            "SELECT league_id, name FROM teams WHERE team_id = 701").fetchone()
        self.assertIsNotNone(row, "a team with a real season must not be deleted")
        self.assertEqual(row["league_id"], 3)
        self.assertEqual(row["name"], "Tri Valley Bulls 16AA")

    def test_the_leagues_themselves_are_marked_excluded(self):
        self._seed_championship()
        self._run_migration()
        for league_id in db.DROPPED_LEAGUES:
            self.assertEqual(
                db.scalar(self.conn,
                          "SELECT kind FROM leagues WHERE league_id = ?", (league_id,)),
                "excluded")
        self.assertEqual(
            db.scalar(self.conn, "SELECT count(*) FROM divisions WHERE league_id = 37"), 0)

    def test_it_is_safe_to_run_on_a_database_that_has_neither(self):
        self._run_migration()
        self.assertEqual(db.scalar(self.conn, "SELECT count(*) FROM games"), 0)

    def _player(self, player_id, name, game_ids, team_id=700):
        """A player, their spelling, and a stat line in each of these games."""
        self.conn.execute(
            "INSERT INTO players(player_id,canonical_name,display_name,created_at)"
            " VALUES (?,?,?,'x')", (player_id, name.lower(), name))
        self.conn.execute(
            "INSERT INTO player_names(name,player_id,seen) VALUES (?,?,1)",
            (name, player_id))
        self.conn.execute(
            "INSERT INTO player_name_map(name,season_id,team_id,player_id)"
            " VALUES (?,31,?,?)", (name, team_id, player_id))
        self.conn.execute(
            "INSERT INTO player_team_seasons(player_id,season_id,team_id,jersey,games)"
            " VALUES (?,31,?,'9',1)", (player_id, team_id))
        for game_id in game_ids:
            self.conn.execute(
                "INSERT INTO player_game_stats(game_id,player_id,season_id,team_id)"
                " VALUES (?,?,31,?)", (game_id, player_id, team_id))
        self.conn.commit()

    def test_a_player_only_ever_in_the_championship_is_removed(self):
        # Nothing else in the codebase deletes a player, so leaving these would
        # add to the orphan pile rather than clear it.
        self._seed_championship()
        self._player(5001, "Buffalo Skater", [900])
        self._run_migration()
        for table, column in (("players", "player_id"), ("player_names", "player_id"),
                              ("player_name_map", "player_id"),
                              ("player_team_seasons", "player_id")):
            self.assertEqual(
                db.scalar(self.conn,
                          f"SELECT count(*) FROM {table} WHERE {column} = 5001"),
                0, table)

    def test_a_player_with_no_stat_line_is_still_found(self):
        # The way in that is easy to miss: a player whose games were never
        # scoresheeted has no stat line and no roster row, and is tied to the
        # competition only by the team they were listed on.
        self._seed_championship()
        self.conn.execute(
            "INSERT INTO players(player_id,canonical_name,display_name,created_at)"
            " VALUES (5007,'rostered only','Rostered Only','x')")
        self.conn.execute(
            "INSERT INTO player_team_seasons(player_id,season_id,team_id,jersey,games)"
            " VALUES (5007,31,700,'7',0)")
        self.conn.commit()
        self._run_migration()
        self.assertEqual(
            db.scalar(self.conn, "SELECT count(*) FROM players WHERE player_id = 5007"),
            0, "a rostered player with no stats must still be purged")

    def test_a_player_who_also_played_elsewhere_is_kept(self):
        self._seed_championship()
        self._player(5002, "Local Skater", [900, 902], team_id=701)
        self._run_migration()
        self.assertEqual(
            db.scalar(self.conn, "SELECT count(*) FROM players WHERE player_id = 5002"), 1)
        left = db.scalar(
            self.conn, "SELECT count(*) FROM player_game_stats WHERE player_id = 5002")
        self.assertEqual(left, 1, "only the Norcal line survives")

    def test_a_hand_made_decision_protects_a_player(self):
        # An override is a decision somebody made. Deleting the row it points at
        # would throw the decision away silently.
        self._seed_championship()
        self._player(5003, "Disputed Name", [900])
        self.conn.execute(
            "INSERT INTO player_overrides(name,player_id,note)"
            " VALUES ('disputed name',5003,'kept by hand')")
        self.conn.commit()
        self._run_migration()
        self.assertEqual(
            db.scalar(self.conn, "SELECT count(*) FROM players WHERE player_id = 5003"),
            1, "a player named in an override must survive the purge")

    def test_a_pre_existing_orphan_is_not_swept_up(self):
        # 4,330 of these predate the purge, from splits that were undone. They
        # are a separate problem and this is not the change that decides them.
        self._seed_championship()
        self.conn.execute(
            "INSERT INTO players(player_id,canonical_name,display_name,created_at)"
            " VALUES (5004,'ghost','Ghost','x')")
        self.conn.commit()
        self._run_migration()
        self.assertEqual(
            db.scalar(self.conn, "SELECT count(*) FROM players WHERE player_id = 5004"), 1)

    def test_the_rows_that_do_not_cascade_go_too(self):
        self._seed_championship()
        self.conn.execute(
            "INSERT INTO team_stat_rows(season_id,team_id,kind,row_index,data_json)"
            " VALUES (31,700,'skater',0,'{}')")
        self.conn.execute(
            "INSERT INTO players(player_id,canonical_name,display_name,created_at)"
            " VALUES (5005,'bench','Bench','x')")
        self.conn.execute(
            "INSERT INTO player_team_seasons(player_id,season_id,team_id,jersey,games)"
            " VALUES (5005,31,700,'4',1)")
        self.conn.commit()
        self._run_migration()
        self.assertEqual(db.scalar(
            self.conn, "SELECT count(*) FROM team_stat_rows WHERE team_id = 700"), 0)
        self.assertEqual(db.scalar(
            self.conn, "SELECT count(*) FROM player_team_seasons WHERE team_id = 700"), 0)

    def test_it_reports_what_it_removed(self):
        self._seed_championship()
        self._player(5006, "Counted Skater", [900])
        gone = db.purge_leagues(self.conn, db.DROPPED_LEAGUES)
        self.assertEqual(gone["games"], 2)
        self.assertEqual(gone["teams"], 1, "701 is re-pointed, not removed")
        self.assertEqual(gone["players"], 1)

    def test_the_caha_roll_up_is_applied_to_an_existing_database(self):
        # INSERT OR IGNORE cannot update rows that are already there, which is
        # exactly the case this migration exists for.
        self.conn.execute("UPDATE leagues SET parent_id = NULL, stage = NULL")
        self.conn.commit()
        self._run_migration()
        self.assertEqual(
            db.scalar(self.conn, "SELECT stage FROM leagues WHERE league_id = 17"),
            "Weekends")
        self.assertEqual(
            db.scalar(self.conn, "SELECT parent_id FROM leagues WHERE league_id = 24"), 5)


class TestLeagueClassification(LeagueDbTestCase):
    def test_playoff_names_are_recognised(self):
        for name in ("CAHA Playoffs", "Pacific District", "USAH Nationals",
                     "State Championship", "Regional Final"):
            self.assertTrue(pipeline._PLAYOFF_NAMES.search(name), name)

    def test_a_championship_above_the_league_is_not_collected_on_its_name(self):
        # The mistake that put Pacific District and USAH Nationals in the
        # database: both read as playoffs, so both were collected.
        for name in ("Pacific District", "USAH Nationals", "Northwest Regionals",
                     "National Championship"):
            self.assertIsNotNone(pipeline._PLAYOFF_NAMES.search(name), name)
            self.assertIsNotNone(pipeline._CHAMPIONSHIP_NAMES.search(name), name)

    def test_a_league_ending_its_own_season_still_is(self):
        # "Playoff" and "final" describe how a league finishes, not a
        # championship above it, so these must keep collecting themselves.
        for name in ("CAHA Playoffs", "Norcal Playoffs", "League Final"):
            self.assertIsNotNone(pipeline._PLAYOFF_NAMES.search(name), name)
            self.assertIsNone(pipeline._CHAMPIONSHIP_NAMES.search(name), name)

    def test_tournament_names_are_not_mistaken_for_playoffs(self):
        for name in ("Wine Country Face Off", "Silver Stick", "One Hockey",
                     "KHS Thanksgiving", "Lake Tahoe MLK"):
            self.assertIsNone(pipeline._PLAYOFF_NAMES.search(name), name)

    def test_a_short_league_is_treated_as_an_event(self):
        self.assertLess(3, pipeline.SEASON_SPAN_DAYS)

    def test_more_than_one_team_is_sampled(self):
        # Judging on a single team made the Pacific Girls league look like a
        # two-day tournament, which would have dropped a tier league.
        self.assertGreaterEqual(pipeline._CLASSIFY_SAMPLE, 3)

    def test_seeded_leagues_are_not_reclassified(self):
        # A seeded decision must survive discovery; _classify_league returns
        # early for anything already decided.
        before = db.scalar(
            self.conn, "SELECT kind FROM leagues WHERE league_id = 8")
        self.pipeline._classify_league(8, 31, [])
        self.assertEqual(
            db.scalar(self.conn, "SELECT kind FROM leagues WHERE league_id = 8"),
            before)
        self.assertEqual(before, "event")


class TestSeasonWindow(LeagueDbTestCase):
    def _seasons(self, *pairs):
        for season_id, year in pairs:
            self.conn.execute(
                "INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                "VALUES (?,?,?,'x')", (season_id, f"Fall {year}", year))
        self.conn.commit()

    def test_only_the_recent_window_is_collected(self):
        # Real season numbers, including the gaps the site leaves.
        self._seasons((18, 2016), (20, 2017), (22, 2018), (24, 2019),
                      (27, 2021), (28, 2022), (29, 2023), (30, 2024),
                      (31, 2025), (33, 2026))
        self.config.seasons_back = 6
        chosen = self.pipeline.target_seasons()
        self.assertEqual(chosen, [27, 28, 29, 30, 31, 33],
                         "six years back plus the current season")

    def test_the_window_follows_start_year_not_season_number(self):
        # S24->S27 is a two-year gap, not three; counting numbers would be wrong.
        self._seasons((24, 2019), (27, 2021), (31, 2025))
        self.config.seasons_back = 4
        self.assertEqual(self.pipeline.target_seasons(), [27, 31])

    def test_zero_means_no_limit(self):
        self._seasons((2, 2009), (31, 2025))
        self.config.seasons_back = 0
        self.assertEqual(self.pipeline.target_seasons(), [2, 31])

    def test_an_explicit_request_ignores_the_window(self):
        self._seasons((2, 2009), (31, 2025))
        self.config.seasons_back = 6
        self.assertEqual(self.pipeline.target_seasons([2]), [2])

    def test_a_season_with_no_known_year_is_kept(self):
        # It is about to be scanned, which is what establishes the year.
        self._seasons((31, 2025))
        self.conn.execute(
            "INSERT INTO seasons(season_id,label,first_seen_at) VALUES (34,'?','x')")
        self.conn.commit()
        self.assertIn(34, self.pipeline.target_seasons())


class TestLeagueScopedDivisions(LeagueDbTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
            "VALUES (33,'Fall 2026',2026,'x')")
        self.conn.commit()

    def test_same_division_name_in_two_leagues_stays_separate(self):
        # Norcal and SCAHA both run a "12U A".
        for league in (3, 4):
            self.conn.execute(
                "INSERT INTO divisions(season_id,league_id,name) VALUES (33,?,'12U A')",
                (league,))
        self.conn.commit()
        self.assertEqual(
            db.scalar(self.conn, "SELECT COUNT(*) FROM divisions WHERE name='12U A'"), 2)

    def test_the_same_league_cannot_duplicate_a_division(self):
        self.conn.execute(
            "INSERT INTO divisions(season_id,league_id,name) VALUES (33,3,'12U A')")
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO divisions(season_id,league_id,name) VALUES (33,3,'12U A')")

    def test_export_qualifies_only_colliding_division_names(self):
        for league in (3, 4):
            self.conn.execute(
                "INSERT INTO divisions(season_id,league_id,name) VALUES (33,?,'12U A')",
                (league,))
        self.conn.execute(
            "INSERT INTO divisions(season_id,league_id,name) VALUES (33,5,'14U AAA')")
        self.conn.commit()

        labels = set(export._division_labels(self.conn).values())
        self.assertIn("12U A (Norcal)", labels)
        self.assertIn("12U A (SCAHA)", labels)
        # A name unique to one league is left exactly as printed.
        self.assertIn("14U AAA", labels)


class TestTeamOwnershipAcrossLeagues(LeagueDbTestCase):
    """Team ids are global, so one team can appear in several leagues."""

    def _teams(self, name, division):
        ref = tts.TeamRef(team_id=374, name=name,
                          division=tts.DivisionRef(name=division), standings={})
        return [ref]

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
            "VALUES (31,'Fall 2025',2025,'x')")
        self.conn.commit()

    def test_highest_priority_league_owns_the_team_row(self):
        # Norcal (priority 0) first, then a tournament (priority 50).
        self.pipeline._record_league(3, "Norcal", 0)
        self.pipeline._record_league(8, "Wine Country", 51)
        teams = self._teams("Lake Tahoe Grizzlies 14-1", "14U A")
        ids = self.pipeline._store_divisions(31, 3, teams)
        self.pipeline._store_teams(31, 3, 0, teams, ids)

        tourney = self._teams("Lake Tahoe Grizzlies", "14U")
        tids = self.pipeline._store_divisions(31, 8, tourney)
        self.pipeline._store_teams(31, 8, 50, tourney, tids)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT name, league_id FROM teams WHERE team_id = 374").fetchone()
        self.assertEqual(row["league_id"], 3)
        self.assertEqual(row["name"], "Lake Tahoe Grizzlies 14-1")

    def test_order_of_scanning_does_not_change_the_owner(self):
        # Same two leagues, tournament scanned first.
        self.pipeline._record_league(3, "Norcal", 0)
        self.pipeline._record_league(8, "Wine Country", 51)
        tourney = self._teams("Lake Tahoe Grizzlies", "14U")
        tids = self.pipeline._store_divisions(31, 8, tourney)
        self.pipeline._store_teams(31, 8, 50, tourney, tids)

        teams = self._teams("Lake Tahoe Grizzlies 14-1", "14U A")
        ids = self.pipeline._store_divisions(31, 3, teams)
        self.pipeline._store_teams(31, 3, 0, teams, ids)
        self.conn.commit()

        row = self.conn.execute(
            "SELECT name, league_id FROM teams WHERE team_id = 374").fetchone()
        self.assertEqual(row["league_id"], 3, "Norcal must win regardless of order")

    def test_every_league_appearance_is_recorded(self):
        self.pipeline._record_league(3, "Norcal", 0)
        self.pipeline._record_league(8, "Wine Country", 51)
        teams = self._teams("Lake Tahoe Grizzlies 14-1", "14U A")
        self.pipeline._store_teams(
            31, 3, 0, teams, self.pipeline._store_divisions(31, 3, teams))
        tourney = self._teams("Lake Tahoe Grizzlies", "14U")
        self.pipeline._store_teams(
            31, 8, 50, tourney, self.pipeline._store_divisions(31, 8, tourney))
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT league_id, name FROM team_leagues WHERE team_id = 374 "
            "ORDER BY league_id").fetchall()
        self.assertEqual([r["league_id"] for r in rows], [3, 8])
        self.assertEqual(rows[1]["name"], "Lake Tahoe Grizzlies")


class TestSeasonYearCorrection(LeagueDbTestCase):
    def test_start_year_is_corrected_from_real_game_dates(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (33,'Fall 2027',2027,'x')")
        conn.execute("INSERT INTO games(game_id,season_id,date_iso,scoresheet_at,status)"
                     " VALUES (1,33,'2026-09-14','2026-09-15','final')")
        conn.commit()

        self.assertEqual(pipeline.refine_season_years(conn), 1)
        row = conn.execute(
            "SELECT start_year, label FROM seasons WHERE season_id = 33").fetchone()
        self.assertEqual(row["start_year"], 2026)
        self.assertEqual(row["label"], "Fall 2026")

    def test_a_january_game_belongs_to_the_previous_start_year(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (33,'?',NULL,'x')")
        conn.execute("INSERT INTO games(game_id,season_id,date_iso,scoresheet_at,status)"
                     " VALUES (1,33,'2027-01-10','2027-01-11','final')")
        conn.commit()
        pipeline.refine_season_years(conn)
        self.assertEqual(
            db.scalar(conn, "SELECT start_year FROM seasons WHERE season_id=33"), 2026)

    def test_schedule_dates_are_recomputed_after_a_correction(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (33,'Fall 2027',2027,'x')")
        # One parsed game fixes the year; an unparsed one must be redated.
        conn.execute("INSERT INTO games(game_id,season_id,date_iso,scoresheet_at,status)"
                     " VALUES (1,33,'2026-09-14','2026-09-15','final')")
        conn.execute("INSERT INTO games(game_id,season_id,date_text,date_iso,status)"
                     " VALUES (2,33,'Sat Oct 3','2027-10-03','scheduled')")
        conn.commit()

        pipeline.refine_season_years(conn)
        self.assertEqual(
            db.scalar(conn, "SELECT date_iso FROM games WHERE game_id=2"), "2026-10-03")

    def test_correct_years_are_left_alone(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (31,'Fall 2025',2025,'x')")
        conn.execute("INSERT INTO games(game_id,season_id,date_iso,scoresheet_at,status)"
                     " VALUES (1,31,'2025-08-29','2025-08-30','final')")
        conn.commit()
        self.assertEqual(pipeline.refine_season_years(conn), 0)


class TestAllGameClassesInExport(LeagueDbTestCase):
    """Preseason and playoff games count too, not just the regular season."""

    def setUp(self):
        super().setUp()
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (31,'Fall 2025',2025,'x')")
        conn.execute("INSERT INTO divisions(season_id,league_id,name) "
                     "VALUES (31,3,'10U A')")
        division = db.scalar(conn, "SELECT division_id FROM divisions")
        conn.execute("INSERT INTO teams(team_id,season_id,name,club,division_id,"
                     "league_id) VALUES (58,31,'Jr Sharks','Jr Sharks',?,3)",
                     (division,))

        # 2 preseason, 3 regular, 1 playoff -- each with one goal.
        plan = [("preseason", 2), ("regular", 3), ("playoff", 1)]
        game_id = 0
        for game_class, count in plan:
            for _ in range(count):
                game_id += 1
                conn.execute(
                    "INSERT INTO games(game_id,season_id,league_id,division_id,"
                    "home_team_id,status,game_class) VALUES (?,31,3,?,58,'final',?)",
                    (game_id, division, game_class))
                conn.execute(
                    "INSERT INTO game_rosters(game_id,side,slot,jersey,position,"
                    "name,role) VALUES (?, 'home',0,'9','','Kai Garrett','player')",
                    (game_id,))
                conn.execute(
                    "INSERT INTO goals(game_id,side,seq,period,scorer_jersey) "
                    "VALUES (?, 'home',0,'1','9')", (game_id,))
        conn.commit()

        from norcalstats import identity
        identity.rebuild(conn)
        pipeline.rebuild_player_game_stats(conn)
        conn.commit()

    def _publish(self, gp: int, goals: str) -> None:
        import json as _json
        self.conn.execute(
            "INSERT INTO team_stat_rows(season_id,team_id,kind,row_index,name,"
            "jersey,gp,data_json) VALUES (31,58,'skater',0,'Kai Garrett','9',?,?)",
            (gp, _json.dumps({"Goals": goals, "Ass.": "0", "Hat": "0", "Pts": goals})))
        self.conn.commit()

    def test_totals_include_preseason_and_playoff_games(self):
        self._publish(gp=3, goals="3")   # the league counts only the 3 regular games
        entry = export.build_legacy(self.conn)["players"]["Kai Garrett"][0]
        self.assertEqual(entry["GP"], "6", "all six games should count")
        self.assertEqual(entry["G"], "6")
        self.assertEqual(entry["source"], "games")

    def test_each_class_is_broken_out(self):
        self._publish(gp=3, goals="3")
        entry = export.build_legacy(self.conn)["players"]["Kai Garrett"][0]
        by_class = entry["byClass"]
        self.assertEqual(by_class["preseason"]["GP"], 2)
        self.assertEqual(by_class["regular"]["GP"], 3)
        self.assertEqual(by_class["playoff"]["GP"], 1)
        # The regular-season split is what the league itself publishes.
        self.assertEqual(by_class["regular"]["G"], 3)

    def test_a_partial_backfill_keeps_the_published_totals(self):
        # The league says 15 regular games; only 3 are parsed so far.
        self._publish(gp=15, goals="20")
        entry = export.build_legacy(self.conn)["players"]["Kai Garrett"][0]
        self.assertEqual(entry["source"], "published")
        self.assertEqual(entry["GP"], "15", "must not report a partial count")
        self.assertEqual(entry["G"], "20")
        # The partial detail is still exposed for inspection.
        self.assertEqual(entry["byClass"]["regular"]["GP"], 3)

    def test_players_absent_from_published_totals_still_appear(self):
        # No published row at all: a preseason-only player.
        entry = export.build_legacy(self.conn)["players"]["Kai Garrett"][0]
        self.assertEqual(entry["GP"], "6")
        self.assertEqual(entry["source"], "games")

    def test_entries_carry_the_league(self):
        self._publish(gp=3, goals="3")
        entry = export.build_legacy(self.conn)["players"]["Kai Garrett"][0]
        self.assertEqual(entry["league"], "Norcal")

    def test_metadata_lists_leagues_and_classes(self):
        meta = export.build_legacy(self.conn)["metadata"]
        self.assertEqual(meta["leagues"]["3"], "Norcal")
        self.assertIn("preseason", meta["gameClasses"])
        self.assertIn("playoff", meta["gameClasses"])


class TestMigrationV1toV2(unittest.TestCase):
    """An existing v1 database must upgrade in place, keeping its decisions."""

    def _make_v1(self, path: pathlib.Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE seasons (
                season_id INTEGER PRIMARY KEY, label TEXT, start_year INTEGER,
                first_seen_at TEXT, last_scanned_at TEXT,
                complete INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE divisions (
                division_id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL, name TEXT NOT NULL,
                level INTEGER, conf INTEGER, sort_order INTEGER,
                UNIQUE (season_id, name));
            CREATE TABLE player_overrides (
                name TEXT PRIMARY KEY, player_id INTEGER, merge_into TEXT,
                split INTEGER NOT NULL DEFAULT 0, note TEXT);
            INSERT INTO seasons(season_id, label) VALUES (31, 'Fall 2025');
            INSERT INTO divisions(division_id, season_id, name) VALUES (7, 31, '12U A');
            INSERT INTO player_overrides(name, split, note)
                 VALUES ('Gavin Duganne', 1, 'different kid');
        """)
        conn.commit()
        conn.close()

    def test_migration_preserves_data_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            self._make_v1(path)

            conn = db.connect(path)   # triggers the migration
            try:
                self.assertEqual(db.get_meta(conn, "schema_version"),
                                 str(db.SCHEMA_VERSION))

                # The manual decision survived -- this is the whole point.
                row = conn.execute(
                    "SELECT split, note FROM player_overrides "
                    "WHERE name = 'Gavin Duganne'").fetchone()
                self.assertEqual(row["split"], 1)
                self.assertEqual(row["note"], "different kid")

                # division_id values are preserved so foreign keys stay valid.
                division = conn.execute(
                    "SELECT division_id, league_id, name FROM divisions").fetchone()
                self.assertEqual(division["division_id"], 7)
                self.assertEqual(division["league_id"], 3)
                self.assertEqual(division["name"], "12U A")

                # ...and the constraint is now league-scoped.
                conn.execute(
                    "INSERT INTO divisions(season_id, league_id, name) "
                    "VALUES (31, 4, '12U A')")
                conn.commit()
                self.assertEqual(
                    db.scalar(conn, "SELECT COUNT(*) FROM divisions"), 2)
            finally:
                conn.close()

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.sqlite3"
            self._make_v1(path)
            db.connect(path).close()
            conn = db.connect(path)
            try:
                self.assertEqual(db.get_meta(conn, "schema_version"),
                                 str(db.SCHEMA_VERSION))
                self.assertEqual(db.scalar(conn, "SELECT COUNT(*) FROM divisions"), 1)
            finally:
                conn.close()

    def test_a_newer_database_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "future.sqlite3"
            conn = db.connect(path)
            db.set_meta(conn, "schema_version", "99")
            conn.commit()
            conn.close()
            with self.assertRaises(RuntimeError):
                db.connect(path)


if __name__ == "__main__":
    unittest.main()
