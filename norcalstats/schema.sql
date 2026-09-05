-- NorCal youth hockey stats database.
--
-- Design notes
-- ------------
-- * Natural keys come from timetoscore.com: season numbers, team ids and game
--   ids are the site's own. Team ids are reused across seasons with different
--   divisions, so teams are keyed on (team_id, season_id).
-- * Jerseys are TEXT: the site emits leading zeros ("06") and coach markers
--   ("HC", "AC") in the same column.
-- * Everything scraped is stored as recorded. Derived values (player identity,
--   per-game stat lines, season totals) live in separate tables that can be
--   rebuilt from the raw rows without refetching anything.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- structure

CREATE TABLE IF NOT EXISTS seasons (
    season_id       INTEGER PRIMARY KEY,   -- site season number (27, 28, ... 32)
    label           TEXT NOT NULL,         -- "Fall 2025"
    start_year      INTEGER,               -- 2025
    first_seen_at   TEXT NOT NULL,
    last_scanned_at TEXT,
    complete        INTEGER NOT NULL DEFAULT 0  -- 1 = no longer changing
);

-- timetoscore hosts several leagues side by side: Norcal travel (3), SCAHA
-- (4), CAHA tier 1/tier 2 (5) and various tournaments. They share team ids and
-- season numbers but have their own divisions, and their division *names*
-- collide -- Norcal and SCAHA both run a "12U A" -- so league is part of a
-- division's identity.
CREATE TABLE IF NOT EXISTS leagues (
    league_id    INTEGER PRIMARY KEY,      -- site league id
    name         TEXT,                     -- "Norcal", "CAHA", "CAHA Playoffs"
    priority     INTEGER NOT NULL DEFAULT 100,  -- lower wins when teams overlap
    -- What this league is, which decides whether it is collected:
    --   season   a competition running across a season  -> collected
    --   event    a single weekend or tournament         -> skipped
    --   excluded deliberately not wanted                -> skipped
    --   unknown  newly discovered                       -> skipped, and asked about
    kind         TEXT NOT NULL DEFAULT 'unknown',
    --: The competition this league is a round of, where it is one. CAHA runs
    --: four ids on the site -- the main league (5), preseason (16), weekends
    --: (17) and playoffs (24) -- which are one competition to everybody at the
    --: rink. The children point at the parent, and everywhere a reader picks a
    --: league they see the parent alone.
    parent_id    INTEGER REFERENCES leagues(league_id),
    --: What to call this round once its games are shown under the parent. This
    --: is the part the roll-up would otherwise lose, so it rides on the game.
    stage        TEXT,
    --: Longest gap in days between a league's games, used to tell a season
    --: from a weekend event when classifying automatically.
    span_days    INTEGER,
    note         TEXT,
    first_seen_at   TEXT,
    last_scanned_at TEXT
);

-- Which leagues carried games in which season. Leagues come and go *during* a
-- season -- CAHA runs separate ids for preseason (16), weekends (17), the main
-- league (5) and playoffs (24) -- so this is rediscovered rather than assumed.
CREATE TABLE IF NOT EXISTS league_seasons (
    league_id     INTEGER NOT NULL REFERENCES leagues(league_id),
    season_id     INTEGER NOT NULL,
    teams         INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT,
    PRIMARY KEY (league_id, season_id)
);

