"""Resolve raw name spellings to stable player identities.

The existing viewer merges names using string rules alone, because season-total
scrapes give it nothing else to go on. Game-level rosters carry
``(season, team, jersey)``, which is far stronger evidence: two spellings
sharing a sweater on one team in one season are the same child, whatever the
spelling.

Four kinds of name trouble occur in this data, and they pull in opposite
directions:

============  ==========================================  ====================
Trouble       Example                                     How it is handled
============  ==========================================  ====================
Punctuation   ``Avery St. Onge`` / ``Avery St Onge``       merged automatically
Initials      ``Gavin B Duganne`` / ``Gavin Duganne``      merged when safe
Nicknames     ``Bobby Smith`` / ``Robert Smith``           merged only with
                                                           corroboration, else
                                                           raised for review
Collisions    two children both called ``Ryan Smith``      split when the
                                                           evidence is strong,
                                                           else raised
============  ==========================================  ====================

Anything uncertain becomes a :mod:`norcalstats.review` item rather than a
silent guess, and every manual decision is consulted on subsequent runs, so
rebuilding the database from scratch never loses an answer.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import names as N, review
from .db import now
from .review import Item

log = logging.getLogger(__name__)

#: A same-name collision is only split automatically when *both* children have
#: at least this many roster appearances. Below it, the more likely explanation
#: is one child called up to an older division for a game or two, so the names
#: stay together and the case is raised for review instead.
MIN_SPLIT_APPEARANCES = 3

#: ...and when the smaller side spans at least this many seasons. A single
#: season in a mismatched division is almost always a call-up.
MIN_SPLIT_SEASONS = 1

#: ...unless the implied birth years are this far apart, in which case the
#: appearance count does not matter. A child can be called up one division, so
#: a 10U/12U conflict is ambiguous; a 10U/16U conflict is not a call-up at all.
CERTAIN_SPLIT_YEAR_GAP = 3


@dataclass
class Observation:
    name: str
    season_id: int
    team_id: Optional[int]
    jersey: str
    division: str
    is_goalie: bool = False
    team_name: str = ""

    @property
    def gender(self) -> str:
        """``'girls'`` or ``'coed'`` for the team this appearance was on."""
        return N.division_gender(self.division, self.team_name)


@dataclass
class Cluster:
    """One resolved player: every spelling plus where they were seen."""
    key: str
    variants: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    seasons: set[int] = field(default_factory=set)
    team_seasons: set[tuple[int, int]] = field(default_factory=set)
    sweaters: set[tuple[int, int, str]] = field(default_factory=set)
    divisions: set[tuple[int, str]] = field(default_factory=set)
    #: ``(spelling, season, team)`` triples, used to build the resolution map
    #: so a shared spelling can still point at the right child.
    appearance_keys: set[tuple[str, int, Optional[int]]] = field(default_factory=set)
    appearances: int = 0
    goalie: bool = False
    #: Set when this cluster came from splitting one spelling in two.
    person_key: str = ""

    def absorb(self, other: "Cluster") -> None:
        for name, count in other.variants.items():
            self.variants[name] += count
        self.seasons |= other.seasons
        self.team_seasons |= other.team_seasons
        self.sweaters |= other.sweaters
        self.divisions |= other.divisions
        self.appearance_keys |= other.appearance_keys
        self.appearances += other.appearances
        self.goalie = self.goalie or other.goalie

    @property
    def display(self) -> str:
        return N.best_display(self.variants.items())


class _Union:
    """Minimal union-find over cluster keys."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self.parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:  # path compression
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


# --------------------------------------------------------------- gathering


def collect_observations(conn: sqlite3.Connection) -> list[Observation]:
    """Every roster appearance, with the context needed to identify players."""
    sql = """
        SELECT r.name        AS name,
               g.season_id   AS season_id,
               CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END AS team_id,
               COALESCE(r.jersey, '') AS jersey,
               -- A team's own division where known; the printed level otherwise.
               COALESCE(td.name, d.name, g.level, '') AS division,
               CASE WHEN UPPER(COALESCE(r.position,'')) = 'G' THEN 1 ELSE 0 END AS is_goalie,
               COALESCE(t.name, CASE r.side WHEN 'home' THEN g.home_name
                                            ELSE g.away_name END, '') AS team_name
        FROM game_rosters r
        JOIN games g ON g.game_id = r.game_id
        LEFT JOIN divisions d ON d.division_id = g.division_id
        LEFT JOIN teams t ON t.season_id = g.season_id AND t.team_id =
              CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END
        LEFT JOIN divisions td ON td.division_id = t.division_id
        WHERE r.role = 'player' AND r.name <> ''
    """
    return [_row_to_observation(row) for row in conn.execute(sql)]


