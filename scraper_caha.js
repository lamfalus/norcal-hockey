// CAHA Stats Scraper
// ─────────────────────────────────────────────────────────────────────────────
// Run this in the browser console while on https://caha.com/stats.pl
//
// Dynamically discovers division IDs for each year by reading the Stats by
// Division table, then fetches player and goalie stats for every division via
// POST. Covers years 2021–2025 (seasons S27–S31).
//
// Division structure varies by year — e.g. 2021 has "Conference 1/2" in the
// AA column for 12U/14U/16U whereas later years use plain "AA". The dynamic
// approach handles this automatically.
//
// Skipped: High School rows, Women rows, Senior column.
//
// HOW THE POST WORKS:
//   POST /stats.pl with fields:
//     d   = division ID (discovered from the year's division table)
//     y   = year (e.g. 2025)
//     t   = 0   (-- Entire Division --)
//     g   = 0   (Player Stats) or 1 (Goalie Stats)
//     p   = 2   (Regular Season)
//     f   = 500 (row count)
//     id  = "Get Stats"
//
// PLAYER TABLE columns (index 0–11):
//   # | Player | Team | GP | G | A | PTS | PPG | SHG | PEN | PIM | SUSP
//
// GOALIE TABLE columns (index 0–11):
//   # | Player | Team | GP | MINS | SHOTS | SAVES | SVA | GAA | PEN | PIM | SUSP
//
// SEASON MAPPING:  season = year - 1994
//   2021→S27  2022→S28  2023→S29  2024→S30  2025→S31

