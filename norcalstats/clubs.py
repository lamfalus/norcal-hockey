"""Club identity: one name to group by, a shorter one to show.

The site names *teams*, not clubs, and it names them inconsistently. Reducing
"San Jose Jr Sharks 10-5" to a club therefore means undoing three separate
kinds of mess:

* **Squad designators** tacked onto the club name -- ``10A``, ``G14AA``,
  ``10G2``, ``16aa-2``, ``2017 Teal``, a bare ``-2``, or a bare tier with no
  age at all (``BB``, ``AA2``), which is how a preseason tournament enters its
  teams. One club can pick up a dozen apparent identities this way; "San Jose
  Jr Sharks" had twelve.
* **Spelling drift** between seasons and between the people typing them:
  ``Tri-Valley`` and ``Tri Valley``, ``Jr.`` and ``Junior``, ``Los Angles``,
  ``Onterio``, ``Blackstars``. Rules handle the punctuation; the typos need
  the alias table below, because no rule can tell a misspelling from a
  different club.
* **Names that are not clubs at all** -- playoff bracket slots ("12AAA Seed 1"),
  high schools, and teams from out of the area that appear only as somebody's
  opponent.

The last of those is why a club carries a ``kind``. All three of the non-club
kinds still need their names -- they show up on schedules and box scores -- but
none belongs in a list of clubs to browse.

``short_name`` exists because the canonical name is for grouping, not for
reading: a phone at a rink has room for "SJ Jr. Sharks", not "San Jose Jr
Sharks". Multi-word cities collapse to their initials, single-word cities stay
as they are, and anything the rule gets wrong is listed explicitly.
"""

from __future__ import annotations

import re

#: Leagues that make a club local. A club appearing in any of these is one of
#: ours; a club that only ever turns up in the girls tier league, the district
#: playoffs or nationals came to play us and went home again.
HOME_LEAGUES = frozenset({3, 4, 5, 16, 17, 24})

#: Bracket slots published before anyone knows who fills them. They carry no
#: roster and no stats -- 88 of them, every one in the CAHA playoffs.
_BRACKET = re.compile(r"\bSeed\s*\d+$", re.I)

#: The state a visiting team came from, printed after its name at nationals.
_STATE = re.compile(r"\s*\([A-Z]{2}\)$")

#: Everything the site appends to a club name to name one of its squads. The
#: trailing ``Girls`` lookahead matters: "Pasadena Maple Leafs 10B Girls" has
#: its designator in the middle, and dropping the ``Girls`` with it would file
#: a girls team under the co-ed club.
_DESIGNATOR = re.compile(
    r"""(\s+|(?<=[A-Za-z])(?=-\d+$))(
          G\d{1,2}\s*[A-Z]{0,3}            # G14AA, G16AAA
        | \d{1,2}\s*G-?[A-Za-z]?           # 12G-A
        | \d{1,2}\s*G\s*\d*                # 10G2
        | \d{1,3}\s*[A-Za-z]{1,3}-?\d*     # 16aa-2, 12uAA, 100A, 12x, 10B
        | (?:[Aa]{1,3}|[Bb]{1,3})-?\d*    # a tier with no age: BB, B, AA, A, AA2, B2
        | \d{1,2}-\d{1,2}                  # 10-5
        | (19|20)\d{2}([ ].*)?             # 2017, 2017 Teal, 2015 10-2(4)
        | \(\d+\)                          # (5)
        | \d{1,2}
        | Ex | HI(-\d+)?
        | HS(\s+(D\d|VS|VN|JV|Varsity|\d[A-Z]?))?  # HS, HS D2, HS VS, HS 2A
        | (Jr\.?\s+)?Varsity
      )(?=\s*(Girls?)?\s*$)""",
    re.X,
)
_TRAILING_NUMBER = re.compile(r"-\d+$")
_TRAILING_GIRLS = re.compile(r"\s+(Girls?)\s*$", re.I)


def is_bracket(name: str) -> bool:
    """True for a playoff bracket slot rather than a team."""
    return bool(_BRACKET.search(name or ""))


def strip_designators(name: str) -> str:
    """Reduce a team name to its club, keeping any ``Girls`` marker."""
    text = _STATE.sub("", name or "").strip()
    girls = bool(_TRAILING_GIRLS.search(text))
    text = _TRAILING_GIRLS.sub("", text)
    previous = None
    while text != previous:
        previous = text
        text = _TRAILING_NUMBER.sub("", _DESIGNATOR.sub("", text).strip()).strip()
    text = text or (name or "")
    return f"{text} Girls" if girls else text


