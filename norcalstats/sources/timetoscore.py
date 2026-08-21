"""Parsers for stats.caha.timetoscore.com.

Three page types matter:

``display-stats?league=3&season=N``
    The season index: a season ``<select>`` (the authoritative season-number ->
    label map) plus a collapsible table of divisions, each followed by its
    teams' standings rows.

``display-schedule?team=T&season=N&league=3&stat_class=1``
    One team's page: a Game Results table (every game with score and a link to
    its scoresheet) followed by the season-total skater and goalie tables.

``oss-scoresheet?game_id=G&mode=display``
    The full scoresheet: period scores, both rosters with jerseys and positions,
    goalie changes, every goal with assists, and every penalty.

Every parser takes HTML and returns plain dataclasses. Nothing here touches the
network or the database, so all of it is testable against saved fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from ..htmltable import Table, all_tables, clean, find_tables, to_int
from ..names import is_placeholder as _is_placeholder

#: Bumped when parsing changes in a way that should trigger a re-parse of
#: already-archived scoresheets.
PARSE_VERSION = 2

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Jersey column values that identify staff rather than players.
_STAFF_MARKERS = {"HC", "AC", "MGR", "MG", "TR", "M", "C"}


# ---------------------------------------------------------------- dataclasses


@dataclass
class SeasonRef:
    season_id: int
    label: str
    start_year: Optional[int]


@dataclass
class DivisionRef:
    name: str
    level: Optional[int] = None
    conf: Optional[int] = None
    sort_order: int = 0


@dataclass
class TeamRef:
    team_id: int
    name: str
    division: Optional[DivisionRef] = None
    standings: dict[str, object] = field(default_factory=dict)


@dataclass
class ScheduleGame:
    game_id: int
    date_text: str
    time_text: str
    rink: str
    league: str
    level: str
    away_name: str
    home_name: str
    away_goals: Optional[int]
    home_goals: Optional[int]
    game_type: str
    has_scoresheet: bool

    @property
    def is_final(self) -> bool:
        return self.away_goals is not None and self.home_goals is not None


@dataclass
class StatRow:
    kind: str            # skater | goalie
    row_index: int
    name: str
    jersey: str
    gp: Optional[int]
    data: dict[str, str]


@dataclass
class TeamPage:
    games: list[ScheduleGame] = field(default_factory=list)
    stat_rows: list[StatRow] = field(default_factory=list)


@dataclass
class RosterEntry:
    slot: int
    jersey: str
    position: str
    name: str
    role: str = "player"


@dataclass
class GoalEvent:
    seq: int
    period: str
    time_text: str
    time_sec: Optional[int]
    strength: str
    scorer: str
    assist1: str
    assist2: str


@dataclass
class PenaltyEvent:
    seq: int
    period: str
    jersey: str
    infraction: str
    minutes: Optional[float]
    off_ice: str
    start_time: str
    end_time: str
    on_ice: str


@dataclass
class GoalieStint:
    seq: int
    name: str
    note: str


@dataclass
class SideDetail:
    team_name: str = ""
    roster: list[RosterEntry] = field(default_factory=list)
    goals: list[GoalEvent] = field(default_factory=list)
    penalties: list[PenaltyEvent] = field(default_factory=list)
    goalies: list[GoalieStint] = field(default_factory=list)
    period_goals: dict[str, int] = field(default_factory=dict)
    final: Optional[int] = None
    shot_header: Optional[int] = None
    shot_marked: Optional[int] = None
    shot_goals: Optional[int] = None


@dataclass
class Scoresheet:
    game_id: Optional[int] = None
    date_iso: Optional[str] = None
    date_text: str = ""
    time_text: str = ""
    league: str = ""
    level: str = ""
    location: str = ""
    home: SideDetail = field(default_factory=SideDetail)
    away: SideDetail = field(default_factory=SideDetail)
    #: Non-fatal problems worth recording (score mismatches, missing sections).
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when at least one roster was found -- an empty sheet is not."""
        return bool(self.home.roster or self.away.roster)


# ------------------------------------------------------------------- URLs


def season_index_path(league: int, season: int) -> str:
    return f"/display-stats?league={league}&season={season}"


