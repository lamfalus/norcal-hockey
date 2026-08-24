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

    def test_boys_tier_one_ladder_is_one_birth_year(self):
        # 11U through 16U AAA all ice in the same league and season, which is
        # only possible if each is a single year.
        for age in range(11, 17):
            division = f"{age}U AAA"
            self.assertEqual(N.birth_year_span(division), 1, division)
            self.assertEqual(
                N.birth_year_window(division, 2025), (2025 - age, 2025 - age))

    def test_eighteen_aaa_is_the_exception_at_the_top(self):
        # 17- and 18-year-olds, so it is an ordinary two-year band.
        self.assertEqual(N.birth_year_span("18U AAA"), 2)
        self.assertEqual(N.birth_year_window("18U AAA", 2025), (2007, 2008))

    def test_lower_tiers_stay_two_years(self):
        # The single-year ladder is Tier I only; AA and below are bands.
        for division in ("12U AA", "14U AA", "16U A", "10U BB"):
            self.assertEqual(N.birth_year_span(division), 2, division)
        self.assertEqual(N.birth_year_window("14U AA", 2025), (2011, 2012))

    def test_girls_tier_one_is_two_years_all_the_way_up(self):
        # The girls ladder does not go single-year at any age.
        for age, window in ((12, (2013, 2014)), (14, (2011, 2012)),
                            (16, (2009, 2010))):
            division = f"Girls {age}AAA"
            self.assertEqual(N.birth_year_span(division), 2, division)
            self.assertEqual(N.birth_year_window(division, 2025), window)

    def test_girls_nineteen_carries_three_birth_years(self):
        # 17, 18 and 19 -- and it is an age classification, not a tier rule,
        # so 19AA spans the same three years as 19AAA.
        for division in ("Girls 19AAA", "Girls 19AA"):
            self.assertEqual(N.birth_year_span(division), 3, division)
            self.assertEqual(N.birth_year_window(division, 2025), (2006, 2008))

    def test_combined_band_reaches_down_to_its_lower_classification(self):
        # "Girls 16/19AA" is 15- to 19-year-olds: the 19U ceiling and the 16U
        # floor, not the top classification alone.
        self.assertEqual(N.division_ages("Girls 16/19AA"), [16, 19])
        self.assertEqual(N.birth_year_window("Girls 16/19AA", 2025), (2006, 2010))

    def test_team_numbers_do_not_widen_a_band(self):
        # Only a slash marks a combined band; a trailing number is which team.
        self.assertEqual(N.division_ages("Girls 16AA 5"), [16])
        self.assertEqual(N.birth_year_window("Girls 16AA 5", 2025), (2009, 2010))

    def test_one_single_year_season_settles_a_career_of_bands(self):
        # Two years of 10U, two of 12U and a one-game call-up leave two
        # candidate years -- the call-up alone contradicts the rest, so the
        # strict pass fails and the tolerant one can only answer with a range.
        # The 13U AAA season is what decides between them.
        career = [(27, "10U BB"), (28, "10U A"), (28, "12U A"),
                  (29, "12U AA"), (30, "12U AA"), (31, "13U AAA")]
        years = {27: 2021, 28: 2022, 29: 2023, 30: 2024, 31: 2025}

        bands = [(s, d) for s, d in career if d != "13U AAA"]
        cluster = identity.Cluster(key="range", divisions=set(bands))
        self.assertEqual(identity._birth_window(cluster, years), (2012, 2013))

        cluster = identity.Cluster(key="exact", divisions=set(career))
        self.assertEqual(identity._birth_window(cluster, years), (2012, 2012))

    @staticmethod
    def career(seasons):
        """A cluster from ``{season_id: {division: appearances}}``."""
        cluster = identity.Cluster(key="k")
        for season, divisions in seasons.items():
            for division, count in divisions.items():
                cluster.division_counts[(season, division)] = count
        cluster.divisions = set(cluster.division_counts)
        return cluster

    def test_a_call_up_does_not_outvote_the_season_it_was_borrowed_from(self):
        # Two seasons of 10U and two of 12U pin this player to 2012 -- but one
        # afternoon up in 12U in 2022 implies born 2010-11, which nothing else
        # agrees with. Counted equally it empties the strict intersection and
        # costs the player a year his real seasons never left ambiguous.
        cluster = self.career({
            27: {"10U BB": 20},
            28: {"10U A": 29, "12U A": 1},
            29: {"12U AA": 5},
            30: {"12U AA": 24},
        })
        years = {27: 2021, 28: 2022, 29: 2023, 30: 2024}

        self.assertNotIn((28, "12U A"), identity._primary_divisions(cluster))
        self.assertEqual(identity._birth_window(cluster, years), (2012, 2012))

        # Counted equally -- one division, one vote -- the single game wins and
        # the answer falls back to a range.
        flat = identity.Cluster(key="k", divisions=set(cluster.divisions))
        self.assertEqual(identity._birth_window(flat, years), (2012, 2013))

    def test_a_real_second_roster_still_counts(self):
        # A girl playing a girls team and a co-ed one is genuinely in both, so
        # neither is dropped. The rule is share of a season, not a raw count.
        cluster = identity.Cluster(key="k")
        cluster.division_counts[(31, "12U A")] = 14
        cluster.division_counts[(31, "Girls 12AA")] = 11
        cluster.divisions = set(cluster.division_counts)
        self.assertEqual(len(identity._primary_divisions(cluster)), 2)

    def test_a_call_up_still_sets_a_floor(self):
        # Dropping a call-up from the strict pass drops its upper bound, which
        # was never true of a call-up. Its lower bound is a real age limit and
        # has to survive: nobody in a 12U game is older than 12U admits, so
        # this is a 12U-aged player whose season is spent up at 14U.
        cluster = identity.Cluster(key="k")
        cluster.division_counts[(31, "14U A")] = 20
        cluster.division_counts[(31, "12U A")] = 1
        cluster.divisions = set(cluster.division_counts)
        self.assertEqual(identity._primary_divisions(cluster), {(31, "14U A")})
        # 14U/2025 alone would say 2011-12; the single 12U game rules both out.
        self.assertEqual(identity._birth_window(cluster, {31: 2025}), (2013, 2014))

    def test_home_division_cap_does_not_depend_on_set_order(self):
        # ``Cluster.divisions`` is a set, and the 13U AAA window ties with the
        # 14U AAA band that starts on the same year. Answering differently
        # between runs would move a player's badge for no reason.
        career = [(31, "13U AAA"), (32, "14U AAA"), (30, "12U AA")]
        years = {30: 2024, 31: 2025, 32: 2026}
        answers = {
            identity._birth_window(identity.Cluster(key="k", divisions=set(order)), years)
            for order in (career, career[::-1], career[1:] + career[:1])
        }
        self.assertEqual(answers, {(2012, 2012)})