def fold(name: str) -> str:
    """The comparison key: what two spellings of one club have in common."""
    text = strip_designators(name).lower()
    text = text.replace(".", " ").replace("-", " ").replace("'", "")
    text = re.sub(r"\bjunior\b", "jr", text)
    text = re.sub(r"\bhockey ?club\b", "hc", text)
    return re.sub(r"\s+", " ", text).strip()


#: Spellings no rule can reconcile, keyed on the folded form so every variant
#: of the alias resolves too. A typo is indistinguishable from a different club
#: without knowing the clubs, so each of these was confirmed rather than
#: guessed.
ALIASES = {
    # Misspellings.
    "aliso viejo avalance": "Aliso Viejo Avalanche",
    "coachella vally jr firebirds": "Coachella Valley Jr Firebirds",
    "cochella valley jr firebirds": "Coachella Valley Jr Firebirds",
    "san mateo blackstars": "San Mateo Black Stars",
    "santa claria jr flyers": "Santa Clarita Jr Flyers",
    "los angles jr kings": "Los Angeles Jr Kings",
    "onterio jr reign": "Junior Reign",
    "onterio moose": "Ontario Moose",
    "shattucks st marys": "Shattuck-St. Mary's",
    "tahoe hockey hockey academy": "Tahoe Hockey Academy",
    "delta hockey acadamy girls": "Delta Hockey Academy Girls",
    "long islang gulls": "Long Island Gulls",
    "mavricks hc": "Mavericks Hockey Club",
    "team team north dakota": "Team North Dakota",
    "bellarmine college preparatory bells": "Bellarmine Bells",
    # One CAHA Weekends event in '25-'26 entered all seventeen of its teams in
    # shorthand, which is where every one-off short club name comes from.
    "empire": "Empire Hockey Club",
    "surf": "California Surf",
    "sdia": "SDIA Oilers",
    "redwings": "Bay Harbor Red Wings",
    "california bears": "California Golden Bears",
    "jr firebirds": "Coachella Valley Jr Firebirds",
    "san diego gulls": "San Diego Jr Gulls",
    "oc hockey": "Orange County Hockey Club",
    # Abbreviations used elsewhere.
    "fresno monsters": "Fresno Jr Monsters",
    "bakersfield condors": "Bakersfield Jr Condors",
    "las vegas golden knights": "Las Vegas Jr Golden Knights",
    "las vegas jr knights": "Las Vegas Jr Golden Knights",
    "las vegas jr knights girls": "Las Vegas Jr Golden Knights Girls",
    # One organisation whose tiers are entered under different names.
    "ontario jr reign": "Junior Reign",
    "california gold rush": "Goldrush Hockey Club",
    "california goldrush": "Goldrush Hockey Club",
    "santa clarita flyers": "Santa Clarita Jr Flyers",
    "santa clarita lady flyers": "Santa Clarita Flyers Girls",
    # Renamed; the old name is still the one the site prints most.
    "delta knights": "Stockton Colts Girls",
    # Where one club is spelled two ways and neither is wrong, the spelling the
    # site uses most often wins. Without this the club still groups correctly --
    # the fold key is the same either way -- but the name it is filed under
    # would depend on which spelling happened to be read first.
    "anaheim jr ducks": "Anaheim Jr Ducks",
    "bakersfield jr condors": "Bakersfield Jr Condors",
    "goldrush hc": "Goldrush Hockey Club",
    "las vegas jr golden knights": "Las Vegas Jr Golden Knights",
    "las vegas jr golden knights girls": "Las Vegas Jr Golden Knights Girls",
    "san diego jr gulls": "San Diego Jr Gulls",
    "tri valley blue devils": "Tri Valley Blue Devils",
    "tri valley bulls": "Tri Valley Bulls",
}

#: Where a club's girls side would otherwise fold into its co-ed club. Girls
#: programmes are their own clubs here -- Jr. Sharks and Jr. Sharks Girls are
#: listed separately, because that is how they are organised and how people
#: look for them.
GIRLS_ALIASES = {
    "california goldrush": "Goldrush Hockey Club Girls",
    "delta knights": "Stockton Colts Girls",
}