def team_path(league: int, season: int, team_id: int) -> str:
    return (f"/display-schedule?team={team_id}&season={season}"
            f"&league={league}&stat_class=1")


def scoresheet_path(game_id: int) -> str:
    return f"/oss-scoresheet?game_id={game_id}&mode=display"


# ------------------------------------------------------- season index parsing


def parse_season_list(html: str) -> list[SeasonRef]:
    """Season numbers and labels from the ``<select name='season'>`` control.

    This is the authoritative mapping. The ``season = year - 1994`` shortcut in
    the old README only holds for recent seasons (the site lists 18 = Fall 2016
    and 24 = Fall 2019), so the dropdown is read rather than computed.
    """
    block = re.search(
        r"<select[^>]*name\s*=\s*['\"]?season['\"]?[^>]*>(.*?)</select>",
        html, re.S | re.I,
    )
    if not block:
        return []

    seasons: list[SeasonRef] = []
    for value, label in re.findall(
        r"<option[^>]*value\s*=\s*[\"']?(-?\d+)[\"']?[^>]*>(.*?)</option>",
        block.group(1), re.S | re.I,
    ):
        season_id = int(value)
        text = clean(re.sub(r"<[^>]+>", " ", label))
        if season_id <= 0 or not text:
            continue  # "Current" and friends
        year = re.search(r"(19|20)\d{2}", text)
        seasons.append(SeasonRef(season_id, text, int(year.group()) if year else None))
    seasons.sort(key=lambda s: s.season_id)
    return seasons


def parse_current_season(html: str) -> Optional[int]:
    """The season number a "Current" (``season=0``) page actually refers to.

    A new season goes live before it is added to the season dropdown -- the
    2026-27 season was reachable as season 33, and linked to as such, while the
    dropdown still ended at 31. Every link on the page carries the real number,
    so it is read from there instead of waiting for the dropdown to catch up.
    """
    numbers = [
        int(m) for m in re.findall(r"[?&]season=(\d+)", html or "")
        if int(m) > 0
    ]
    if not numbers:
        return None
    # The page links overwhelmingly to its own season; take the most common.
    counts: dict[int, int] = {}
    for number in numbers:
        counts[number] = counts.get(number, 0) + 1
    return max(counts, key=lambda n: (counts[n], n))


def parse_league_name(html: str) -> str:
    """The league's own name, from the page's top-level header.

    ``"Norcal Schedule"`` -> ``"Norcal"``.

    The league header links to the whole league, while a division header adds a
    ``level=`` parameter -- so the two are told apart by the link rather than by
    position. Some leagues publish no header row at all, in which case the first
    heading is a *division* name ("12U A"); those return "" rather than
    mislabelling the league.
    """
    for match in re.finditer(
        r"<a[^>]+href=[\"']([^\"']*display-schedule\.php[^\"']*)[\"'][^>]*>(.*?)</a>",
        html or "", re.S | re.I,
    ):
        href, text = match.group(1), match.group(2)
        if "level=" in href:
            continue  # a division, not the league
        name = clean(re.sub(r"<[^>]+>", " ", text))
        name = re.sub(r"\s*Schedule\s*$", "", name, flags=re.I).strip()
        if name:
            return name
    return ""


