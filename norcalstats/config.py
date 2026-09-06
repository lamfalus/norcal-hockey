"""Configuration: defaults, config-file loading, and path layout.

Settings resolve in this order (later wins):
    built-in defaults  ->  config file (JSON)  ->  NORCAL_* environment vars
        ->  command-line flags
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATHS = [
    Path("norcalstats.json"),
    Path.home() / ".config" / "norcalstats" / "config.json",
    Path("/etc/norcalstats/config.json"),
]


@dataclass
class Config:
    # -- source ---------------------------------------------------------
    base_url: str = "https://stats.caha.timetoscore.com"
    #: Leagues to crawl, in priority order. **Empty means discover them all**,
    #: which is the default: timetoscore hosts around thirty, and a single
    #: competition is spread across several ids that come and go during the
    #: season -- CAHA alone runs preseason (16), weekends (17), the main league
    #: (5) and playoffs (24).
    #:
    #: Set this only to restrict collection to specific leagues; the order then
    #: decides which one owns a team's name and division.
    leagues: list[int] = field(default_factory=list)
    #: Highest league id to probe when discovering. Ids are small and dense;
    #: this leaves generous room for new ones.
    max_league_id: int = 60
    #: Re-probe for leagues that appeared mid-season. Turning this off pins
    #: collection to the leagues already in the database.
    discover_leagues: bool = True
    #: Seasons to crawl. Empty means "the recent window", below.
    seasons: list[int] = field(default_factory=list)
    #: How many seasons back to collect, in addition to the current one.
    #: Season numbers are irregular, so this filters on start year.
    #: 0 means no limit.
    seasons_back: int = 6

    # -- politeness -----------------------------------------------------
    #: Seconds between requests. The Pi has all night; be a good citizen.
    delay: float = 1.5
    timeout: float = 30.0
    retries: int = 3
    retry_backoff: float = 4.0
    user_agent: str = (
        "norcal-hockey-stats/2.0 (personal youth-hockey stats archive; "
        "+https://github.com/lamfalus/norcal-hockey)"
    )
    #: Hard ceiling on requests per run, so a bug cannot hammer the site.
    max_requests: int = 20000

    # -- storage --------------------------------------------------------
    data_dir: Path = Path("data")
    db_path: Optional[Path] = None          # defaults to <data_dir>/norcal.sqlite3
    raw_dir: Optional[Path] = None          # defaults to <data_dir>/raw
    #: Keep a gzipped copy of every page so the parser can be improved and
    #: re-run offline without refetching.
    keep_raw: bool = True

    # -- refresh policy -------------------------------------------------
    #: Re-check schedules for a season this many days after its last game
    #: before treating it as complete.
    stale_days: int = 21
    #: Also re-fetch scoresheets seen within this window, to pick up late
    #: scorekeeper corrections.
    recheck_days: int = 10
    #: Fetch the PDF scorecard's Goaltender Records (per-goalie shots/saves) for
    #: the current season. A second request per played game, so it is a
    #: deliberate opt-in and scoped to the newest season until backfilled.
    collect_scorecards: bool = True

    # -- sweep (same-day result checks on game days) --------------------
    #: A game first becomes eligible for a result check this many minutes after
    #: its scheduled start. Youth games run ~90 minutes and the PDF is posted
    #: shortly after the buzzer, so a game rarely enters the window before its
    #: sheet exists. Once eligible it is re-checked every sweep until complete.
    sweep_first_look_minutes: int = 100
    #: Stop sweeping a game this many hours after its scheduled start and leave
    #: it to the nightly run -- a sheet that has not appeared by then usually
    #: needs a hand, and there is no point pinging the site for it all day.
    sweep_giveup_hours: float = 5.0
    #: The wall-clock zone the schedule's printed times are in. Used to decide
    #: which games are due; falls back to the system clock where the zone
    #: database is unavailable (which on the Pi is already this zone).
    sweep_timezone: str = "America/Los_Angeles"

    # -- export / publish ----------------------------------------------
    export_dir: Path = Path(".")
    legacy_json: str = "norcal_hockey_players_s27-s31.json"
    rich_json: str = "norcal_hockey_stats.json"
    #: The two name-keyed JSON files the first viewer read. They land in the
    #: repository itself, so every nightly run leaves a tracked file modified.
    #: Turn this off once nothing reads them.
    legacy_exports: bool = True
    #: Where the web app's dataset is written. Under ``data_dir`` by default,
    #: which is ignored by git, so refreshing it nightly changes nothing the
    #: repository can see. ``None`` disables it.
    app_dir: Optional[Path] = None
    publish: bool = False
    git_remote: str = "origin"
    git_branch: str = "main"
    commit_message: str = "Update stats database ({summary})"
    #: The app dataset is 38 files rebuilt every night, so it is published to a
    #: branch of its own holding nothing else, reset to a single parentless
    #: commit each time. Committing it alongside the code would grow the history
    #: by megabytes a night and never give any of it back.
    publish_app: bool = False
    app_branch: str = "data"

    # -- telegram notifications ----------------------------------------
    #: Announce completed 12U Norcal games to a Telegram channel as their
    #: results land. Both must be set for anything to send; they hold a secret,
    #: so they live only in the (gitignored) config on the Pi, never the repo.
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # -- logging --------------------------------------------------------
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        if isinstance(self.leagues, int):
            self.leagues = [self.leagues]
        # Keep the caller's priority order, dropping any repeats.
        self.leagues = list(dict.fromkeys(int(x) for x in self.leagues))
        self.data_dir = Path(self.data_dir)
        self.export_dir = Path(self.export_dir)
        if self.db_path is None:
            self.db_path = self.data_dir / "norcal.sqlite3"
        if self.raw_dir is None:
            self.raw_dir = self.data_dir / "raw"
        if self.app_dir is None:
            self.app_dir = self.data_dir / "app"
        self.app_dir = Path(self.app_dir)
        self.db_path = Path(self.db_path)
        self.raw_dir = Path(self.raw_dir)
        if self.log_file is not None:
            self.log_file = Path(self.log_file)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None, **overrides: Any) -> "Config":
        """Build a config from file + environment + explicit overrides."""
        data: dict[str, Any] = {}

        candidates = [path] if path else DEFAULT_CONFIG_PATHS
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                data.update(json.loads(Path(candidate).read_text(encoding="utf-8")))
                break
        else:
            if path:
                raise FileNotFoundError(f"config file not found: {path}")

        known = {f.name for f in fields(cls)}
        for name in known:
            env = os.environ.get(f"NORCAL_{name.upper()}")
            if env is not None:
                data[name] = _coerce(cls, name, env)

        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in out.items()}


def _coerce(cls: type, name: str, raw: str) -> Any:
    """Convert an environment string to the field's declared type."""
    declared = {f.name: f.type for f in fields(cls)}[name]
    text = str(declared)
    if "bool" in text:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if "list[int]" in text:
        return [int(p) for p in raw.replace(",", " ").split()]
    if "int" in text and "Optional" not in text:
        return int(raw)
    if "float" in text:
        return float(raw)
    if "Path" in text:
        return Path(raw)
    return raw
