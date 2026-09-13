"""The Telegram log/alert channel: which review questions it pushes, the error
and stopped-early footers, dedupe, the digest cap, and silence on a clean run.

No network: the sender is monkeypatched to record calls.
"""

import pathlib
import tempfile
import unittest

from norcalstats import db, notify, pipeline
from norcalstats.config import Config
from norcalstats.fetch import Fetcher


class LogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config = Config(data_dir=base, export_dir=base, keep_raw=False,
                             telegram_bot_token="TOKEN",
                             telegram_log_chat_id="-100log")
        self.conn = db.connect(self.config.db_path)
        self.sent: list[tuple] = []
        self._orig = notify.send_message
        notify.send_message = lambda token, chat_id, text, **kw: (
            self.sent.append((token, chat_id, text)) or True)

    def tearDown(self) -> None:
        notify.send_message = self._orig
        self.conn.close()
        self.tmp.cleanup()

    def add_item(self, fp, *, kind="new_league", status="open",
                 subject="something", notified_at=None):
        self.conn.execute(
            "INSERT INTO review_items(fingerprint, kind, status, subject, "
            "notified_at, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (fp, kind, status, subject, notified_at, "t", "t"))
        self.conn.commit()

    def pipe(self):
        return pipeline.Pipeline(self.conn, self.config,
                                 Fetcher(self.config.base_url, offline=True))


class TestSelection(LogTestCase):
    def test_pushes_only_new_open_items_and_stamps_them(self):
        self.add_item("a", kind="new_league", subject="league 44 (Foo Cup)")
        self.add_item("b", kind="ambiguous_team", subject="Alex Chen 12-1 vs 12-2")
        self.add_item("c", status="resolved", subject="already answered")
        self.add_item("d", status="stale", subject="no longer applies")
        self.add_item("e", notified_at="2026-01-01", subject="already pushed")

        n = self.pipe().notify_review_log(context="nightly")
        self.assertEqual(n, 2)
        self.assertEqual(len(self.sent), 1)              # one digest, not per-item
        _, chat_id, text = self.sent[0]
        self.assertEqual(chat_id, "-100log")
        self.assertIn("2 new issue(s)", text)
        self.assertIn("league 44 (Foo Cup)", text)
        self.assertIn("Alex Chen 12-1 vs 12-2", text)
        self.assertNotIn("already answered", text)
        # both open items are now stamped
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE status='open' "
            "AND notified_at IS NULL").fetchone()[0], 0)

    def test_never_pushes_the_same_item_twice(self):
        self.add_item("a", subject="one")
        self.assertEqual(self.pipe().notify_review_log(context="nightly"), 1)
        self.assertEqual(self.pipe().notify_review_log(context="nightly"), 0)
        self.assertEqual(len(self.sent), 1)

    def test_caps_the_list_but_stamps_all(self):
        for i in range(20):
            self.add_item(f"f{i}", subject=f"item {i}")
        n = self.pipe().notify_review_log(context="nightly")
        self.assertEqual(n, 20)
        text = self.sent[0][2]
        self.assertIn("+5 more", text)                    # 20 - 15 cap
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE notified_at IS NULL"
            ).fetchone()[0], 0)


class TestFooters(LogTestCase):
    def test_errors_alone_still_send_with_no_items(self):
        n = self.pipe().notify_review_log(context="sweep", errors=3)
        self.assertEqual(n, 0)                            # no items
        self.assertEqual(len(self.sent), 1)               # but an alert went
        self.assertIn("3 error(s)", self.sent[0][2])

    def test_stopped_early_is_flagged(self):
        self.pipe().notify_review_log(context="nightly", stopped_early=True)
        self.assertIn("stopped early", self.sent[0][2])

    def test_clean_run_is_silent(self):
        n = self.pipe().notify_review_log(context="nightly")
        self.assertEqual(n, 0)
        self.assertEqual(self.sent, [])                   # nothing to report


class TestDisabled(LogTestCase):
    def test_no_log_channel_is_a_no_op(self):
        self.config.telegram_log_chat_id = None
        self.add_item("a", subject="one")
        self.assertEqual(self.pipe().notify_review_log(context="nightly"), 0)
        self.assertEqual(self.sent, [])

    def test_results_channel_alone_does_not_enable_the_log(self):
        # Only the results chat id is set, not the log one.
        self.config.telegram_log_chat_id = None
        self.config.telegram_chat_id = "-100results"
        self.add_item("a", subject="one")
        self.assertEqual(self.pipe().notify_review_log(context="nightly"), 0)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
