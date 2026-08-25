#!/usr/bin/env python3
"""Decompress the archived scorecard PDFs into a readable, sortable tree.

The collector stores each scorecard gzipped as ``s{season}_scorecard_{id}.pdf.gz``
-- compact, but a ``.pdf.gz`` will not preview in Google Drive and the name is
just a number. This writes a plain-PDF mirror named for the game and foldered by
season and division:

    NorCal Scorecards/
      2025-26/
        12U AA/
          2025-08-22  Cupertino Cougars 12-1 @ SJ Jr Sharks 12-2  (6-4)  #56739.pdf

Date-led so a folder sorts chronologically, visitor @ home, score in parens, and
the game id last so a file is still traceable back to the database. ``rclone``
then copies this tree to Drive; the mirror is what makes that copy incremental.

Standard library only. Read-only on the database.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from norcalstats.config import Config  # noqa: E402

log = logging.getLogger("export-scorecards")

# Characters no filesystem (or Drive) should carry in a name. Slash would make a
# spurious folder; the rest are Windows-illegal and best avoided everywhere.
_ILLEGAL = re.compile(r'[/\\:*?"<>|]+')


def clean(text: str) -> str:
    """A filename-safe fragment: illegal characters gone, whitespace collapsed."""
    text = _ILLEGAL.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    # A trailing dot or space is stripped by Windows/Drive and confuses syncs.
    return text.strip(" .") or "unknown"


def season_label(start_year: int) -> str:
    """2025 -> '2025-26'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def rows(conn: sqlite3.Connection):
    return conn.execute("""
        SELECT g.game_id, g.season_id, g.date_iso, g.level, g.division_id,
               g.home_team_id, g.away_team_id, g.home_name, g.away_name,
               g.home_goals, g.away_goals,
               s.start_year,
               ht.name AS home_team, at.name AS away_team,
               d.name  AS div_name
          FROM games g
          JOIN seasons s ON s.season_id = g.season_id
          LEFT JOIN teams ht ON ht.team_id = g.home_team_id AND ht.season_id = g.season_id
          LEFT JOIN teams at ON at.team_id = g.away_team_id AND at.season_id = g.season_id
          LEFT JOIN divisions d ON d.division_id = g.division_id
         WHERE g.scorecard_at IS NOT NULL
         ORDER BY g.date_iso, g.game_id
    """)


def plan(r: sqlite3.Row) -> tuple[str, str, str]:
    """(season_folder, division_folder, filename) for one game."""
    season = season_label(r["start_year"]) if r["start_year"] else f"S{r['season_id']}"
    division = clean(r["level"] or r["div_name"] or "Unknown division")
    # Each field is cleaned on its own, then joined with double-space separators
    # -- so the spacing that groups the name survives (cleaning the whole string
    # would collapse it), while nothing illegal slips through inside a field.
    home = clean(r["home_team"] or r["home_name"] or f"team {r['home_team_id']}")
    away = clean(r["away_team"] or r["away_name"] or f"team {r['away_team_id']}")
    date = r["date_iso"] or "0000-00-00"
    hg, ag = r["home_goals"], r["away_goals"]
    score = f"  ({ag}-{hg})" if hg is not None and ag is not None else ""
    name = f"{date}  {away} @ {home}{score}  #{r['game_id']}.pdf"
    return season, division, name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path,
                        help="mirror root (default: <data_dir>/scorecards)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written, touch nothing")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    raw_dir = Path(config.raw_dir)
    out = args.out or (Path(config.data_dir) / "scorecards")
    conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    written = skipped = missing = 0
    for n, r in enumerate(rows(conn)):
        if args.limit and n >= args.limit:
            break
        src = raw_dir / f"s{r['season_id']}_scorecard_{r['game_id']}.pdf.gz"
        if not src.is_file():
            missing += 1
            continue
        season, division, name = plan(r)
        dest_dir = out / season / division
        dest = dest_dir / name

        # Already there under this game id (even if the readable part changed):
        # skip, so a re-run costs nothing and never duplicates a game.
        if args.dry_run:
            print(f"{season}/{division}/{name}")
            written += 1
            continue
        if list(dest_dir.glob(f"*#{r['game_id']}.pdf")):
            skipped += 1
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(src, "rb") as fh:
            data = fh.read()
        tmp = dest.with_suffix(".pdf.tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        written += 1

    log.info("%s %d scorecard(s)%s%s",
             "would write" if args.dry_run else "wrote", written,
             f", {skipped} already present" if skipped else "",
             f", {missing} with no archived PDF" if missing else "")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
