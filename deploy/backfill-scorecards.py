#!/usr/bin/env python3
"""Backfill the PDF goalie scorecards, one older season per run.

The nightly ``update`` collects scorecards for the current season only, because
each is a second request per game and the whole history is ~8,600 played games.
This walks the older seasons instead -- oldest first, one per invocation -- so a
timer can fill them in a season a night without ever hammering the site.

It only *fetches*: the nightly ``update`` that follows derives, exports and
publishes, and its derive applies every stored goalie record across all seasons.
So a season fetched here at 00:30 goes live with the 03:30 run.

A season is "done" once every played game has been attempted -- a stored record,
a non-reconciling rejection, or a fetch error all count -- so one permanently
broken game can never pin the backfill on a season forever.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Run from the repo root (the systemd unit sets WorkingDirectory), so the
# package imports without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from norcalstats import db, pipeline  # noqa: E402
from norcalstats.cli import _fetcher  # noqa: E402
from norcalstats.config import Config  # noqa: E402

log = logging.getLogger("backfill-scorecards")


def _remaining(conn, season: int) -> int:
    """Games in a season a scorecard could still be fetched for.

    Must match ``pending_scorecards``' own filter -- notably ``has_scoresheet``:
    a game with a final score but no scoresheet posted has no scorecard either,
    so it is not "remaining" and must not pin the backfill on this season
    forever.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM games "
        " WHERE season_id = ? AND status = 'final' AND has_scoresheet = 1"
        "   AND home_goals IS NOT NULL AND away_goals IS NOT NULL"
        "   AND scorecard_at IS NULL AND scorecard_error IS NULL",
        (season,),
    ).fetchone()[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--limit", type=int,
                        help="cap scorecards this run (default: a whole season)")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    conn = db.connect(config.db_path)
    try:
        played = [r["season_id"] for r in conn.execute(
            "SELECT DISTINCT season_id FROM games "
            " WHERE status='final' AND home_goals IS NOT NULL "
            " ORDER BY season_id")]
        if not played:
            log.info("no played games yet; nothing to backfill")
            return 0

        # The newest played season is the nightly's job; everything older is ours.
        current = max(played)
        historical = [s for s in played if s < current]

        target = next((s for s in historical if _remaining(conn, s) > 0), None)
        if target is None:
            log.info("scorecard backfill complete: seasons %s all attempted "
                     "(current season %d is the nightly's)",
                     ", ".join(f"S{s}" for s in historical), current)
            return 0

        pipe = pipeline.Pipeline(conn, config, _fetcher(config))
        pending = pipe.pending_scorecards([target])
        # Anything already archived but flagged for re-parse (a parser-version
        # bump) is read from disk, not re-downloaded -- only genuinely new games
        # cost a request.
        from_archive = pipe.reparsable_scorecards([target])
        reparse = [g for g in pending if g in from_archive]
        fetch = [g for g in pending if g not in from_archive]
        log.info("backfilling S%d: %d pending (%d re-parsed from archive, "
                 "%d to fetch; %d never attempted)",
                 target, len(pending), len(reparse), len(fetch),
                 _remaining(conn, target))
        # Fetch only; the nightly update derives, exports and publishes.
        if reparse:
            pipe.fetch_scorecards(reparse, use_cache=True, limit=args.limit)
        pipe.fetch_scorecards(fetch, use_cache=False, limit=args.limit)
        log.info("S%d: stored %d, %d with nothing reconciling, %d error(s); "
                 "%d still unattempted",
                 target, pipe.stats.scorecards, pipe.stats.scorecards_empty,
                 pipe.stats.errors, _remaining(conn, target))
        if pipe.stats.stopped_early:
            log.warning("hit the request ceiling; re-run to continue S%d", target)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
