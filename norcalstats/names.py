"""Player-name normalization, club extraction, and birth-year inference.

Youth-hockey scorekeepers enter names by hand, so the same child appears as
``Gavin B Duganne``, ``Gavin Duganne``, ``GAVIN DUGANNE`` and ``Gavin Duganne
Jr.`` across five seasons. These helpers reduce spellings to comparable keys.

The merge *decisions* live in :mod:`norcalstats.identity`; this module only
provides the string handling, so it can be unit-tested in isolation.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

#: Generational suffixes, matched case-insensitively at the end of a name.
_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "v", "vi", "vii", "viii",
    "2", "3", "4", "2nd", "3rd", "4th",
}

_ROMAN = {"ii", "iii", "iv", "v", "vi", "vii", "viii"}

#: Lowercase particles that should stay lowercase mid-name.
_PARTICLES = {"de", "la", "van", "von", "der", "den", "del", "di", "da", "du"}

_WS = re.compile(r"\s+")
_NON_NAME = re.compile(r"[^\w\s'\-]", re.UNICODE)


def collapse(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def strip_periods(name: str) -> str:
    """Remove periods -- ``Avery St. Onge`` and ``Avery St Onge`` are one child."""
    return collapse((name or "").replace(".", " "))


def title_case(name: str) -> str:
    """Normalize casing, preserving roman numerals and inner capitals.

    ``ALEKSANDR RAZZHIGAEV`` -> ``Aleksandr Razzhigaev``; already-mixed names
    such as ``Avery McLeod`` are left alone.
    """
    words = collapse(name).split()
    out: list[str] = []
    for word in words:
        low = word.lower()
        if low in _ROMAN:
            out.append(word.upper())
        elif low in _PARTICLES and out:
            out.append(low)
        elif word.isupper() or word.islower():
            # Only recase words that are uniformly cased; keep "McLeod" intact.
            out.append(_cap(word))
        else:
            out.append(word)
    return " ".join(out)


def _cap(word: str) -> str:
    # Recurse through hyphens and apostrophes: "o'brien" -> "O'Brien".
    for sep in ("-", "'"):
        if sep in word:
            return sep.join(_cap(part) for part in word.split(sep))
    return word[:1].upper() + word[1:].lower()


def clean_name(name: str) -> str:
    """The spelling stored as a player's display name."""
    return title_case(strip_periods(name))


