"""The Telegram result announcer: which games it picks, the message it builds,
that it never repeats one, and that it is silent without credentials.

No network: the sender is monkeypatched to record calls.
"""

import pathlib
import tempfile
import unittest

from norcalstats import db, notify, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher


class NotifyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False,
                             telegram_bot_token="TOKEN", telegram_chat_id="-100999")
        self.conn = db.connect(self.config.db_path)
        self.conn.execute(
            "INSERT INTO seasons(season_id, label, start_year, first_seen_at) "
            "VALUES (33, 'Fall 2026', 2026, '2026-01-01')")
        self.conn.commit()
        self.sent: list[tuple] = []
        self._orig = notify.send_message
        notify.send_message = lambda token, chat_id, text, **kw: (
            self.sent.append((token, chat_id, text)) or True)

    def tearDown(self) -> None:
        notify.send_message = self._orig
        self.conn.close()
        self.tmp.cleanup()

    def add(self, game_id, **kw):
        row = {"game_id": game_id, "season_id": 33, "league_id": 3,
               "level": "12U AA", "status": "final", "has_scoresheet": 1,
               "home_goals": 5, "away_goals": 2, "away_name": "Away",
               "home_name": "Home", "date_iso": "2026-09-04", "notified_at": None}
        row.update(kw)
        db.upsert(self.conn, "games", row, keys=["game_id"])
        self.conn.commit()

    def pipe(self):
        return pipeline.Pipeline(self.conn, self.config, Fetcher(self.config.base_url, offline=True))


class TestSelection(NotifyTestCase):
    def test_announces_only_eligible_12u_norcal_finals(self):
        self.add(1)                                   # eligible
        self.add(2, level="14U A")                    # not 12U
        self.add(3, league_id=5)                      # not Norcal
        self.add(4, has_scoresheet=0)                 # no scoresheet to link
        self.add(5, status="scheduled",
                 home_goals=None, away_goals=None)    # not final
        self.add(6, notified_at="2026-09-04T00:00:00+00:00")  # already announced

        sent = self.pipe().notify_ready_games()
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.sent), 1)
        # and it stamped the one it sent
        self.assertIsNotNone(self.conn.execute(
            "SELECT notified_at FROM games WHERE game_id = 1").fetchone()["notified_at"])

    def test_message_is_teams_and_score_linked_to_the_scoresheet(self):
        self.add(58961, away_name="Cupertino Cougars 12-1",
                 home_name="Santa Clara Blackhawks 12-1", away_goals=3, home_goals=4)
        self.pipe().notify_ready_games()
        _, chat_id, text = self.sent[0]
        self.assertEqual(chat_id, "-100999")
        self.assertIn("oss-scoresheet?game_id=58961", text)
        self.assertIn("Cupertino Cougars 12-1 [3–4] Santa Clara Blackhawks 12-1", text)
        self.assertTrue(text.startswith("<a href="))

    def test_never_announces_the_same_game_twice(self):
        self.add(1)
        self.assertEqual(self.pipe().notify_ready_games(), 1)
        self.assertEqual(self.pipe().notify_ready_games(), 0)  # second run: nothing new
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_send_leaves_the_game_for_next_run(self):
        self.add(1)
        notify.send_message = lambda *a, **k: False        # channel unreachable
        self.assertEqual(self.pipe().notify_ready_games(), 0)
        self.assertIsNone(self.conn.execute(
            "SELECT notified_at FROM games WHERE game_id = 1").fetchone()["notified_at"])


class TestDisabled(NotifyTestCase):
    def test_no_credentials_is_a_silent_no_op(self):
        self.config.telegram_bot_token = None
        self.add(1)
        self.assertEqual(self.pipe().notify_ready_games(), 0)
        self.assertEqual(self.sent, [])


class TestLink(unittest.TestCase):
    def test_escapes_text_and_url(self):
        out = notify.link("A & B <x>", "https://h/x?a=1&b=2")
        self.assertIn("A &amp; B &lt;x&gt;", out)
        self.assertIn("a=1&amp;b=2", out)
        self.assertTrue(out.startswith('<a href="https://h/x?a=1&amp;b=2">'))


if __name__ == "__main__":
    unittest.main()
