# NorCal Youth Hockey Stats DB

A self-contained, single-file web application for browsing NorCal youth hockey player statistics across multiple seasons. Hosted on GitHub Pages at [lamfalus.github.io/norcal-hockey](https://lamfalus.github.io/norcal-hockey/norcal_hockey_viewer.html).

---

## Project Files

| File | Purpose |
|------|---------|
| `norcal_hockey_viewer.html` | The entire web app — all HTML, CSS, and JavaScript in one file |
| `norcal_hockey_players_s27-s31.json` | Player stats database (seasons S27–S31) |
| `scraper.js` | Browser console script used to rebuild the JSON from timetoscore.com |

---

## Data Source

All stats come from the NorCal CAHA league pages on [stats.caha.timetoscore.com](https://stats.caha.timetoscore.com). Seasons are numbered internally by the site:

| Season # | Hockey Season |
|----------|--------------|
| S27 | 2021–22 |
| S28 | 2022–23 |
| S29 | 2023–24 |
| S30 | 2024–25 |
| S31 | 2025–26 |

The start year for a given season number is: `1994 + seasonNumber` (e.g. S28 starts in 2022).

---

## The JSON Database

### Structure

```json
{
  "metadata": {
    "generated": "2024-...",
    "seasons": [27, 28, 29, 30, 31],
    "playerCount": 3121,
    "entryCount": 7000,
    "teamCount": 210
  },
  "players": {
    "Player Name": [
      {
        "season": 28,
        "division": "10U A",
        "team": "Cupertino Cougars 10A",
        "type": "skater",
        "jersey": "16",
        "GP": "10",
        "G": "3",
        "A": "5",
        "Hat": "0",
        "PIM": "",
        "PtsPerGame": "0.80",
        "Pts": "8"
      }
    ]
  }
}
```

Each player maps to an array of **entries** — one per team per season. A player who played on two teams in one season has two entries for that season. A player who is both a skater and a goalie has entries of `type: "skater"` and `type: "goalie"`.

### Skater entry fields
`season`, `division`, `team`, `type` ("skater"), `jersey`, `GP`, `G`, `A`, `Hat`, `PIM`, `PtsPerGame`, `Pts`

### Goalie entry fields
`season`, `division`, `team`, `type` ("goalie"), `jersey`, `GP`, `Shots`, `GA`, `GAA`, `Save%`, `SO`

> **Note on SO column:** The goalie stats table on timetoscore.com has columns `Name, #, GP, Shots, GA, GAA, Save%, Goals, Ass., Pts, SO, TOI, W, L, …`. SO is at index 10. Earlier scraper versions incorrectly used index 7 (which captured the goalie's skater Goals, usually 0). The current `scraper.js` uses the correct index.

---

## How the App Works

### Auto-fetch

On page load, the app fetches the JSON from GitHub's raw content CDN:

```
https://raw.githubusercontent.com/lamfalus/norcal-hockey/main/norcal_hockey_players_s27-s31.json
```

This endpoint serves `Access-Control-Allow-Origin: *`, so the fetch works from any origin including a locally served file. A status indicator in the header confirms when data has loaded. The JSON is embedded as a fallback stub inside the HTML so the page is never completely empty.

### Name Normalization

After each data load, all player names are run through a multi-pass normalization pipeline via `refreshBirthYears()`. The passes run in order:

1. **`stripPeriodsFromNames()`** — removes all `.` characters from every name and collapses any resulting double spaces. This handles punctuation variants like `Avery St. Onge` → `Avery St Onge`.

2. **`mergeCaseVariants()`** — groups names that are identical when lowercased and merges them into a single canonical record (keeping the spelling with the most entries). This handles data-entry inconsistencies like `Carlos Ayon Ii` → `Carlos Ayon II`. Only merges when no season overlap exists across the variants.

3. **`mergeNumericSuffixVariants()`** — groups names that share the same base after stripping a trailing Roman numeral or Arabic number (`II`, `III`, `2`, `3`, etc.) and merges them. This handles cases like `Carlos Ayon II` and `Carlos Ayon 3` being the same player entered inconsistently across seasons. Only merges when no season overlap exists.

4. **`resolveCollisions()`** — handles edge cases where the above merges would produce conflicting season data.

5. **`mergeMiddleNameVariants()`** — groups names that share the same first and last name (ignoring any middle name or initial) and merges them. This handles cases like `Shiv Ritesh Kadu` → `Shiv Kadu`. Only merges when no season overlap exists and the shorter (two-word) form is the canonical name.

After normalization, entries with the same resulting name are combined into a single player record. This is how two-way players (appearing in both skater and goalie tables) are unified.

### Birth Year Inference

For players with entries in two or more seasons, the app infers a probable birth year from their division history. Each age-group division implies a 2-year birth window (e.g., playing 10U in a season that starts in 2022 means born in 2012 or 2013). By intersecting the windows across all of a player's seasons, the app narrows the range. If it resolves to a single year, that birth year is displayed as a small badge (e.g., `'12`) next to the player's name everywhere it appears.

### Two-Way Players

Some players appear in both the skater stats table and the goalie stats table for the same season (e.g., a forward who also backs up in goal). After name normalization merges their entries, the Player Lookup view detects this (`isTwoWay = skaterEntries.length > 0 && goalieEntries.length > 0`) and:
- Labels them with an **⛸ + 🥅 Two-Way** badge instead of "🥅 Goalie"
- Shows career summary cards for both skater stats and goalie stats
- Renders both skater and goalie tables in each season section where they appear in both roles

### Club Extraction

Team names like `"Cupertino Cougars 10A"` or `"San Jose Jr Sharks 10A-1"` are parsed by `extractClub()` to produce a canonical club name (e.g., `"Cupertino Cougars"`, `"San Jose Jr Sharks"`). This is used in Club View aggregation and the Player Flow diagram.

---

## The Four Views

### 1. Browse by Season

Browse all players for a selected season with filters:

- **Season** — S27 through S31
- **View by** — Division or Team
- **Filter** — narrows to a specific division or team within the selected view
- **Type** — Skaters / Goalies / All
- **Min GP** — minimum games played (1–9), useful for filtering out callups and single-game appearances

All columns in both the skater and goalie tables are **sortable** by clicking the column header. Skaters default to sort by Points descending; goalies default to GAA ascending. Clicking a player name navigates directly to their full history in Player Lookup.

### 2. Club View

Browse all players historically associated with a club (e.g., "Cupertino Cougars" across all their teams and divisions). Controls:

- **Club** — select from all clubs in the database
- **Season** — optionally narrow to a single season; defaults to all seasons

The view shows aggregate totals at the top (total unique players, total goals, etc.) followed by team cards. Clicking a team card expands to show that team's full roster in a single unified table. Skaters and goalies are rendered in the same table with shared columns — **#** (rank), **Player**, **Jersey**, and **GP** align vertically between both groups. A section divider and goalie sub-header row separate the two groups within the table.

### 3. Player Lookup

Type-ahead search across all ~3,100 player names. The autocomplete list shows the player's name, inferred birth year badge, and a summary of which seasons they played. Keyboard navigation (↑↓ arrows, Enter, Escape) is supported.

Selecting a player shows:

- **Header** — name, birth year badge, and role badge (blank for skaters, 🥅 Goalie, or ⛸ + 🥅 Two-Way)
- **Career summary cards** — aggregate GP, Goals, Assists, Points, Hat Tricks, PIM for skaters; GP, Goals Allowed, Shutouts for goalies; both sets for two-way players
- **Season-by-season breakdown** — one section per season, each showing skater and/or goalie tables with team, division, and per-season stats

Clicking any player name in Browse or Club View calls `goToPlayer()` which switches to Player Lookup and loads that player's history automatically.

### 4. Player Flow

An interactive SVG alluvial (Sankey-style) diagram showing player migration between clubs across seasons. Flow data includes both skaters and goalies. Controls:

- **From Season / To Season** — define the range of seasons to visualize
- **Age Division** — optionally filter to a specific division (e.g., 10U A only)
- **Min players per flow** — hides thin ribbons below a threshold to reduce clutter
- **Focus Club** — pin a specific club to highlight only its flows

**Hover behavior:**
- Hovering a source (left) club block highlights all outgoing ribbons with a player count label on each, dimming others. The native tooltip shows the total player count for that club.
- Hovering a destination (right) club block highlights all incoming ribbons.
- When a **Focus Club** is selected from the dropdown, hover behavior becomes focus-aware:
  - Hovering a source block shows only the count flowing to the focused club (not the total), and the tooltip updates to reflect that specific flow.
  - Hovering a destination block shows only the count received from the focused club, with a matching tooltip.
  - The block total label is suppressed while a focus club is active, so only the relevant flow count is shown.
- Mouseout restores all ribbons and labels.

**Click behavior:**
- Clicking any club block opens a player list popup showing who moved between clubs.
- When a focus club is active, clicking a source block shows players flowing to that focus club; clicking a destination block shows players received from the focus club.

A color-coded legend below the diagram identifies each club.

---

## Rebuilding the JSON

Run `scraper.js` in the browser console while on any `stats.caha.timetoscore.com` page. The script:

1. For each season (S27–S31), fetches the division/team listing page to get all team IDs and their divisions
2. Fetches each team's schedule page (`display-schedule?team=ID&season=N&league=3&stat_class=1`)
3. Parses `Table[1]` (skater stats, rows 3+) and `Table[2]` (goalie stats, rows 3+)
4. Waits 200ms between requests to avoid hammering the server
5. Auto-downloads the result as `norcal_hockey_players_s27-s31.json`

After downloading, replace the file in the GitHub repo and commit. GitHub's raw CDN typically reflects the new file within 30–60 seconds, after which a page refresh will pull the updated data.

---

## Local Testing

With Node.js installed, serve the project folder locally:

```bash
npx serve . -l 8080
```

Then open `http://localhost:8080/norcal_hockey_viewer.html`. The auto-fetch will still pull the JSON from GitHub since it uses an absolute URL.