-- Seeded so the default league_id below always resolves, and so the ids are
-- documented in the database itself. Names are refreshed from the site on the
-- first scan of each league.
-- Known leagues and what to do with them. Priority decides which league owns a
-- team's name and division when it plays in several at once, so the season-long
-- leagues rank above the preseason and playoff ids of the same competition.
--
-- The CAHA family is marked 'season' explicitly: its preseason and playoff
-- rounds are short, and would otherwise look like weekend tournaments to the
-- automatic classifier, even though they are part of the tier competition.
INSERT OR IGNORE INTO leagues (league_id, name, priority, kind, note) VALUES
    (3,  'Norcal',            0, 'season',   'travel: A / B / BB'),
    (4,  'SCAHA',             2, 'season',   'Southern California'),
    (5,  'CAHA',              1, 'season',   'tier 1 and tier 2: AA / AAA'),
    (16, 'CAHA Preseason',   10, 'season',   'tier competition, preseason round'),
    (17, 'CAHA Weekends',    11, 'season',   'tier competition, weekend round'),
    (24, 'CAHA Playoffs',    12, 'season',   'tier competition, playoffs'),
    (34, 'PGHL',              3, 'season',   'Pacific Girls: Girls 12/14/16/19 AA and AAA'),
    -- High school leagues: not wanted.
    (15, 'ADHSHL',          200, 'excluded', 'Anaheim Ducks high school league'),
    (26, 'LA Kings High School', 201, 'excluded', 'high school'),
    (27, 'SHSHL',           202, 'excluded', 'San Jose Sharks high school league'),
    (28, 'High School Exhibition', 203, 'excluded', 'high school'),
    (23, 'ACHA',            204, 'excluded', 'college hockey, not youth'),
    (36, 'ACHA MD2',        206, 'excluded', 'college hockey, not youth'),
    -- National and regional championships. They are the playoff progression
    -- for tier teams, but the field is drawn from the whole country: 47 of the
    -- clubs in them are Alaska, Boston, Buffalo, Chicago, Cleveland, Colorado
    -- and the like. A California team reaching one is incidental, so there is
    -- no season-long record here worth keeping.
    (37, 'Pacific District', 207, 'excluded', 'regional championship, out-of-state field'),
    (38, 'USAH Nationals',   208, 'excluded', 'national championship, out-of-state field'),
    -- A catch-all bucket for games played at out-of-area tournaments. It runs
    -- all season, so its date span looks like a league's, but every game in it
    -- is labelled "Tournament".
    (19, 'Out-of-area tournaments', 205, 'event', 'bucket for tournament games'),
    -- Single-weekend tournaments: no season-long record to keep.
    (6,  'KHS Thanksgiving',    210, 'event', 'tournament'),
    (8,  'Wine Country Face Off', 211, 'event', 'tournament'),
    (10, 'KHS Labor Day',       212, 'event', 'tournament'),
    (11, 'KHS Christmas',       213, 'event', 'tournament'),
    (13, 'Lake Tahoe MLK',      214, 'event', 'tournament'),
    (18, 'Silver Stick',        215, 'event', 'tournament'),
    (21, 'TVMHA Pure Hockey',   216, 'event', 'tournament'),
    (25, 'Roseville Elite',     217, 'event', 'tournament'),
    (32, 'BCP Holiday Invtl',   218, 'event', 'tournament'),
    (33, 'TSC Pres Day',        219, 'event', 'tournament'),
    (35, 'San Jose Labor Day',  220, 'event', 'tournament'),
    (39, 'One Hockey',          222, 'event', 'tournament'),
    -- Mid-season MLK holiday weekend (Sat-Mon), with an out-of-area field.
    -- Its "Championship" and "Consolation" games are the tournament's own
    -- bracket, not a league playoff.
    (40, 'MLK Weekend Tournament', 223, 'event', 'mid-season holiday tournament'),
    -- The California Dreamin' Labor Day Festival: a SoCal preseason tournament,
    -- so the automatic classifier reads it as an 'event' and would skip it. Its
    -- field is almost entirely California clubs already tracked in SCAHA and
    -- CAHA, so it is collected by hand. Priority 230 keeps it below every season
    -- league -- it never owns a team's name or division -- and it is left out of
    -- clubs.HOME_LEAGUES on purpose, so an out-of-state entrant that plays
    -- nowhere else stays a visitor rather than becoming a browsable club. Only
    -- S33 carries games under this id; earlier seasons are empty.
    (41, 'California Dreamin Labor Day Festival', 230, 'season', 'SoCal Labor Day tournament, collected by hand');
-- The CAHA family is four ids for one competition, and the rounds point at the
-- main league. That is applied in db.py rather than here: parent_id and stage
-- arrive through ADDED_COLUMNS, which runs *after* this script, so a database
-- created before those columns existed has no parent_id for this file to set.