def parse_season_index(html: str) -> list[TeamRef]:
    """Teams for one season, each attached to the division it appears under.

    Walks the collapsible table in document order: a row linking to
    ``display-schedule.php?...level=N`` starts a division section, and the team
    rows that follow belong to it. The site's ``data-id``/``data-parent``
    attributes are *not* used -- their ids are reused across sections (a given
    ``data-id`` shows up as both a section header and a child row), which is
    what made the original scraper drop divisions.
    """
    tables = all_tables(html)
    if not tables:
        return []
    # The season index is a single large collapsible table.
    table = max(tables, key=len)

    teams: list[TeamRef] = []
    current: Optional[DivisionRef] = None
    columns: dict[str, int] = {}
    order = 0
    seen: set[int] = set()

    for row in table.rows:
        links = row.links
        text = row.joined
        if not links and not text:
            continue

        # Each division is introduced by a standings header row. Its labels are
        # captured so team rows can be read by name rather than by offset --
        # the rows carry a leading toggle cell and the column set drifts
        # between seasons.
        labels = [c.text.strip().lower() for c in row.cells]
        if "team" in labels and "gp" in labels:
            columns = {label: i for i, label in enumerate(labels) if label}
            continue

        division_link = next(
            (h for h in links if "display-schedule.php" in h and "level=" in h), None
        )
        if division_link:
            name = re.sub(r"\s*Schedule\s*$", "", text).strip()
            # Skip the league-wide header ("Norcal Schedule").
            if name and not re.fullmatch(r"(?i)norcal", name):
                level = re.search(r"[?&]level=(\d+)", division_link)
                conf = re.search(r"[?&]conf=(\d+)", division_link)
                order += 1
                current = DivisionRef(
                    name=name,
                    level=int(level.group(1)) if level else None,
                    conf=int(conf.group(1)) if conf else None,
                    sort_order=order,
                )
            continue

        name_col = next(
            (i for i, cell in enumerate(row.cells)
             if any(re.search(r"display-schedule\?team=\d+", h) for h in cell.links)),
            None,
        )
        if name_col is None:
            continue
        team_link = next(
            h for h in row.cells[name_col].links
            if re.search(r"display-schedule\?team=\d+", h)
        )
        team_id = int(re.search(r"team=(\d+)", team_link).group(1))
        if team_id in seen:
            continue
        seen.add(team_id)

        teams.append(TeamRef(
            team_id=team_id,
            name=row.text(name_col),
            division=current,
            standings=_standings(row, columns),
        ))
    return teams


#: Standings header label -> column name in the ``standings`` table.
_STANDINGS_COLUMNS = {
    "gp": "gp", "w": "w", "l": "l", "t": "t", "otl": "otl",
    "gf": "gf", "ga": "ga", "+/-": "diff", "pts": "pts",
}


def _standings(row, columns: dict[str, int]) -> dict[str, object]:
    """Standings for a team row, read by header label."""
    if not columns:
        return {}
    out: dict[str, object] = {}
    for label, key in _STANDINGS_COLUMNS.items():
        i = columns.get(label)
        text = row.text(i) if i is not None else ""
        out[key] = to_int(text) if re.search(r"-?\d", text or "") else None
    pct_col = next((i for label, i in columns.items() if "gf+ga" in label), None)
    pct = re.search(r"-?\d*\.?\d+", row.text(pct_col) if pct_col is not None else "")
    out["pct"] = float(pct.group()) if pct else None
    return out


# --------------------------------------------------------- team page parsing


def parse_team_page(html: str) -> TeamPage:
    """Game results plus the site's season-total stat tables for one team."""
    tables = all_tables(html)
    page = TeamPage()

    results = _find_first(tables, ("game results",), ("game", "date", "away", "home"))
    if results is not None:
        page.games = _parse_game_results(results)

    skaters = _find_first(tables, ("player stats",), ("name", "gp", "goals", "ass."))
    if skaters is not None:
        page.stat_rows.extend(_parse_stat_table(skaters, "skater"))

    goalies = _find_first(tables, ("goalie stats",), ("name", "gp", "shots", "gaa"))
    if goalies is not None:
        page.stat_rows.extend(_parse_stat_table(goalies, "goalie"))

    return page


def _find_first(tables: list[Table], *needle_sets: tuple[str, ...]) -> Optional[Table]:
    for needles in needle_sets:
        found = find_tables(tables, *needles, rows=3)
        if found:
            return found[0]
    return None