#: Divisions that are high school hockey -- "High School 1A", "HS Varsity",
#: "High School Girls". These sit inside CAHA rather than in one of the
#: excluded high school leagues, so they arrive whether or not they are wanted.
#: The ``hs\d`` arm is defensive: every division the site prints today spells it
#: "HS Varsity" or "High School 1A", but the brackets show it also gets written
#: "HS1B", with nothing between the letters and the tier.
_HS_DIVISION = re.compile(r"\b(hs|high\s+school)\b|\bhs(?=\d)", re.I)


def is_high_school_division(name: str) -> bool:
    return bool(_HS_DIVISION.search(name or ""))

#: Sides that a single appearance in a home league would otherwise make local:
#: four out-of-area clubs that came once, and one alumni team that existed for
#: an evening. The Jr Sharks Girls alumni played one exhibition in December
#: 2021 and never signed a roster in -- all thirteen of their entries are the
#: site's "Not Signed In" placeholder -- so a club page for them could only
#: ever be empty. The game still names them on the 16AAA side's schedule.
FORCED_VISITORS = frozenset({
    "team wyoming wild",
    "new mexico ice wolves",
    "shattuck st marys",
    "bishop gorman",
    "jr sharks girls alumni",
})

#: Multi-word cities collapse to their initials; single-word cities are already
#: short enough to leave alone.
CITY_ABBREVIATIONS = {
    "San Francisco": "SF", "San Jose": "SJ", "San Diego": "SD",
    "San Mateo": "SM", "Santa Clara": "SC", "Santa Clarita": "SC",
    "Santa Rosa": "SR", "Santa Barbara": "SB", "Los Angeles": "LA",
    "Las Vegas": "LV", "Tri Valley": "TV", "Aliso Viejo": "AV",
    "Bay Harbor": "BH", "Coachella Valley": "CV", "Orange County": "OC",
    "Lake Tahoe": "Tahoe", "Vacaville": "VV",
}

#: Display names the rule cannot produce.
DISPLAY_OVERRIDES = {
    "Orange County Hockey Club": "OC Hockey",
    # A PGHL programme named for a school. "SM" is already San Mateo's.
    "St Marys High School Rams Girls": "St Marys Rams Girls",
    # The club rebranded; the site still mostly prints the old name, so the old
    # one groups the history and the new one is what people read.
    "Stockton Colts Girls": "Delta Knights",
}


def canonical_name(team_name: str, gender: str = "coed") -> str:
    """The club a team belongs to, spelled one way."""
    # A bracket slot keeps its name whole. Stripping designators would take the
    # seed number with them -- "12AAA Seed 1" becomes "12AAA Seed" -- and the
    # result no longer looks like a bracket to anything downstream.
    if is_bracket(team_name):
        return (team_name or "").strip()
    key = fold(team_name)
    if gender == "girls" and key in GIRLS_ALIASES:
        return GIRLS_ALIASES[key]
    name = ALIASES.get(key) or strip_designators(team_name)
    # An alias can resolve onto a name that is itself an alias.
    return ALIASES.get(fold(name), name)


def short_name(canonical: str) -> str:
    """The name to show. Falls back to the canonical name unchanged."""
    if canonical in DISPLAY_OVERRIDES:
        return DISPLAY_OVERRIDES[canonical]
    text = canonical
    for city, abbreviation in CITY_ABBREVIATIONS.items():
        if text.startswith(city + " "):
            text = abbreviation + text[len(city):]
            break
    text = re.sub(r"\s*\bHockey Club\b", "", text).strip()
    text = re.sub(r"\bJunior\b", "Jr", text)
    text = re.sub(r"\bJr\b", "Jr.", text)
    return re.sub(r"\s+", " ", text).strip() or canonical


def classify(canonical: str, league_ids: set[int], *,
             only_high_school: bool = False) -> str:
    """``club``, ``high_school``, ``visitor`` or ``bracket``.

    A club counts as a high school only when it has never played anywhere but a
    high school division. One season in a real division is enough to make it a
    club: Reno Ice, the SF Sabercats and the Stockton Colts all enter a high
    school team, and St Mary's girls are a PGHL programme that happens to be
    named for a school -- three of their four seasons are Girls 16/19AAA.
    """
    if is_bracket(canonical):
        return "bracket"
    if only_high_school:
        return "high_school"
    if fold(canonical) in FORCED_VISITORS:
        return "visitor"
    return "club" if league_ids & HOME_LEAGUES else "visitor"
