"""Command-line interface.

    norcalstats update              nightly incremental run (the cron job)
    norcalstats backfill            one-time historical crawl
    norcalstats reparse             re-parse archived pages, no network
    norcalstats derive              rebuild identities and stats only
    norcalstats export              write the JSON exports
    norcalstats publish             commit and push the exports
    norcalstats status              what is in the database
    norcalstats seasons             seasons the site currently lists
    norcalstats audit               data-quality findings
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import (
    db, export as export_mod, pipeline, publish as publish_mod, review as review_mod,
)
from .config import Config
from .fetch import Fetcher
from .sources import timetoscore as tts

log = logging.getLogger("norcalstats")


def _setup_logging(level: str, log_file: Optional[Path]) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="norcalstats",
        description="NorCal youth hockey stats collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", type=Path, help="path to a JSON config file")
    parser.add_argument("--data-dir", type=Path, help="where the database and page archive live")
    parser.add_argument("--db", dest="db_path", type=Path, help="database path")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--log-file", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_crawl_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--season", type=int, action="append", dest="seasons",
                       help="season number; repeatable (default: all known)")
        p.add_argument("--league", type=int, action="append", dest="leagues",
                       help="league id; repeatable, in priority order "
                            "(3=Norcal, 5=CAHA tier 1/2, 4=SCAHA)")
        p.add_argument("--team", type=int, action="append", dest="teams",
                       help="restrict to these team ids; repeatable")
        p.add_argument("--delay", type=float, help="seconds between requests")
        p.add_argument("--limit", type=int, help="cap scoresheets fetched this run")
        p.add_argument("--max-requests", type=int, help="hard ceiling on requests")
        p.add_argument("--no-export", action="store_true", help="skip writing JSON")
        p.add_argument("--publish", action="store_true", help="commit and push exports")
        p.add_argument("--dry-run", action="store_true", help="do not commit or push")

    p_update = sub.add_parser("update", help="incremental run (nightly)")
    add_crawl_args(p_update)

    p_backfill = sub.add_parser("backfill", help="one-time historical crawl")
    add_crawl_args(p_backfill)
    p_backfill.add_argument("--from-season", type=int, help="lowest season to crawl")
    p_backfill.add_argument("--to-season", type=int, help="highest season to crawl")

    p_reparse = sub.add_parser("reparse", help="re-parse archived pages (no network)")
    p_reparse.add_argument("--season", type=int, action="append", dest="seasons")
    p_reparse.add_argument("--team", type=int, action="append", dest="teams")
    p_reparse.add_argument("--no-export", action="store_true")

    sub.add_parser("derive", help="rebuild identities and stats from stored rows")

    p_export = sub.add_parser("export", help="write JSON exports")
    p_export.add_argument("--out", type=Path, help="output directory")
    p_export.add_argument("--game-logs", action="store_true",
                          help="include per-game logs in the rich export")
    p_export.add_argument("--app", type=Path, metavar="DIR",
                          help="also write the web app dataset (core + shards)")

    p_publish = sub.add_parser("publish", help="commit and push the exports")
    p_publish.add_argument("--repo", type=Path, help="repository path (default: export dir)")
    p_publish.add_argument("--dry-run", action="store_true")
    p_publish.add_argument("--no-push", action="store_true", help="commit but do not push")
    p_publish.add_argument("--force", action="store_true",
                           help="publish even if the export lost most of its players")

    sub.add_parser("status", help="summarize the database")
    sub.add_parser("seasons", help="list the seasons the site currently offers")

    p_leagues = sub.add_parser("leagues", help="leagues carrying games, per season")
    p_leagues.add_argument("--season", type=int, help="only this season")
    p_leagues.add_argument("--discover", action="store_true",
                           help="probe the site for leagues rather than reading the database")
    p_leagues.add_argument("--all", action="store_true",
                           help="show skipped leagues too")
    p_leagues.add_argument("--include", type=int, action="append", default=[],
                           metavar="ID", help="start collecting this league")
    p_leagues.add_argument("--exclude", type=int, action="append", default=[],
                           metavar="ID", help="stop collecting this league")

    p_multi = sub.add_parser(
        "double-rostered",
        help="players who played for more than one team in a season")
    p_multi.add_argument("--season", type=int, help="only this season")
    p_multi.add_argument("--girls", action="store_true",
                         help="only girls-team + co-ed-team pairings")
    p_multi.add_argument("--limit", type=int, default=40)

    p_audit = sub.add_parser("audit", help="show data-quality findings")
    p_audit.add_argument("--kind", help="only this anomaly kind")
    p_audit.add_argument("--limit", type=int, default=20)

    p_review = sub.add_parser(
        "review", help="questions about names and teams that need your decision")
    review_sub = p_review.add_subparsers(dest="review_command")

    p_list = review_sub.add_parser("list", help="open questions (the default)")
    p_list.add_argument("--kind", help="same_name, nickname, ambiguous_team, ...")
    p_list.add_argument("--limit", type=int, default=25)
    p_list.add_argument("-v", "--verbose", action="store_true", help="show evidence")

    p_show = review_sub.add_parser("show", help="one question in full")
    p_show.add_argument("item_id", type=int)

    p_answer = review_sub.add_parser("answer", help="decide a question")
    p_answer.add_argument("item_id", type=int)
    p_answer.add_argument(
        "action", choices=sorted(review_mod.ACTIONS),
        help="; ".join(f"{k}: {v}" for k, v in sorted(review_mod.ACTIONS.items())))
    p_answer.add_argument("--note", default="", help="why, for your future self")
    p_answer.add_argument(
        "--person", action="append", default=[], metavar="SEASON=KEY",
        help="for 'split': which child played which season, e.g. --person 29=a")
    p_answer.add_argument("--no-rebuild", action="store_true",
                          help="do not re-derive immediately")

    p_reopen = review_sub.add_parser("reopen", help="undo a decision")
    p_reopen.add_argument("item_id", type=int)

    return parser


def _config(args: argparse.Namespace) -> Config:
    overrides = {
        "data_dir": getattr(args, "data_dir", None),
        "db_path": getattr(args, "db_path", None),
        "log_level": getattr(args, "log_level", None),
        "log_file": getattr(args, "log_file", None),
        "delay": getattr(args, "delay", None),
        "max_requests": getattr(args, "max_requests", None),
        "leagues": getattr(args, "leagues", None),
    }
    if getattr(args, "out", None):
        overrides["export_dir"] = args.out
    return Config.load(getattr(args, "config", None),
                       **{k: v for k, v in overrides.items() if v is not None})


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config(args)
    _setup_logging(config.log_level, config.log_file)

    command = args.command
    if command == "seasons":
        return _cmd_seasons(config)

    conn = db.connect(config.db_path)
    try:
        if command in ("update", "backfill"):
            return _cmd_crawl(conn, config, args, mode=command)
        if command == "reparse":
            return _cmd_reparse(conn, config, args)
        if command == "derive":
            result = pipeline.Pipeline(conn, config, _fetcher(config)).derive()
            print(f"{result['players']} players from {result['names']} spellings")
            return 0
        if command == "export":
            if getattr(args, "app", None):
                config._app_dir = args.app
            return _cmd_export(conn, config, game_logs=args.game_logs)
        if command == "publish":
            return _cmd_publish(conn, config, args)
        if command == "status":
            return _cmd_status(conn, config)
        if command == "audit":
            return _cmd_audit(conn, args)
        if command == "review":
            return _cmd_review(conn, config, args)
        if command == "leagues":
            return _cmd_leagues(conn, config, args)
        if command == "double-rostered":
            return _cmd_double_rostered(conn, args)
    finally:
        conn.close()
    return 1


def _fetcher(config: Config, *, offline: bool = False) -> Fetcher:
    return Fetcher(
        config.base_url, delay=config.delay, timeout=config.timeout,
        retries=config.retries, backoff=config.retry_backoff,
        user_agent=config.user_agent, raw_dir=config.raw_dir,
        keep_raw=config.keep_raw, max_requests=config.max_requests,
        offline=offline,
    )


# --------------------------------------------------------------- commands


def _cmd_seasons(config: Config) -> int:
    fetcher = _fetcher(config)
    leagues = config.leagues or [3]
    page = fetcher.get(tts.season_index_path(leagues[0], 0), key="season-list")
    seasons = tts.parse_season_list(page.html)
    if not seasons:
        print("could not read the season list", file=sys.stderr)
        return 1
    for season in seasons:
        print(f"  S{season.season_id:<3} {season.label:<12} start_year={season.start_year}")
    print(f"\n{len(seasons)} seasons in the dropdown; newest is S{seasons[-1].season_id} "
          f"({seasons[-1].label})")

    current = tts.parse_current_season(page.html)
    if current and all(s.season_id != current for s in seasons):
        print(f"\nCurrent season is S{current}, which the dropdown has not listed yet.\n"
              f"It will still be collected -- run: norcalstats update --season {current}")
    elif current:
        print(f"Current season: S{current}")

    print("\nleagues configured:")
    for league_id in leagues:
        try:
            league_page = fetcher.get(
                tts.season_index_path(league_id, 0), key=f"league-{league_id}")
            name = tts.parse_league_name(league_page.html) or "?"
            teams = len(tts.parse_season_index(league_page.html))
            print(f"  {league_id:<3} {name:<10} {teams} teams currently listed")
        except Exception as exc:  # noqa: BLE001 - informational command
            print(f"  {league_id:<3} could not be read: {exc}")
    return 0


def _cmd_crawl(conn, config: Config, args, *, mode: str) -> int:
    seasons = args.seasons
    if mode == "backfill" and (args.from_season or args.to_season):
        low = args.from_season or 0
        high = args.to_season or 999
        known = [r["season_id"] for r in conn.execute("SELECT season_id FROM seasons")]
        if not known:
            pipeline.Pipeline(conn, config, _fetcher(config)).discover_seasons()
            known = [r["season_id"] for r in conn.execute("SELECT season_id FROM seasons")]
        seasons = sorted(s for s in known if low <= s <= high)

    stats = pipeline.run(
        conn, config, mode=mode, seasons=seasons,
        limit=args.limit,
        only_teams=set(args.teams) if args.teams else None,
    )
    print(stats.summary())
    if stats.stopped_early:
        print("\n  Stopped at the request ceiling before finishing."
              "\n  Re-run the same command to continue, or raise --max-requests.")

    if not args.no_export:
        _cmd_export(conn, config)
    wants_publish = config.publish or config.publish_app
    if args.publish or (wants_publish and not args.dry_run):
        _cmd_publish(conn, config, args)
    # Exit non-zero only when the run itself could not complete. Pages that
    # failed to fetch are reported by `audit`; letting them fail the process
    # made systemd mark every single night as failed, which hides a real one.
    return 2 if stats.stopped_early else 0


def _cmd_reparse(conn, config: Config, args) -> int:
    stats = pipeline.run(
        conn, config, mode="reparse", seasons=args.seasons,
        offline=True, force_scoresheets=True,
        only_teams=set(args.teams) if args.teams else None,
    )
    print(stats.summary())
    if not args.no_export:
        _cmd_export(conn, config)
    return 0


def _cmd_export(conn, config: Config, *, game_logs: bool = False) -> int:
    # What the viewer is serving now, so the new file can be put next to it.
    live = _live_player_count(config) if config.legacy_exports else None

    if config.legacy_exports:
        written = export_mod.export_all(
            conn,
            export_dir=config.export_dir,
            legacy_name=config.legacy_json,
            rich_name=config.rich_json,
            include_game_logs=game_logs,
        )
        for name, size in written.items():
            print(f"  {name}: {export_mod._human(size)}")

    # An explicit --app wins; otherwise the configured directory is used, so a
    # nightly run keeps the app dataset current without being told to.
    app_dir = getattr(config, "_app_dir", None) or config.app_dir
    if app_dir:
        from . import appdata
        files = appdata.write_app(conn, app_dir)
        total = sum(files.values())
        print(f"  app dataset -> {app_dir}: {len(files)} files, "
              f"{export_mod._human(total)}"
              f" (core {export_mod._human(files['core.json'])})")

    if not config.legacy_exports:
        return 0

    fresh = export_mod.player_count(Path(config.export_dir) / config.legacy_json)
    if live and fresh is not None:
        change = fresh - live
        print(f"\n  players: {fresh:,} (published: {live:,}, {change:+,})")
        if fresh < live * (1 - publish_mod.MAX_SHRINK):
            print("  WARNING: far fewer players than the published file. If the "
                  "backfill is still running this is expected;\n"
                  "           publishing will refuse until it catches up.")
    return 0


def _live_player_count(config: Config) -> Optional[int]:
    """Player count in the currently published export, if there is one."""
    for candidate in (Path.cwd() / config.legacy_json,
                      Path(config.export_dir) / config.legacy_json):
        if candidate.is_file():
            return export_mod.player_count(candidate)
    return None


def _cmd_publish(conn, config: Config, args) -> int:
    repo = getattr(args, "repo", None) or config.export_dir
    counts = db.counts(conn)
    message = config.commit_message.format(summary=publish_mod.summarize(counts))
    push = not getattr(args, "no_push", False)
    dry_run = getattr(args, "dry_run", False)
    published = False

    if config.legacy_exports:
        try:
            sha = publish_mod.publish(
                Path(repo),
                [n for n in (config.legacy_json, config.rich_json) if n],
                message=message,
                remote=config.git_remote,
                branch=config.git_branch,
                push=push,
                force=getattr(args, "force", False),
                dry_run=dry_run,
            )
        except publish_mod.PublishError as exc:
            log.error("publish failed: %s", exc)
            return 3
        if sha:
            print(f"published {sha[:8]}")
            published = True

    # The app dataset goes to a branch of its own, so it neither needs nor
    # disturbs the working tree the legacy exports are committed from.
    if config.publish_app and config.app_dir:
        try:
            sha = publish_mod.publish_app_dataset(
                Path(repo),
                Path(config.app_dir),
                message=message,
                remote=config.git_remote,
                branch=config.app_branch,
                push=push,
                dry_run=dry_run,
            )
        except publish_mod.PublishError as exc:
            log.error("publishing the app dataset failed: %s", exc)
            return 3
        if sha:
            print(f"published app dataset {sha[:8]} to {config.app_branch}")
            published = True

    if not published:
        print("nothing to publish")
    return 0


def _cmd_status(conn, config: Config) -> int:
    counts = db.counts(conn)
    print(f"database: {config.db_path}")
    for name, value in counts.items():
        print(f"  {name:<20} {value:>8,}")

    print("\nseasons with data:")
    for row in conn.execute("""
        SELECT s.season_id, s.label, COUNT(g.game_id) AS games,
               SUM(CASE WHEN g.scoresheet_at IS NOT NULL THEN 1 ELSE 0 END) AS parsed,
               s.last_scanned_at
          FROM seasons s LEFT JOIN games g ON g.season_id = s.season_id
         GROUP BY s.season_id HAVING games > 0 ORDER BY s.season_id
    """):
        print(f"  S{row['season_id']:<3} {row['label']:<11} "
              f"{row['games']:>5} games, {row['parsed'] or 0:>5} parsed  "
              f"last scan {row['last_scanned_at'] or 'never'}")

    row = conn.execute(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row:
        print(f"\nlast run: {row['mode']} at {row['started_at']} -> "
              f"{row['finished_at'] or 'unfinished'}\n  {row['note'] or ''}")
    return 0


def _cmd_audit(conn, args) -> int:
    where, params = ("WHERE kind = ?", [args.kind]) if args.kind else ("", [])
    print("findings by kind:")
    for row in conn.execute(
        f"SELECT kind, COUNT(*) n FROM anomalies {where} GROUP BY kind ORDER BY n DESC",
        params,
    ):
        print(f"  {row['kind']:<22} {row['n']:>6}")

    print(f"\nsamples (up to {args.limit}):")
    for row in conn.execute(
        f"SELECT kind, game_id, season_id, detail FROM anomalies {where} "
        "ORDER BY id LIMIT ?", [*params, args.limit],
    ):
        target = f"game {row['game_id']}" if row["game_id"] else f"S{row['season_id']}"
        print(f"  {row['kind']:<22} {target:<14} {row['detail']}")
    return 0


def _cmd_leagues(conn, config: Config, args) -> int:
    for league_id, kind in ([(i, "season") for i in args.include]
                            + [(i, "excluded") for i in args.exclude]):
        changed = conn.execute(
            "UPDATE leagues SET kind = ?, note = 'set by hand' WHERE league_id = ?",
            (kind, league_id),
        ).rowcount
        if not changed:
            conn.execute(
                "INSERT INTO leagues(league_id, name, priority, kind, note) "
                "VALUES (?, ?, ?, ?, 'set by hand')",
                (league_id, f"league {league_id}",
                 pipeline._default_priority(league_id), kind),
            )
        print(f"league {league_id}: {kind}")
    if args.include or args.exclude:
        conn.commit()

    if args.discover:
        season = args.season or db.scalar(conn, "SELECT MAX(season_id) FROM seasons")
        if not season:
            print("no seasons known yet; run 'update' or pass --season", file=sys.stderr)
            return 1
        worker = pipeline.Pipeline(conn, config, _fetcher(config))
        worker.discover_leagues(season, force=True)

    clauses, params = [], []
    if args.season:
        clauses.append("ls.season_id = ?")
        params.append(args.season)
    if not args.all:
        clauses.append("l.kind = 'season'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(f"""
        SELECT ls.season_id, ls.league_id, l.name, l.kind, l.span_days, ls.teams,
               (SELECT COUNT(*) FROM games g
                 WHERE g.league_id = ls.league_id AND g.season_id = ls.season_id) AS games
          FROM league_seasons ls JOIN leagues l ON l.league_id = ls.league_id
          {where}
         ORDER BY ls.season_id DESC, l.priority, ls.league_id
    """, params).fetchall()

    if not rows:
        print("no leagues recorded yet; run 'leagues --discover' or 'update'")
        return 0

    marks = {"season": "collected", "event": "skipped (event)",
             "excluded": "skipped", "unknown": "skipped (unclassified)"}
    season = None
    for row in rows:
        if row["season_id"] != season:
            season = row["season_id"]
            label = db.scalar(
                conn, "SELECT label FROM seasons WHERE season_id = ?", (season,))
            print(f"\nS{season} ({label or '?'})")
        span = f"{row['span_days']}d" if row["span_days"] is not None else ""
        note = "" if args.all is False else f"  {marks.get(row['kind'], row['kind']):<22}{span}"
        print(f"   {row['league_id']:>3}  {row['name']:<26} "
              f"{row['teams']:>4} teams  {row['games']:>5} games{note}")

    if not args.all:
        skipped = db.scalar(conn, "SELECT COUNT(*) FROM leagues WHERE kind <> 'season'")
        print(f"\n{skipped} league(s) not collected (tournaments, high school, "
              "unclassified). Show them with --all.")
    print("Change one with:  norcalstats leagues --include <id> | --exclude <id>")
    return 0


def _cmd_double_rostered(conn, args) -> int:
    """Players with more than one team in a season, and their split stats.

    Mostly girls who play a girls team alongside a co-ed one. That is normal,
    so it is no longer raised for review -- but it is worth being able to look
    at, which is what this is for.
    """
    where, params = ["s.team_id IS NOT NULL"], []
    if args.season:
        where.append("s.season_id = ?")
        params.append(args.season)

    rows = conn.execute(f"""
        SELECT p.display_name AS name, s.season_id AS season,
               COUNT(DISTINCT s.team_id) AS teams,
               COUNT(DISTINCT t.gender)  AS genders,
               GROUP_CONCAT(DISTINCT t.gender) AS mix
          FROM player_game_stats s
          JOIN players p ON p.player_id = s.player_id
          JOIN teams   t ON t.team_id = s.team_id AND t.season_id = s.season_id
         WHERE {' AND '.join(where)}
         GROUP BY s.player_id, s.season_id
        HAVING teams > 1 {'AND genders > 1' if args.girls else ''}
         ORDER BY teams DESC, name
         LIMIT ?
    """, [*params, args.limit]).fetchall()

    if not rows:
        print("no double-rostered players found")
        return 0

    for row in rows:
        print(f"\n{row['name']}  (S{row['season']}, {row['teams']} teams)")
        for line in conn.execute("""
            SELECT t.name AS team, COALESCE(t.gender,'?') AS gender,
                   COALESCE(d.name,'?') AS division, COALESCE(l.name,'?') AS league,
                   COUNT(*) AS gp, SUM(s.goals) AS g, SUM(s.assists) AS a
              FROM player_game_stats s
              JOIN teams t ON t.team_id = s.team_id AND t.season_id = s.season_id
              LEFT JOIN divisions d ON d.division_id = t.division_id
              LEFT JOIN leagues   l ON l.league_id = t.league_id
             WHERE s.player_id = (SELECT player_id FROM players WHERE display_name = ?)
               AND s.season_id = ?
             GROUP BY s.team_id ORDER BY gp DESC
        """, (row["name"], row["season"])):
            print(f"   {line['division']:<16} {line['gender']:<6} {line['league']:<10} "
                  f"{line['gp']:>3}gp {line['g']:>3}g {line['a']:>3}a   {line['team']}")

    print(f"\n{len(rows)} shown. Filters: --season N, --girls, --limit N")
    return 0


def _cmd_review(conn, config: Config, args) -> int:
    command = getattr(args, "review_command", None) or "list"

    if command == "list":
        counts = review_mod.summary(conn)
        if counts:
            parts = [f"{r['n']} {r['kind']} ({r['status']})" for r in counts]
            print("  " + ", ".join(parts) + "\n")
        items = review_mod.open_items(
            conn, kind=getattr(args, "kind", None), limit=getattr(args, "limit", 25)
        )
        if not items:
            print("No open questions.")
            return 0
        for row in items:
            print(review_mod.format_item(row, verbose=getattr(args, "verbose", False)))
            print()
        print(f"Answer one with:  norcalstats review answer <id> "
              f"<{'|'.join(sorted(review_mod.ACTIONS))}>")
        return 0

    if command == "show":
        row = review_mod.get(conn, args.item_id)
        if row is None:
            print(f"no review item {args.item_id}", file=sys.stderr)
            return 1
        print(review_mod.format_item(row, verbose=True))
        print(f"      status: {row['status']}"
              + (f" ({row['decision']})" if row["decision"] else ""))
        return 0

    if command == "answer":
        person_map = {}
        for pair in getattr(args, "person", []):
            season, _, person = pair.partition("=")
            if not person:
                print(f"bad --person value '{pair}'; expected SEASON=KEY", file=sys.stderr)
                return 1
            try:
                person_map[int(season)] = person
            except ValueError:
                print(f"bad season in '{pair}'", file=sys.stderr)
                return 1
        try:
            action = review_mod.resolve(
                conn, args.item_id, args.action,
                note=args.note, person_map=person_map or None,
            )
        except review_mod.DecisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"item {args.item_id}: {action}")
        if not args.no_rebuild:
            print("re-deriving with the new decision...")
            pipeline.Pipeline(conn, config, _fetcher(config, offline=True)).derive()
        return 0

    if command == "reopen":
        try:
            review_mod.reopen(conn, args.item_id)
        except review_mod.DecisionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"item {args.item_id} reopened; its overrides were removed")
        return 0

    print(f"unknown review command: {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
