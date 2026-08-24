"""The web app's dataset: a whole core file plus detail loaded on demand.

The split is by granularity, not by season. Season sharding would break the two
views that exist to span seasons -- club player flow, and a club's teams across
the years -- so everything cross-season stays in one file and only per-game
detail is sharded.
"""

import json
import pathlib
import tempfile
import unittest

from norcalstats import appdata, db, identity, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher


class AppDataTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False)
        self.conn = db.connect(self.config.db_path)
        self.out = base / "app"
        self._seed()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (31,'Fall 2025',2025,'x')")
        conn.execute("INSERT INTO divisions(season_id,league_id,name,gender) "
                     "VALUES (31,3,'10U A','coed')")
        division = db.scalar(conn, "SELECT division_id FROM divisions")
        for team, name in ((58, "Jr Sharks"), (129, "Cougars")):
            conn.execute(
                "INSERT INTO teams(team_id,season_id,name,club,division_id,"
                "league_id,gender) VALUES (?,31,?,?,?,3,'coed')",
                (team, name, name, division))
        conn.execute("INSERT INTO standings(season_id,team_id,gp,w,l,pts,updated_at) "
                     "VALUES (31,58,15,14,1,28,'x')")

        # Two games, so a player has a game log worth sharding.
        for game_id, hg, ag in ((1, 3, 1), (2, 2, 4)):
            conn.execute(
                "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
                "home_team_id,away_team_id,home_goals,away_goals,status,game_class) "
                "VALUES (?,31,3,?,?,58,129,?,?,'final','regular')",
                (game_id, division, f"2025-10-0{game_id}", hg, ag))
            conn.execute(
                "INSERT INTO game_rosters(game_id,side,slot,jersey,position,name,role) "
                "VALUES (?, 'home',0,'9','','Kai Garrett','player')", (game_id,))
            conn.execute(
                "INSERT INTO period_scores(game_id,side,period,goals) "
                "VALUES (?, 'home','1',?)", (game_id, hg))
            conn.execute(
                "INSERT INTO goals(game_id,side,seq,period,time_text,time_sec,"
                "strength,scorer_jersey) VALUES (?, 'home',0,'2','5:50',350,'PP','9')",
                (game_id,))
            conn.execute(
                "INSERT INTO penalties(game_id,side,seq,period,jersey,infraction,"
                "minutes) VALUES (?, 'home',0,'2','9','Tripping',2)", (game_id,))
        conn.commit()

        identity.rebuild(conn)
        pipeline.rebuild_player_game_stats(conn)
        conn.commit()

    def write(self):
        return appdata.write_app(self.conn, self.out)

    def core(self):
        return json.loads((self.out / "core.json").read_text(encoding="utf-8"))