def collect_stat_observations(conn: sqlite3.Connection) -> list[Observation]:
    """Names from the site's season-total tables.

    These cover players who appear in the published totals but never on a parsed
    scoresheet (games without detail), so they are folded in as well.
    """
    sql = """
        SELECT s.name AS name, s.season_id AS season_id, s.team_id AS team_id,
               COALESCE(s.jersey, '') AS jersey,
               COALESCE(d.name, '')  AS division,
               CASE WHEN s.kind = 'goalie' THEN 1 ELSE 0 END AS is_goalie,
               COALESCE(t.name, '') AS team_name
        FROM team_stat_rows s
        LEFT JOIN teams t     ON t.team_id = s.team_id AND t.season_id = s.season_id
        LEFT JOIN divisions d ON d.division_id = t.division_id
        WHERE s.name <> ''
    """
    return [_row_to_observation(row) for row in conn.execute(sql)]


def _row_to_observation(row: sqlite3.Row) -> Observation:
    keys = row.keys()
    return Observation(
        name=row["name"], season_id=row["season_id"], team_id=row["team_id"],
        jersey=row["jersey"], division=row["division"],
        is_goalie=bool(row["is_goalie"]),
        team_name=row["team_name"] if "team_name" in keys else "",
    )


# --------------------------------------------------------------- clustering


def build_clusters(
    observations: Iterable[Observation],
    *,
    overrides: Optional[dict[str, dict]] = None,
    splits: Optional[dict[str, dict[tuple[int, Optional[int]], str]]] = None,
    season_years: Optional[dict[int, Optional[int]]] = None,
    review_items: Optional[list[Item]] = None,
) -> list[Cluster]:
    """Group observations into one cluster per real child."""
    overrides = overrides or {}
    splits = splits or {}
    season_years = season_years or {}
    items = review_items if review_items is not None else []

    by_name: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        if obs.name.strip():
            key = N.name_key(obs.name)
            if key:
                by_name[key].append(obs)

    # One spelling can cover more than one child.
    clusters: dict[str, Cluster] = {}
    for key, group in by_name.items():
        for person_key, members in _partition_people(
            key, group, splits, season_years, items
        ).items():
            cluster_key = key if not person_key else f"{key}#{person_key}"
            clusters[cluster_key] = _make_cluster(cluster_key, members, person_key)

    union = _Union()
    for key in clusters:
        union.add(key)

    _merge_by_sweater(clusters, union, overrides)
    _merge_by_name_shape(clusters, union, overrides)
    _handle_nicknames(clusters, union, overrides, items)
    _apply_override_merges(clusters, union, overrides)

    merged: dict[str, Cluster] = {}
    for key, cluster in clusters.items():
        root = union.find(key)
        if root not in merged:
            merged[root] = Cluster(key=root, person_key=cluster.person_key)
        merged[root].absorb(cluster)
    return sorted(merged.values(), key=lambda c: c.key)


def _make_cluster(key: str, group: list[Observation], person_key: str) -> Cluster:
    cluster = Cluster(key=key, person_key=person_key)
    for obs in group:
        cluster.variants[obs.name] += 1
        cluster.appearances += 1
        cluster.seasons.add(obs.season_id)
        cluster.appearance_keys.add((obs.name, obs.season_id, obs.team_id))
        if obs.team_id is not None:
            cluster.team_seasons.add((obs.season_id, obs.team_id))
            if obs.jersey:
                cluster.sweaters.add(
                    (obs.season_id, obs.team_id, obs.jersey.lstrip("0") or "0")
                )
        if obs.division:
            cluster.divisions.add((obs.season_id, obs.division))
        cluster.goalie = cluster.goalie or obs.is_goalie
    return cluster


# ------------------------------------------------- same-name / same-person