def _parse_game_results(table: Table) -> list[ScheduleGame]:
    header, start = _header_row(table, "game", "date")
    if header is None:
        return []
    index = _column_index(header)

    games: list[ScheduleGame] = []
    for row in table.rows[start:]:
        if len(row) < 6 or all(c.is_header for c in row.cells):
            continue
        raw_id = row.text(index.get("game", 0))
        links = row.links

        # The printed id is only a label, and some leagues prefix it:
        # SCAHA shows "SCAHA-10000983*" for what is really game 28592. The
        # scoresheet link carries the true id, so it wins wherever present.
        linked = next(
            (m.group(1) for m in
             (re.search(r"game_id=(\d+)", h) for h in links) if m),
            None,
        )
        match = re.search(r"\d+", raw_id)
        if linked:
            game_id = int(linked)
        elif match:
            game_id = int(match.group())
        else:
            continue

        # A trailing '*' on the id, or a scoresheet link, means detail exists.
        has_sheet = "*" in raw_id or any("oss-scoresheet" in h for h in links)
        games.append(ScheduleGame(
            game_id=game_id,
            date_text=row.text(index.get("date", 1)),
            time_text=row.text(index.get("time", 2)),
            rink=row.text(index.get("rink", 3)),
            league=row.text(index.get("league", 4)),
            level=row.text(index.get("level", 5)),
            away_name=row.text(index.get("away", 6)),
            away_goals=_opt_int(row.text(index.get("away_goals", 7))),
            home_name=row.text(index.get("home", 8)),
            home_goals=_opt_int(row.text(index.get("home_goals", 9))),
            game_type=row.text(index.get("type", 10)),
            has_scoresheet=has_sheet,
        ))
    return games


def _column_index(header) -> dict[str, int]:
    """Map header labels to column positions.

    ``Goals`` appears twice (after Away and after Home); they become
    ``away_goals`` and ``home_goals``.
    """
    index: dict[str, int] = {}
    last_team: Optional[str] = None
    for i, cell in enumerate(header.cells):
        label = cell.text.strip().lower()
        if label in ("away", "visitor"):
            index["away"] = i
            last_team = "away"
        elif label == "home":
            index["home"] = i
            last_team = "home"
        elif label == "goals" and last_team:
            index[f"{last_team}_goals"] = i
        elif label:
            index.setdefault(label, i)
    return index


def _header_row(table: Table, *needles: str) -> tuple[Optional[object], int]:
    """Locate the header row containing every needle; return it and the next index."""
    for i, row in enumerate(table.rows[:5]):
        lowered = [c.text.strip().lower() for c in row.cells]
        if all(any(n == cell for cell in lowered) for n in needles):
            return row, i + 1
    return None, 0


def _parse_stat_table(table: Table, kind: str) -> list[StatRow]:
    """Season totals as published by the site, kept verbatim for cross-checks."""
    header, start = _header_row(table, "name", "gp")
    if header is None:
        return []
    labels = [c.text.strip() for c in header.cells]

    rows: list[StatRow] = []
    for i, row in enumerate(table.rows[start:]):
        if len(row) < 3 or all(c.is_header for c in row.cells):
            continue
        name = row.text(0)
        if not name or name.lower() == "name":
            continue
        data = {
            (labels[j] if j < len(labels) and labels[j] else f"col{j}"): cell.text
            for j, cell in enumerate(row.cells)
        }
        rows.append(StatRow(
            kind=kind,
            row_index=i,
            name=name,
            jersey=row.text(1),
            gp=_opt_int(row.text(2)),
            data=data,
        ))
    return rows


# --------------------------------------------------------- scoresheet parsing


def parse_scoresheet(html: str, game_id: Optional[int] = None) -> Scoresheet:
    """Parse a full scoresheet.

    Sides are identified by content rather than position: roster tables carry a
    ``"<Team> Players in Game <id>"`` caption, and the period-score table labels
    its rows ``Visitor``/``Home``. Scoring and penalty tables have no captions,
    so they are matched in document order (visitor first) and then validated
    against each side's final score.
    """
    sheet = Scoresheet(game_id=game_id)
    tables = all_tables(html)
    if not tables:
        sheet.warnings.append("no tables found")
        return sheet

    _parse_game_info(tables, sheet)
    _parse_period_scores(tables, sheet)
    _parse_rosters(tables, sheet)
    _parse_goalie_changes(tables, sheet)
    _parse_shot_grid(tables, sheet)
    _parse_events(tables, sheet)
    _validate(sheet)
    return sheet


def _parse_game_info(tables: list[Table], sheet: Scoresheet) -> None:
    info = next((t for t in tables if "date:" in t.head(4).lower()), None)
    if info is None:
        return
    for row in info.rows:
        for text in row.texts:
            key, _, value = text.partition(":")
            key, value = key.strip().lower(), value.strip()
            if not value:
                continue
            if key == "date":
                sheet.date_text = value
                sheet.date_iso = _parse_sheet_date(value)
            elif key == "time":
                sheet.time_text = value
            elif key == "league":
                sheet.league = value
            elif key == "level":
                sheet.level = value
            elif key == "location":
                sheet.location = value


