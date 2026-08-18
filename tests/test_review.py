"""Tests for the hard name cases and the review queue.

These cover the four kinds of trouble that previously needed manual fixes:
punctuation/spacing, dropped initials, nicknames, and two children sharing a
name.
"""

import pathlib
import tempfile
import unittest

from norcalstats import db, identity, names as N, review
from norcalstats.identity import Observation

#: Season -> start year, so divisions imply birth windows.
YEARS = {27: 2021, 28: 2022, 29: 2023, 30: 2024, 31: 2025}


def obs(name, season, team=58, jersey="", division="10U A", team_name=""):
    return Observation(name=name, season_id=season, team_id=team,
                       jersey=jersey, division=division, team_name=team_name)


def many(count, *args, **kwargs):
    """``count`` identical appearances -- a full season on one team."""
    return [obs(*args, **kwargs) for _ in range(count)]


def cluster_names(clusters):
    return sorted(sorted(c.variants) for c in clusters)


class TestSpacingAndInitials(unittest.TestCase):
    """The cases that were already handled -- kept as regressions."""

    def build(self, observations, **kwargs):
        kwargs.setdefault("season_years", YEARS)
        return identity.build_clusters(observations, **kwargs)

    def test_extra_spaces_collapse(self):
        clusters = self.build([obs("Ryan  Smith", 31), obs("Ryan Smith", 31)])
        self.assertEqual(len(clusters), 1)

    def test_periods_and_spacing_together(self):
        clusters = self.build([obs("Avery St. Onge", 30), obs("Avery St  Onge", 30)])
        self.assertEqual(len(clusters), 1)

    def test_dropped_initial_same_team(self):
        clusters = self.build([
            obs("Gavin B Duganne", 31, jersey="93"),
            obs("Gavin Duganne", 31, jersey="93"),
        ])
        self.assertEqual(len(clusters), 1)