class TestCoreFile(AppDataTestCase):
    def test_it_carries_everything_the_cross_season_views_need(self):
        self.write()
        core = self.core()
        for key in ("seasons", "leagues", "divisions", "teams", "standings", "players"):
            self.assertTrue(core[key], f"core.{key} is empty")

    def test_clubs_and_divisions_survive_for_the_flow_chart(self):
        # Player Flow and Club View span seasons, so they must never need a
        # shard. Everything they read lives here.
        self.write()
        core = self.core()
        self.assertTrue(all("club" in t for t in core["teams"]))
        self.assertTrue(all("season" in t for t in core["teams"]))

    def test_a_player_with_no_spelling_is_not_exported(self):
        # Undoing a split leaves the spare player rows behind, carrying a
        # display name but no spelling and no stat line. They are unreachable
        # by search and empty when opened.
        self.conn.execute(
            "INSERT INTO players(player_id, display_name, canonical_name)"
            " VALUES (999999, 'Ghost Player', 'ghost player')")
        self.conn.commit()
        self.write()
        names = [p["name"] for p in self.core()["players"]]
        self.assertNotIn("Ghost Player", names)
        self.assertTrue(names, "real players must still be exported")

    def test_players_carry_per_season_summaries(self):
        self.write()
        player = next(p for p in self.core()["players"] if p["seasons"])
        season = player["seasons"][0]
        self.assertEqual(season["season"], 31)
        self.assertEqual(season["team"], 58)
        self.assertEqual(season["class"], "regular")
        self.assertEqual(season["gp"], 2)

    def test_special_teams_are_summarised(self):
        # Both goals were PP, and those flags are set by the scoresheet system
        # rather than typed, so they are trustworthy.
        self.write()
        player = next(p for p in self.core()["players"] if p["seasons"])
        self.assertEqual(player["seasons"][0]["ppg"], 2)

    def test_standings_are_included(self):
        self.write()
        standing = self.core()["standings"][0]
        self.assertEqual((standing["w"], standing["l"], standing["pts"]), (14, 1, 28))

    def test_no_game_level_detail_leaks_into_core(self):
        self.write()
        text = (self.out / "core.json").read_text(encoding="utf-8")
        self.assertNotIn("Tripping", text, "penalties belong in the season shards")

    def test_a_two_way_player_is_split_by_role(self):
        # Kai skates out in the first two games and takes a turn in net in the
        # third. Folding those together would put the skating games into the
        # goalie GP, so each role gets its own row.
        self.conn.execute(
            "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
            "home_team_id,away_team_id,home_goals,away_goals,status,game_class) "
            "VALUES (3,31,3,(SELECT division_id FROM divisions),'2025-10-03',"
            "58,129,1,2,'final','regular')")
        self.conn.execute(
            "INSERT INTO game_rosters(game_id,side,slot,jersey,position,name,role) "
            "VALUES (3,'home',0,'9','G','Kai Garrett','player')")
        self.conn.commit()
        identity.rebuild(self.conn)
        pipeline.rebuild_player_game_stats(self.conn)
        self.conn.commit()

        self.write()
        player = next(p for p in self.core()["players"] if p["name"] == "Kai Garrett")
        by_role = {s["goalie"]: s for s in player["seasons"]}
        self.assertEqual(sorted(by_role), [False, True], "expected a row per role")
        self.assertEqual(by_role[False]["gp"], 2)
        self.assertEqual(by_role[True]["gp"], 1)


class TestShards(AppDataTestCase):
    def test_a_player_log_lands_in_its_own_shard(self):
        self.write()
        player = next(p for p in self.core()["players"] if p["seasons"])
        shard = json.loads(
            (self.out / f"logs/p{player['shard']:02d}.json").read_text(encoding="utf-8"))
        log = shard["players"][str(player["id"])]
        self.assertEqual(len(log), 2, "one line per game")
        self.assertEqual(log[0]["opp"], 129)
        self.assertEqual(log[0]["date"], "2025-10-01")

    def test_the_shard_is_derivable_from_the_player_id_alone(self):
        # The app must know which file to fetch without an index.
        self.write()
        for player in self.core()["players"]:
            self.assertEqual(player["shard"], appdata.shard_for(player["id"]))
            self.assertTrue((self.out / f"logs/p{player['shard']:02d}.json").exists())

    def test_a_scheduled_game_is_carried_without_a_score(self):
        # A team page is a schedule as much as a record, and the current season
        # regularly has nothing but scheduled games.
        self.conn.execute(
            "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
            "home_team_id,away_team_id,status,game_class) "
            "VALUES (9,31,3,(SELECT division_id FROM divisions),'2026-01-15',"
            "58,129,'scheduled','regular')")
        self.conn.commit()
        self.write()
        payload = json.loads((self.out / "games" / "s31.json").read_text(encoding="utf-8"))
        game = payload["games"]["9"]
        self.assertEqual(game["status"], "scheduled")
        self.assertNotIn("hg", game, "an unplayed game must not report a score")
        self.assertNotIn("ag", game)
        # and a played one still carries its result, with no status noise
        played = payload["games"]["1"]
        self.assertEqual((played["hg"], played["ag"]), (3, 1))
        self.assertNotIn("status", played)

    def test_game_detail_is_per_season(self):
        self.write()
        detail = json.loads(
            (self.out / "games/s31.json").read_text(encoding="utf-8"))
        game = detail["games"]["1"]
        self.assertEqual(len(game["goals"]), 1)
        self.assertEqual(game["goals"][0]["str"], "PP")
        self.assertEqual(game["penalties"][0]["inf"], "Tripping")
        self.assertEqual(game["periods"]["home"]["1"], 3)

    def test_goals_reference_players_by_id(self):
        self.write()
        detail = json.loads((self.out / "games/s31.json").read_text(encoding="utf-8"))
        goal = detail["games"]["1"]["goals"][0]
        core_ids = {p["id"] for p in self.core()["players"]}
        self.assertIn(goal["by"], core_ids, "scorer must resolve against core")

    def test_every_shard_is_written_even_when_empty(self):
        # A missing file would be a 404 for the app; an empty one is fine.
        written = self.write()
        logs = [n for n in written if n.startswith("logs/")]
        self.assertEqual(len(logs), appdata.SHARD_COUNT)