def _parse_period_scores(tables: list[Table], sheet: Scoresheet) -> None:
    table = next((t for t in tables if "team name" in t.head(1).lower()), None)
    if table is None:
        sheet.warnings.append("no period-score table")
        return

    header = table.rows[0]
    # 'Team Name' spans two columns, so data rows carry one extra leading cell.
    labels: list[str] = []
    for cell in header.cells:
        labels.extend([cell.text.strip()] * max(1, cell.colspan))

    for row in table.rows[1:]:
        side_label = row.text(0).strip().lower()
        if side_label == "visitor":
            side = sheet.away
        elif side_label == "home":
            side = sheet.home
        else:
            continue
        side.team_name = row.text(1)
        for i, label in enumerate(labels):
            if i < 2 or i >= len(row):
                continue
            value = row.text(i)
            if not value:
                continue
            if label.lower() == "final":
                side.final = to_int(value, 0)
            elif label and label.lower() not in ("timeout", "team name"):
                side.period_goals[label] = to_int(value, 0)


def _parse_rosters(tables: list[Table], sheet: Scoresheet) -> None:
    """Rosters, matched to sides by the team name in each caption."""
    captions = [t for t in tables if "players in game" in t.head(2).lower()]
    for order, caption in enumerate(captions):
        text = caption.head(2)
        match = re.search(r"(.*?)\s+Players in game", text, re.I)
        team_name = clean(match.group(1)) if match else ""

        grid = next(
            (t for t in caption.descendants()
             if "name" in t.head(1).lower() and "#" in t.head(1)),
            None,
        )
        if grid is None:
            continue

        side = _match_side(sheet, team_name, order)
        if side is None:
            continue
        if team_name and not side.team_name:
            side.team_name = team_name
        side.roster = _parse_roster_grid(grid)


def _match_side(sheet: Scoresheet, team_name: str, order: int) -> Optional[SideDetail]:
    """Pick the side a roster belongs to, by name when possible."""
    key = team_name.strip().lower()
    away_name = sheet.away.team_name.strip().lower()
    home_name = sheet.home.team_name.strip().lower()
    if key and away_name != home_name:
        if key == away_name:
            return sheet.away
        if key == home_name:
            return sheet.home
    # Fall back to document order: visitor is always rendered first.
    return sheet.away if order == 0 else sheet.home


def _parse_roster_grid(table: Table) -> list[RosterEntry]:
    """Roster grid: two ``# / P / Name`` player triples per row."""
    entries: list[RosterEntry] = []
    slot = 0
    for row in table.rows:
        cells = row.texts
        if not cells or all(c.is_header for c in row.cells):
            continue
        for start in range(0, len(cells), 3):
            group = cells[start:start + 3]
            if len(group) < 3:
                continue
            jersey, position, name = (g.strip() for g in group)
            if not name or name.lower() == "name":
                continue
            if jersey.upper() in _STAFF_MARKERS:
                role = "coach"
            elif _is_placeholder(name):
                # "Not Signed In", "Home Unknown Goalie 1": the sheet exists but
                # the roster does not. Kept so the gap is visible, but never
                # treated as a person.
                role = "placeholder"
            else:
                role = "player"
            entries.append(RosterEntry(
                slot=slot, jersey=jersey, position=position.upper(),
                name=name, role=role,
            ))
            slot += 1
    return entries


def _parse_goalie_changes(tables: list[Table], sheet: Scoresheet) -> None:
    """The 'Home Goalie Changes' / 'Visitor Changes' block."""
    table = next((t for t in tables if "goalie changes" in t.text.lower()), None)
    if table is None:
        return

    side: Optional[SideDetail] = None
    counters = {"home": 0, "away": 0}
    for row in table.rows:
        text = row.joined.strip()
        lowered = text.lower()
        if not text or lowered == "comments":
            continue
        if "home" in lowered and "change" in lowered:
            side = sheet.home
            continue
        if ("visitor" in lowered or "away" in lowered) and "change" in lowered:
            side = sheet.away
            continue
        if side is None:
            continue

        name, note = text, ""
        for marker in ("Starting", "Start", "Period", "period", "In", "Out"):
            pos = text.find(marker)
            if pos > 0:
                name, note = text[:pos].strip(), text[pos:].strip()
                break
        which = "home" if side is sheet.home else "away"
        side.goalies.append(GoalieStint(seq=counters[which], name=name, note=note))
        counters[which] += 1