def fold(text: str) -> str:
    """Accent- and case-insensitive form used for comparison only."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return collapse(_NON_NAME.sub(" ", stripped)).lower()


def split_suffix(name: str) -> tuple[str, str]:
    """Split a trailing generational suffix: ``("Carlos Ayon", "ii")``."""
    parts = collapse(name).split()
    if len(parts) > 2 and parts[-1].lower().strip(".") in _SUFFIXES:
        return " ".join(parts[:-1]), parts[-1].lower().strip(".")
    return collapse(name), ""


def name_key(name: str) -> str:
    """Strict match key: same key means certainly the same spelling of a name."""
    return fold(strip_periods(name))


def base_key(name: str) -> str:
    """Match key ignoring generational suffixes."""
    base, _ = split_suffix(strip_periods(name))
    return fold(base)


def core_key(name: str) -> str:
    """Match key of first + last name only, ignoring middle names/initials.

    ``Shiv Ritesh Kadu`` and ``Shiv Kadu`` share a core key; so do
    ``Gavin B Duganne`` and ``Gavin Duganne``.
    """
    base, _ = split_suffix(strip_periods(name))
    parts = fold(base).split()
    if len(parts) < 2:
        return fold(base)
    return f"{parts[0]} {parts[-1]}"


def is_initial_variant(a: str, b: str) -> bool:
    """True when two names differ only by a middle name or initial."""
    pa, pb = fold(strip_periods(a)).split(), fold(strip_periods(b)).split()
    if len(pa) < 2 or len(pb) < 2:
        return False
    if (pa[0], pa[-1]) != (pb[0], pb[-1]):
        return False
    if len(pa) == len(pb):
        return False  # same shape, genuinely different middle names
    longer, shorter = (pa, pb) if len(pa) > len(pb) else (pb, pa)
    if len(shorter) != 2:
        return False
    # Any middle token is acceptable: an initial ("B") or a full middle name.
    return len(longer) >= 3


def best_display(variants: Iterable[tuple[str, int]]) -> str:
    """Pick the nicest spelling from ``(name, times_seen)`` pairs.

    Prefers the most frequently seen spelling, breaking ties toward properly
    mixed-case names over ALL CAPS, then toward the fuller name.
    """
    best: Optional[tuple[int, int, int, str]] = None
    for raw, count in variants:
        name = clean_name(raw)
        mixed = 0 if raw.isupper() else 1
        score = (count, mixed, len(name.split()), name)
        if best is None or score > best:
            best = score
    return best[3] if best else ""


# --------------------------------------------------------------- nicknames

#: Canonical first name -> the diminutives seen for it. Youth rosters mix
#: "Robert Smith" and "Bobby Smith" freely between the scoresheet and the
#: published stat table.
#:
#: A nickname match is never enough to merge on its own -- it only makes two
#: names *candidates*, which are then confirmed by a shared sweater or raised
#: for review. That keeps genuinely different children (Alex Chen and
#: Alexander Chen may well be two kids) from being silently combined.
NICKNAMES: dict[str, tuple[str, ...]] = {
    "alexander": ("alex", "alec", "xander", "sasha", "sandy"),
    "alexandra": ("alex", "allie", "ally", "sasha", "lexi", "sandra"),
    "andrew": ("andy", "drew"),
    "anthony": ("tony", "ant"),
    "benjamin": ("ben", "benji", "benny"),
    "bradley": ("brad",),
    "brandon": ("bran",),
    "cameron": ("cam",),
    "charles": ("charlie", "chuck", "chas"),
    "christopher": ("chris", "topher"),
    "christian": ("chris",),
    "daniel": ("dan", "danny"),
    "david": ("dave", "davey"),
    "dominic": ("dom", "nick"),
    "donald": ("don", "donny"),
    "douglas": ("doug",),
    "edward": ("ed", "eddie", "ted", "teddy"),
    "elizabeth": ("liz", "beth", "lizzy", "eliza", "betsy"),
    "emily": ("em", "emmy"),
    "frederick": ("fred", "freddie"),
    "gregory": ("greg",),
    "isabella": ("bella", "izzy", "isabel"),
    "jacob": ("jake", "jakey"),
    "james": ("jim", "jimmy", "jamie"),
    "jeffrey": ("jeff",),
    "jennifer": ("jen", "jenny"),
    "jonathan": ("jon", "jonny", "johnny"),
    "joseph": ("joe", "joey"),
    "joshua": ("josh",),
    "katherine": ("kate", "katie", "kathy", "kat", "katharine", "catherine"),
    "kenneth": ("ken", "kenny"),
    "lawrence": ("larry",),
    "leonardo": ("leo",),
    "madeline": ("maddie", "maddy"),
    "matthew": ("matt", "matty"),
    "maxwell": ("max",),
    "michael": ("mike", "mikey", "mick"),
    "mitchell": ("mitch",),
    "nathan": ("nate", "nat"),
    "nathaniel": ("nate", "nathan"),
    "nicholas": ("nick", "nicky", "nico"),
    "olivia": ("liv", "livvy"),
    "patrick": ("pat", "paddy"),
    "peter": ("pete", "petey"),
    "philip": ("phil", "phillip"),
    "rebecca": ("becca", "becky"),
    "richard": ("rick", "ricky", "dick", "rich", "richie"),
    "robert": ("rob", "bob", "bobby", "robbie", "bobbie"),
    "ronald": ("ron", "ronnie"),
    "samuel": ("sam", "sammy"),
    "sebastian": ("seb", "bash"),
    "stephen": ("steve", "steven", "stevie"),
    "steven": ("steve", "stevie", "stephen"),
    "theodore": ("theo", "ted", "teddy"),
    "thomas": ("tom", "tommy", "thom"),
    "timothy": ("tim", "timmy"),
    "victoria": ("vicky", "tori", "vic"),
    "vincent": ("vince", "vinny"),
    "william": ("will", "bill", "billy", "willie", "liam"),
    "zachary": ("zach", "zack", "zak"),
}

#: Diminutive -> the canonical names it could stand for.
_NICKNAME_ROOTS: dict[str, set[str]] = {}
for _canonical, _variants in NICKNAMES.items():
    _NICKNAME_ROOTS.setdefault(_canonical, set()).add(_canonical)
    for _variant in _variants:
        _NICKNAME_ROOTS.setdefault(_variant, set()).add(_canonical)


def first_name_roots(name: str) -> set[str]:
    """Canonical first names a spelling could stand for.

    ``"Bobby Smith"`` -> ``{"robert"}``; ``"Alex Chen"`` -> ``{"alexander",
    "alexandra"}``; an unknown first name maps to itself.
    """
    parts = fold(strip_periods(name)).split()
    if not parts:
        return set()
    first = parts[0]
    return set(_NICKNAME_ROOTS.get(first, {first}))


def is_nickname_variant(a: str, b: str) -> bool:
    """True when two names share a surname and their first names are the
    same name in long and short form.

    Requires an actual nickname relationship, so ``Alex``/``Alexander`` matches
    but ``Alex``/``Andrew`` does not.
    """
    pa, pb = fold(strip_periods(a)).split(), fold(strip_periods(b)).split()
    if len(pa) < 2 or len(pb) < 2 or pa[-1] != pb[-1]:
        return False
    if pa[0] == pb[0]:
        return False  # identical already; not a nickname question
    return bool(first_name_roots(a) & first_name_roots(b))


# ------------------------------------------------------------------- clubs

#: Trailing team designators to strip when reducing a team name to its club:
#: "San Jose Jr Sharks 10-5" -> "San Jose Jr Sharks".
_TEAM_SUFFIX = re.compile(
    r"""\s+(
        \d{1,2}\s*U(\s*[A-Z]{1,3})?(-\d+)?   |  # 10U, 12U A, 14U AA-2
        \d{1,2}\s*[A-Z]{1,3}(-\d+)?          |  # 10A, 12AA-1
        \d{1,2}-\d{1,2}                      |  # 10-5
        [A-Z]{1,3}-?\d*                      |  # A, AA, BB, B2
        \(\d+\)                                 # (2)
    )\s*$""",
    re.X,
)


def extract_club(team_name: str) -> str:
    """Reduce a team name to its club.

    ``"Cupertino Cougars 10A"`` -> ``"Cupertino Cougars"``;
    ``"San Jose Jr Sharks 10-5"`` -> ``"San Jose Jr Sharks"``.
    """
    name = collapse(team_name)
    previous = None
    while name and name != previous:
        previous = name
        name = _TEAM_SUFFIX.sub("", name).strip()
    return name or collapse(team_name)


# -------------------------------------------------------------- birth years


#: Placeholders the site prints in the name column when a roster was not
#: submitted electronically, or a goalie could not be identified. They are not
#: people: "Not Signed In" alone appeared on 81 different teams and up to 36
#: times in a single game, which silently merged whole rosters into one
#: "player" and then broke every derived stat.
_PLACEHOLDER_NAMES = re.compile(
    r"""^\s*(
        not\s*signed\s*in
      | (home|visitor|away)?\s*unknown\s*(goalie|player|skater)?\s*\d*
      | unknown
      | no\s*goalie
      | tbd | n/?a | none | player | goalie | skater
    )\s*\d*\s*$""",
    re.I | re.X,
)


def is_placeholder(name: str) -> bool:
    """True when a roster entry names no actual person."""
    return bool(_PLACEHOLDER_NAMES.match(collapse(name or "")))


#: Girls play in two arrangements here: a dedicated division ("Girls 16-U"),
#: or a girls team entered into an otherwise co-ed division ("San Jose Jr
#: Sharks Girls" in 10U B West, "Stockton Colts Girls 10G-1").
#: "Lady Blue Devils" and a "10G-1" suffix both mark a girls team.
_GIRLS = re.compile(
    r"\b(girls?|ladies|lady|women|female)\b|\d{1,2}\s*G(-\d+)?\b", re.I)


def is_girls(*labels: str) -> bool:
    """True when any label marks a girls division or team."""
    return any(_GIRLS.search(label or "") for label in labels)


def division_gender(division: str, team: str = "") -> str:
    """``'girls'`` or ``'coed'``.

    Nothing here is labelled "boys": the non-girls divisions are open, and girls
    regularly play in them, which is exactly why double-rostering happens.
    """
    return "girls" if is_girls(division, team) else "coed"


def division_age(division: str) -> Optional[int]:
    """Age group implied by a division name: ``"10U A"`` -> ``10``.

    Also handles ``"Girls 16-U"`` and bare ``"12U"``.
    """
    match = re.search(r"(\d{1,2})\s*-?\s*U\b", division or "", re.I)
    return int(match.group(1)) if match else None


def birth_year_window(division: str, start_year: int) -> Optional[tuple[int, int]]:
    """The two-year birth window implied by playing ``division`` in a season.

    A 10U player in a season starting 2022 was born in 2012 or 2013 -- the same
    convention the existing viewer uses, so inferred years stay consistent.
    """
    age = division_age(division)
    if not age or not start_year:
        return None
    return (start_year - age, start_year - age + 1)


#: How many years younger than a division's nominal age a player may be and
#: still plausibly be on that team.
#:
#: Age rules are one-directional: a division of age N admits players *under* N,
#: so a child can play up but never down. Two years covers the ordinary cases,
#: including a 15-year-old on a 19U team (19U nominally means born 2006-07 in a
#: 2025 season; she is born 2009-10, and the windows still meet). It is
#: deliberately short of four years, which would let a 12U player match a 16U
#: team -- a jump that does not happen at these ages.
PLAY_UP_TOLERANCE = 2


def widen_for_play_up(
    window: Optional[tuple[int, int]], tolerance: int = PLAY_UP_TOLERANCE
) -> Optional[tuple[int, int]]:
    """Widen a birth window toward younger players only.

    The lower bound is a hard age limit and never moves; the upper bound is
    extended, because younger players turn up on older teams.
    """
    if not window:
        return None
    low, high = window
    return (low, high + tolerance)


def intersect_windows(
    windows: Iterable[Optional[tuple[int, int]]],
    tolerance: int = 0,
) -> Optional[tuple[int, int]]:
    """Narrow a player's birth year by intersecting every season's window.

    With ``tolerance`` set, each window is first widened toward younger players,
    so a player who played up in some seasons still resolves.
    """
    low: Optional[int] = None
    high: Optional[int] = None
    for window in windows:
        if not window:
            continue
        lo, hi = widen_for_play_up(window, tolerance) if tolerance else window
        low = lo if low is None else max(low, lo)
        high = hi if high is None else min(high, hi)
    if low is None or high is None or low > high:
        return None
    return (low, high)
