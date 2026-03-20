// NorCal Hockey Stats Scraper
// Run this in the browser console while on any stats.caha.timetoscore.com page.
// It will scrape all player and goalie stats for seasons S27-S31 and
// auto-download the result as norcal_hockey_players_s27-s31.json.
//
// Goalie SO column fix: SO is at column index 10 (not 7).
// Goalie table columns:
//   Name(0) #(1) GP(2) Shots(3) GA(4) GAA(5) Save%(6)
//   Goals(7) Ass.(8) Pts(9) SO(10) TOI(11) W(12) L(13) ...

(async function() {
  const SEASONS = [27,28,29,30,31];
  const LEAGUE  = 3;
  const delay   = ms => new Promise(r => setTimeout(r, ms));
  const players = {};
  let teamCount = 0, entryCount = 0;

  function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
  function cell(cells, i){ return norm(cells[i] ? cells[i].innerText : ''); }

  function add(name, entry) {
    const n = norm(name);
    if (!n || n === 'Name') return;
    if (!players[n]) players[n] = [];
    players[n].push(entry);
    entryCount++;
  }

  async function getDoc(url) {
    const r = await fetch(url);
    const h = await r.text();
    return new DOMParser().parseFromString(h, 'text/html');
  }

  // Parse the display-stats page to get all teams and their divisions
  async function getTeams(season) {
    const doc   = await getDoc(`/display-stats?league=${LEAGUE}&season=${season}`);
    const teams = [];
    const seen  = new Set();
    let curDiv  = '';
    doc.querySelectorAll('table tr').forEach(row => {
      const rowText = row.innerText.trim();
      // Division header rows contain "Schedule" but no team links
      if (rowText.includes('Schedule') && !row.querySelector('a[href*="display-schedule"]')) {
        const m = rowText.match(/^(.*?)\s*Schedule/);
        if (m) curDiv = m[1].trim();
        return;
      }
      // Team rows contain a link to display-schedule
      const link = row.querySelector('a[href*="display-schedule"]');
      if (!link) return;
      const m = link.href.match(/[?&]team=(\d+)/);
      if (!m) return;
      const teamId = m[1];
      if (seen.has(teamId)) return;
      seen.add(teamId);
      teams.push({ teamId, teamName: norm(link.innerText), division: curDiv });
    });
    return teams;
  }

  for (const season of SEASONS) {
    console.log(`\n── Season ${season} ──`);
    const teams = await getTeams(season);
    console.log(`  ${teams.length} teams`);

    for (const t of teams) {
      const url = `/display-schedule?team=${t.teamId}&season=${season}&league=${LEAGUE}&stat_class=1`;
      try {
        const doc    = await getDoc(url);
        const tables = doc.querySelectorAll('table');

        // ── Skater stats: Table[1], skip first 2 header rows ──
        // Columns: Name(0) #(1) GP(2) Goals(3) Ass.(4) PPG(5) PPA(6) SHG(7) SHA(8)
        //          GWG(9) GWA(10) PSG(11) ENG(12) UAG(13) IGI(14) GIA(15) TGA(16)
        //          TGA(17) FGS(18) OGS(19) OAG(20) Shots(21) +/-(22) Hat(23) Pts(24)
        if (tables[1]) {
          [...tables[1].querySelectorAll('tr')].slice(2).forEach(row => {
            const c = [...row.querySelectorAll('td')];
            if (c.length < 5) return;
            const name = norm(c[0].innerText);
            if (!name || name === 'Name') return;
            const gp  = parseInt(cell(c,2))  || 0;
            const pts = c.length > 24 ? parseInt(cell(c,24)) || 0 : 0;
            add(name, {
              season,
              division:   t.division,
              team:       t.teamName,
              type:       'skater',
              jersey:     cell(c,1),
              GP:         cell(c,2),
              G:          cell(c,3),
              A:          cell(c,4),
              Hat:        c.length > 23 ? cell(c,23) : '0',
              PIM:        '',
              PtsPerGame: gp > 0 ? (pts/gp).toFixed(2) : '0.00',
              Pts:        String(pts),
            });
          });
        }

        // ── Goalie stats: Table[2], skip first 2 header rows ──
        // Columns: Name(0) #(1) GP(2) Shots(3) GA(4) GAA(5) Save%(6)
        //          Goals(7) Ass.(8) Pts(9) SO(10) TOI(11) W(12) L(13) OTL(14) ...
        if (tables[2]) {
          [...tables[2].querySelectorAll('tr')].slice(2).forEach(row => {
            const c = [...row.querySelectorAll('td')];
            if (c.length < 7) return;
            const name = norm(c[0].innerText);
            if (!name || name === 'Name') return;
            add(name, {
              season,
              division:  t.division,
              team:      t.teamName,
              type:      'goalie',
              jersey:    cell(c,1),
              GP:        cell(c,2),
              Shots:     cell(c,3),
              GA:        cell(c,4),
              GAA:       cell(c,5),
              'Save%':   cell(c,6),
              SO:        c.length > 10 ? cell(c,10) : '0',  // index 10, NOT 7
            });
          });
        }

        teamCount++;
        if (teamCount % 10 === 0) console.log(`  ${teamCount} teams scraped…`);
      } catch(e) {
        console.warn(`  ✗ team ${t.teamId} S${season}: ${e.message}`);
      }
      await delay(200);
    }
  }

  const output = {
    metadata: {
      generated:   new Date().toISOString(),
      seasons:     SEASONS,
      playerCount: Object.keys(players).length,
      entryCount,
      teamCount,
    },
    players
  };

  console.log(`\n✓ ${Object.keys(players).length} players, ${entryCount} entries, ${teamCount} teams`);

  const blob = new Blob([JSON.stringify(output, null, 2)], {type:'application/json'});
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'norcal_hockey_players_s27-s31.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
})();