def _partition_people(
    key: str,
    group: list[Observation],
    splits: dict[str, dict[tuple[int, Optional[int]], str]],
    season_years: dict[int, Optional[int]],
    items: list[Item],
) -> dict[str, list[Observation]]:
    """Split one spelling into the distinct children it covers.

    A manual decision in ``player_splits`` always wins. Otherwise the division
    each observation sits in implies a two-year birth window, and windows that
    cannot overlap mean two different children -- a child cannot be 10U and 16U
    in the same season.
    """
    manual = splits.get(key)
    if manual:
        assigned: dict[str, list[Observation]] = defaultdict(list)
        for obs in group:
            person = (
                manual.get((obs.season_id, obs.team_id))
                or manual.get((obs.season_id, None))
                or "a"
            )
            assigned[person].append(obs)
        # A map that puts everyone in one bucket means "these are one child".
        return {"": group} if len(assigned) == 1 else dict(assigned)

    # A player's birth window comes from the division they mainly played that
    # season. Secondary appearances -- a call-up, or a girl double-rostered on
    # a girls team as well as a co-ed one -- are extra teams for the same
    # child, not evidence of a second child.
    buckets = _bucket_by_birth_window(_primary_appearances(group), season_years)
    if len(buckets) < 2:
        _maybe_flag_double_roster(key, group, items)
        return {"": group}

    buckets.sort(key=lambda b: (-len(b["obs"]), b["window"] or (0, 0)))
    largest, *rest = buckets
    smallest = min(rest, key=lambda b: len(b["obs"]))

    display = N.best_display((o.name, 1) for o in group)
    detail = [
        f"{_window_label(b['window'])}: "
        f"{len(b['obs'])} season(s) "
        f"{sorted({o.season_id for o in b['obs']})}, "
        f"divisions {sorted({o.division for o in b['obs'] if o.division})}"
        for b in buckets
    ]
    # Say why the sides could not be one child, so the decision can be made
    # from the item alone.
    spread = _window_gap(largest["window"], smallest["window"])
    if spread:
        detail.append(
            f"{spread} year(s) apart -- further than a player can play up "
            f"({N.PLAY_UP_TOLERANCE} years, or "
            f"{TERMINAL_PLAY_UP_TOLERANCE} into 18U/19U)"
        )
    seasons_in_small = {o.season_id for o in smallest["obs"]}
    gap = _window_gap(largest["window"], smallest["window"])

    # Either side having enough presence is convincing; so is a birth-year gap
    # too wide for a call-up, however few games were played.
    strong = (
        len(smallest["obs"]) >= MIN_SPLIT_APPEARANCES
        and len(seasons_in_small) >= MIN_SPLIT_SEASONS
    ) or gap >= CERTAIN_SPLIT_YEAR_GAP

    if not strong:
        # Far more likely a call-up to an older division than a second child,
        # so keep them together -- but say so.
        items.append(Item(
            kind="same_name",
            subject=(f"{display}: division history implies two birth years, "
                     f"but the smaller side is only "
                     f"{len(smallest['obs'])} appearance(s)"),
            evidence={"names": [display], "detail": detail,
                      "seasons": sorted({o.season_id for o in group})},
            suggestion=("probably one child playing up a division -- "
                        "'split' if they are actually two people"),
            applied="kept as one player",
            confidence=0.35,
            parts=(key, "weak"),
        ))
        return {"": group}

    persons = {chr(ord("a") + i): b["obs"] for i, b in enumerate(buckets)}
    items.append(Item(
        kind="same_name",
        subject=f"{display}: one spelling covering {len(buckets)} different children",
        evidence={"names": [display], "detail": detail,
                  "seasons": sorted({o.season_id for o in group})},
        suggestion=("'merge' if this is really one player, or 'split' with an "
                    "explicit season map to change the grouping"),
        applied=f"split into {len(buckets)} players ({', '.join(persons)})",
        confidence=0.8,
        parts=(key, "split"),
    ))
    return persons


#: A division is a genuine second team, rather than a one-off call-up, when it
#: accounts for at least this share of the player's busiest division that
#: season (1/2 -- so 3 games against 15 is a call-up, 12 against 15 is not).
SECONDARY_SHARE = 2