CREATE TABLE IF NOT EXISTS divisions (
    division_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id   INTEGER NOT NULL REFERENCES seasons(season_id),
    league_id   INTEGER NOT NULL DEFAULT 3 REFERENCES leagues(league_id),
    name        TEXT NOT NULL,             -- "10U A"
    level       INTEGER,                   -- site level id from the section link
    conf        INTEGER,
    sort_order  INTEGER,
    gender      TEXT,                      -- girls | coed
    UNIQUE (season_id, league_id, name)
);

-- Clubs are derived, never fetched: the site names teams, not clubs, and it
-- spells them differently between seasons. One row per club, holding the name
-- everything groups by, the shorter name the app shows, and what the thing
-- actually is -- a third of the names the site prints are playoff bracket
-- slots, high schools, or teams from out of the area that only ever appear as
-- somebody's opponent. All of those keep their names, because schedules and
-- box scores need them; none belongs in a list of clubs to browse.
CREATE TABLE IF NOT EXISTS clubs (
    name       TEXT PRIMARY KEY,           -- canonical, what teams.club holds
    short_name TEXT NOT NULL,              -- "SJ Jr. Sharks"
    kind       TEXT NOT NULL DEFAULT 'club'  -- club | visitor | high_school | bracket
);

CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER NOT NULL,          -- site team id
    season_id   INTEGER NOT NULL REFERENCES seasons(season_id),
    name        TEXT NOT NULL,             -- "San Jose Jr Sharks"
    club        TEXT,                      -- canonical club, derived
    division_id INTEGER REFERENCES divisions(division_id),
    -- Two teams from one club can share a division; this disambiguates them
    -- for display ("San Jose Jr Sharks 10U A-2").
    club_seq    INTEGER NOT NULL DEFAULT 1,
    -- Girls play either in a dedicated girls division or as a girls team
    -- entered in an otherwise co-ed one, so this is tracked per team.
    gender      TEXT,                      -- girls | coed
    -- The league this team primarily plays in. Team ids are global, so one
    -- team can also appear in a tournament league; team_leagues records all of
    -- them and the lowest-priority league owns these columns.
    league_id   INTEGER DEFAULT 3 REFERENCES leagues(league_id),
    first_seen_at TEXT,
    PRIMARY KEY (team_id, season_id)
);

-- Every league a team appeared in for a season, with the name and division it
-- used there. A Norcal travel team also entered in a tournament has two rows.
CREATE TABLE IF NOT EXISTS team_leagues (
    team_id     INTEGER NOT NULL,
    season_id   INTEGER NOT NULL,
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id),
    division_id INTEGER REFERENCES divisions(division_id),
    name        TEXT,
    PRIMARY KEY (team_id, season_id, league_id)
);

CREATE INDEX IF NOT EXISTS idx_team_leagues_div ON team_leagues(division_id);

CREATE INDEX IF NOT EXISTS idx_teams_season   ON teams(season_id);
CREATE INDEX IF NOT EXISTS idx_teams_division ON teams(division_id);
CREATE INDEX IF NOT EXISTS idx_teams_club     ON teams(club);

CREATE TABLE IF NOT EXISTS standings (
    season_id  INTEGER NOT NULL,
    team_id    INTEGER NOT NULL,
    gp INTEGER, w INTEGER, l INTEGER, t INTEGER, otl INTEGER,
    gf INTEGER, ga INTEGER, diff INTEGER, pct REAL, pts INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, season_id),
    FOREIGN KEY (team_id, season_id) REFERENCES teams(team_id, season_id)
);

-- -------------------------------------------------------------------- games