def _parse_shot_grid(tables: list[Table], sheet: Scoresheet) -> None:
    """Shot-tracking grid counts.

    Recorded for completeness but flagged unreliable: scorekeepers routinely
    leave it half-filled (a 19-goal game showing two marked goals), so it is
    never used as a stats source.
    """
    table = next((t for t in tables if re.search(r"saves\s*:", t.head(2), re.I)), None)
    if table is None:
        return

    for row in table.rows:
        label = row.text(0)
        match = re.search(r"(home|visitor|away)\s*saves\s*:\s*(\d+)", label, re.I)
        if not match:
            continue
        # "Home Saves" counts the shots the HOME goalie faced.
        side = sheet.home if match.group(1).lower() == "home" else sheet.away
        side.shot_header = int(match.group(2))

        grid = next((t for cell in row.cells for t in cell.tables), None)
        if grid is None:
            continue
        marked = goals = 0
        for grid_row in grid.rows:
            for cell in grid_row.cells:
                colour = cell.attrs.get("bgcolor", "").strip().lower()
                if not colour:
                    continue  # uncoloured cell = shot never taken
                marked += 1
                # Grey (#BBBBBB) marks a save; a red/pink fill marks a goal.
                if not colour.startswith("#b"):
                    goals += 1
        side.shot_marked = marked
        side.shot_goals = goals


def _parse_events(tables: list[Table], sheet: Scoresheet) -> None:
    """Scoring and penalty tables, assigned to sides by score agreement."""
    scoring = [t for t in tables if t.head(1).strip().lower().startswith("scoring")]
    penalties = [t for t in tables if t.head(1).strip().lower().startswith("penalties")]

    parsed_goals = [_parse_scoring(t) for t in scoring]
    parsed_pens = [_parse_penalties(t) for t in penalties]

    away_first = _visitor_listed_first(sheet, parsed_goals)
    order = [sheet.away, sheet.home] if away_first else [sheet.home, sheet.away]

    for side, goals in zip(order, parsed_goals):
        side.goals = goals
    for side, pens in zip(order, parsed_pens):
        side.penalties = pens

    if len(parsed_goals) > 2 or len(parsed_pens) > 2:
        sheet.warnings.append(
            f"unexpected section count: {len(parsed_goals)} scoring, "
            f"{len(parsed_pens)} penalty tables"
        )


def _visitor_listed_first(sheet: Scoresheet, parsed: list[list[GoalEvent]]) -> bool:
    """Decide which side each scoring table belongs to.

    The visitor's table is rendered first, but rather than trust layout alone we
    check the goal counts against each side's final score and only fall back to
    document order when the scores cannot distinguish them.
    """
    if len(parsed) < 2:
        return True
    away, home = sheet.away.final, sheet.home.final
    if away is None or home is None or away == home:
        return True
    first, second = len(parsed[0]), len(parsed[1])
    if first == away and second == home:
        return True
    if first == home and second == away:
        return False
    return True


def _parse_scoring(table: Table) -> list[GoalEvent]:
    header, start = _header_row(table, "per", "goal")
    if header is None:
        return []
    goals: list[GoalEvent] = []
    for row in table.rows[start:]:
        if len(row) < 4 or all(c.is_header for c in row.cells):
            continue
        period = row.text(0).strip()
        if not period:
            continue
        scorer = row.text(3).strip()
        if not scorer:
            continue
        time_text = row.text(1).strip()
        goals.append(GoalEvent(
            seq=len(goals),
            period=period,
            time_text=time_text,
            time_sec=_parse_clock(time_text),
            strength=row.text(2).strip(),
            scorer=scorer,
            assist1=row.text(4).strip(),
            assist2=row.text(5).strip(),
        ))
    return goals