def _primary_appearances(group: list[Observation]) -> list[Observation]:
    """Reduce a name's appearances to the teams that genuinely defined a season.

    One observation is kept per (season, division) that carried real weight.
    A handful of games in an older division -- a call-up -- is dropped, so it
    cannot masquerade as a second child; a full second roster (a girl playing
    both girls and co-ed) is kept, because it is a real part of her season.
    """
    by_season: dict[int, dict[str, list[Observation]]] = defaultdict(
        lambda: defaultdict(list))
    for obs in group:
        by_season[obs.season_id][obs.division].append(obs)

    kept: list[Observation] = []
    for season in sorted(by_season):
        divisions = by_season[season]
        busiest = max(len(v) for v in divisions.values())
        for division in sorted(divisions):
            members = divisions[division]
            if len(members) * SECONDARY_SHARE >= busiest:
                kept.append(members[0])
    return kept


def _maybe_flag_double_roster(
    key: str, group: list[Observation], items: list[Item]
) -> None:
    """Note players whose two teams in a season are worth a second look.

    Always kept as one player -- this is normal. Only the *notable* cases are
    raised: a girls team alongside a co-ed one, divisions more than one step
    apart, or two divisions the player genuinely played a season of.

    Being called up one age group for a couple of games is the most ordinary
    thing in youth hockey. Flagging those buries the real questions: on a
    three-team sample, doing so produced a review item for one player in five.
    """
    interesting: dict[int, set[tuple[str, str]]] = {}
    by_season: dict[int, Counter] = defaultdict(Counter)
    for obs in group:
        if obs.division:
            by_season[obs.season_id][(obs.division, obs.gender)] += 1

    for season, counts in by_season.items():
        divisions = set(counts)
        if len(divisions) < 2:
            continue
        ages = {a for a in (N.division_age(d) for d, _ in divisions) if a}
        genders = {g for _, g in divisions}
        # Divisions the player played a real part of, rather than visiting.
        regular_ages = {
            a for a in (
                N.division_age(division)
                for division, gender in divisions
                if counts[(division, gender)] >= MIN_SPLIT_APPEARANCES
            ) if a
        }

        if (
            len(genders) > 1                          # girls and co-ed
            or (ages and max(ages) - min(ages) > 2)   # more than one step apart
            or len(regular_ages) > 1                  # a genuine dual roster
        ):
            interesting[season] = divisions

    if not interesting:
        return

    display = N.best_display((o.name, 1) for o in group)
    detail = [
        f"S{season}: " + ", ".join(f"{d} ({g})" for d, g in sorted(divisions))
        for season, divisions in sorted(interesting.items())
    ]
    mixed = any(len({g for _, g in d}) > 1 for d in interesting.values())
    items.append(Item(
        kind="double_roster",
        subject=(f"{display}: played {'girls and co-ed teams' if mixed else 'two age groups'} "
                 f"in {'the same season' if len(interesting) == 1 else 'one or more seasons'}"),
        evidence={"names": [display], "detail": detail,
                  "seasons": sorted({o.season_id for o in group})},
        suggestion=("nothing to do if this is one child; 'split' with a season "
                    "map if it is actually two"),
        applied="kept as one player, with separate stats for each team",
        confidence=0.3,
        parts=(key, "double"),
    ))


#: Division ages with nothing above them. They carry a wide spread of ages
#: because there is nowhere else for an older teenager to play -- a 19U roster
#: routinely holds 15- to 19-year-olds, and in a sparse girls program it may be
#: the only team above 16U.
TERMINAL_DIVISION_AGES = (18, 19)

#: Play-up tolerance for those terminal divisions. Four years lets a 16U (and
#: even a 14U) player appear on a 19U roster, while still keeping 12U and below
#: out of it -- a 12-year-old on a 19U team would be a seven-year jump.
TERMINAL_PLAY_UP_TOLERANCE = 4


def _play_up_tolerance(division: str) -> int:
    """How far below a division's nominal age a player may plausibly sit."""
    if N.division_age(division) in TERMINAL_DIVISION_AGES:
        return TERMINAL_PLAY_UP_TOLERANCE
    return N.PLAY_UP_TOLERANCE