(async function() {

  const YEARS        = [2021, 2022, 2023, 2024, 2025];
  const SKIP_ROWS    = ['High School', 'Women'];
  const SKIP_COLS    = ['Senior'];
  // Map column header → gender
  const COL_GENDER   = { 'AA': 'Boys', 'AAA': 'Boys', 'Girls AA': 'Girls', 'Girls AAA': 'Girls' };
  const delay        = ms => new Promise(r => setTimeout(r, ms));

  // ── Name helpers ───────────────────────────────────────────────────────────

  function toTitleCase(str) {
    return str.toLowerCase().replace(/\b\w+\b/g, word => {
      if (/^(ii|iii|iv|vi|vii|viii)$/i.test(word)) return word.toUpperCase();
      if (/^(jr|sr)$/i.test(word)) return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
      return word.charAt(0).toUpperCase() + word.slice(1);
    });
  }
  // "JAXON GILLIS #7"   → "Jaxon Gillis"
  function parseName(raw)   { return toTitleCase(raw.replace(/\s*#\w+\s*$/, '').trim()); }
  // "JAXON GILLIS #7"   → "7"
  function parseJersey(raw) { const m = raw.match(/#(\w+)\s*$/); return m ? m[1] : ''; }
  // "Jr Reign (1)"      → "Jr Reign"
  function cleanTeam(raw)   {
    var m = (raw || '').match(/^(.*?)\s*\((\d+)\)\s*$/);
    return m ? m[1].trim() + '-' + m[2] : (raw || '').trim();
  }
  // "18U / 19U"         → "18U/19U"
  function normAge(raw)     { return raw.replace(/\s*\/\s*/g, '/').trim(); }

  function cellText(cells, i) { return cells[i] ? (cells[i].textContent || '').trim() : ''; }

  // ── Discover divisions for a given year ────────────────────────────────────

  async function getDivisionsForYear(year) {
    const resp = await fetch('/stats.pl?y=' + year);
    const html = await resp.text();
    const doc  = new DOMParser().parseFromString(html, 'text/html');

    // Division table is table index 5
    const divTable = doc.querySelectorAll('table')[5];
    if (!divTable) return [];

    const rows = [...divTable.querySelectorAll('tr')];
    if (rows.length < 3) return [];

    // Row 0 = merged title/year-selector row
    // Row 1 = column headers (Age/Group, AA, AAA, Girls AA, Girls AAA, Senior)
    // Rows 2+ = data rows
    const colHeaders = [...rows[1].querySelectorAll('th,td')]
                         .map(c => c.textContent.trim());

    const divisions = [];

    rows.slice(2).forEach(row => {
      const cells = [...row.querySelectorAll('td')];
      if (!cells.length) return;

      const ageGroup = normAge(cells[0].textContent.trim());
      if (SKIP_ROWS.includes(ageGroup)) return;

      cells.forEach((cell, colIdx) => {
        if (colIdx === 0) return;                        // skip age label column
        const colHeader = colHeaders[colIdx] || '';
        if (SKIP_COLS.includes(colHeader)) return;

        const gender = COL_GENDER[colHeader];
        if (!gender) return;                             // unknown column, skip

        // Each cell may have multiple links (e.g. AAA Major + AAA Minor)
        [...cell.querySelectorAll('a')].forEach(link => {
          const m = (link.getAttribute('href') || '').match(/d=(\d+)/);
          if (!m) return;

          const divId    = parseInt(m[1]);
          const linkText = link.textContent.trim();

          // Tier: prefer the link text (e.g. "AAA Major", "Conference 1")
          // For single-link cells where link text matches col header, use col header
          const tier  = linkText || colHeader;
          const label = ageGroup + ' ' + gender + ' ' + tier;

          divisions.push({ id: divId, label, age: ageGroup, tier, gender });
        });
      });
    });

    return divisions;
  }

  // ── Fetch a stats table via POST ───────────────────────────────────────────

  async function fetchStats(divId, year, type) {
    const body = new URLSearchParams({
      d:  String(divId),
      y:  String(year),
      t:  '0',
      g:  String(type),   // 0 = Player Stats, 1 = Goalie Stats
      p:  '2',            // Regular Season
      f:  '500',
      id: 'Get Stats'
    });
    const resp = await fetch('/stats.pl', {
      method:  'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body:    body.toString()
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const html = await resp.text();
    const doc  = new DOMParser().parseFromString(html, 'text/html');
    return doc.querySelectorAll('table')[7] || null;
  }

  // ── Main collection loop ───────────────────────────────────────────────────

  const players    = {};
  let   entryCount = 0;
  const errors     = [];
  const allDivisions = {};   // year → division list (stored for metadata)

  function add(name, entry) {
    if (!name) return;
    if (!players[name]) players[name] = [];
    players[name].push(entry);
    entryCount++;
  }

  for (const year of YEARS) {
    const season = year - 1994;
    console.log(`\n── Year ${year} (S${season}) ──`);

    const divisions = await getDivisionsForYear(year);
    allDivisions[year] = divisions.map(d => d.label);
    console.log(`  Found ${divisions.length} divisions: ${divisions.map(d=>d.label).join(', ')}`);

    const total = divisions.length * 2;
    let   done  = 0;

    for (const div of divisions) {

      // ── Player (skater) stats ──
      try {
        const table = await fetchStats(div.id, year, 0);
        if (table) {
          [...table.querySelectorAll('tr')].slice(1).forEach(row => {
            const c = [...row.querySelectorAll('td')];
            if (c.length < 5) return;
            const raw = cellText(c, 1);
            if (!raw || raw === 'Player') return;
            const name = parseName(raw);
            if (!name) return;
            add(name, {
              season,
              year,
              divisionId: div.id,
              division:   div.label,
              ageGroup:   div.age,
              tier:       div.tier,
              gender:     div.gender,
              team:       cleanTeam(cellText(c, 2)),
              jersey:     parseJersey(raw),
              type:       'skater',
              GP:   cellText(c, 3),
              G:    cellText(c, 4),
              A:    cellText(c, 5),
              PTS:  cellText(c, 6),
              PPG:  cellText(c, 7),
              SHG:  cellText(c, 8),
              PEN:  cellText(c, 9),
              PIM:  cellText(c, 10),
              SUSP: cellText(c, 11),
            });
          });
        }
      } catch(e) {
        const msg = `S${season} ${div.label} skaters: ${e.message}`;
        errors.push(msg);
        console.warn('✗ ' + msg);
      }
      done++;
      console.log(`  [${done}/${total}] ${div.label} — skaters`);
      await delay(2500);

      // ── Goalie stats ──
      // Columns: # | Player | Team | GP | MINS | SHOTS | SAVES | SVA | GAA | PEN | PIM | SUSP
      try {
        const table = await fetchStats(div.id, year, 1);
        if (table) {
          [...table.querySelectorAll('tr')].slice(1).forEach(row => {
            const c = [...row.querySelectorAll('td')];
            if (c.length < 5) return;
            const raw = cellText(c, 1);
            if (!raw || raw === 'Player') return;
            const name = parseName(raw);
            if (!name) return;
            add(name, {
              season,
              year,
              divisionId: div.id,
              division:   div.label,
              ageGroup:   div.age,
              tier:       div.tier,
              gender:     div.gender,
              team:       cleanTeam(cellText(c, 2)),
              jersey:     parseJersey(raw),
              type:       'goalie',
              GP:    cellText(c, 3),
              MINS:  cellText(c, 4),
              SHOTS: cellText(c, 5),
              SAVES: cellText(c, 6),
              SVA:   cellText(c, 7),
              GAA:   cellText(c, 8),
              PEN:   cellText(c, 9),
              PIM:   cellText(c, 10),
              SUSP:  cellText(c, 11),
            });
          });
        }
      } catch(e) {
        const msg = `S${season} ${div.label} goalies: ${e.message}`;
        errors.push(msg);
        console.warn('✗ ' + msg);
      }
      done++;
      console.log(`  [${done}/${total}] ${div.label} — goalies`);
      await delay(2500);
    }
  }

  // ── Output ─────────────────────────────────────────────────────────────────

  const output = {
    metadata: {
      generated:   new Date().toISOString(),
      source:      'caha.com/stats.pl',
      seasons:     YEARS.map(y => ({ year: y, season: y - 1994 })),
      divisionsPerYear: allDivisions,
      playerCount: Object.keys(players).length,
      entryCount,
      errors,
    },
    players
  };

  console.log(`\n✓ ${Object.keys(players).length} players, ${entryCount} entries`);
  if (errors.length) console.warn(`${errors.length} errors:`, errors);

  const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'caha_players_s27-s31.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

})();