def obs(name, season, team, jersey="", division="10U A", goalie=False):
    return Observation(name=name, season_id=season, team_id=team,
                       jersey=jersey, division=division, is_goalie=goalie)


class TestDivisionFromTeamName(unittest.TestCase):
    """Reading a division off a team name, for sides that matched no team row.

    503 schedule sides never resolved. The printed name is all that is left,
    and a third of the time it states the division outright.
    """

    def test_it_reads_the_age_and_the_tier(self):
        for name, expected in (
            ("Bakersfield Jr Condors 14A", "14U A"),
            ("Ventura Mariners 12BB", "12U BB"),
            ("Anaheim Jr Ducks 11AAA", "11U AAA"),
            ("Tri Valley Blue Devils 16A", "16U A"),
            ("Anaheim Jr Ducks-1 10UA HI", "10U A"),
            ("OC Hockey 10UBB-1 HI-1", "10U BB"),
            ("Los Angeles Jr Kings 12AAA-2", "12U AAA"),
        ):
            self.assertEqual(N.division_from_team_name(name), expected, name)

    def test_a_girls_team_is_spelled_the_way_the_site_spells_it(self):
        # The girls divisions drop the U and close up: "Girls 14AA", not
        # "Girls 14U AA". The result has to be comparable with the real ones.
        for name, expected in (
            ("San Jose Jr Sharks Girls 14AA", "Girls 14AA"),
            ("San Jose Jr Sharks Girls 16AAA", "Girls 16AAA"),
            ("Tri Valley Lady Blue Devils 19AA", "Girls 19AA"),
        ):
            self.assertEqual(N.division_from_team_name(name), expected, name)

    def test_an_age_without_a_tier_says_nothing(self):
        # "10-5" is the club's own numbering, not a tier. Answering "10U" would
        # file the side in a bucket it does not belong to; saying nothing is
        # the honest answer, and these are recoverable only by resolving the
        # team properly.
        for name in ("San Jose Jr Sharks 10-5", "Anaheim Jr Ducks",
                     "Santa Rosa Flyers", "Santa Clara Blackhawks",
                     "Orange County Hockey Club", ""):
            self.assertIsNone(N.division_from_team_name(name), name)

    def test_the_longest_tier_wins(self):
        # AAA must not read as AA, and BB must not read as B.
        self.assertEqual(N.division_tier("Bears 12AAA"), "AAA")
        self.assertEqual(N.division_tier("Bears 12AA"), "AA")
        self.assertEqual(N.division_tier("Bears 12BB"), "BB")
        self.assertEqual(N.division_tier("Bears 12B"), "B")
        self.assertEqual(N.division_tier("Bears"), None)

    def test_a_club_carrying_its_own_number_does_not_confuse_it(self):
        # "Anaheim Jr Ducks-1 10UA": the tier belongs to the age nearest the
        # end, not to the club's numbering.
        self.assertEqual(N.division_from_team_name("Anaheim Jr Ducks-1 10UA HI"), "10U A")
        self.assertEqual(N.division_from_team_name("San Diego Gulls 10UBB HI-2"), "10U BB")


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