class TestScheduleIndex(AppDataTestCase):
    """One file listing every game, which is what the app opens on.

    The date order it is read in runs across leagues and across seasons, so no
    per-season file can answer it.
    """

    def schedule(self):
        return json.loads((self.out / "schedule.json").read_text(encoding="utf-8"))

    def rows(self):
        payload = self.schedule()
        col = {name: i for i, name in enumerate(payload["columns"])}
        return [{name: row[i] for name, i in col.items()} for row in payload["games"]]

    def test_every_game_is_listed(self):
        self.write()
        payload = self.schedule()
        self.assertEqual(payload["metadata"]["games"], 2)
        self.assertEqual(len(payload["games"]), 2)
        self.assertEqual(payload["columns"], list(appdata.SCHEDULE_COLUMNS))

    def test_a_row_is_a_header_and_nothing_more(self):
        # The whole point of the file: the events stay in the season shard, so
        # a list of ten thousand games does not carry ten thousand box scores.
        self.write()
        for row in self.schedule()["games"]:
            self.assertEqual(len(row), len(appdata.SCHEDULE_COLUMNS))
        blob = (self.out / "schedule.json").read_text(encoding="utf-8")
        for leaked in ("goals", "penalties", "periods", "Tripping"):
            self.assertNotIn(leaked, blob)

    def test_it_carries_what_a_list_prints(self):
        self.write()
        game = next(r for r in self.rows() if r["id"] == 1)
        self.assertEqual(game["season"], 31)
        self.assertEqual(game["date"], "2025-10-01")
        self.assertEqual(game["league"], 3)
        self.assertEqual((game["home"], game["away"]), (58, 129))
        self.assertEqual((game["hg"], game["ag"]), (3, 1))
        self.assertEqual(game["class"], "regular")

    def test_an_unplayed_game_carries_no_score(self):
        # Nil-nil and "not played" are different things, and 5,613 of 14,196
        # games are the second one.
        self.conn.execute(
            "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
            "home_team_id,away_team_id,status,game_class) "
            "VALUES (9,31,3,(SELECT division_id FROM divisions),'2026-01-15',"
            "58,129,'scheduled','regular')")
        self.conn.commit()
        self.write()
        game = next(r for r in self.rows() if r["id"] == 9)
        self.assertIsNone(game["hg"])
        self.assertIsNone(game["ag"])
        self.assertEqual(self.schedule()["metadata"]["played"], 2)

    def test_only_an_unidentified_side_carries_its_printed_name(self):
        # 503 games have a side the collector could not pin to a team row. The
        # name is all the app has for those, and dead weight for the rest.
        self.conn.execute(
            "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
            "home_team_id,home_name,away_name,status,game_class) "
            "VALUES (9,31,3,(SELECT division_id FROM divisions),'2026-01-15',"
            "58,'Jr Sharks','Some Visiting Team','final','regular')")
        self.conn.commit()
        self.write()
        unknown = next(r for r in self.rows() if r["id"] == 9)
        self.assertIsNone(unknown["away"])
        self.assertEqual(unknown["awayName"], "Some Visiting Team")
        self.assertIsNone(unknown["homeName"], "an identified side needs no name")

    def test_games_are_ordered_by_date_then_by_time_of_day(self):
        # The app shows a day as a block and never sorts: the order in the file
        # is the order on the screen.
        div = db.scalar(self.conn, "SELECT division_id FROM divisions")
        for game_id, time_text in ((11, "7:15 PM"), (12, "8:00 AM"), (13, "12 Noon")):
            self.conn.execute(
                "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
                "time_text,home_team_id,away_team_id,status,game_class) "
                "VALUES (?,31,3,?,'2026-01-15',?,58,129,'scheduled','regular')",
                (game_id, div, time_text))
        self.conn.commit()
        self.write()
        same_day = [r for r in self.rows() if r["date"] == "2026-01-15"]
        self.assertEqual([r["time"] for r in same_day],
                         ["8:00 AM", "12 Noon", "7:15 PM"])
        dates = [r["date"] for r in self.rows()]
        self.assertEqual(dates, sorted(dates))

    def test_an_unreadable_time_sorts_to_the_end_of_its_day(self):
        # Rather than to the start, where a new spelling would silently look
        # like a game at midnight.
        self.assertEqual(appdata._minute_of_day("8:00 AM"), 480)
        self.assertEqual(appdata._minute_of_day("12 Noon"), 720)
        self.assertEqual(appdata._minute_of_day("12:30 AM"), 30)
        self.assertEqual(appdata._minute_of_day("12:30 PM"), 750)
        self.assertEqual(appdata._minute_of_day("half past four"), 1440)
        self.assertEqual(appdata._minute_of_day(None), 1440)

    def test_the_file_is_written_alongside_the_rest(self):
        written = self.write()
        self.assertIn("schedule.json", written)


