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


if __name__ == "__main__":
    unittest.main()