def _parse_penalties(table: Table) -> list[PenaltyEvent]:
    header, start = _header_row(table, "per", "infraction")
    if header is None:
        return []
    events: list[PenaltyEvent] = []
    for row in table.rows[start:]:
        if len(row) < 4 or all(c.is_header for c in row.cells):
            continue
        period = row.text(0).strip()
        infraction = row.text(2).strip()
        if not period or not infraction:
            continue
        minutes = re.search(r"\d*\.?\d+", row.text(3))
        events.append(PenaltyEvent(
            seq=len(events),
            period=period,
            jersey=row.text(1).strip(),
            infraction=infraction,
            minutes=float(minutes.group()) if minutes else None,
            off_ice=row.text(4).strip(),
            start_time=row.text(5).strip(),
            end_time=row.text(6).strip(),
            on_ice=row.text(7).strip(),
        ))
    return events


def _validate(sheet: Scoresheet) -> None:
    """Cross-check parsed detail against the sheet's own summary numbers."""
    for name, side in (("away", sheet.away), ("home", sheet.home)):
        if side.final is None:
            continue
        if side.goals and len(side.goals) != side.final:
            sheet.warnings.append(
                f"{name}: {len(side.goals)} scoring lines vs final {side.final}"
            )
        periods = sum(side.period_goals.values())
        if side.period_goals and periods != side.final:
            sheet.warnings.append(
                f"{name}: period goals {periods} vs final {side.final}"
            )


# ------------------------------------------------------------------ helpers


#: Printed game-type label -> normalized class. The site numbers regular-season
#: games individually ("Regular 1" ... "Regular 15"), and only those count
#: toward the season totals and standings it publishes.
_GAME_CLASS_PATTERNS = [
    (re.compile(r"^\s*regular\b", re.I), "regular"),
    (re.compile(r"^\s*(pre[\s-]?season)\b", re.I), "preseason"),
    (re.compile(r"^\s*(exhibition|scrimmage|friendly)\b", re.I), "exhibition"),
    (re.compile(r"(playoff|championship|round\s*robin|semi|final|consolation|"
                r"quarter|crossover|placement)", re.I), "playoff"),
]


def classify_game_type(game_type: str) -> str:
    """Bucket a printed game-type label. Unknown labels become ``'other'``."""
    text = (game_type or "").strip()
    if not text:
        return "other"
    for pattern, label in _GAME_CLASS_PATTERNS:
        if pattern.search(text):
            return label
    return "other"


def _opt_int(text: str) -> Optional[int]:
    """Int value of a cell, or None when the cell is blank (game not played)."""
    text = (text or "").strip()
    if not text or not re.search(r"\d", text):
        return None
    return to_int(text)


def _parse_clock(text: str) -> Optional[int]:
    """Game clock to seconds. Handles ``"9:10"`` and the site's ``"48.6"``."""
    text = (text or "").strip()
    if not text:
        return None
    if ":" in text:
        minutes, _, seconds = text.partition(":")
        try:
            return int(float(minutes)) * 60 + int(float(seconds))
        except ValueError:
            return None
    try:
        return int(float(text))  # under a minute, printed as seconds.tenths
    except ValueError:
        return None


def _parse_sheet_date(text: str) -> Optional[str]:
    """``"08-29-25"`` -> ``"2025-08-29"``."""
    match = re.match(r"(\d{1,2})-(\d{1,2})-(\d{2,4})", text.strip())
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def schedule_date_to_iso(text: str, start_year: Optional[int]) -> Optional[str]:
    """``"Fri Aug 29"`` + season start year -> ISO date.

    A season spans two calendar years, so months from August onward belong to
    the start year and January onward to the next.
    """
    if not start_year:
        return None
    match = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})", text or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    year = start_year if month >= 8 else start_year + 1
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def iter_jerseys(entries: Iterable[RosterEntry]) -> dict[str, RosterEntry]:
    """Jersey -> roster entry, for resolving event jerseys to players.

    Leading zeros are inconsistent between the roster and the event tables
    (``"06"`` vs ``"6"``), so both spellings are indexed.
    """
    index: dict[str, RosterEntry] = {}
    for entry in entries:
        if entry.role != "player" or not entry.jersey:
            continue
        index.setdefault(entry.jersey, entry)
        index.setdefault(entry.jersey.lstrip("0") or "0", entry)
    return index