CREATE TABLE IF NOT EXISTS games (
    game_id      INTEGER PRIMARY KEY,      -- site game id, globally unique
    season_id    INTEGER NOT NULL REFERENCES seasons(season_id),
    -- Games never appear in more than one league, so this is unambiguous.
    league_id    INTEGER DEFAULT 3 REFERENCES leagues(league_id),
    division_id  INTEGER REFERENCES divisions(division_id),
    date_text    TEXT,                     -- "Fri Aug 29" as printed
    date_iso     TEXT,                     -- "2025-08-29" once known
    time_text    TEXT,
    rink         TEXT,
    league       TEXT,
    level        TEXT,                     -- "10U A" as printed on the row
    game_type    TEXT,                     -- as printed: "Regular 7", "Preseason", ...
    -- Normalized bucket. Only 'regular' games count toward the season totals
    -- and standings the site publishes, so exports and reconciliation depend
    -- on this rather than on the printed label.
    game_class   TEXT,                     -- regular | preseason | exhibition | playoff | other
    away_team_id INTEGER,
    home_team_id INTEGER,
    away_name    TEXT,
    home_name    TEXT,
    away_goals   INTEGER,
    home_goals   INTEGER,
    status       TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | final
    has_scoresheet INTEGER NOT NULL DEFAULT 0,
    -- Hash of the schedule row. When this changes the result was corrected and
    -- the scoresheet is refetched.
    schedule_hash   TEXT,
    scoresheet_sha  TEXT,
    scoresheet_at   TEXT,                  -- last successful scoresheet parse
    parse_version   INTEGER,               -- parser version that produced detail
    parse_error     TEXT,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_games_class    ON games(season_id, game_class);
CREATE INDEX IF NOT EXISTS idx_games_season   ON games(season_id);
CREATE INDEX IF NOT EXISTS idx_games_date     ON games(date_iso);
CREATE INDEX IF NOT EXISTS idx_games_status   ON games(status, has_scoresheet);
CREATE INDEX IF NOT EXISTS idx_games_home     ON games(home_team_id, season_id);
CREATE INDEX IF NOT EXISTS idx_games_away     ON games(away_team_id, season_id);
CREATE INDEX IF NOT EXISTS idx_games_pending  ON games(scoresheet_at) WHERE scoresheet_at IS NULL;

CREATE TABLE IF NOT EXISTS period_scores (
    game_id INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side    TEXT NOT NULL,                 -- home | away
    period  TEXT NOT NULL,                 -- "1".."3", "OT", "SO"
    goals   INTEGER,
    PRIMARY KEY (game_id, side, period)
);

-- Everything below is replaced wholesale whenever a scoresheet is re-parsed,
-- so ON DELETE CASCADE keeps re-parsing simple and idempotent.

CREATE TABLE IF NOT EXISTS game_rosters (
    game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side      TEXT NOT NULL,               -- home | away
    slot      INTEGER NOT NULL,            -- order on the sheet
    jersey    TEXT,
    position  TEXT,                        -- "G" for goalies, else blank
    name      TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'player',  -- player | coach
    player_id INTEGER REFERENCES players(player_id),
    PRIMARY KEY (game_id, side, slot)
);

CREATE INDEX IF NOT EXISTS idx_roster_player ON game_rosters(player_id);
CREATE INDEX IF NOT EXISTS idx_roster_name   ON game_rosters(name);

CREATE TABLE IF NOT EXISTS goals (
    game_id  INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side     TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    period   TEXT,
    time_text TEXT,
    time_sec INTEGER,                      -- seconds remaining in the period
    strength TEXT,                         -- '', PP, SH, EN, PS
    scorer_jersey  TEXT,
    assist1_jersey TEXT,
    assist2_jersey TEXT,
    scorer_player_id  INTEGER REFERENCES players(player_id),
    assist1_player_id INTEGER REFERENCES players(player_id),
    assist2_player_id INTEGER REFERENCES players(player_id),
    PRIMARY KEY (game_id, side, seq)
);

CREATE INDEX IF NOT EXISTS idx_goals_scorer ON goals(scorer_player_id);

CREATE TABLE IF NOT EXISTS penalties (
    game_id    INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side       TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    period     TEXT,
    jersey     TEXT,
    infraction TEXT,
    minutes    REAL,
    off_ice    TEXT,
    start_time TEXT,
    end_time   TEXT,
    on_ice     TEXT,
    player_id  INTEGER REFERENCES players(player_id),
    PRIMARY KEY (game_id, side, seq)
);

CREATE INDEX IF NOT EXISTS idx_pen_player ON penalties(player_id);

CREATE TABLE IF NOT EXISTS goalie_stints (
    game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side      TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    name      TEXT,
    note      TEXT,                        -- "Starting", "2nd period", ...
    player_id INTEGER REFERENCES players(player_id),
    PRIMARY KEY (game_id, side, seq)
);

-- Shot-by-shot grid. Scorekeepers frequently under-record it (a 19-goal game
-- can show 2 marked goals), so it is advisory only -- never a stats source.
CREATE TABLE IF NOT EXISTS shot_marks (
    game_id      INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side         TEXT NOT NULL,            -- side of the GOALIE being shot at
    header_saves INTEGER,                  -- the "Home Saves:22" figure
    marked       INTEGER,                  -- coloured cells in the grid
    goals_marked INTEGER,                  -- cells coloured as goals
    reliable     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, side)
);