def _bucket_by_birth_window(
    group: list[Observation], season_years: dict[int, Optional[int]]
) -> list[dict]:
    """Greedily group observations into mutually compatible birth windows.

    Compatibility allows for playing up -- a child can appear on an older team,
    but never a younger one -- so each window is widened toward younger players
    by an amount that depends on the division, then intersected.

    Each bucket keeps two windows: ``wide`` decides compatibility, while
    ``window`` holds the strict intersection so the reported birth year stays as
    precise as the data allows.
    """
    buckets: list[dict] = []
    # Sorting keeps the outcome identical between runs, so player ids are stable.
    for obs in sorted(group, key=lambda o: (o.season_id, o.division, o.name)):
        window = N.birth_year_window(obs.division, season_years.get(obs.season_id) or 0)
        wide = N.widen_for_play_up(window, _play_up_tolerance(obs.division))

        for bucket in buckets:
            if window is None or bucket["wide"] is None:
                bucket["obs"].append(obs)
                bucket["window"] = bucket["window"] or window
                bucket["wide"] = bucket["wide"] or wide
                break
            combined = N.intersect_windows([bucket["wide"], wide])
            if combined:
                bucket["wide"] = combined
                bucket["window"] = (
                    N.intersect_windows([bucket["window"], window])
                    if bucket["window"] else window
                ) or bucket["window"] or window
                bucket["obs"].append(obs)
                break
        else:
            buckets.append({"window": window, "wide": wide, "obs": [obs]})
    return buckets


def _window_gap(a: Optional[tuple[int, int]], b: Optional[tuple[int, int]]) -> int:
    """Years between two birth windows; 0 when they touch or are unknown."""
    if not a or not b:
        return 0
    low, high = sorted((a, b))
    return max(0, high[0] - low[1])


def _window_label(window: Optional[tuple[int, int]]) -> str:
    if not window:
        return "birth year unknown"
    low, high = window
    return f"born {low}" if low == high else f"born {low}-{high}"


# ------------------------------------------------------------- merge rules


def _blocked(a: Cluster, b: Cluster, overrides: dict[str, dict]) -> bool:
    """True when an override explicitly forbids merging these clusters."""
    for cluster in (a, b):
        for variant in cluster.variants:
            rule = overrides.get(N.name_key(variant))
            if rule and rule.get("split"):
                return True
    return False


def _merge_by_sweater(
    clusters: dict[str, Cluster], union: _Union, overrides: dict[str, dict]
) -> None:
    """Merge spellings that shared a sweater on one team in one season.

    This is the strongest available evidence and needs no string similarity:
    ``ALEKSANDR RAZZHIGAEV`` and ``Aleksandr Razzhigaev`` wearing #19 for the
    same team in the same season are one player.
    """
    by_sweater: dict[tuple[int, int, str], list[str]] = defaultdict(list)
    for key, cluster in clusters.items():
        for sweater in cluster.sweaters:
            by_sweater[sweater].append(key)

    for keys in by_sweater.values():
        if len(keys) < 2:
            continue
        # A sweater can change hands mid-season, so the names must still look
        # like spellings of one name.
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if _blocked(clusters[a], clusters[b], overrides):
                    continue
                if _related(clusters[a], clusters[b]):
                    union.union(a, b)


def _related(a: Cluster, b: Cluster) -> bool:
    """True when two clusters' names look like spellings of the same name."""
    for name_a in a.variants:
        for name_b in b.variants:
            if N.core_key(name_a) == N.core_key(name_b):
                return True
            if N.base_key(name_a) == N.base_key(name_b):
                return True
            if N.is_nickname_variant(name_a, name_b):
                return True
    return False


