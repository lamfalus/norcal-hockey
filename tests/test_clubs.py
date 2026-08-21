"""Reducing a team name to a club, and deciding what kind of thing it is.

Every case here is one the real data actually contains, because the failures
that matter are not the ones a rule anticipates -- they are the spellings a
league secretary typed at eleven at night.
"""

import unittest

from norcalstats import clubs


class TestDesignators(unittest.TestCase):
    def test_the_ordinary_suffixes_come_off(self):
        for team, club in (
            ("Cupertino Cougars 10A", "Cupertino Cougars"),
            ("San Jose Jr Sharks 10-5", "San Jose Jr Sharks"),
            ("Goldrush Hockey Club 12A-2", "Goldrush Hockey Club"),
            ("Santa Clarita Jr Flyers 14AA-2", "Santa Clarita Jr Flyers"),
        ):
            self.assertEqual(clubs.canonical_name(team), club)

    def test_a_birth_year_squad_is_still_the_club(self):
        # One club produced twelve identities this way.
        for team in ("San Jose Jr Sharks 2016", "San Jose Jr Sharks 2017 Teal",
                     "San Jose Jr Sharks 2015 10-2(4)",
                     "San Jose Jr Sharks 10-6 2017 10-3(6)"):
            self.assertEqual(clubs.canonical_name(team), "San Jose Jr Sharks")

    def test_a_girls_marker_survives_the_strip(self):
        # The designator sits *before* the marker here, and taking it with the
        # designator would file a girls team under the co-ed club.
        self.assertEqual(
            clubs.canonical_name("Pasadena Maple Leafs 10B Girls", "girls"),
            "Pasadena Maple Leafs Girls")

    def test_a_high_school_suffix_does_not_invent_a_club(self):
        # Reno Ice enters a high school team without being a high school.
        self.assertEqual(clubs.canonical_name("Reno Ice HS"), "Reno Ice")
        self.assertEqual(clubs.canonical_name("Stockton Colts HS D2"), "Stockton Colts")
        self.assertEqual(
            clubs.canonical_name("San Francisco Sabercats HS VN"),
            "San Francisco Sabercats")


class TestSpellings(unittest.TestCase):
    def test_punctuation_and_case_do_not_make_a_second_club(self):
        for variant, club in (
            ("Tri-Valley Blue Devils", "Tri Valley Blue Devils"),
            ("Tri-Valley Bulls", "Tri Valley Bulls"),
            ("Bakersfield Jr. Condors", "Bakersfield Jr Condors"),
            ("San Diego jr Gulls", "San Diego Jr Gulls"),
            ("Anaheim Jr. Ducks (CA)", "Anaheim Jr Ducks"),
            ("Goldrush Hockeyclub", "Goldrush Hockey Club"),
        ):
            self.assertEqual(clubs.canonical_name(variant), club)

    def test_confirmed_typos_resolve(self):
        for typo, club in (
            ("Los Angles Jr Kings 14AA", "Los Angeles Jr Kings"),
            ("Onterio Moose", "Ontario Moose"),
            ("Aliso Viejo Avalance", "Aliso Viejo Avalanche"),
            ("Cochella Valley Jr Firebirds", "Coachella Valley Jr Firebirds"),
            ("Santa Claria Jr Flyers", "Santa Clarita Jr Flyers"),
            ("San Mateo Blackstars", "San Mateo Black Stars"),
            ("Mavricks Hockey Club 12A", "Mavericks Hockey Club"),
        ):
            self.assertEqual(clubs.canonical_name(typo), club)

    def test_one_events_shorthand_does_not_become_eight_clubs(self):
        # A single CAHA Weekends event entered every team in shorthand.
        for short, club in (
            ("Empire 10BB", "Empire Hockey Club"),
            ("Surf 10UBB HI", "California Surf"),
            ("SDIA 10UBB HI", "SDIA Oilers"),
            ("Redwings 10UBB HI", "Bay Harbor Red Wings"),
            ("California Bears 10A HI", "California Golden Bears"),
            ("OC Hockey 10UBB-1 HI-1", "Orange County Hockey Club"),
        ):
            self.assertEqual(clubs.canonical_name(short), club)


class TestGirlsProgrammes(unittest.TestCase):
    def test_a_girls_programme_is_its_own_club(self):
        self.assertEqual(
            clubs.canonical_name("San Jose Jr Sharks Girls 10G-1", "girls"),
            "San Jose Jr Sharks Girls")
        self.assertEqual(
            clubs.canonical_name("San Jose Jr Sharks 10-1"), "San Jose Jr Sharks")

    def test_a_differently_named_girls_side_still_splits(self):
        # Same organisation, but the girls side carries the club's own name for
        # it, so it stays a club of its own.
        self.assertEqual(
            clubs.canonical_name("Santa Clarita Lady Flyers", "girls"),
            "Santa Clarita Flyers Girls")
        self.assertEqual(
            clubs.canonical_name("Santa Clarita Flyers 18AA"),
            "Santa Clarita Jr Flyers")

    def test_the_same_name_splits_on_gender_where_it_has_to(self):
        # "California Goldrush" is the co-ed club in one season and the girls
        # side in another; only the gender tells them apart.
        self.assertEqual(
            clubs.canonical_name("California Goldrush 8A-1"), "Goldrush Hockey Club")
        self.assertEqual(
            clubs.canonical_name("California Goldrush 12G-A", "girls"),
            "Goldrush Hockey Club Girls")


