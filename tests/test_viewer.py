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


class TestTheDivisionCollapse(unittest.TestCase):
    """The schedule filter treats a division's flights and conferences as one.

    Getting this wrong in the generous direction is the dangerous half: merging
    "10U B" with "10U BB" would silently mix two tiers, and a filter that
    answers with the wrong games is worse than one that answers with too few.
    The rule lives in the viewer, so it is read back out of the file rather
    than restated here, which is what makes this a test of the shipped rule.
    """

    def collapse(self):
        text = VIEWER.read_text(encoding="utf-8")
        match = re.search(r"var DIVISION_SUBSECTION = /(.+?)/;", text)
        self.assertIsNotNone(match, "DIVISION_SUBSECTION is missing from the viewer")
        # JS \s and character classes carry over; the pattern is anchored with $.
        pattern = re.compile(match.group(1).replace("\\s", r"\s"))

        def core(name):
            previous = None
            while previous != name:
                previous = name
                name = pattern.sub("", name)
            return name
        return core

    def test_a_sub_section_folds_into_its_division(self):
        core = self.collapse()
        for name, expected in (
            ("10U B East", "10U B"),
            ("10U B West", "10U B"),
            ("10U B II", "10U B"),
            ("12U AA Flight I", "12U AA"),
            ("14U B Flight II", "14U B"),
            ("10U Flight 2", "10U"),
            ("18U AA East", "18U AA"),
            ("High School 2B Pool A", "High School 2B"),
        ):
            self.assertEqual(core(name), expected, name)

    def test_a_tier_is_never_stripped(self):
        # The failure that would matter: two different competitions merged.
        core = self.collapse()
        for name in ("10U B", "10U BB", "10U A", "12U AAA", "13U AAA", "14U BB",
                     "16U AAA", "Girls 16/19AA", "Girls 12AAA", "Mite B",
                     "High School D2", "High School 1A", "HS Jr Varsity"):
            self.assertEqual(core(name), name, name)

    def test_the_tiers_that_look_alike_stay_apart(self):
        core = self.collapse()
        self.assertNotEqual(core("10U BB"), core("10U B"))
        self.assertNotEqual(core("12U AAA"), core("12U AA"))


if __name__ == "__main__":
    unittest.main()