def _merge_by_name_shape(
    clusters: dict[str, Cluster], union: _Union, overrides: dict[str, dict]
) -> None:
    """Merge middle-name and generational-suffix variants.

    Applied only when the two clusters never appear in the same season, or when
    they shared a team -- otherwise two different children with similar names
    (siblings, cousins, common names) would be conflated.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for key, cluster in clusters.items():
        representative = next(iter(cluster.variants))
        groups[N.core_key(representative)].append(key)

    for keys in groups.values():
        if len(keys) < 2:
            continue
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                ca, cb = clusters[a], clusters[b]
                if union.find(a) == union.find(b) or _blocked(ca, cb, overrides):
                    continue
                if not _name_shape_compatible(ca, cb):
                    continue
                shared_team = bool(ca.team_seasons & cb.team_seasons)
                overlap = ca.seasons & cb.seasons
                if shared_team or not overlap:
                    union.union(a, b)


def _name_shape_compatible(a: Cluster, b: Cluster) -> bool:
    for name_a in a.variants:
        for name_b in b.variants:
            if N.is_initial_variant(name_a, name_b):
                return True
            base_a, suffix_a = N.split_suffix(name_a)
            base_b, suffix_b = N.split_suffix(name_b)
            if N.fold(base_a) == N.fold(base_b) and suffix_a != suffix_b:
                return True
    return False


def _handle_nicknames(
    clusters: dict[str, Cluster],
    union: _Union,
    overrides: dict[str, dict],
    items: list[Item],
) -> None:
    """Deal with ``Bobby Smith`` / ``Robert Smith``.

    A nickname alone is never enough: ``Alex Chen`` and ``Alexander Chen`` may
    be two children. The pair is merged only when something else corroborates
    it -- a shared team-season, or a jersey they both wore. Otherwise it is
    raised for review and left unmerged, because a wrong merge is much harder
    to notice than a missing one.
    """
    by_surname: dict[str, list[str]] = defaultdict(list)
    for key, cluster in clusters.items():
        name = next(iter(cluster.variants))
        parts = N.fold(name).split()
        if len(parts) >= 2:
            by_surname[parts[-1]].append(key)

    for keys in by_surname.values():
        if len(keys) < 2:
            continue
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                ca, cb = clusters[a], clusters[b]
                if _blocked(ca, cb, overrides) or not _nickname_pair(ca, cb):
                    continue

                shared_team = ca.team_seasons & cb.team_seasons
                shared_sweater = {s[:2] for s in ca.sweaters} & {s[:2] for s in cb.sweaters}
                names = sorted({ca.display, cb.display})

                if union.find(a) == union.find(b):
                    # Already merged by a stronger rule (a shared sweater).
                    # Still worth surfacing: nicknames are the easiest way to
                    # combine two children by mistake.
                    items.append(Item(
                        kind="nickname",
                        subject=f"{names[0]} and {names[1]} treated as one player",
                        evidence={"names": names, "detail": [
                            "merged on shared sweater/team evidence, "
                            "not on the nickname alone",
                            f"shared team-season(s): {sorted(shared_team)}",
                        ]},
                        suggestion="'separate' if these are two different children",
                        applied="merged",
                        confidence=0.9,
                        parts=(ca.key, cb.key),
                    ))
                    continue

                if shared_team or shared_sweater:
                    union.union(a, b)
                    items.append(Item(
                        kind="nickname",
                        subject=f"{names[0]} and {names[1]} treated as one player",
                        evidence={"names": names, "detail": [
                            f"shared team-season(s): {sorted(shared_team)}"]},
                        suggestion="'separate' if these are two different children",
                        applied="merged",
                        confidence=0.75,
                        parts=(ca.key, cb.key),
                    ))
                else:
                    items.append(Item(
                        kind="nickname",
                        subject=f"{names[0]} and {names[1]} may be the same player",
                        evidence={"names": names, "detail": [
                            f"{ca.display}: seasons {sorted(ca.seasons)}, "
                            f"{ca.appearances} appearance(s)",
                            f"{cb.display}: seasons {sorted(cb.seasons)}, "
                            f"{cb.appearances} appearance(s)",
                            "never shared a team, so this was not merged",
                        ]},
                        suggestion="'merge' if one child, 'separate' to stop asking",
                        applied="kept separate",
                        confidence=0.4,
                        parts=(ca.key, cb.key),
                    ))


def _nickname_pair(a: Cluster, b: Cluster) -> bool:
    return any(
        N.is_nickname_variant(name_a, name_b)
        for name_a in a.variants for name_b in b.variants
    )


def _apply_override_merges(
    clusters: dict[str, Cluster], union: _Union, overrides: dict[str, dict]
) -> None:
    """Honour manual ``merge_into`` instructions, which outrank every rule."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for key, cluster in clusters.items():
        for variant in cluster.variants:
            by_name[N.name_key(variant)].append(key)

    for key, rule in overrides.items():
        target = rule.get("merge_into")
        if not target:
            continue
        target_key = N.name_key(target)
        for source in by_name.get(key, []):
            for destination in by_name.get(target_key, []):
                union.union(source, destination)


# ------------------------------------------------------------------ writing