class TestClassification(unittest.TestCase):
    def test_a_bracket_slot_is_not_a_club(self):
        for name in ("12AAA Seed 1", "HS 2B-Conf A Seed 3", "Girls 19AA Seed 2"):
            self.assertTrue(clubs.is_bracket(name))
            self.assertEqual(clubs.classify(name, {24}), "bracket")

    def test_a_bracket_slot_keeps_its_seed_number(self):
        # The seed number looks exactly like a squad designator, so stripping
        # one leaves "12AAA Seed" -- which no longer reads as a bracket, and
        # quietly turns 88 empty slots into 42 clubs.
        for name in ("12AAA Seed 1", "HS 1A Seed 3", "14AA B-2 Seed 2"):
            canonical = clubs.canonical_name(name)
            self.assertEqual(canonical, name)
            self.assertEqual(clubs.classify(canonical, {24}), "bracket")

    def test_playing_a_home_league_makes_a_club_local(self):
        self.assertEqual(clubs.classify("Reno Ice", {3, 5}), "club")
        self.assertEqual(clubs.classify("San Jose Jr Sharks", {3}), "club")

    def test_a_team_that_only_visits_is_a_visitor(self):
        # The girls tier league and the district playoffs bring in teams from
        # Alaska and Washington that play nobody else here.
        self.assertEqual(clubs.classify("Anchorage North Stars", {37}), "visitor")
        self.assertEqual(clubs.classify("Sno-King Jr Thunderbirds", {34, 37}), "visitor")

    def test_a_single_home_game_does_not_make_a_visitor_local(self):
        self.assertEqual(clubs.classify("Team Wyoming Wild", {5}), "visitor")
        self.assertEqual(clubs.classify("Bishop Gorman", {5}), "visitor")

    def test_a_one_night_side_is_not_a_club(self):
        # One exhibition, thirteen "Not Signed In" roster entries, no stat line
        # it could ever contribute. A club page for it would be empty.
        self.assertEqual(
            clubs.classify(clubs.canonical_name("Jr Sharks Girls Alumni"), {3}),
            "visitor")

    def test_high_schools_are_classified_not_dropped(self):
        # They still need naming on a schedule.
        self.assertEqual(
            clubs.classify("Bellarmine Bells", {5}, only_high_school=True),
            "high_school")
        self.assertEqual(
            clubs.classify("West Ranch Wildcats", {5}, only_high_school=True),
            "high_school")

    def test_one_real_division_stops_a_club_being_a_high_school(self):
        # St Mary's girls are a PGHL programme named for a school: three of
        # their four seasons are Girls 16/19AAA, not high school hockey. The
        # same protects Reno Ice and the Stockton Colts, which each enter a
        # high school team without being one.
        self.assertEqual(
            clubs.classify("St Marys High School Rams Girls", {34, 5},
                           only_high_school=False),
            "club")
        self.assertEqual(
            clubs.classify("Reno Ice", {3, 5}, only_high_school=False), "club")

    def test_a_high_school_division_is_recognised(self):
        # Every division name the site actually prints, plus the run-together
        # spelling it uses in bracket names.
        for name in ("High School 1A", "High School 1B", "High School 2A",
                     "High School 2B", "High School 2B Pool A", "High School D2",
                     "High School Girls", "HS Varsity", "HS Jr Varsity", "HS1B"):
            self.assertTrue(clubs.is_high_school_division(name), name)
        for name in ("12U AA", "Girls 16AAA", "10U B West", "Mite B"):
            self.assertFalse(clubs.is_high_school_division(name), name)


class TestShortNames(unittest.TestCase):
    def test_a_multi_word_city_becomes_its_initials(self):
        for club, short in (
            ("San Jose Jr Sharks", "SJ Jr. Sharks"),
            ("San Francisco Sabercats", "SF Sabercats"),
            ("Santa Clara Blackhawks", "SC Blackhawks"),
            ("Santa Rosa Flyers", "SR Flyers"),
            ("Tri Valley Lady Blue Devils", "TV Lady Blue Devils"),
            ("San Mateo Black Stars", "SM Black Stars"),
            ("Vacaville Jets", "VV Jets"),
            ("Lake Tahoe Grizzlies", "Tahoe Grizzlies"),
        ):
            self.assertEqual(clubs.short_name(club), short)

    def test_a_single_word_city_is_left_alone(self):
        for club in ("Oakland Bears", "Cupertino Cougars", "Reno Ice",
                     "Ventura Mariners", "Stockton Colts"):
            self.assertEqual(clubs.short_name(club), club)

    def test_hockey_club_is_dropped_and_junior_shortened(self):
        self.assertEqual(clubs.short_name("Empire Hockey Club"), "Empire")
        self.assertEqual(clubs.short_name("Goldrush Hockey Club Girls"), "Goldrush Girls")
        self.assertEqual(clubs.short_name("Junior Reign"), "Jr. Reign")

    def test_a_rename_shows_the_new_name_over_the_grouping_one(self):
        # The site still prints the old name most often, so it groups the
        # history; the new one is what anybody would recognise.
        self.assertEqual(clubs.short_name("Stockton Colts Girls"), "Delta Knights")
        self.assertEqual(clubs.short_name("Orange County Hockey Club"), "OC Hockey")

    def test_every_short_name_is_unique(self):
        names = [
            "San Jose Jr Sharks", "San Jose Jr Sharks Girls", "Santa Rosa Flyers",
            "Santa Clarita Jr Flyers", "Santa Clarita Flyers Girls",
            "Santa Clara Blackhawks", "Oakland Bears", "California Golden Bears",
            "Stockton Colts", "Stockton Colts Girls", "Vacaville Jets",
            "Vacaville Jets Girls",
        ]
        shorts = [clubs.short_name(n) for n in names]
        self.assertEqual(len(shorts), len(set(shorts)), sorted(shorts))


if __name__ == "__main__":
    unittest.main()