-- Per-goalie shots and saves from the PDF scorecard's Goaltender Records
-- table, which the HTML scoresheet does not carry. This is the one true source
-- for a goalie's goals-against when a side used more than one: the goalie
-- changes above give who and when, but only this says how many each faced.
-- Only stored when the side's records reconcile with the score, so a blank or
-- inconsistent table is left to the derived fallback instead.
-- Keyed by the goalie's order within its side (seq), not jersey: old
-- scorecards often print no jersey number, so two goalies who split a game
-- both arrive with a blank jersey and only their order distinguishes them.
CREATE TABLE IF NOT EXISTS goalie_records (
    game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    side      TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    jersey    TEXT,                       -- may be blank on older sheets
    shots     INTEGER,                   -- total shots faced
    saves     INTEGER,                   -- total saves
    goals_against INTEGER,               -- shots - saves
    by_period TEXT,                      -- JSON: {"1":{"shots":n,"saves":n},...}
    player_id INTEGER REFERENCES players(player_id),
    PRIMARY KEY (game_id, side, seq)
);

-- ------------------------------------------------- site-published aggregates

-- The season-total tables the site publishes per team, kept for cross-checking
-- our derived totals. Column sets drift between seasons, so the full row is
-- retained as JSON alongside the stable typed columns.
CREATE TABLE IF NOT EXISTS team_stat_rows (
    season_id  INTEGER NOT NULL,
    team_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,              -- skater | goalie
    row_index  INTEGER NOT NULL,
    name       TEXT,
    jersey     TEXT,
    gp         INTEGER,
    data_json  TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (season_id, team_id, kind, row_index)
);

-- --------------------------------------------------------- player identity

CREATE TABLE IF NOT EXISTS players (
    player_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL UNIQUE,   -- normalized match key
    display_name   TEXT NOT NULL,          -- best-looking spelling seen
    birth_year     INTEGER,                -- inferred when the window narrows to 1
    birth_year_min INTEGER,
    birth_year_max INTEGER,
    created_at     TEXT
);

-- Every raw spelling seen, mapped to its most common player. Good enough for
-- aliases and display; NOT sufficient for linking, because one spelling can
-- belong to two children (see player_name_map).
CREATE TABLE IF NOT EXISTS player_names (
    name      TEXT PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    seen      INTEGER NOT NULL DEFAULT 1
);