def rebuild(conn: sqlite3.Connection, *, include_stat_rows: bool = True) -> dict[str, int]:
    """Rebuild player identities from stored rows. Idempotent."""
    observations = collect_observations(conn)
    if include_stat_rows:
        observations += collect_stat_observations(conn)

    season_years = {
        row["season_id"]: row["start_year"]
        for row in conn.execute("SELECT season_id, start_year FROM seasons")
    }
    items: list[Item] = []
    clusters = build_clusters(
        observations,
        overrides=review.load_overrides(conn),
        splits=review.load_splits(conn),
        season_years=season_years,
        review_items=items,
    )

    existing = {
        row["name"]: row["player_id"]
        for row in conn.execute("SELECT name, player_id FROM player_names")
    }

    timestamp = now()
    conn.execute("DELETE FROM player_names")
    conn.execute("DELETE FROM player_name_map")
    seen_ids: set[int] = set()
    #: name -> (appearances, player_id), to pick the primary player for a
    #: spelling that turned out to cover more than one child.
    primary: dict[str, tuple[int, int]] = {}

    for cluster in clusters:
        display = cluster.display
        canonical = cluster.key

        # Reuse an id already tied to any of these spellings so player_ids --
        # and anything exported from them -- stay stable between runs.
        player_id = _existing_id(conn, canonical)
        if player_id is None:
            player_id = next(
                (existing[v] for v in cluster.variants
                 if v in existing and existing[v] not in seen_ids),
                None,
            )

        window = _birth_window(cluster, season_years)
        low, high = window if window else (None, None)
        birth_year = low if window and low == high else None

        if player_id is None:
            cur = conn.execute(
                "INSERT INTO players(display_name, birth_year, birth_year_min, "
                "birth_year_max, created_at, canonical_name) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(canonical_name) DO UPDATE SET "
                "display_name = excluded.display_name, birth_year = excluded.birth_year, "
                "birth_year_min = excluded.birth_year_min, "
                "birth_year_max = excluded.birth_year_max",
                (display, birth_year, low, high, timestamp, canonical),
            )
            player_id = cur.lastrowid or _existing_id(conn, canonical)
        else:
            conn.execute(
                "UPDATE players SET display_name = ?, birth_year = ?, birth_year_min = ?, "
                "birth_year_max = ?, canonical_name = ? WHERE player_id = ?",
                (display, birth_year, low, high, canonical, player_id),
            )
        seen_ids.add(player_id)

        # Precise map first: spelling + season + team -> this child.
        conn.executemany(
            "INSERT INTO player_name_map(name, season_id, team_id, player_id) "
            "VALUES (?,?,?,?) ON CONFLICT(name, season_id, team_id) DO NOTHING",
            [(name, season, team, player_id)
             for name, season, team in sorted(
                 cluster.appearance_keys, key=lambda k: (k[0], k[1], k[2] or -1))],
        )

        # Then the coarse alias map, keeping whichever child wore the spelling
        # most often as its primary owner.
        for variant, count in cluster.variants.items():
            best = primary.get(variant)
            if best is None or count > best[0]:
                primary[variant] = (count, player_id)

    conn.executemany(
        "INSERT INTO player_names(name, player_id, seen) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET player_id = excluded.player_id, "
        "seen = excluded.seen",
        [(name, pid, count) for name, (count, pid) in sorted(primary.items())],
    )

    _link_rows(conn)
    _rebuild_team_seasons(conn)
    review.record(conn, items,
                  sweep=("same_name", "nickname", "double_roster", "initials"))
    conn.commit()

    return {
        "players": len(clusters),
        "names": sum(len(c.variants) for c in clusters),
        "observations": len(observations),
        "questions": len(items),
    }


def _birth_window(
    cluster: Cluster, season_years: dict[int, Optional[int]]
) -> Optional[tuple[int, int]]:
    """Infer a player's birth window from the divisions they played.

    The strict intersection is preferred, since it is the most precise. A player
    who played up has divisions that cannot all be satisfied at once, so the
    play-up tolerance is applied as a fallback rather than always -- which would
    needlessly widen the answer for everyone else.
    """
    pairs = [
        (division, N.birth_year_window(division, season_years.get(season) or 0))
        for season, division in cluster.divisions
    ]
    strict = N.intersect_windows(window for _, window in pairs)
    if strict:
        return strict

    # This player played up somewhere, so the nominal windows cannot all hold at
    # once. Widen exactly as the bucketing did -- including the larger band for
    # terminal 18U/19U divisions -- or a player the resolver deliberately kept
    # together ends up with no birth window at all.
    widened = N.intersect_windows(
        N.widen_for_play_up(window, _play_up_tolerance(division))
        for division, window in pairs
    )
    if widened is None:
        return None

    # The youngest division a player appeared in is the best evidence of their
    # actual age group -- they play up, not down -- so it caps the answer and
    # keeps it from drifting younger than the data supports.
    home = max((w for _, w in pairs if w), key=lambda w: w[0], default=None)
    return N.intersect_windows([widened, home]) or widened


