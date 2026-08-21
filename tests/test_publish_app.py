"""Publishing the app dataset to a branch that holds nothing else.

The dataset is 38 files rebuilt every night. Committing it beside the code
would grow the history by megabytes a night, so it goes to its own branch as a
single parentless commit that is replaced each time -- and it has to do that
without disturbing whoever is working in the repository at the time.
"""

import json
import pathlib
import subprocess
import tempfile
import unittest

from norcalstats import publish


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()


class AppPublishTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.remote = base / "remote.git"
        self.repo = base / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / ".gitignore").write_text("data/\n", encoding="utf-8")
        (self.repo / "code.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore", "code.py")
        git(self.repo, "commit", "-qm", "code")
        git(self.repo, "push", "-q", "origin", "main")

        self.app = self.repo / "data" / "app"
        (self.app / "logs").mkdir(parents=True)
        self.write_dataset(players=100)

    def tearDown(self):
        self.tmp.cleanup()

    def write_dataset(self, *, players):
        (self.app / "core.json").write_text(
            json.dumps({"metadata": {"counts": {"players": players}}}), encoding="utf-8")
        (self.app / "logs" / "p00.json").write_text('{"players":{}}', encoding="utf-8")

    def publish(self, **kw):
        kw.setdefault("message", "app dataset")
        return publish.publish_app_dataset(self.repo, self.app, **kw)


class TestPublishing(AppPublishTestCase):
    def test_the_dataset_lands_at_the_branch_root(self):
        # The app fetches "core.json", not "data/app/core.json".
        self.publish()
        listed = git(self.repo, "ls-tree", "-r", "--name-only", "origin/data")
        self.assertEqual(sorted(listed.split()), ["core.json", "logs/p00.json"])

    def test_the_branch_never_grows(self):
        for n in range(4):
            self.write_dataset(players=100 + n)
            self.publish()
        self.assertEqual(git(self.repo, "rev-list", "--count", "origin/data"), "1")

    def test_an_unchanged_dataset_publishes_nothing(self):
        self.assertIsNotNone(self.publish())
        self.assertIsNone(self.publish())

    def test_the_code_branch_is_untouched(self):
        before = git(self.repo, "rev-parse", "main")
        self.publish()
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")


class TestItLeavesTheRepositoryAlone(AppPublishTestCase):
    def test_it_does_not_disturb_work_in_progress(self):
        # The nightly run can fire while somebody is midway through an edit, so
        # it must not touch the working tree or the index.
        (self.repo / "code.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "staged.py").write_text("y = 1\n", encoding="utf-8")
        git(self.repo, "add", "staged.py")
        before = git(self.repo, "status", "--porcelain")

        self.publish()

        self.assertEqual(git(self.repo, "status", "--porcelain"), before)
        self.assertEqual((self.repo / "code.py").read_text(encoding="utf-8"), "x = 2\n")

    def test_nothing_outside_the_dataset_is_published(self):
        (self.repo / "secret.txt").write_text("not for publishing", encoding="utf-8")
        self.publish()
        listed = git(self.repo, "ls-tree", "-r", "--name-only", "origin/data")
        self.assertNotIn("secret.txt", listed)
        self.assertNotIn("code.py", listed)


class TestRefusals(AppPublishTestCase):
    def test_a_missing_dataset_is_refused(self):
        with self.assertRaises(publish.PublishError):
            publish.publish_app_dataset(
                self.repo, self.repo / "data" / "nope", message="x")

    def test_a_dataset_without_core_is_refused(self):
        (self.app / "core.json").unlink()
        with self.assertRaises(publish.PublishError):
            self.publish()

    def test_a_dry_run_pushes_nothing(self):
        self.assertIsNone(self.publish(dry_run=True))
        result = subprocess.run(["git", "rev-parse", "--verify", "refs/heads/data"],
                                cwd=str(self.remote), capture_output=True)
        self.assertNotEqual(result.returncode, 0, "dry run created the branch")


if __name__ == "__main__":
    unittest.main()
