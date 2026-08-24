"""The viewer is one file with no build step, so the few things about it that
could quietly become untrue are checked here instead."""

import datetime
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
VIEWER = REPO / "norcal_hockey_viewer.html"


class TestViewerVersion(unittest.TestCase):
    """The version is hand-maintained, which is the only thing it can be.

    There is no build step to stamp it -- that is a deliberate property of a
    single self-contained file, not an oversight -- so nothing stops an edit
    landing without a bump, and a version that lies is worse than no version at
    all. This is what stops it.
    """

    def version(self) -> str:
        text = VIEWER.read_text(encoding="utf-8")
        match = re.search(r"var VIEWER_VERSION = '([0-9.]+)';", text)
        self.assertIsNotNone(match, "VIEWER_VERSION is missing from the viewer")
        return match.group(1)

    def test_it_is_a_calendar_date(self):
        # The version *is* the release date, so there are not two facts that can
        # disagree with each other.
        version = self.version()
        self.assertRegex(version, r"^\d{4}\.\d{2}\.\d{2}$")
        datetime.date.fromisoformat(version.replace(".", "-"))

    def test_it_is_not_older_than_the_last_edit(self):
        """An edit that forgets the bump fails here on the next run."""
        try:
            out = subprocess.run(
                ["git", "log", "-1", "--format=%cd", "--date=short", "--", VIEWER.name],
                cwd=REPO, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git is not available")
        if out.returncode != 0 or not out.stdout.strip():
            self.skipTest("no git history for the viewer")

        committed = datetime.date.fromisoformat(out.stdout.strip())
        declared = datetime.date.fromisoformat(self.version().replace(".", "-"))
        self.assertGreaterEqual(
            declared, committed,
            f"the viewer was last committed {committed} but calls itself "
            f"{declared}. Bump VIEWER_VERSION to the date of the change.")

    def test_the_footer_reports_both_dates(self):
        # Two different facts, and conflating them is the reason the footer
        # exists: when the data was collected, and when the page was written.
        text = VIEWER.read_text(encoding="utf-8")
        self.assertIn('id="foot-data"', text)
        self.assertIn('id="foot-viewer"', text)
        self.assertIn("renderFooter(core.metadata && core.metadata.generated)", text)
        self.assertIn("renderFooter(null)", text)


if __name__ == "__main__":
    unittest.main()