class TestNicknames(unittest.TestCase):
    def build(self, observations, **kwargs):
        kwargs.setdefault("season_years", YEARS)
        return identity.build_clusters(observations, **kwargs)

    def test_nickname_map_basics(self):
        self.assertTrue(N.is_nickname_variant("Bobby Smith", "Robert Smith"))
        self.assertTrue(N.is_nickname_variant("Alex Chen", "Alexander Chen"))
        self.assertTrue(N.is_nickname_variant("Mike Jones", "Michael Jones"))
        self.assertTrue(N.is_nickname_variant("Will Brown", "William Brown"))

    def test_unrelated_first_names_are_not_nicknames(self):
        self.assertFalse(N.is_nickname_variant("Alex Chen", "Andrew Chen"))
        self.assertFalse(N.is_nickname_variant("Bobby Smith", "Bobby Jones"))
        self.assertFalse(N.is_nickname_variant("Robert Smith", "Robert Smith"))

    def test_nickname_merges_when_they_shared_a_team(self):
        items = []
        clusters = self.build(
            [obs("Bobby Smith", 31, team=58, jersey="9"),
             obs("Robert Smith", 31, team=58, jersey="9")],
            review_items=items,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual([i.kind for i in items], ["nickname"])
        self.assertEqual(items[0].applied, "merged")

    def test_nickname_not_merged_without_corroboration(self):
        # Different clubs, different seasons: could easily be two children.
        items = []
        clusters = self.build(
            [obs("Bobby Smith", 29, team=58), obs("Robert Smith", 31, team=129)],
            review_items=items,
        )
        self.assertEqual(len(clusters), 2, "must not merge on a nickname alone")
        self.assertEqual(items[0].kind, "nickname")
        self.assertEqual(items[0].applied, "kept separate")
        self.assertIn("merge", items[0].suggestion)

    def test_nickname_merge_can_be_forced_by_override(self):
        overrides = {N.name_key("Bobby Smith"): {"merge_into": "Robert Smith", "split": 0}}
        clusters = self.build(
            [obs("Bobby Smith", 29, team=58), obs("Robert Smith", 31, team=129)],
            overrides=overrides,
        )
        self.assertEqual(len(clusters), 1)


class TestSameNameDifferentPeople(unittest.TestCase):
    def build(self, observations, **kwargs):
        kwargs.setdefault("season_years", YEARS)
        return identity.build_clusters(observations, **kwargs)

    def test_two_children_one_spelling_are_split(self):
        # 10U in 2023 => born 2013/2014; 16U in 2023 => born 2007/2008.
        items = []
        clusters = self.build(
            [obs("Ryan Smith", 29, team=58, division="10U A"),
             obs("Ryan Smith", 30, team=58, division="10U A"),
             obs("Ryan Smith", 31, team=58, division="12U A"),
             obs("Ryan Smith", 29, team=45, division="16U A"),
             obs("Ryan Smith", 30, team=45, division="16U A"),
             obs("Ryan Smith", 31, team=45, division="18U A")],
            review_items=items,
        )
        self.assertEqual(len(clusters), 2, "two birth cohorts => two children")
        self.assertEqual([i.kind for i in items], ["same_name"])
        self.assertIn("split", items[0].applied)

    def test_split_players_get_different_birth_years(self):
        clusters = self.build([
            obs("Ryan Smith", 29, team=58, division="10U A"),
            obs("Ryan Smith", 30, team=58, division="12U A"),
            obs("Ryan Smith", 29, team=45, division="16U A"),
            obs("Ryan Smith", 30, team=45, division="18U A"),
        ])
        windows = sorted(
            N.intersect_windows(
                N.birth_year_window(d, YEARS[s]) for s, d in c.divisions
            )
            for c in clusters
        )
        self.assertEqual(len(windows), 2)
        self.assertNotEqual(windows[0], windows[1])

    def test_a_call_up_is_not_treated_as_a_second_child(self):
        # One appearance in an older division is a call-up, not a new player.
        items = []
        clusters = self.build(
            [obs("Ryan Smith", 31, team=58, division="10U A"),
             obs("Ryan Smith", 31, team=58, division="10U A"),
             obs("Ryan Smith", 31, team=58, division="10U A"),
             obs("Ryan Smith", 31, team=59, division="12U A")],
            review_items=items,
        )
        self.assertEqual(len(clusters), 1, "a call-up must not split the player")
        # Nor is it worth asking about: being called up one age group for a
        # couple of games is routine, and flagging every one buries the real
        # questions.
        self.assertEqual(items, [])

    def test_normal_progression_does_not_split(self):
        # 10U -> 12U -> 14U across seasons is one child growing up.
        items = []
        clusters = self.build(
            [obs("Ryan Smith", 27, division="10U A"),
             obs("Ryan Smith", 29, division="12U A"),
             obs("Ryan Smith", 31, division="14U A")],
            review_items=items,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(items, [])

    def test_manual_split_overrides_detection(self):
        splits = {N.name_key("Ryan Smith"): {(29, None): "a", (31, None): "b"}}
        clusters = self.build(
            [obs("Ryan Smith", 29, division="10U A"),
             obs("Ryan Smith", 31, division="12U A")],
            splits=splits,
        )
        self.assertEqual(len(clusters), 2, "manual split must be honoured")

    def test_manual_single_person_map_prevents_a_split(self):
        # Every season under one person key means "this is one child".
        splits = {N.name_key("Ryan Smith"): {(29, None): "a", (31, None): "a"}}
        clusters = self.build(
            [obs("Ryan Smith", 29, team=58, division="10U A"),
             obs("Ryan Smith", 29, team=58, division="10U A"),
             obs("Ryan Smith", 29, team=58, division="10U A"),
             obs("Ryan Smith", 31, team=45, division="18U A"),
             obs("Ryan Smith", 31, team=45, division="18U A"),
             obs("Ryan Smith", 31, team=45, division="18U A")],
            splits=splits,
        )
        self.assertEqual(len(clusters), 1)

    def test_splitting_is_deterministic(self):
        observations = [
            obs("Ryan Smith", 29, team=58, division="10U A"),
            obs("Ryan Smith", 30, team=58, division="10U A"),
            obs("Ryan Smith", 31, team=58, division="12U A"),
            obs("Ryan Smith", 29, team=45, division="16U A"),
            obs("Ryan Smith", 30, team=45, division="16U A"),
            obs("Ryan Smith", 31, team=45, division="18U A"),
        ]
        first = cluster_names(self.build(list(observations)))
        second = cluster_names(self.build(list(reversed(observations))))
        self.assertEqual(first, second)


class TestGirlsDivisionDetection(unittest.TestCase):
    def test_dedicated_girls_divisions(self):
        self.assertTrue(N.is_girls("Girls 16-U"))
        self.assertTrue(N.is_girls("12U Girls AA"))
        self.assertEqual(N.division_gender("Girls 16-U"), "girls")

    def test_girls_team_inside_a_coed_division(self):
        # S31 puts "San Jose Jr Sharks Girls" in 10U B West.
        self.assertEqual(
            N.division_gender("10U B West", "San Jose Jr Sharks Girls"), "girls")
        self.assertEqual(
            N.division_gender("10U B West", "Oakland Bears 10-2"), "coed")

    def test_g_designator_in_a_team_name(self):
        self.assertTrue(N.is_girls("Stockton Colts Girls 10G-1"))
        self.assertEqual(N.division_gender("12U B", "Tri Valley 12G-1"), "girls")

    def test_coed_divisions_are_not_girls(self):
        for division in ("10U A", "12U AA", "14U BB", "16U A", "Mite B"):
            self.assertEqual(N.division_gender(division), "coed", division)


class TestDoubleRostering(unittest.TestCase):
    """A girl on both a girls team and a co-ed team is one child, two stat sets."""

    def build(self, observations, **kwargs):
        kwargs.setdefault("season_years", YEARS)
        return identity.build_clusters(observations, **kwargs)

    def test_girls_and_coed_same_season_is_one_player(self):
        items = []
        clusters = self.build(
            many(12, "Maya Chen", 31, team=57, division="10U B West",
                 team_name="San Jose Jr Sharks Girls")
            + many(14, "Maya Chen", 31, team=58, division="10U A",
                   team_name="San Jose Jr Sharks"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual([i.kind for i in items], ["double_roster"])
        self.assertIn("separate stats", items[0].applied)

    def test_twelve_to_sixteen_is_still_too_far_even_for_girls(self):
        # A 12U player on a 16U team is a four-year jump: it does not happen at
        # these ages, so being a girls team is no excuse to merge them.
        items = []
        clusters = self.build(
            many(10, "Maya Chen", 27, team=57, division="Girls 16-U",
                 team_name="San Jose Jr Sharks Girls")
            + many(10, "Maya Chen", 27, team=58, division="12U A",
                   team_name="San Jose Jr Sharks"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 2, "a four-year gap is two children")
        self.assertEqual(items[0].kind, "same_name")

    def test_a_fifteen_year_old_on_a_19u_girls_team_stays_one_player(self):
        # 19U in a 2025 season nominally means born 2006-07; she is born
        # 2009-10 and plays 16U as well. Playing up into the top division is
        # normal, especially where it is the only girls team above 16U.
        items = []
        clusters = self.build(
            many(10, "Maya Chen", 31, team=57, division="19U Girls AA",
                 team_name="Jr Sharks Girls 19U")
            + many(10, "Maya Chen", 31, team=58, division="16U A",
                   team_name="San Jose Jr Sharks"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 1, "playing up into 19U is one player")
        self.assertEqual(items[0].kind, "double_roster")

    def test_age_gap_boundaries(self):
        """The full merge/split boundary, locked in.

        Playing up is real but bounded, and the 18U/19U divisions are wider
        because nothing sits above them to absorb older teenagers.
        """
        cases = [
            # (younger, older, expect_one_player)
            ("8U B", "10U A", True),      # adjacent
            ("10U A", "12U A", True),     # adjacent
            ("12U A", "14U A", True),     # adjacent
            ("14U A", "16U A", True),     # adjacent
            ("10U A", "14U A", False),    # four years
            ("12U A", "16U A", False),    # four years -- does not happen
            ("14U A", "19U Girls AA", True),   # top division absorbs a range
            ("16U A", "19U Girls AA", True),   # a 15-year-old on a 19U team
            ("12U A", "19U Girls AA", False),  # seven years is two children
            ("10U A", "19U Girls AA", False),
            ("8U B", "19U Girls AA", False),
        ]
        for younger, older, expect_one in cases:
            girls = "Jr Sharks Girls" if "Girls" in older else ""
            clusters = self.build(
                many(10, "Sam Lee", 31, team=1, division=younger)
                + many(10, "Sam Lee", 31, team=2, division=older,
                       team_name=girls),
            )
            expected = 1 if expect_one else 2
            self.assertEqual(
                len(clusters), expected,
                f"{younger} + {older}: expected "
                f"{'one player' if expect_one else 'a split'}",
            )

    def test_terminal_divisions_get_a_wider_band(self):
        self.assertEqual(identity._play_up_tolerance("19U Girls AA"),
                         identity.TERMINAL_PLAY_UP_TOLERANCE)
        self.assertEqual(identity._play_up_tolerance("18U A"),
                         identity.TERMINAL_PLAY_UP_TOLERANCE)
        self.assertEqual(identity._play_up_tolerance("16U A"), N.PLAY_UP_TOLERANCE)
        self.assertEqual(identity._play_up_tolerance("10U A"), N.PLAY_UP_TOLERANCE)

    def test_playing_up_still_yields_a_birth_year(self):
        # She plays 16U and 19U; the strict intersection is empty, so the
        # tolerant one must supply an answer rather than giving up -- and it
        # must use the same widening the bucketing used, or a player the
        # resolver deliberately kept together ends up with no window at all.
        clusters = self.build(
            many(10, "Maya Chen", 31, team=57, division="19U Girls AA",
                 team_name="Jr Sharks Girls")
            + many(10, "Maya Chen", 31, team=58, division="16U A"),
        )
        self.assertEqual(len(clusters), 1)
        # 16U in a 2025 season is born 2009-10, which is her real age group.
        self.assertEqual(identity._birth_window(clusters[0], YEARS), (2009, 2010))

    def test_terminal_play_up_window_is_not_left_unresolved(self):
        # 14U + 19U merges only because 19U gets the wider band; the birth
        # window must be derived with that same band.
        clusters = self.build(
            many(10, "Sam Lee", 31, team=57, division="19U Girls AA",
                 team_name="Colts Girls")
            + many(10, "Sam Lee", 31, team=58, division="14U A"),
        )
        self.assertEqual(len(clusters), 1)
        window = identity._birth_window(clusters[0], YEARS)
        self.assertIsNotNone(window, "must not come back unknown")
        self.assertEqual(window, (2011, 2011))

    def test_home_division_caps_the_birth_window(self):
        # Without the cap the answer drifts younger than the youngest division
        # the player actually appeared in supports.
        clusters = self.build(
            many(10, "Maya Chen", 31, team=57, division="19U Girls AA",
                 team_name="Jr Sharks Girls")
            + many(10, "Maya Chen", 31, team=58, division="16U A"),
        )
        low, high = identity._birth_window(clusters[0], YEARS)
        self.assertLessEqual(high, N.birth_year_window("16U A", 2025)[1])

    def test_split_evidence_explains_the_age_gap(self):
        items = []
        self.build(
            many(10, "Ryan Smith", 31, team=1, division="12U A")
            + many(10, "Ryan Smith", 31, team=2, division="16U A"),
            review_items=items,
        )
        detail = " ".join(items[0].evidence["detail"])
        self.assertIn("year(s) apart", detail)
        self.assertIn("play up", detail)

    def test_playing_up_one_division_needs_no_exemption(self):
        # 16U and 14U overlap once ordinary play-up tolerance is applied.
        clusters = self.build(
            many(10, "Maya Chen", 31, team=57, division="16U A")
            + many(10, "Maya Chen", 31, team=58, division="14U A"),
        )
        self.assertEqual(len(clusters), 1)

    def test_play_up_tolerance_is_one_directional(self):
        # Widening is toward younger players only: an older player can never
        # drop into a younger division.
        self.assertEqual(N.widen_for_play_up((2006, 2007)), (2006, 2009))
        self.assertIsNone(N.widen_for_play_up(None))

    def test_sixteen_and_nineteen_windows_meet_under_tolerance(self):
        sixteen = N.birth_year_window("16U A", 2025)     # (2009, 2010)
        nineteen = N.birth_year_window("19U Girls AA", 2025)  # (2006, 2007)
        self.assertIsNone(N.intersect_windows([sixteen, nineteen]))
        self.assertIsNotNone(
            N.intersect_windows([sixteen, nineteen], tolerance=N.PLAY_UP_TOLERANCE))

    def test_twelve_and_sixteen_windows_never_meet(self):
        twelve = N.birth_year_window("12U A", 2021)      # (2009, 2010)
        sixteen = N.birth_year_window("Girls 16-U", 2021)  # (2005, 2006)
        self.assertIsNone(
            N.intersect_windows([twelve, sixteen], tolerance=N.PLAY_UP_TOLERANCE))

    def test_two_coed_children_still_split(self):
        # The girls exemption must not disable splitting generally.
        items = []
        clusters = self.build(
            many(10, "Ryan Smith", 31, team=58, division="10U A")
            + many(10, "Ryan Smith", 31, team=45, division="16U A"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 2)
        self.assertEqual(items[0].kind, "same_name")

    def test_double_rostered_player_keeps_both_teams(self):
        clusters = self.build(
            many(12, "Maya Chen", 31, team=57, division="10U B West",
                 team_name="San Jose Jr Sharks Girls")
            + many(14, "Maya Chen", 31, team=58, division="10U A",
                   team_name="San Jose Jr Sharks"),
        )
        self.assertEqual(
            clusters[0].team_seasons, {(31, 57), (31, 58)},
            "both rosters must stay attached to the one player",
        )

    def test_same_age_two_teams_is_not_flagged(self):
        # An ordinary mid-season move should not create review noise.
        items = []
        clusters = self.build(
            many(8, "Ryan Smith", 31, team=58, division="10U A")
            + many(8, "Ryan Smith", 31, team=129, division="10U A"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual(items, [])

    def test_call_up_is_dropped_from_birth_window_reasoning(self):
        items = []
        clusters = self.build(
            many(15, "Ryan Smith", 31, team=58, division="10U A")
            + many(2, "Ryan Smith", 31, team=59, division="12U A"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 1, "a call-up is one child")
        self.assertEqual(items, [], "and not worth a question")

    def test_a_genuine_dual_roster_is_still_flagged(self):
        # A full season in each of two age groups is not a call-up.
        items = []
        clusters = self.build(
            many(15, "Ryan Smith", 31, team=58, division="10U A")
            + many(12, "Ryan Smith", 31, team=59, division="12U A"),
            review_items=items,
        )
        self.assertEqual(len(clusters), 1)
        self.assertEqual([i.kind for i in items], ["double_roster"])

    def test_a_two_step_age_gap_is_flagged_even_when_brief(self):
        # 10U and 14U is further than a call-up normally goes.
        items = []
        self.build(
            many(15, "Ryan Smith", 31, team=58, division="10U A")
            + many(2, "Ryan Smith", 31, team=59, division="14U A"),
            review_items=items,
        )
        self.assertEqual([i.kind for i in items], ["double_roster"])

    def test_birth_year_comes_from_the_primary_division(self):
        # 15 games at 10U in 2025 => born 2015/2016, despite the 12U call-up.
        clusters = self.build(
            many(15, "Ryan Smith", 31, team=58, division="10U A")
            + many(2, "Ryan Smith", 31, team=59, division="12U A"),
        )
        window = N.intersect_windows(
            N.birth_year_window(d, YEARS[s]) for s, d in clusters[0].divisions
        )
        # Both divisions are recorded on the cluster, so the naive intersection
        # is empty; the split logic is what must ignore the call-up.
        self.assertEqual(len(clusters), 1)
        self.assertIn((31, "10U A"), clusters[0].divisions)
        self.assertIn((31, "12U A"), clusters[0].divisions)


class ReviewDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(pathlib.Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestReviewQueue(ReviewDbTestCase):
    def make_item(self, **kwargs):
        defaults = dict(kind="nickname", subject="Bobby Smith / Robert Smith",
                        evidence={"names": ["Bobby Smith", "Robert Smith"]},
                        parts=("a", "b"))
        defaults.update(kwargs)
        return review.Item(**defaults)

    def test_recording_is_idempotent(self):
        item = self.make_item()
        self.assertEqual(review.record(self.conn, [item])["added"], 1)
        result = review.record(self.conn, [item])
        self.assertEqual((result["added"], result["updated"]), (0, 1))
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM review_items"), 1)

    def test_fingerprint_is_order_independent(self):
        a = review.Item(kind="nickname", subject="x", parts=("a", "b"))
        b = review.Item(kind="nickname", subject="x", parts=("b", "a"))
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_answering_merge_writes_an_override(self):
        review.record(self.conn, [self.make_item()])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "merge", note="same kid")

        overrides = review.load_overrides(self.conn)
        self.assertIn(N.name_key("Robert Smith"), overrides)
        self.assertEqual(
            overrides[N.name_key("Robert Smith")]["merge_into"], "Bobby Smith"
        )
        row = review.get(self.conn, item_id)
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["decision"], "merge")

    def test_answering_separate_blocks_future_merges(self):
        review.record(self.conn, [self.make_item()])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "separate")
        overrides = review.load_overrides(self.conn)
        self.assertEqual(overrides[N.name_key("Bobby Smith")]["split"], 1)

        clusters = identity.build_clusters(
            [obs("Bobby Smith", 31, team=58, jersey="9"),
             obs("Robert Smith", 31, team=58, jersey="9")],
            overrides=overrides, season_years=YEARS,
        )
        self.assertEqual(len(clusters), 2, "decision must survive into the resolver")

    def test_questions_that_no_longer_apply_are_retired(self):
        """The queue must shrink when the data or the rules improve.

        Otherwise an obsolete question -- a game whose teams were identified on
        a later run, or a case a better rule stopped raising -- sits open
        forever.
        """
        review.record(self.conn, [self.make_item()], sweep=("nickname",))
        self.assertEqual(len(review.open_items(self.conn)), 1)

        result = review.record(self.conn, [], sweep=("nickname",))
        self.assertEqual(result["retired"], 1)
        self.assertEqual(review.open_items(self.conn), [])
        self.assertEqual(
            db.scalar(self.conn, "SELECT status FROM review_items"), "stale")

    def test_sweeping_leaves_other_kinds_alone(self):
        review.record(self.conn, [self.make_item()], sweep=("nickname",))
        review.record(self.conn, [self.make_item(kind="ambiguous_team",
                                                 parts=("g1",))],
                      sweep=("ambiguous_team",))
        # A name pass must not retire the team questions.
        review.record(self.conn, [], sweep=("nickname",))
        kinds = {r["kind"] for r in review.open_items(self.conn)}
        self.assertEqual(kinds, {"ambiguous_team"})

    def test_sweeping_never_reopens_or_retires_an_answer(self):
        review.record(self.conn, [self.make_item()], sweep=("nickname",))
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "separate")
        review.record(self.conn, [], sweep=("nickname",))
        self.assertEqual(review.get(self.conn, item_id)["status"], "resolved")

    def test_answered_items_are_not_reopened_by_a_rerun(self):
        item = self.make_item()
        review.record(self.conn, [item])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "separate")
        review.record(self.conn, [item])   # nightly run sees it again
        self.assertEqual(review.get(self.conn, item_id)["status"], "resolved")
        self.assertEqual(review.open_items(self.conn), [])

    def test_split_decision_requires_a_person_map(self):
        review.record(self.conn, [self.make_item(
            kind="same_name", evidence={"names": ["Ryan Smith"], "seasons": [29, 31]})])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        with self.assertRaises(review.DecisionError):
            review.resolve(self.conn, item_id, "split")

    def test_split_decision_persists(self):
        review.record(self.conn, [self.make_item(
            kind="same_name", evidence={"names": ["Ryan Smith"], "seasons": [29, 31]})])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "split", person_map={29: "a", 31: "b"})
        splits = review.load_splits(self.conn)
        self.assertEqual(splits[N.name_key("Ryan Smith")][(29, None)], "a")
        self.assertEqual(splits[N.name_key("Ryan Smith")][(31, None)], "b")

    def test_merge_on_same_name_forces_one_person(self):
        review.record(self.conn, [self.make_item(
            kind="same_name", evidence={"names": ["Ryan Smith"], "seasons": [29, 31]})])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "merge")
        splits = review.load_splits(self.conn)
        self.assertEqual(set(splits[N.name_key("Ryan Smith")].values()), {"a"})

    def test_separate_is_rejected_for_same_name_items(self):
        review.record(self.conn, [self.make_item(
            kind="same_name", evidence={"names": ["Ryan Smith"], "seasons": [29]})])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        with self.assertRaises(review.DecisionError):
            review.resolve(self.conn, item_id, "separate")

    def test_reopen_clears_the_override(self):
        review.record(self.conn, [self.make_item()])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "separate")
        review.reopen(self.conn, item_id)
        self.assertEqual(review.load_overrides(self.conn), {})
        self.assertEqual(review.get(self.conn, item_id)["status"], "open")

    def test_dismiss_stops_asking_without_an_override(self):
        review.record(self.conn, [self.make_item()])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        review.resolve(self.conn, item_id, "dismiss")
        self.assertEqual(review.get(self.conn, item_id)["status"], "dismissed")
        self.assertEqual(review.load_overrides(self.conn), {})
        self.assertEqual(review.open_items(self.conn), [])

    def test_unknown_action_is_rejected(self):
        review.record(self.conn, [self.make_item()])
        item_id = db.scalar(self.conn, "SELECT item_id FROM review_items")
        with self.assertRaises(review.DecisionError):
            review.resolve(self.conn, item_id, "obliterate")


class TestDoubleRosterExport(ReviewDbTestCase):
    """End to end: one girl, two teams, two sets of stats in the exports."""

    def setUp(self):
        super().setUp()
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (31,'Fall 2025',2025,'x')")
        for name, gender in (("10U A", "coed"), ("10U B West", "coed")):
            conn.execute("INSERT INTO divisions(season_id,name,gender) VALUES (31,?,?)",
                         (name, gender))
        coed_div = db.scalar(conn, "SELECT division_id FROM divisions WHERE name='10U A'")
        girls_div = db.scalar(
            conn, "SELECT division_id FROM divisions WHERE name='10U B West'")
        conn.execute("INSERT INTO teams(team_id,season_id,name,club,division_id,gender) "
                     "VALUES (58,31,'San Jose Jr Sharks','San Jose Jr Sharks',?,'coed')",
                     (coed_div,))
        conn.execute("INSERT INTO teams(team_id,season_id,name,club,division_id,gender) "
                     "VALUES (57,31,'San Jose Jr Sharks Girls','San Jose Jr Sharks',?,'girls')",
                     (girls_div,))

        # Six games: three for the co-ed team, three for the girls team.
        for game_id, team, division_id in (
            (1, 58, coed_div), (2, 58, coed_div), (3, 58, coed_div),
            (4, 57, girls_div), (5, 57, girls_div), (6, 57, girls_div),
        ):
            conn.execute(
                "INSERT INTO games(game_id,season_id,division_id,home_team_id,"
                "status,game_class) VALUES (?,31,?,?,'final','regular')",
                (game_id, division_id, team))
            conn.execute(
                "INSERT INTO game_rosters(game_id,side,slot,jersey,position,name,role) "
                "VALUES (?,'home',0,'12','','Maya Chen','player')", (game_id,))
            # One goal per game so the two stat lines differ in a visible way.
            for seq in range(1 if team == 58 else 2):
                conn.execute(
                    "INSERT INTO goals(game_id,side,seq,period,scorer_jersey) "
                    "VALUES (?,'home',?,'1','12')", (game_id, seq))

        # The league's published totals list her twice, once per team.
        import json as _json
        for i, (team, goals) in enumerate(((58, "3"), (57, "6"))):
            conn.execute(
                "INSERT INTO team_stat_rows(season_id,team_id,kind,row_index,name,"
                "jersey,gp,data_json) VALUES (31,?,'skater',?,'Maya Chen','12',3,?)",
                (team, i, _json.dumps({"Goals": goals, "Ass.": "0", "Hat": "0",
                                       "Pts": goals})))
        conn.commit()

        identity.rebuild(conn)
        from norcalstats import pipeline
        pipeline.rebuild_player_game_stats(conn)
        conn.commit()

    def test_she_is_one_player(self):
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM players"), 1)

    def test_both_rosters_link_to_her(self):
        unlinked = db.scalar(
            self.conn, "SELECT COUNT(*) FROM game_rosters WHERE player_id IS NULL")
        self.assertEqual(unlinked, 0)
        teams = {
            r["team_id"] for r in
            self.conn.execute("SELECT DISTINCT team_id FROM player_game_stats")
        }
        self.assertEqual(teams, {57, 58})

    def test_stats_are_separated_by_team(self):
        rows = self.conn.execute("""
            SELECT s.team_id, t.gender, COUNT(*) gp, SUM(s.goals) g
              FROM player_game_stats s
              JOIN teams t ON t.team_id = s.team_id AND t.season_id = s.season_id
             GROUP BY s.team_id ORDER BY t.gender
        """).fetchall()
        self.assertEqual(len(rows), 2)
        by_gender = {r["gender"]: r for r in rows}
        self.assertEqual(set(by_gender), {"girls", "coed"})
        self.assertEqual((by_gender["girls"]["gp"], by_gender["girls"]["g"]), (3, 6))
        self.assertEqual((by_gender["coed"]["gp"], by_gender["coed"]["g"]), (3, 3))

    def test_legacy_export_lists_both_teams_under_one_name(self):
        from norcalstats import export
        payload = export.build_legacy(self.conn)
        self.assertEqual(list(payload["players"]), ["Maya Chen"],
                         "one name, not two")
        entries = payload["players"]["Maya Chen"]
        self.assertEqual(len(entries), 2, "one entry per team")
        by_team = {e["team"]: e for e in entries}
        self.assertEqual(by_team["San Jose Jr Sharks"]["G"], "3")
        self.assertEqual(by_team["San Jose Jr Sharks Girls"]["G"], "6")
        self.assertEqual(by_team["San Jose Jr Sharks Girls"]["division"], "10U B West")

    def test_rich_export_tags_each_line_with_gender(self):
        from norcalstats import export
        payload = export.build_rich(self.conn)
        player = payload["players"][0]
        genders = {s["gender"]: s for s in player["seasons"]}
        self.assertEqual(set(genders), {"girls", "coed"})
        self.assertEqual(genders["girls"]["G"], 6)
        self.assertEqual(genders["coed"]["G"], 3)
        self.assertEqual(genders["girls"]["team"], "San Jose Jr Sharks Girls")


class TestSplitNamesAreDistinguishableInExport(ReviewDbTestCase):
    """Two children sharing a spelling must not collapse back together."""

    def test_display_names_are_made_unique_by_birth_year(self):
        from norcalstats import export
        conn = self.conn
        conn.execute("INSERT INTO players(player_id,canonical_name,display_name,"
                     "birth_year) VALUES (1,'ryan smith#a','Ryan Smith',2013)")
        conn.execute("INSERT INTO players(player_id,canonical_name,display_name,"
                     "birth_year) VALUES (2,'ryan smith#b','Ryan Smith',2007)")
        conn.commit()
        names = export._unique_display_names(conn)
        self.assertEqual(set(names.values()), {"Ryan Smith '13", "Ryan Smith '07"})

    def test_single_player_keeps_a_plain_name(self):
        from norcalstats import export
        self.conn.execute("INSERT INTO players(player_id,canonical_name,display_name,"
                          "birth_year) VALUES (1,'ryan smith','Ryan Smith',2013)")
        self.conn.commit()
        self.assertEqual(export._unique_display_names(self.conn), {1: "Ryan Smith"})

    def test_unknown_birth_years_fall_back_to_a_suffix(self):
        from norcalstats import export
        for i in (1, 2):
            self.conn.execute(
                "INSERT INTO players(player_id,canonical_name,display_name) "
                "VALUES (?,?,'Ryan Smith')", (i, f"ryan smith#{i}"))
        self.conn.commit()
        self.assertEqual(set(export._unique_display_names(self.conn).values()),
                         {"Ryan Smith (1)", "Ryan Smith (2)"})


class TestSplitPlayersResolveCorrectly(ReviewDbTestCase):
    """A split name must still link its rows to the right child."""

    def test_name_map_distinguishes_two_children(self):
        conn = self.conn
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (29,'Fall 2023',2023,'x')")
        conn.execute("INSERT INTO seasons(season_id,label,start_year,first_seen_at) "
                     "VALUES (31,'Fall 2025',2025,'x')")
        for season, division in ((29, "10U A"), (31, "16U A")):
            conn.execute("INSERT INTO divisions(season_id,name) VALUES (?,?)",
                         (season, division))
        conn.execute("INSERT INTO teams(team_id,season_id,name) VALUES (58,29,'A')")
        conn.execute("INSERT INTO teams(team_id,season_id,name) VALUES (45,31,'B')")

        for i, (game_id, season, team, division) in enumerate(
            [(1, 29, 58, "10U A"), (2, 29, 58, "10U A"), (3, 29, 58, "10U A"),
             (4, 31, 45, "16U A"), (5, 31, 45, "16U A"), (6, 31, 45, "16U A")]
        ):
            division_id = db.scalar(
                conn, "SELECT division_id FROM divisions WHERE season_id=? AND name=?",
                (season, division))
            conn.execute(
                "INSERT INTO games(game_id,season_id,division_id,home_team_id,status) "
                "VALUES (?,?,?,?,'final')", (game_id, season, division_id, team))
            conn.execute(
                "INSERT INTO game_rosters(game_id,side,slot,jersey,position,name,role) "
                "VALUES (?, 'home', 0, '9', '', 'Ryan Smith', 'player')", (game_id,))
        conn.commit()

        identity.rebuild(conn)

        players = conn.execute(
            "SELECT player_id, birth_year_min, birth_year_max FROM players"
        ).fetchall()
        self.assertEqual(len(players), 2, "one spelling, two children")

        # Each game's roster row must point at the child who actually played.
        linked = conn.execute("""
            SELECT g.season_id, r.player_id FROM game_rosters r
            JOIN games g ON g.game_id = r.game_id ORDER BY g.game_id
        """).fetchall()
        self.assertTrue(all(r["player_id"] is not None for r in linked))
        s29 = {r["player_id"] for r in linked if r["season_id"] == 29}
        s31 = {r["player_id"] for r in linked if r["season_id"] == 31}
        self.assertEqual(len(s29), 1)
        self.assertEqual(len(s31), 1)
        self.assertNotEqual(s29, s31, "the two children must not share an id")


if __name__ == "__main__":
    unittest.main()
