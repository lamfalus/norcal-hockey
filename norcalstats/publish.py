"""Commit and push the exported JSON from the Raspberry Pi.

This is the one part of the system that changes something outside the Pi, so it
is deliberately conservative:

* It is **opt-in** -- nothing pushes unless ``publish`` is enabled.
* It stages **only the export files it was given**, never ``git add -A``, so a
  stray file in the working tree can never be published by accident.
* It refuses to run when the repository has unrelated staged changes.
* It is a no-op when the exports are byte-identical to what is already
  committed, so an uneventful night creates no commit.
* Nothing here handles credentials. Authentication must already be configured
  on the Pi (an SSH deploy key, or a credential helper), so no token is ever
  read, logged, or stored by this code.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if check and result.returncode != 0:
        raise PublishError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def is_repo(repo: Path) -> bool:
    result = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def changed_files(repo: Path, paths: Sequence[str]) -> list[str]:
    """Which of ``paths`` differ from HEAD (tracked changes or untracked)."""
    result = _git(repo, "status", "--porcelain", "--", *paths, check=False)
    changed = []
    for line in result.stdout.splitlines():
        name = line[3:].strip().strip('"')
        if name:
            changed.append(name)
    return changed


def has_unrelated_staged_changes(repo: Path, paths: Sequence[str]) -> list[str]:
    """Staged files that are not among ``paths`` -- a reason to refuse."""
    result = _git(repo, "diff", "--cached", "--name-only", check=False)
    allowed = {Path(p).as_posix() for p in paths}
    return [
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in allowed
    ]


def publish(
    repo: Path,
    files: Iterable[str],
    *,
    message: str,
    remote: str = "origin",
    branch: str = "main",
    push: bool = True,
    dry_run: bool = False,
) -> Optional[str]:
    """Commit the given export files and push. Returns the commit sha, or None.

    Returns ``None`` when there was nothing to commit.
    """
    repo = Path(repo).resolve()
    files = [f for f in files]
    if not files:
        return None
    if not is_repo(repo):
        raise PublishError(f"not a git repository: {repo}")

    missing = [f for f in files if not (repo / f).exists()]
    if missing:
        raise PublishError(f"export file(s) missing: {', '.join(missing)}")

    blocked = has_unrelated_staged_changes(repo, files)
    if blocked:
        raise PublishError(
            "refusing to commit: unrelated staged changes present "
            f"({', '.join(blocked[:5])}). Resolve them, then re-run."
        )

    changed = changed_files(repo, files)
    if not changed:
        log.info("exports unchanged; nothing to publish")
        return None

    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != branch:
        raise PublishError(
            f"on branch '{current}' but configured to publish '{branch}'; "
            "checkout the right branch or change git_branch"
        )

    if dry_run:
        log.info("[dry run] would commit %s and push to %s/%s",
                 ", ".join(changed), remote, branch)
        return None

    # Stage only the export files, never the whole tree.
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-m", message)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    log.info("committed %s (%s)", sha[:8], ", ".join(changed))

    if push:
        _git(repo, "push", remote, f"HEAD:{branch}")
        log.info("pushed to %s/%s", remote, branch)
    return sha


def summarize(counts: dict[str, int]) -> str:
    """Short commit-message summary, e.g. ``"2971 players, 8104 games"``."""
    parts = []
    for key, label in (("players", "players"), ("games", "games")):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    return ", ".join(parts) or "no changes"