def _existing_id(conn: sqlite3.Connection, canonical: str) -> Optional[int]:
    row = conn.execute(
        "SELECT player_id FROM players WHERE canonical_name = ?", (canonical,)
    ).fetchone()
    return row["player_id"] if row else None


def _link_rows(conn: sqlite3.Connection) -> None:
    """Point roster, goal, penalty and goalie rows at their resolved players.

    Names alone are not enough once one spelling can mean two children, so
    roster rows resolve through the player's own team-seasons where possible.
    """
    conn.execute("UPDATE game_rosters SET player_id = NULL")
    # Prefer the exact spelling+season+team match, so a shared name resolves to
    # the right child; fall back to season, then to the spelling alone.
    conn.execute("""
        UPDATE game_rosters
           SET player_id = (
               SELECT m.player_id
                 FROM player_name_map m, games g
                WHERE g.game_id  = game_rosters.game_id
                  AND m.name     = game_rosters.name
                  AND m.season_id = g.season_id
                  AND (m.team_id IS NULL OR m.team_id = CASE game_rosters.side
                          WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END)
                ORDER BY (m.team_id IS NULL)
                LIMIT 1
           )
    """)
    conn.execute("""
        UPDATE game_rosters
           SET player_id = (
               SELECT m.player_id FROM player_name_map m, games g
                WHERE g.game_id = game_rosters.game_id
                  AND m.name = game_rosters.name AND m.season_id = g.season_id
                LIMIT 1
           )
         WHERE player_id IS NULL
    """)
    conn.execute("""
        UPDATE game_rosters
           SET player_id = (
               SELECT n.player_id FROM player_names n WHERE n.name = game_rosters.name
           )
         WHERE player_id IS NULL
    """)
    conn.execute("""
        UPDATE goalie_stints
           SET player_id = (SELECT player_id FROM player_names p WHERE p.name = goalie_stints.name)
    """)

    # Goals and penalties reference jerseys, which only mean something within
    # one game and side -- resolve them through that game's roster.
    for column, source in (
        ("scorer_player_id", "scorer_jersey"),
        ("assist1_player_id", "assist1_jersey"),
        ("assist2_player_id", "assist2_jersey"),
    ):
        conn.execute(f"""
            UPDATE goals
               SET {column} = (
                   SELECT r.player_id FROM game_rosters r
                    WHERE r.game_id = goals.game_id
                      AND r.side    = goals.side
                      AND r.role    = 'player'
                      AND CAST(TRIM(r.jersey) AS INTEGER) = CAST(TRIM(goals.{source}) AS INTEGER)
                      AND TRIM(COALESCE(goals.{source}, '')) <> ''
                    LIMIT 1
               )
             WHERE TRIM(COALESCE({source}, '')) <> ''
        """)

    conn.execute("""
        UPDATE penalties
           SET player_id = (
               SELECT r.player_id FROM game_rosters r
                WHERE r.game_id = penalties.game_id
                  AND r.side    = penalties.side
                  AND r.role    = 'player'
                  AND CAST(TRIM(r.jersey) AS INTEGER) = CAST(TRIM(penalties.jersey) AS INTEGER)
                LIMIT 1
           )
         WHERE TRIM(COALESCE(jersey, '')) <> ''
    """)


def _rebuild_team_seasons(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM player_team_seasons")
    conn.execute("""
        INSERT INTO player_team_seasons(player_id, season_id, team_id, jersey, games, is_goalie)
        SELECT r.player_id,
               g.season_id,
               CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END,
               COALESCE(r.jersey, ''),
               COUNT(DISTINCT r.game_id),
               MAX(CASE WHEN UPPER(COALESCE(r.position,'')) = 'G' THEN 1 ELSE 0 END)
          FROM game_rosters r
          JOIN games g ON g.game_id = r.game_id
         WHERE r.player_id IS NOT NULL AND r.role = 'player'
           AND CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END IS NOT NULL
         GROUP BY r.player_id, g.season_id,
                  CASE r.side WHEN 'home' THEN g.home_team_id ELSE g.away_team_id END,
                  COALESCE(r.jersey, '')
    """)
