"""Tests for name handling and player-identity resolution."""

import unittest

from norcalstats import identity, names as N
from norcalstats.identity import Observation


class TestNameNormalization(unittest.TestCase):
    def test_all_caps_is_recased(self):
        self.assertEqual(N.clean_name("ALEKSANDR RAZZHIGAEV"), "Aleksandr Razzhigaev")

    def test_inner_capitals_preserved(self):
        self.assertEqual(N.clean_name("Avery McLeod"), "Avery McLeod")
        self.assertEqual(N.clean_name("Sean O'Brien"), "Sean O'Brien")

    def test_periods_stripped(self):
        self.assertEqual(N.clean_name("Avery St. Onge"), "Avery St Onge")

    def test_roman_numerals_uppercase(self):
        self.assertEqual(N.clean_name("carlos ayon iii"), "Carlos Ayon III")

    def test_hyphenated_names(self):
        self.assertEqual(N.clean_name("mary-jane smith-jones"), "Mary-Jane Smith-Jones")

    def test_keys(self):
        self.assertEqual(N.name_key("Avery St. Onge"), N.name_key("Avery St Onge"))
        self.assertEqual(N.core_key("Gavin B Duganne"), N.core_key("Gavin Duganne"))
        self.assertEqual(N.core_key("Shiv Ritesh Kadu"), N.core_key("Shiv Kadu"))
        self.assertEqual(N.base_key("Carlos Ayon II"), N.base_key("Carlos Ayon"))

    def test_accents_folded_for_comparison_only(self):
        self.assertEqual(N.name_key("José Núñez"), N.name_key("Jose Nunez"))
        self.assertEqual(N.clean_name("José Núñez"), "José Núñez")

    def test_initial_variant_detection(self):
        self.assertTrue(N.is_initial_variant("Gavin B Duganne", "Gavin Duganne"))
        self.assertTrue(N.is_initial_variant("Shiv Ritesh Kadu", "Shiv Kadu"))
        # Two different middle names of the same length are not variants.
        self.assertFalse(N.is_initial_variant("Gavin B Duganne", "Gavin C Duganne"))
        self.assertFalse(N.is_initial_variant("Max Duganne", "Nathan Duganne"))

    def test_best_display_prefers_frequency_then_case(self):
        self.assertEqual(
            N.best_display([("PARKER ANDERSON", 1), ("Parker Anderson", 5)]),
            "Parker Anderson",
        )
        # Equal counts: mixed case wins over shouting.
        self.assertEqual(
            N.best_display([("IAN SUN", 2), ("Ian Sun", 2)]), "Ian Sun",
        )


class TestClubExtraction(unittest.TestCase):
    def test_strips_team_designators(self):
        cases = {
            "Cupertino Cougars 10A": "Cupertino Cougars",
            "San Jose Jr Sharks 10-5": "San Jose Jr Sharks",
            "Tri Valley Blue Devils 10A-2": "Tri Valley Blue Devils",
            "Reno Ice 12-1": "Reno Ice",
            "San Jose Jr Sharks": "San Jose Jr Sharks",
        }
        for team, club in cases.items():
            self.assertEqual(N.extract_club(team), club, team)

    def test_never_returns_empty(self):
        self.assertTrue(N.extract_club("10A"))


class TestBirthYear(unittest.TestCase):
    def test_division_age(self):
        self.assertEqual(N.division_age("10U A"), 10)
        self.assertEqual(N.division_age("Girls 16-U"), 16)
        self.assertEqual(N.division_age("12U AA"), 12)
        self.assertIsNone(N.division_age("Mite B"))

    def test_girls_divisions_without_a_U(self):
        """PGHL names its divisions "Girls 12AAA", with no U at all.

        Requiring the U left every girls division with no age, so birth-year
        inference and the same-name split were blind across the league.
        """
        self.assertEqual(N.division_age("Girls 12AAA"), 12)
        self.assertEqual(N.division_age("Girls 14AA"), 14)
        self.assertEqual(N.division_age("12AAA-1"), 12)

    def test_combined_bands_take_the_ceiling(self):
        self.assertEqual(N.division_age("Girls 16/19AA"), 19)
        self.assertEqual(N.division_age("Girls 14U/15U"), 15)

    def test_team_suffixes_are_not_ages(self):
        # "-2" is which team, not an age group.
        self.assertEqual(N.division_age("10U B-2"), 10)
        self.assertEqual(N.division_age("14U B Flight I"), 14)

    def test_window_matches_the_viewer_convention(self):
        # 10U in a season starting 2022 => born 2012 or 2013.
        self.assertEqual(N.birth_year_window("10U A", 2022), (2012, 2013))

    def test_intersecting_windows_narrows_to_one_year(self):
        windows = [N.birth_year_window("10U A", 2022), N.birth_year_window("12U A", 2023)]
        # 10U/2022 -> (2012, 2013); 12U/2023 -> (2011, 2012); overlap = 2012.
        self.assertEqual(N.intersect_windows(windows), (2012, 2012))

    def test_contradictory_windows_yield_none(self):
        self.assertIsNone(N.intersect_windows([(2012, 2013), (2016, 2017)]))


