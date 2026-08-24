# NorCal Youth Hockey Stats

A game-level statistics archive for NorCal youth hockey, collected automatically
from [stats.caha.timetoscore.com](https://stats.caha.timetoscore.com).

Two pieces:

| Piece | What it is |
|---|---|
| **`norcalstats/`** | A dependency-free Python collector that runs unattended on a Raspberry Pi, parses every scoresheet, and maintains a SQLite database |
| **`norcal_hockey_viewer.html`** | The single-file web viewer, hosted at [lamfalus.github.io/norcal-hockey](https://lamfalus.github.io/norcal-hockey/norcal_hockey_viewer.html) |

---

## Where it stands

Running unattended and publishing itself. A Raspberry Pi collects every night at
03:30, writes the app dataset, and force-pushes it to the `data` branch with a
repo-scoped deploy key. GitHub Pages serves the viewer from `main`, the viewer
reads the dataset from `data`, and neither needs a hand.

| | |
|---|---|
| Seasons | Fall 2021 – Fall 2026 (S27–S31, S33 — the site has no S32) |
| Leagues | 4 — Norcal, CAHA, SCAHA, PGHL |
| Games | 14,019, of which 8,130 have a result |
| Stat lines | 240,901 across 63,631 goals and 49,193 penalties |
| Teams / clubs | 2,346 team-seasons resolving to 65 clubs |
| Players | 10,338 in the published dataset |
| Open review questions | 169 |
| Schema | v6 |

Counts are a snapshot taken 2026-08-24, after the first run on schema v6; the
collector adds to them nightly.

The viewer opens on the **schedule**: what is on next and what just happened,
across every league and division at once. Behind that are browse by season, club
pages, player pages with per-game logs and linemates, team pages with schedule,
standings and scoring by period, box scores, and the club-to-club player flow
chart. It works on a phone — that took measuring rather than guessing, and the
notes are in [The viewer](#the-viewer).

**What is left**, in the order it is worth doing:

- **`ambiguous_team`, 40 questions.** Same-club-versus-same-club games where
  neither side could be identified, so **5,262 stat lines belong to no team** and
  are missing from team totals. The only open category that costs data.
- The other 129 questions are correct as auto-applied: genuine second children
  sharing a name, and call-ups playing two age groups. Two are `new_league`,
  which is the one category worth reading rather than dismissing.
- `caha_players_s27-s31.json` is a v1 artifact nothing has written since v1, and
  the legacy exports are switched off. Both can go once nothing points at them.
- 4,535 orphan player rows — the leftovers of splits that were later undone.
  Excluded from the export, still in the database. `purge_leagues` deliberately
  does not sweep them up: they are a separate question from any one purge.

---

## What changed in v2

The original scraper was a browser-console script that copied the league's
**season-total tables** and downloaded a JSON file you committed by hand. v2
reads the **scoresheets** instead and keeps a real database.

| | v1 (`scraper.js`) | v2 (`norcalstats/`) |
|---|---|---|
| Runs in | Browser console, manually | Pi, nightly, unattended |
| Granularity | Season totals | Every goal, assist, penalty, roster and goalie change |
| Storage | One JSON file | SQLite + JSON exports |
| Re-runs | Re-scrapes everything | Fetches only games that changed |
| Seasons | Hardcoded `[27..31]` | Discovered, including before the dropdown lists them |
| Leagues | Norcal only (plus a separate caha.com parser) | Discovered automatically; season-long ones collected |
| Game types | Regular season only | Preseason, regular, playoff and exhibition |
| Divisions | Often blank | Always populated |
| Names | Merged by string rules | Shared sweater first, then string rules |
| Nicknames | Not handled | Merged with corroboration, else queued for review |
| Two kids, one name | Silently merged | Split apart, or queued for review |
| Double-rostered girls | Stats merged into one line | One player, separate stats per team |
| Uncertain cases | Silent guess | Logged as questions you answer once |

Everything the game-level data enables — game logs, head-to-head records,
scoring by period, penalty histories, splits by preseason/regular/playoff — is
new in v2.

### One source, the leagues that matter

timetoscore hosts around thirty leagues side by side, so the separate
`caha.com` parser is no longer needed. A single competition is split across
several ids that come and go as the season progresses — CAHA alone runs four.
These are collected:

| id | League | id | League |
|---|---|---|---|
| 3 | **Norcal** — travel A / B / BB | 16 | CAHA Preseason ↴ |
| 5 | **CAHA** — tier 1 / tier 2, AA & AAA | 17 | CAHA Weekends ↴ |
| 4 | **SCAHA** — Southern California | 24 | CAHA Playoffs ↴ |
| 34 | **PGHL** — Pacific Girls, 12/14/16/19 AA & AAA | | |

The three marked ↴ are rounds of the CAHA tier competition, not leagues in
their own right. They are still crawled separately, because the site publishes
them separately — but they **roll up to CAHA (5)** everywhere a reader picks a
league, and the round each game belonged to is kept as a label on the game.
That is the only place the distinction still lives, and it has to be a label
rather than a league because the two do not agree: the id named "CAHA
Preseason" holds 819 games the schedule types as *Regular*. `leagues.parent_id`
records the roll-up and `leagues.stage` names the round.

These are not, because they carry no season-long record:

- **Pacific District (37) and USAH Nationals (38)** — end-of-season
  championships. They *are* the playoff progression for tier teams, which is
  why they were collected at first, but the field is drawn from the whole
  country: 47 of the clubs in them are Alaska, Boston, Buffalo, Chicago,
  Cleveland, Colorado and the like, and a California team reaching one is
  incidental. Removed in v6 — and *removed*, not merely stopped: since they
  will not be collected again, the rows are not history worth carrying. 350
  games, the 97 teams that existed here for no other reason, and the 1,258
  players who came with them. See [Purging a league](#purging-a-league).
- **High school** — SHSHL, ADHSHL, LA Kings High School
- **College** — ACHA and ACHA MD2 (Stanford, Grand Canyon, Berkeley)
- **Weekend tournaments** — Silver Stick, Wine Country Face Off, One Hockey,
  KHS Thanksgiving/Christmas/Labor Day, Lake Tahoe MLK, and the rest
- **League 19**, a catch-all bucket for games played at out-of-area tournaments

Playoffs *are* kept — CAHA Playoffs and Pacific District (tier 1 playoffs) both
conclude leagues followed all season. That needs saying because playoffs are
short, so a "tournaments are brief" rule would throw them out along with the
weekend invitationals. Every league in the table above is an explicit decision,
not a guess by the classifier.

There is no index of leagues on the site, so the id range is **probed**, and the
current season is re-probed every run — that is what catches a league switching
on mid-season. A league nobody has classified yet is **not** collected; it is
measured and raised for review instead. The classifier uses, in order:

1. a name naming a championship *above* the league — district, regional,
   national → **skip and ask**. This is the rule that was missing: Pacific
   District and USAH Nationals both read as playoffs, which is exactly how they
   were collected in the first place. Such a league is measured and filed as a
   `new_league` question at low confidence, and stays uncollected until somebody
   answers with `--include` or `--exclude`;
2. a name that looks like the end of a league's own season — playoff, final →
   collect. That is what CAHA Playoffs is, and it is wanted;
3. games labelled `Tournament` on the schedule → skip, however long the league
   runs (league 19 spans 105 days but is entirely one-off events);
4. games spanning 30+ days → collect; otherwise skip.

Several teams are sampled, not one: the Pacific Girls league looked like a
two-day tournament judged on its first team alone, which would have quietly
dropped a tier league.

```bash
python3 -m norcalstats.cli leagues            # what is collected
python3 -m norcalstats.cli leagues --all      # including what is skipped, and why
python3 -m norcalstats.cli leagues --include 31   # override either way
python3 -m norcalstats.cli leagues --exclude 37
```

A fixed list in `"leagues": [3, 5]`, or repeated `--league` flags, bypasses the
policy entirely. Order is priority order, which matters because **team ids are
global**: one team appears in several leagues at once, and the highest-priority
league owns its name and division. Season-long leagues rank above preseason and
playoff ids, so a team is labelled by the league it actually plays in. Every
appearance is still recorded in `team_leagues`.

### Purging a league

Excluding a league stops it being collected; it does not remove what is already
there. `db.purge_leagues()` does, and the v6 migration runs it for the two
championships. Deleting the games is the easy half — rosters, goals, penalties,
period scores, goalie stints, shot marks and per-game stats all cascade from
`games`. Four things do not:

- **`teams`** — a team that also plays a real season keeps its row and is
  re-pointed at the best league it has left; only a team whose whole record here
  was the purged competition goes, taking its `standings` and `team_stat_rows`
  with it. Neither of those cascades.
- **`player_team_seasons`** — keyed on team rather than on game, so it survives
  the cascade untouched.
- **`players`** — nothing else in this codebase has ever deleted a player, which
  is why the database still carries orphans from undone splits. The players who
  were only ever here for the purged competition go too. They are identified
  three ways before the delete — a stat line, a roster row, or *only* a team
  membership, which is the one that matters for a team whose games were never
  scoresheeted — and each is then confirmed to have nothing left anywhere. A
  player named in a hand-made override or split is kept regardless: that
  decision is not the purge's to discard. Pre-existing orphans are not swept up
  either; they are a separate question.
- **`review_items`** — left alone. They are keyed by an opaque fingerprint and
  record questions that were genuinely asked; a stale one can be answered, a
  deleted one cannot.

The archived pages under `data/raw` are not touched, and do not need to be: the
crawl only walks leagues marked `season`, and a purged game leaves no scoresheet
pending, so `reparse` cannot bring one back. They are a re-fetchable cache. To
reclaim the space anyway, delete the ones no game points at.

### How far back

Six seasons plus the current one, by default (`"seasons_back": 6`; `0` for no
limit). That filters on **start year**, not season number — the numbers are
irregular, and the gap from S24 to S27 is two seasons, not three. Today that
means S27 (Fall 2021) through S33 (Fall 2026), and the site's older seasons back
to Fall 2009 are left alone.

Divisions are keyed by league, because the names collide — Norcal and SCAHA both
run a `12U A`. Where that happens the legacy export qualifies the name
(`12U A (SCAHA)`); names unique to one league are left exactly as printed. Every
exported entry carries its `league`, so the viewer can filter on it.

### Every kind of game

Preseason, regular season, playoff and exhibition games are all collected, and
all of them count toward the exported totals. The league publishes season totals
for the **regular season only**, which is all the old scraper could see; the
game-level data covers the rest.

Every stat line is therefore tagged with the class of game it came from. In
the app dataset each season split carries its own `class`, which is what the
viewer's **Games counted** filter switches on; the legacy export folds the
same thing into a `byClass` breakdown:

```json
{ "season": 31, "team": "San Jose Jr Sharks", "GP": "28", "G": "24",
  "league": "Norcal", "source": "games",
  "byClass": { "preseason": {"GP": 8, "G": 6},
               "regular":   {"GP": 15, "G": 14},
               "playoff":   {"GP": 5, "G": 4} } }
```

The `regular` class is what the league itself publishes, so the old numbers
remain available for comparison. This matters more than it sounds: preseason
and exhibition games are a third of all stat lines, so a total that silently
includes them is not the number anyone expects.

One safeguard: derived totals are used only when they are **at least as complete
as the published regular-season line**. Half-way through a backfill the parsed
games are a subset, and quietly replacing a complete published figure with a
partial one would be worse than not using it. Those entries fall back to the
published totals and say so via `"source": "published"`.

### Two things the old code got wrong

**Season numbers are not `year - 1994`.** That holds for recent seasons but not
older ones: the site lists S18 as Fall 2016 and S24 as Fall 2019. Nor are they
contiguous — **the 2026–27 season is S33; S32 does not exist.** Any arithmetic
would have pointed the collector at an empty season all year.

The dropdown is read instead. But a new season goes live *before* it is listed
there, so the collector also asks the "Current" page what season it actually is,
and picks up the new season the day it appears. Its start year comes from the
calendar rather than from the season number, and is then confirmed against real
game dates as soon as the first scoresheet is parsed — scoresheets carry
absolute dates, so the year is settled definitively and any schedule dates
derived from a guess are recomputed.

**Divisions were being dropped.** The old scraper mapped teams to divisions
using the page's `data-id`/`data-parent` attributes, but those ids are reused
across sections — the same `data-id` appears as both a section header and a
child row — so divisions came out blank for entire seasons. v2 walks the table
in document order and keys off the division link instead.

---

## Quick start

Requires Python 3.9+. **No third-party packages** — deliberately, so nothing
rots or breaks mid-season on the Pi.

```bash
python3 -m norcalstats.cli seasons
```

That lists the seasons the site currently offers and confirms connectivity.

```bash
python3 -m norcalstats.cli backfill --from-season 27
```

The one-time historical crawl. Defaults to six seasons back plus the current
one, across the season-long leagues. It takes a few hours at the
default polite rate and is safe to interrupt — re-running resumes where it left
off, because the database records which scoresheets have been parsed.

```bash
python3 -m norcalstats.cli update
```

The nightly run. It refreshes schedules and standings, then fetches scoresheets
**only** for games that are newly final, never parsed, changed since last seen,
or recent enough that a scorekeeper might still correct them.

---

## Commands

| Command | Purpose |
|---|---|
| `update` | Incremental run — this is what the timer calls |
| `backfill` | One-time historical crawl (`--from-season`, `--to-season`) |
| `reparse` | Re-parse the archived pages offline, no network at all |
| `derive` | Rebuild player identities and stat lines from stored rows |
| `export` | Write the app dataset, and the legacy exports if enabled |
| `publish` | Push the app dataset, and commit the legacy exports if enabled |
| `status` | What is in the database |
| `seasons` | Seasons the site currently lists |
| `leagues` | Leagues carrying games, per season (`--discover` to probe) |
| `audit` | Data-quality findings |
| `review` | Questions about names and teams needing your decision |

Useful flags: `--season N`, `--league N` and `--team ID` (all repeatable), `--limit N`
to cap scoresheets per run, `--delay` to change the request spacing, and
`--dry-run` for publishing.

---

## How it works

A run has four stages. Only the first three touch the network.

**1 — Discover seasons.** Read the season dropdown, then ask the "Current" page
what season is actually live, so a new season is picked up the day it appears
rather than whenever the dropdown catches up.

**2 — Scan leagues and teams.** For each season and each configured league,
read the season index (divisions, teams and standings) and then each team page
(its game list plus the league's own published season totals, kept for
cross-checking).

**3 — Fetch scoresheets.** Only for games that need one. This is what makes a
nightly run cheap: a typical night fetches the handful of games played that day
rather than the whole season.

**4 — Derive.** Resolve player identities and materialize per-game stat lines
from the stored events. No network access, so it can be re-run any time.

### The page archive

Every page fetched is stored gzipped under `data/raw/`. When the parser
improves, `reparse` rebuilds the entire database from that archive **without
refetching anything** — no extra load on a volunteer-run website, and no risk of
losing history if a page later changes. This is also how the collector is
tested.

### Player identity

Name trouble in this data pulls in two opposite directions: some spellings need
combining, and some need pulling apart. Both are handled, and the risky
judgements are logged rather than guessed at.

| Trouble | Example | What happens |
|---|---|---|
| Spacing / punctuation | `Avery St. Onge`, `Avery  St Onge` | merged automatically |
| Capitalisation | `PARKER ANDERSON` / `Parker Anderson` | merged automatically |
| Dropped initials | `Gavin B Duganne` / `Gavin Duganne` | merged when it can't conflate two children |
| Generational suffixes | `Carlos Ayon II` / `Carlos Ayon 3` | merged when it can't conflate two children |
| **Nicknames** | `Bobby Smith` / `Robert Smith` | merged **only** with corroboration; otherwise raised for review |
| **Two children, one name** | two boys called `Ryan Smith` | **split** into separate players when the evidence is strong; otherwise raised |
| **Double-rostered** | a girl on a girls team *and* a co-ed team | one player, two sets of stats |
| **Playing up** | a 15-year-old on a 19U girls team | one player; bounded by how far up is plausible |

The strongest signal is one the old season-total scrape never had: game rosters
carry `(season, team, jersey)`, so two spellings sharing a sweater on one team
in one season are provably the same child, whatever the spelling.

**Nicknames are treated as suspects, not proof.** `Alex Chen` and
`Alexander Chen` may well be two different children, so a nickname match alone
never merges anyone — it needs a shared team-season or jersey. Otherwise the
pair is raised for review and left separate, because a wrong merge is far harder
to notice later than a missing one.

**Two children sharing a name are detected from division history.** Each age
division implies a [birth window](#how-wide-a-birth-window-is), so a `Ryan
Smith` who appears in both 10U and 16U in one season is two boys, not one. They become separate players
with separate birth years and separate stats.

The obvious trap here is a **call-up** — a 10U child playing a game or two up in
12U looks exactly like a second child. A player's birth window is therefore taken
from the division they *mainly* played that season; a handful of games elsewhere
is a second team, not a second child. A split also needs meaningful presence on
both sides (3+ appearances). Anything short of that stays merged and is raised
for review, with the reasoning shown.

#### Playing up

Age rules run one way: a division of age N admits players *under* N, so a child
can play up but never down. Birth windows are therefore widened **toward younger
players only** — the lower bound is a hard age limit and never moves.

The tolerance is two years, with one exception. **18U/19U is the top of the
ladder**, so it has no division above it to absorb older teenagers and ends up
carrying a wide spread of ages — and in a sparse girls program it may be the only
team above 16U. Those divisions get a four-year band.

That produces the following, for a season starting 2025:

| Combination | Result | Why |
|---|---|---|
| 14U + 16U | one player | adjacent divisions |
| 16U + 19U | one player | a 15-year-old on a 19U team |
| 14U + 19U | one player | top division carries a wide range |
| 12U + 16U | **split** | four years; does not happen at these ages |
| 12U + 19U | **split** | seven years apart |
| 10U + 19U | **split** | not one child |

Both boundaries are pinned by tests, so the tuning cannot drift silently. If the
real backfill shows it erring either way, the dials are `PLAY_UP_TOLERANCE` and
`TERMINAL_PLAY_UP_TOLERANCE` in `identity.py`.

#### How wide a birth window is

Birth years are inferred by intersecting a player's seasons, so how many years
each division admits is the whole of the arithmetic. It is not always two.

| Divisions | Birth years | |
|---|---|---|
| 11U–16U **AAA** | **1** | Tier I boys run a single-year ladder |
| 18U AAA | 2 | the top of the boys ladder carries 17s and 18s |
| Girls 12AAA, 14AAA, 16AAA | 2 | the girls ladder is two years at every rung |
| **Girls 19U**, any tier | **3** | the top of the girls ladder carries 17s, 18s and 19s |
| everything else | 2 | |

The single-year rungs are why a career resolves at all. A player who spends six
seasons in two-year bands can still finish with two candidate years and nothing
to choose between them; one season of 14U AAA fixes the year outright. It is
visible in the data too — 11U, 12U, 13U, 14U, 15U and 16U AAA all ice in the
same league in the same season, which is only possible if each is one year.

Both ends of the ladder are exceptions, in opposite directions, and the girls
one matters more. **A window that is too narrow is worse than one that is too
wide**: too wide merely fails to narrow the answer, while too narrow
contradicts the player's other seasons and can lose the birth year entirely.
Reading a girls 19U team as two years does exactly that to a third of its
roster.

A combined band is read from both ends. `Girls 16/19AA` tops out at the 19U
ceiling and reaches down to the 16U floor, so it spans 15- to 19-year-olds
rather than the top classification alone. Only a slash marks one — reading
every number in a name as an age would let a team number widen the window,
turning `Girls 16AA 5` into a band reaching down to five-year-olds.

#### What counts as a season

The windows are intersected over the divisions a player *mainly* played, not
over every division they were ever seen in. One guest game in an older division
implies a birth year two years off from the rest of a career, and counted
equally it empties the intersection — so a player whose every real season
agrees ends up with two candidate years because of a single afternoon. A
division carries a season when it saw at least half as many appearances as the
busiest one that season, which is the same rule the [same-name
split](#player-identity) uses, so a full second roster survives and a call-up
does not.

A call-up's **lower** bound is kept even so. Age rules run one way, so nobody in
a 12U game is older than 12U admits, however few games it was — what a call-up
never justified is its *upper* bound, which is the half that was breaking the
intersection. A player with twenty games at 14U and one at 12U is a 12U-aged
player spending the season up, and the single game is what says so.

Where two windows tie on their earliest year, the **narrower** one wins. That
is not only precision: the divisions are held in a set, so picking arbitrarily
between a single-year division and the two-year band starting alongside it
would answer differently between runs, and a player's badge would move for no
reason.

### Double-rostered players

A girl may be rostered on a girls team *and* a co-ed team in the same season.
She is **one player with two sets of stats**, and both parts of that matter.

Girls appear in two arrangements here, and both are recognised: a dedicated
division (`Girls 16-U`), or a girls team entered into an otherwise co-ed
division (`San Jose Jr Sharks Girls` in 10U B West, `Stockton Colts Girls
10G-1`, `Tri Valley Lady Blue Devils`). Gender is therefore tracked per **team**,
not just per division.

Identity handles this through the **play-up model** below, not through a
girls-specific exemption — a girl's stats are separated by team either way.

Her stats stay separate because every stat line is grouped by team:

- The **legacy export** lists one entry per team under her single name — a girls
  entry and a co-ed entry, each with its own division, GP, goals and assists.
  This is the same mechanism that already handled mid-season team changes.
- The **rich export** tags each season split with `team`, `division` and
  `gender`, so girls and co-ed totals can be read off directly or summed.

The same applies to a call-up: two teams, two stat lines, one player. Both show
up in the review queue as `double_roster` items so they can be seen rather than
merely assumed.

Note the corroboration this gives you, per your own suggestion: when one name
appears on two rosters, the season summary shows both lines side by side. If
they're one child double-rostered, that's the correct and useful view; if they
turn out to be two children, the two lines are what makes it obvious, and
`review answer <id> split` separates them permanently. Once split, the exports
key them apart by birth year (`Ryan Smith '13` / `Ryan Smith '07`) so they never
collapse back into one entry.

### The review queue

Anything the collector cannot decide confidently becomes a question rather than
a silent guess:

```bash
python3 -m norcalstats.cli review list
```

```
[7] same_name (80% sure): Ryan Smith: one spelling covering 2 different children
      did: split into 2 players (a, b)
      ask: 'merge' if this is really one player, or 'split' with an explicit season map

[9] nickname (40% sure): Bobby Smith and Robert Smith may be the same player
      did: kept separate
      ask: 'merge' if one child, 'separate' to stop asking
```

Every item says what it **did** as well as what it's **asking**, so nothing is
happening behind your back. `review show <id>` prints the evidence — seasons,
divisions, appearance counts, which teams were involved.

Answer one with:

```bash
python3 -m norcalstats.cli review answer 9 merge --note "confirmed, same kid"
```

| Action | Meaning |
|---|---|
| `merge` | one player (for `same_name`, puts every season back under one child) |
| `separate` | different players; stop merging them |
| `split` | one spelling, several children — needs `--person 29=a --person 31=b` |
| `dismiss` | not a problem; stop asking |

Two properties make this survive a long season:

- **You are never asked twice.** Items are identified by what they're *about*,
  so a nightly run updates an existing question instead of re-adding it, and an
  answered question is never reopened.
- **Decisions outlive rebuilds.** Answers are written to `player_overrides` and
  `player_splits`, which the resolver consults on every run. You can delete the
  whole database and rebuild from the page archive without losing a single
  decision. `review reopen <id>` undoes one.

Ambiguous *teams* land in the same queue — when a club enters two teams under
one name and they play each other, neither side can be identified from the
schedule. The item names the candidate teams and tells you which side is
already known.

### Data quality

The collector records what it cannot verify rather than hiding it. `audit`
reports goals credited to a jersey that is not on the roster, final games whose
scoresheet never parsed, games whose teams could not be identified, and derived
totals that disagree with the league's published numbers.

Two things worth knowing about the source data:

- **Only `Regular N` games count.** The league numbers regular-season games
  individually (`Regular 1` … `Regular 15`), and its published totals and
  standings exclude preseason, exhibition and playoff games. The database stores
  a `game_class` for exactly this reason.
- **The shot grid is unreliable.** Scorekeepers routinely leave it half-filled —
  one 19-goal game in the fixtures has two goals marked. It is recorded and
  flagged `reliable = 0`, and is never used as a stats source.
- **A scoresheet is checked against the fixture it was fetched for.** Game ids
  come from the link, never from the printed label, but an earlier parser
  invented one when it could not read it — and asking the site for game 1
  returns the site's real game 1, played in 2010. Its roster then lands on a
  fixture a decade later. A sheet whose date is not the date the schedule
  printed is refused, which catches it: thirteen games in one database, and
  nothing else in 8,490.

That last one earns its paragraph because of how far the damage travelled. The
foreign dates decided the season's start year, the start year is what birth
years are inferred from, and a birth year fourteen years out reads as two
children sharing a name — so the resolver split real players apart and raised
thousands of questions about it. A season's year is now settled by the year its
games agree on rather than by its earliest one, so no handful of rows can move
it again.

Reconciliation was verified against the league's own numbers: for two fully
parsed teams, all 28 players matched the published GP, goals and assists
exactly, derived purely from scoresheets.

---

## Database

SQLite, at `data/norcal.sqlite3`. The schema is in
[`norcalstats/schema.sql`](norcalstats/schema.sql).

Scraped as recorded: `seasons`, `divisions`, `teams`, `standings`, `games`,
`game_rosters`, `goals`, `penalties`, `goalie_stints`, `period_scores`,
`shot_marks`, `team_stat_rows`.

Derived and rebuildable: `players`, `player_names`, `player_team_seasons`,
`player_game_stats`, `clubs`.

`clubs` is worth a note. The site names *teams*, not clubs, and spells them
differently between seasons, so a club is a reading of the team names rather
than anything fetched. Each row carries the canonical name everything groups
by, the shorter name the app shows, and a `kind` — `club`, `visitor`,
`high_school` or `bracket`. Half the names the site prints are not clubs at
all; they all keep their names, because schedules need them, but only real
clubs are offered as somewhere to browse.

Bookkeeping: `fetch_log`, `runs`, `anomalies`, `player_overrides`.

Some questions the old format could not answer:

```sql
-- Best single game of the season
SELECT p.display_name, g.date_iso, s.goals, s.assists, s.points
  FROM player_game_stats s
  JOIN players p ON p.player_id = s.player_id
  JOIN games   g ON g.game_id   = s.game_id
 WHERE g.season_id = 31 AND g.game_class = 'regular'
 ORDER BY s.points DESC LIMIT 10;

-- Which period does a team score in?
SELECT period, COUNT(*) FROM goals
 WHERE game_id IN (SELECT game_id FROM games WHERE home_team_id = 58)
 GROUP BY period;
```

---

## Exports

### The app dataset

What the viewer reads. Written to `data/app` — configurable as `app_dir`, and
under `data/` by default, which git ignores, so refreshing it nightly changes
nothing the repository can see.

It is split by **granularity, not by season**. The obvious split — one file per
season — would break the two views that exist to span seasons: the club player
flow, and a club's teams across the years.

**`core.json`** — everything cross-season and aggregate: players with their
per-season splits, teams, divisions, clubs, leagues, standings. Loaded once,
and enough on its own for every list and table in the app. 8.6 MB, about 821 KB
gzipped.

**`schedule.json`** — every game there has ever been, header only: when,
where, who, and the score where there is one. One file rather than six because
the date order the app opens on runs across seasons as well as across leagues,
so no per-season file can answer it. Sorted by date and then time of day, which
the app relies on — it shows a day as a block and never sorts. Rows are arrays
rather than objects, since the same fifteen keys repeated 14,196 times cost
more than the data does: as arrays it is 1.7 MB, 225 KB gzipped.

Two of its columns are there for sides that matched no team row: `homeName` /
`awayName` carry the name the site printed, and `homeDiv` / `awayDiv` carry the
division that name states where it states one. See
[Reading a division off a name](#reading-a-division-off-a-name).

**`logs/pNN.json`** — per-game lines for a player, in 32 buckets chosen by
`player_id % 32`. The app works out which file to fetch by arithmetic, so there
is no index to load and keep in step. Opening a player costs one bucket, around
100 KB gzipped, covering every season they played.

**`games/sNN.json`** — every game of a season: goals, penalties and period
scores where it has been played, and the fixture where it has not. Scheduled
games are carried without a score, so a team page is a schedule as much as a
record — 5,732 games are scheduled rather than final, including every one of
the current season's.

Six seasons come to 40 files and 71 MB, about 7.2 MB gzipped, which GitHub
serves compressed. Transfer was never the constraint; parse time and memory on
a phone at a rink is, and that is what loading detail on demand solves. In
practice a cold load fetches `core.json` and `schedule.json` — 821 KB and
225 KB gzipped, 1.05 MB between them — and nothing else until somebody clicks.

Note that this is the **export** being split, not the database. The collector
keeps one SQLite file (94 MB on the Pi) and shards only on the way out, because
the shape that suits a query is not the shape that suits a phone.

### The legacy exports

Off when `"legacy_exports": false`. They predate the app dataset and land in
the repository itself, so every run leaves a tracked file modified — turn them
off once nothing reads them.

**`norcal_hockey_players_s27-s31.json`** — the name-keyed format the first
viewer expected, byte-for-byte (same keys, same order). *The filename keeps its
historical name because that viewer fetches that exact URL; it contains every
season in the database.*

**`norcal_hockey_stats.json`** — the richer non-app export: standings,
per-season splits by game class, career aggregates, and every game. Pass
`--game-logs` to include per-player game logs (much larger).

`caha_players_s27-s31.json` is a v1 scraper artifact, still committed but no
longer written by anything.

---

## Running on the Raspberry Pi

### Installing

```bash
git clone https://github.com/lamfalus/norcal-hockey
cd norcal-hockey
bash deploy/install.sh
```

(Invoked through `bash` rather than as `./deploy/install.sh`, so it runs even if
the executable bit was lost — the collector is authored on Windows, where git
does not track it by default.)

This checks the Python version, writes a default `norcalstats.json`, and
installs a systemd timer that runs nightly at 03:30 with a randomized delay.
`Persistent=true` means a missed run (Pi off, power cut) happens on next boot.

```bash
systemctl list-timers norcalstats@$USER.timer     # when it next runs
journalctl -u norcalstats@$USER.service -f        # watch a run
sudo systemctl start norcalstats@$USER.service    # run now
```

**Nothing on the Pi updates itself.** The unit runs the collector out of its
checkout and never touches git, so code changes reach it only by pulling, and
they have to be pulled *before* the timer fires or the night's run uses the old
code. A schema change needs nothing: `db.connect()` migrates on open, so the
first command after a pull upgrades the database.

Then seed the archive:

```bash
python3 -m norcalstats.cli backfill --from-season 27
```

### Checking a run before it goes live

Crawl commands write the exports automatically. The app dataset lands under
`data/`, where git is not looking, but the legacy exports land in the
repository itself. Pass `--no-export` while a backfill is still running, then
inspect deliberately:

```bash
python3 -m norcalstats.cli status              # what the database holds
python3 -m norcalstats.cli audit               # data-quality findings
python3 -m norcalstats.cli review list         # names and teams needing a decision
python3 -m norcalstats.cli export --out /tmp/check
```

`--out` writes somewhere harmless so nothing is overwritten while you look.

Export always reports how the new file compares with the published one:

```
  players: 44 (published: 2,900, -2,856)
  WARNING: far fewer players than the published file.
```

and **publishing refuses outright** when an export loses more than 10% of its
players:

```
refusing to publish an export that lost most of its players:
  norcal_hockey_players_s27-s31.json: 44 players, down from 2900 (98% fewer)
```

A partly-filled database produces a perfectly valid but tiny file, and
overwriting good published data with it is the worst thing the collector could
do. `--force` overrides the check when a drop really is intended.

### Publishing to GitHub

Both switches are **off by default**. This installation runs with
`"publish_app": true` and `"legacy_exports": false`, so the nightly run
publishes the app dataset and writes nothing into the repository itself.

Both need git authentication on the Pi. Here that is an ed25519 **deploy key**,
scoped to this repository, with write access, and `IdentitiesOnly yes` so ssh
offers nothing else. GitHub's host key is **pre-seeded in `known_hosts`**: the
systemd unit runs with `ProtectHome=read-only`, so ssh cannot write one itself
and the first unattended push would fail on an unknown host rather than on
anything to do with the key.

The collector never handles credentials. It shells out to `git`, which uses
whatever authentication is already configured.

**`"publish_app": true`** sends the app dataset to a branch of its own
(`app_branch`, default `data`) as a single **parentless commit, force-pushed**,
replacing what was there. 40 files rebuilt nightly would otherwise add
megabytes to the history every night and never give any of it back; a branch
with no history has nothing to grow. It is written with git plumbing against a
temporary index, so the working tree, the index and the checked-out branch are
never touched — the nightly run can publish while you are midway through an
edit — and it stages only the dataset directory, so nothing else can be swept
in.

It compares the tree it is about to push against the published one and skips
the push when they match. **In practice they never match.** Every file carries
a `metadata.generated` timestamp, so all 40 differ every night whatever the
data did; `app dataset unchanged; nothing to publish` has never been logged.

That is worth leaving alone rather than optimising. The viewer's footer reads
`generated` as *when the collector last ran*, and that is what makes a Pi that
has quietly stopped visible. Skipping unchanged publishes would freeze the date
through a quiet week and make a healthy collector look dead — the failure this
is meant to catch, reported backwards.

**`"publish": true`** commits the legacy exports to `git_branch` in the normal
way. It stages **only the export files**, never `git add -A`, refuses to run
if unrelated changes are staged, has a shrink guard that rejects an export that
lost most of its players, and creates no commit when nothing changed.

Try it safely first:

```bash
python3 -m norcalstats.cli publish --dry-run
```

### Being a good neighbour

timetoscore is a small volunteer-run site. The defaults reflect that: one
request at a time, 1.5s apart, exponential backoff on errors, a hard per-run
request ceiling, and an archive that means an improved parser costs zero extra
requests.

---

## Development

```bash
python3 -m unittest discover -s tests -t .
```

276 tests, no network access — they run against real pages saved in
`tests/fixtures/` from both ends of the backfill range (2021 and 2025), so a
format change in either direction is caught. The fixtures deliberately include
the awkward cases: cells opened `<td>` and closed `</th>`, a game with a score
but no scoresheet, two teams in one division sharing a club name, and jerseys
with leading zeros.

---

## The viewer

`norcal_hockey_viewer.html` is one self-contained file — all HTML, CSS and
JavaScript, no build step.

It reads the [app dataset](#the-app-dataset). On load it tries `data/app/`
first, so a checkout needs no editing, and falls back to the published `data`
branch, so the same file works from anywhere:

```
https://raw.githubusercontent.com/lamfalus/norcal-hockey/data/
```

Whichever answers first is remembered, so that costs one request, not two.

### The views

**Schedule** — the landing view, and the only one that reads across leagues,
divisions and seasons at once; everything else in the app starts from a season,
a club or a name, and this starts from a date. Two bands split at today: **Next
up** reads forward from there, **Recent results** reads back. Each day is a
block with its own heading and count, games within it in time order, and any
row opens its box score.

Three things about it are decisions rather than defaults:

- **Games with no result are listed and marked, not dropped.** 5,375 of 14,196
  past games were scheduled and never given a score. They were real fixtures
  with both teams named, so hiding them would make the day counts lie about
  what was played. That includes the 92 `ambiguous_team` games — same club
  against itself, neither side resolved to a team row, but named on both sides
  and 32 of them carrying a full box score.
- **Empty bracket slots are the one thing dropped.** 238 games where a side is
  a seed placeholder rather than a team — "14AAA Seed 1" against "14AAA Seed
  4", all of them CAHA playoff draws printed before the field was known. Not
  one was ever given a result or a real opponent, so none can appear on
  anybody's schedule. Excluded by the club's `kind`, not by a missing team id.
  A seed slot is not a team — it is the shape of one the draw does not know
  yet — but the collector still writes it a row, so its `team_id` is not null.
  The games that genuinely have no `team_id` are the `ambiguous_team` ones
  above, which are worth keeping, so filtering on the missing id would have
  dropped exactly the wrong 92 games and kept all 238 of these.
- **It has its own Type filter, and the global "Games counted" one is hidden
  while it is showing.** A fixture list is not a stat total. On the day the
  2026 season opens, 117 of its 120 games are preseason or exhibition, so the
  global default — regular plus playoffs — would have shown three.
- **Both bands page rather than render.** 8,130 played games is more than any
  list wants to hold, and a phone least of all, so each band shows 60 and says
  how many it is holding back.
- **It works without the index.** `schedule.json` is a convenience, not a
  precondition: where a published dataset has no index the view reads the newest
  season's own file instead and says which season it is showing. That season is
  50 KB against a `core.json` this page has already fetched at 8.6 MB, so
  refusing to list fixtures the dataset plainly contains was never a good trade.
  The index earns its keep on the results band, which spans six seasons and
  15.8 MB of per-season files.

**Browse by Season** — every skater and goalie in a season, narrowed by club
and division, sortable on any column.

**Club View** — a club's teams season by season, with rosters. Teams are
grouped by identity, never by name: in the older seasons the site gives every
one of a club's teams the same name, and eighteen different squads are all
called "Anaheim Jr Ducks" in 2021.

**Player Lookup** — a career, one section per season, each with a collapsible
**game log**: date, opponent, home or away, result, goals, assists, points,
penalty minutes, and power-play or short-handed marks. One fetch of the
player's shard covers every season they played.

**Player Flow** — an alluvial diagram of movement between clubs across seasons.

**Team page** — reached by clicking a team anywhere, not from the tab bar,
since it only means anything once you have picked one. The league's standings
row, every game played or still to come, the roster, and **scoring by period**
as goal difference per period. The standings are labelled regular season on
purpose: the league's table counts that alone while the schedule counts every
class, so the two game totals differ and one of them has to say which it is.

**Box score** — opens over whatever you were reading, because you always arrive
from a schedule or a game log and want to go back to it. Line score by period,
each goal with its time, scorer, assists and strength, and the penalties.

**Linemates** — on a player page, in both directions: goals somebody set up for
them, and goals they set up for somebody else. Assists are recorded on about
71% of goals, so the counts are a floor and the panel says so.

Tallied **a season at a time**, defaulting to the most recent, because a
linemate is a property of a roster and a roster lasts one season. A career
total ranks the boy somebody fed twenty times in one year level with the one
they shared a bench with twice a year for five, and reads as though both were
linemates. The season chips carry an *All seasons* option, since who a player
keeps ending up with across teams is worth seeing — it is just a different
question, and one worth asking on purpose. The chips are hidden altogether
where a player has only one season, which is its own career total.

Two-way players are shown with both stat lines, and birth years appear as
badges — which is not decoration: a quarter of players share a display name
with somebody else, so the year is how you tell two children apart.

### Reading a division off a name

503 schedule sides never matched a team row, so they have no division to filter
on. A third of the time the printed name states one outright — "San Jose Jr
Sharks Girls 16AAA" is a Girls 16AAA side whether or not anybody matched it —
and `names.division_from_team_name` reads it off, 207 of the 554 named sides,
every label naming a division the dataset actually has.

It requires **both** an age and a tier. "San Jose Jr Sharks 10-5" gives an age
and a club's own numbering, and answering `10U` would file the side somewhere it
does not belong; saying nothing is the honest answer. A bare "Anaheim Jr Ducks",
which is most of the remainder, was never recoverable this way.

The viewer therefore answers "what division is this side in" three ways, in
order of what each is worth: the team's own division where there is a team; else
the division the name states; else the division the game itself was filed in —
last, because that describes the fixture rather than the side, and in cross-tier
play it is nobody's division. For the 92 games where a club plays itself and the
site prints one name twice it is right 91 times. Between them, every game in the
schedule is reachable by some division filter; before, 92 were reachable by
none.

### Filters

Two pickers sit above the tabs, because both change what counts as a game in
every view below.

**League** — one of the four, or all of them. Four rather than nine: the CAHA
rounds roll up into CAHA, and the two out-of-state championships are gone. A
game that came from a round carries its name as a label instead, shown in the
Type column beside the game's own class — "Regular · Weekends" — and collapsed
to one word where the two would say the same thing.

**Games counted** — regular plus playoffs by default. Preseason and exhibition
games are a third of all stat lines, so folding them into a season total
quietly inflates it. The filter makes that a decision rather than an accident.
It is hidden on the Schedule view, along with the note beside it that counts
player seasons: neither means anything above a fixture list, and a control that
visibly does nothing is worse than no control. League still applies there.

The Schedule view adds three of its own: **Division**, **Club** and **Type**.

**Division** filters on the divisions the two *teams* are in, not on the game's
own level. Those are different questions and the answer differs for 2,868 of
27,097 team appearances — 24% in preseason. A 13U AAA team can play a 14U AA
team in a game the league files under 14U AAA, and that game should be findable
from either side, so it appears under `13U AAA` and under `14U AA` and not under
`14U AAA`. The table column still shows the game's **Level**; two different
words for two different things is the point.

Divisions are listed by name, because an id means nothing outside its own season
and the list spans six of them. A trailing flight, conference, pool or roman
numeral is folded into the division it belongs to — `10U B`, `10U B East`,
`10U B West` and `10U B II` are one entry, which takes the picker from 62 to 43.
Keeping them apart meant choosing `10U B` silently missed 383 games, and a short
list says nothing about being short. Only those markers are stripped and only
from the end: a tier is part of the name, so `10U BB` is not `10U B`, and
`tests/test_viewer.py` reads the rule back out of the shipped file to say so.

### Where the numbers came from

A footer on every page carries the two dates that are easy to confuse, because
a stale dataset behind a fresh viewer looks exactly like a working one:

```
Data collected Aug 23, 2026 (today)  ·  Viewer 2026.08.23
```

**Data collected** is `core.json`'s `metadata.generated` — when the collector
last ran and published. The collector runs unattended, so the way it fails is
by going quiet; two days without a publish is not a slow night, and past that
the line turns amber and says so. Nothing else on the page would show it.

**Viewer** is this file's own version, and it is a calendar date on purpose:
the version *is* the release date, so there is one fact to keep true rather
than two that can disagree. It is hand-maintained, because there is no build
step to stamp it — that is a property of a single self-contained file, not an
oversight. `tests/test_viewer.py` compares it against the file's last commit
date and fails when an edit lands without a bump.

The viewer version still shows when the dataset cannot be loaded at all, which
is exactly the moment somebody needs to quote it.

### What it deliberately does not do

Identity is settled by the collector, never in the browser. Players and teams
are addressed by id — a display name is a search term, not a handle, and a team
id is only unique within its season. Clubs arrive already canonicalised, each
with a short name to show and a kind, so only real clubs reach the pickers
while bracket slots, high schools and visiting teams keep their names for
schedules.

### On a phone

Most of the reading happens at a rink, so this was measured rather than
guessed. Three quarters of a 375px screen was chrome before a single row of
data: header, filter bar, and five dropdowns stacked one per line. It is 28%
now, and thirteen rows of data are visible where two were.

- The browse filters collapse behind a line naming what is set —
  `'25-'26 · TV Blue Devils · goalies · 5+ GP` — and open on a tap. Open in the
  markup, so it works with no script; closed at boot only where the screen is
  small enough to want it.
- Columns a phone does not need at a glance are dropped: number, division, PIM,
  PPG, SHG, points per game, type, rink. All of them details the player's or the
  game's own page carries. They are named rather than numbered, because the
  tables here have six shapes and index 3 means something different in each.
- **The table is made to fit rather than made to scroll**, and that is not a
  style choice. Any scrolling ancestor becomes the anchor for a `position:
  sticky` header inside it, so wrapping a table in a scroll box unpins its
  header from the title bar and strands it among the rows. Setting `overflow-x`
  alone is enough to do it: the browser computes the other axis to `auto` as
  soon as one of them is not `visible`. Whatever still will not fit gets a
  scroll box and gives up its header, which is measured per table rather than
  assumed — today two short ones.

Everything above `600px` is unaffected: thirteen columns, nothing dropped,
nothing scrolling.

To serve it locally:

```bash
python3 -m http.server 8777
```

Then open `http://localhost:8777/norcal_hockey_viewer.html`. With `data/app`
present it reads that; without it, the published branch.

---

## Repository layout

```
norcalstats/            the collector
  cli.py                command-line interface
  config.py             configuration
  db.py, schema.sql     database
  fetch.py              polite HTTP + page archive
  htmltable.py          tolerant table parser
  names.py              name and birth-year handling
  clubs.py              club identity: canonical name, short name, kind
  identity.py           player identity resolution
  pipeline.py           crawl orchestration
  export.py             the legacy JSON exports
  appdata.py            the app dataset the viewer reads
  review.py             the review queue and its decisions
  publish.py            git commit/push, and the app dataset branch
  sources/timetoscore.py  page parsers
deploy/                 systemd units and installer
tests/                  test suite and HTML fixtures
norcal_hockey_viewer.html
```

`scraper.js` and `scraper_caha.js` are the original v1 scripts, kept for
reference.