-- The precise resolution map: which child a spelling refers to in a given
-- season and team. This is what makes two children sharing a name work --
-- "Ryan Smith" in S29 on team 58 and "Ryan Smith" in S31 on team 129 can be
-- different player_ids.
CREATE TABLE IF NOT EXISTS player_name_map (
    name      TEXT NOT NULL,
    season_id INTEGER NOT NULL,
    team_id   INTEGER,                     -- NULL = any team that season
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    PRIMARY KEY (name, season_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_name_map_player ON player_name_map(player_id);

-- Manual overrides: highest-authority identity decisions, never auto-modified.
CREATE TABLE IF NOT EXISTS player_overrides (
    name       TEXT PRIMARY KEY,
    player_id  INTEGER REFERENCES players(player_id),
    merge_into TEXT,                       -- canonical name to merge this into
    split      INTEGER NOT NULL DEFAULT 0, -- 1 = never merge this name
    note       TEXT
);

-- One spelling that belongs to two or more different children. Each row
-- assigns a season (optionally a specific team) to a distinct person, so
-- "Ryan Smith" born 2012 and "Ryan Smith" born 2015 stay separate players.
-- Rows written by hand or by an answered review item; never auto-modified.
CREATE TABLE IF NOT EXISTS player_splits (
    name       TEXT NOT NULL,              -- normalized name key
    season_id  INTEGER NOT NULL,
    team_id    INTEGER,                    -- NULL = every team that season
    person_key TEXT NOT NULL,              -- 'a', 'b', or a birth year
    note       TEXT,
    PRIMARY KEY (name, season_id, team_id)
);

-- A game whose roster the source filed under the wrong team. Timetoscore
-- sometimes files one club's two squads under a single team id, so a squad's
-- game lands on the other's team and inflates its roster. This re-homes such a
-- game: "in game G, the side attributed to from_team is really to_team." Read
-- on every derive and applied before the stat lines are rebuilt, so it outlives
-- a re-fetch and a wipe-and-rebuild alike. Written by hand or by
-- `norcalstats reassign-game`; never auto-modified.
CREATE TABLE IF NOT EXISTS game_team_overrides (
    game_id    INTEGER NOT NULL,
    from_team  INTEGER NOT NULL,
    to_team    INTEGER NOT NULL,
    note       TEXT,
    decided_at TEXT,
    PRIMARY KEY (game_id, from_team)
);

-- ---------------------------------------------------------- review queue

-- Questions the collector cannot answer on its own. Each item is identified by
-- a stable fingerprint so a nightly re-run never duplicates a question and
-- never reopens one already answered.
CREATE TABLE IF NOT EXISTS review_items (
    item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,   -- same_name | nickname | initials | suffix
                                 -- | ambiguous_team | unmatched_jersey
    status      TEXT NOT NULL DEFAULT 'open',   -- open | resolved | dismissed
    confidence  REAL,            -- 0-1: how sure the automatic guess is
    subject     TEXT NOT NULL,   -- one-line description
    evidence    TEXT,            -- JSON: what was seen
    suggestion  TEXT,            -- what the collector would do by default
    applied     TEXT,            -- what it actually did this run
    decision    TEXT,            -- what the user chose
    note        TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    decided_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status, kind);

CREATE TABLE IF NOT EXISTS player_team_seasons (
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    season_id INTEGER NOT NULL,
    team_id   INTEGER NOT NULL,
    jersey    TEXT,
    games     INTEGER NOT NULL DEFAULT 0,
    is_goalie INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, season_id, team_id, jersey)
);

-- ------------------------------------------------------------- derived stats

CREATE TABLE IF NOT EXISTS player_game_stats (
    game_id   INTEGER NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    season_id INTEGER NOT NULL,
    team_id   INTEGER,
    side      TEXT,
    jersey    TEXT,
    goals     INTEGER NOT NULL DEFAULT 0,
    assists   INTEGER NOT NULL DEFAULT 0,
    points    INTEGER NOT NULL DEFAULT 0,
    pim       REAL NOT NULL DEFAULT 0,
    penalties INTEGER NOT NULL DEFAULT 0,
    ppg       INTEGER NOT NULL DEFAULT 0,
    shg       INTEGER NOT NULL DEFAULT 0,
    is_goalie INTEGER NOT NULL DEFAULT 0,
    goals_against INTEGER,
    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_pgs_player ON player_game_stats(player_id, season_id);
CREATE INDEX IF NOT EXISTS idx_pgs_team   ON player_game_stats(team_id, season_id);

-- ------------------------------------------------------------ bookkeeping

CREATE TABLE IF NOT EXISTS fetch_log (
    url        TEXT PRIMARY KEY,
    sha256     TEXT,
    status     INTEGER,
    fetched_at TEXT,
    bytes      INTEGER,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT,
    started_at  TEXT,
    finished_at TEXT,
    seasons     TEXT,
    pages       INTEGER NOT NULL DEFAULT 0,
    games_seen  INTEGER NOT NULL DEFAULT 0,
    games_parsed INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

-- Data-quality findings, rewritten on each audit.
CREATE TABLE IF NOT EXISTS anomalies (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    season_id INTEGER,
    game_id   INTEGER,
    player_id INTEGER,
    detail    TEXT,
    found_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_anom_kind ON anomalies(kind);