def obs(name, season, team, jersey="", division="10U A", goalie=False):
    return Observation(name=name, season_id=season, team_id=team,
                       jersey=jersey, division=division, is_goalie=goalie)


class TestIdentityResolution(unittest.TestCase):
    def _players(self, observations, **kwargs):
        return {
            tuple(sorted(c.variants)): c
            for c in identity.build_clusters(observations, **kwargs)
        }

    def test_identical_names_are_one_player(self):
        clusters = identity.build_clusters([obs("Ian Sun", 31, 58), obs("Ian Sun", 31, 58)])
        self.assertEqual(len(clusters), 1)

    def test_case_variants_merge_via_shared_sweater(self):
        clusters = identity.build_clusters([
            obs("PARKER ANDERSON", 31, 58, "19"),
            obs("Parker Anderson", 31, 58, "19"),
        ])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(N.best_display(clusters[0].variants.items()), "Parker Anderson")

    def test_middle_initial_variants_merge_on_the_same_team(self):
        clusters = identity.build_clusters([
            obs("Gavin B Duganne", 31, 58, "93"),
            obs("Gavin Duganne", 31, 58, "93"),
        ])
        self.assertEqual(len(clusters), 1)

    def test_middle_initial_variants_merge_across_non_overlapping_seasons(self):
        clusters = identity.build_clusters([
            obs("Shiv Ritesh Kadu", 29, 58),
            obs("Shiv Kadu", 31, 129),
        ])
        self.assertEqual(len(clusters), 1)

    def test_similar_names_in_the_same_season_stay_separate(self):
        # Two children on different teams in one season must not be merged.
        clusters = identity.build_clusters([
            obs("Gavin B Duganne", 31, 58, "93"),
            obs("Gavin Duganne", 31, 129, "7"),
        ])
        self.assertEqual(len(clusters), 2)

    def test_different_people_sharing_a_sweater_are_not_merged(self):
        # Same team, same number, different names -- a mid-season roster change.
        clusters = identity.build_clusters([
            obs("Kai Garrett", 31, 58, "27"),
            obs("Bennett Morris", 31, 58, "27"),
        ])
        self.assertEqual(len(clusters), 2)

    def test_generational_suffix_variants_merge(self):
        clusters = identity.build_clusters([
            obs("Carlos Ayon II", 29, 58),
            obs("Carlos Ayon 3", 31, 58),
        ])
        self.assertEqual(len(clusters), 1)

    def test_split_override_blocks_a_merge(self):
        overrides = {N.name_key("Gavin Duganne"): {"split": 1}}
        clusters = identity.build_clusters(
            [obs("Gavin B Duganne", 29, 58, "93"), obs("Gavin Duganne", 31, 58, "93")],
            overrides=overrides,
        )
        self.assertEqual(len(clusters), 2)

    def test_merge_override_forces_a_merge(self):
        overrides = {
            N.name_key("Bobby Smith"): {"merge_into": "Robert Smith"},
        }
        clusters = identity.build_clusters(
            [obs("Bobby Smith", 31, 58, "9"), obs("Robert Smith", 31, 58, "12")],
            overrides=overrides,
        )
        self.assertEqual(len(clusters), 1)

    def test_birth_year_window_collected_from_divisions(self):
        clusters = identity.build_clusters([
            obs("Ian Sun", 30, 58, division="10U A"),
            obs("Ian Sun", 31, 58, division="12U A"),
        ])
        self.assertEqual(len(clusters), 1)
        self.assertEqual(
            clusters[0].divisions, {(30, "10U A"), (31, "12U A")},
        )


if __name__ == "__main__":
    unittest.main()