class TestTheLeagueRollUp(AppDataTestCase):
    """One competition, one entry in the league picker, the round on the game.

    The site runs CAHA under four ids and collection follows it, so the export
    is the layer that has to put them back together.
    """

    def _caha_game(self, game_id, league_id, date="2025-11-02"):
        division = db.scalar(self.conn, "SELECT division_id FROM divisions")
        self.conn.execute(
            "INSERT INTO games(game_id,season_id,league_id,division_id,date_iso,"
            "home_team_id,away_team_id,home_goals,away_goals,status,game_class) "
            "VALUES (?,31,?,?,?,58,129,4,2,'final','regular')",
            (game_id, league_id, division, date))
        self.conn.commit()

    def schedule_rows(self):
        payload = json.loads(
            (self.out / "schedule.json").read_text(encoding="utf-8"))
        col = {name: i for i, name in enumerate(payload["columns"])}
        return [{name: row[i] for name, i in col.items()} for row in payload["games"]]

    def test_only_the_leagues_a_reader_picks_are_listed(self):
        self.write()
        listed = {l["id"] for l in self.core()["leagues"]}
        self.assertIn(5, listed, "CAHA itself is a league")
        for round_id in (16, 17, 24):
            self.assertNotIn(round_id, listed,
                             "a round of CAHA is not a league of its own")
        for dropped in db.DROPPED_LEAGUES:
            self.assertNotIn(dropped, listed)

    def test_a_round_s_game_is_reported_under_the_parent(self):
        self._caha_game(40, 24)
        self.write()
        game = next(r for r in self.schedule_rows() if r["id"] == 40)
        self.assertEqual(game["league"], 5, "a playoff round game is a CAHA game")

    def test_the_round_survives_as_a_label(self):
        self._caha_game(41, 16)
        self._caha_game(42, 17)
        self._caha_game(43, 5)
        self.write()
        by_id = {r["id"]: r for r in self.schedule_rows()}
        self.assertEqual(by_id[41]["stage"], "Preseason")
        self.assertEqual(by_id[42]["stage"], "Weekends")
        self.assertIsNone(by_id[43]["stage"],
                          "the main league is not a round of anything")

    def test_the_label_reaches_the_game_detail_too(self):
        self._caha_game(44, 24)
        self.write()
        detail = json.loads(
            (self.out / "games/s31.json").read_text(encoding="utf-8"))
        self.assertEqual(detail["games"]["44"]["stage"], "Playoffs")
        self.assertEqual(detail["games"]["44"]["league"], 5)

    def test_a_team_and_its_division_roll_up_as_well(self):
        # Otherwise a league filter would keep games the picker cannot reach.
        self.conn.execute("UPDATE teams SET league_id = 24 WHERE team_id = 58")
        self.conn.execute("UPDATE divisions SET league_id = 24")
        self.conn.commit()
        self.write()
        core = self.core()
        team = next(t for t in core["teams"] if t["id"] == 58)
        self.assertEqual(team["league"], 5)
        self.assertTrue(all(d["league"] == 5 for d in core["divisions"]))

    def test_a_standalone_league_is_left_alone(self):
        self.write()
        game = next(r for r in self.schedule_rows() if r["id"] == 1)
        self.assertEqual(game["league"], 3, "Norcal is not part of a family")
        self.assertIsNone(game["stage"])


if __name__ == "__main__":
    unittest.main()
