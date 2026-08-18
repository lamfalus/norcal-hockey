"""The review queue: questions the collector cannot answer on its own.

Automatic name resolution can be confidently right (two spellings sharing a
sweater), confidently wrong (one spelling covering two children), or genuinely
undecidable (``Alex Chen`` and ``Alexander Chen`` on different teams). This
module records the undecidable and the guessed-at, so nothing is silently
merged or split without a way to find out.

Two properties make the queue usable over a long season:

* **Stable fingerprints.** An item is identified by what it is about, not by
  when it was found, so a nightly re-run updates the existing question instead
  of asking it again.
* **Decisions outlive rebuilds.** Answering an item writes to
  ``player_overrides`` / ``player_splits``, which the resolver consults on every
  run. Wiping and rebuilding the whole database preserves every answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import names as N
from .db import now

log = logging.getLogger(__name__)

#: What a reviewer can decide, per item kind.
ACTIONS = {
    "merge": "treat the names as one player",
    "separate": "keep the names as different players",
    "split": "one spelling covering two or more children",
    "dismiss": "not a problem; stop asking",
}


@dataclass
class Item:
    kind: str
    subject: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    applied: str = ""
    confidence: Optional[float] = None
    #: Values that define what this question is about. Two runs producing the
    #: same parts produce the same item.
    parts: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        payload = "|".join([self.kind, *sorted(self.parts)])
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def record(
    conn: sqlite3.Connection,
    items: Iterable[Item],
    *,
    sweep: Optional[Iterable[str]] = None,
) -> dict[str, int]:
    """Upsert review items, leaving already-answered ones answered.

    ``sweep`` names the kinds this call is authoritative for. Any *open* item of
    those kinds that is no longer being raised is retired as stale -- a question
    that no longer applies (because the data improved, or because the rules got
    better) should stop being asked. Answered items are never touched.
    """
    items = list(items)
    timestamp = now()
    added = updated = 0
    for item in items:
        existing = conn.execute(
            "SELECT item_id, status FROM review_items WHERE fingerprint = ?",
            (item.fingerprint,),
        ).fetchone()
        evidence = json.dumps(item.evidence, ensure_ascii=False, sort_keys=True)
        if existing is None:
            conn.execute(
                "INSERT INTO review_items(fingerprint, kind, status, confidence, "
                "subject, evidence, suggestion, applied, first_seen, last_seen) "
                "VALUES (?,?,'open',?,?,?,?,?,?,?)",
                (item.fingerprint, item.kind, item.confidence, item.subject,
                 evidence, item.suggestion, item.applied, timestamp, timestamp),
            )
            added += 1
        else:
            # Refresh the evidence but never reopen a decided question.
            conn.execute(
                "UPDATE review_items SET last_seen = ?, evidence = ?, subject = ?, "
                "suggestion = ?, applied = ?, confidence = ? WHERE item_id = ?",
                (timestamp, evidence, item.subject, item.suggestion,
                 item.applied, item.confidence, existing["item_id"]),
            )
            updated += 1

    retired = 0
    if sweep:
        kinds = list(sweep)
        current = [item.fingerprint for item in items]
        placeholders = ",".join("?" for _ in kinds)
        sql = (f"UPDATE review_items SET status = 'stale', decided_at = ? "
               f"WHERE status = 'open' AND kind IN ({placeholders})")
        params: list[Any] = [timestamp, *kinds]
        if current:
            sql += f" AND fingerprint NOT IN ({','.join('?' for _ in current)})"
            params.extend(current)
        retired = conn.execute(sql, params).rowcount

    conn.commit()
    if retired:
        log.info("retired %d review item(s) that no longer apply", retired)
    return {"added": added, "updated": updated, "retired": retired}


def open_items(
    conn: sqlite3.Connection, *, kind: Optional[str] = None, limit: int = 50
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM review_items WHERE status = 'open'"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY (confidence IS NULL), confidence ASC, item_id ASC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_items WHERE item_id = ?", (item_id,)
    ).fetchone()


def summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, status, COUNT(*) AS n FROM review_items "
        "GROUP BY kind, status ORDER BY status, n DESC"
    ).fetchall()


# ------------------------------------------------------------- decisions


class DecisionError(ValueError):
    pass


def resolve(
    conn: sqlite3.Connection,
    item_id: int,
    action: str,
    *,
    note: str = "",
    person_map: Optional[dict[int, str]] = None,
) -> str:
    """Answer one review item and persist the decision as an override.

    ``person_map`` maps ``season_id -> person_key`` and is required for
    ``split``: it says which seasons belong to which child.
    """
    item = get(conn, item_id)
    if item is None:
        raise DecisionError(f"no review item {item_id}")
    if action not in ACTIONS:
        raise DecisionError(
            f"unknown action '{action}'; choose one of {', '.join(sorted(ACTIONS))}"
        )

    evidence = json.loads(item["evidence"] or "{}")
    names = evidence.get("names") or []

    if action == "merge":
        if item["kind"] == "same_name":
            # One spelling that was split apart: put every season back under a
            # single person rather than merging two different spellings.
            if not names:
                raise DecisionError("this item does not identify a name")
            key = N.name_key(names[0])
            seasons = evidence.get("seasons") or []
            if not seasons:
                raise DecisionError("no seasons recorded for this name")
            conn.execute("DELETE FROM player_splits WHERE name = ?", (key,))
            conn.executemany(
                "INSERT INTO player_splits(name, season_id, team_id, person_key, note) "
                "VALUES (?,?,NULL,'a',?)",
                [(key, season, note) for season in seasons],
            )
        else:
            if len(names) < 2:
                raise DecisionError("this item does not name two players to merge")
            target = names[0]
            for other in names[1:]:
                _override(conn, other, merge_into=target, split=0, note=note)
            _override(conn, target, merge_into=None, split=0, note=note)

    elif action == "separate":
        if item["kind"] == "same_name":
            raise DecisionError(
                "use 'split' with a season map to separate one spelling; "
                "'separate' applies to two different spellings"
            )
        for name in names:
            _override(conn, name, merge_into=None, split=1, note=note)

    elif action == "split":
        if not person_map:
            raise DecisionError(
                "splitting needs a season-to-person map, e.g. --person 29=a --person 31=b"
            )
        if not names:
            raise DecisionError("this item does not identify a name to split")
        key = N.name_key(names[0])
        conn.execute("DELETE FROM player_splits WHERE name = ?", (key,))
        conn.executemany(
            "INSERT INTO player_splits(name, season_id, team_id, person_key, note) "
            "VALUES (?,?,NULL,?,?)",
            [(key, season, person, note) for season, person in person_map.items()],
        )

    conn.execute(
        "UPDATE review_items SET status = ?, decision = ?, note = ?, decided_at = ? "
        "WHERE item_id = ?",
        ("dismissed" if action == "dismiss" else "resolved",
         action, note, now(), item_id),
    )
    conn.commit()
    return action


def _override(
    conn: sqlite3.Connection,
    name: str,
    *,
    merge_into: Optional[str],
    split: int,
    note: str,
) -> None:
    conn.execute(
        "INSERT INTO player_overrides(name, merge_into, split, note) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET merge_into = excluded.merge_into, "
        "split = excluded.split, note = excluded.note",
        (name, merge_into, split, note),
    )


def reopen(conn: sqlite3.Connection, item_id: int) -> None:
    """Undo a decision, clearing the overrides it created."""
    item = get(conn, item_id)
    if item is None:
        raise DecisionError(f"no review item {item_id}")
    evidence = json.loads(item["evidence"] or "{}")
    for name in evidence.get("names") or []:
        conn.execute("DELETE FROM player_overrides WHERE name = ?", (name,))
        conn.execute("DELETE FROM player_splits WHERE name = ?", (N.name_key(name),))
    conn.execute(
        "UPDATE review_items SET status = 'open', decision = NULL, decided_at = NULL "
        "WHERE item_id = ?", (item_id,)
    )
    conn.commit()


def load_overrides(conn: sqlite3.Connection) -> dict[str, dict]:
    """Manual name decisions, keyed by normalized name."""
    return {
        N.name_key(row["name"]): dict(row)
        for row in conn.execute("SELECT * FROM player_overrides")
    }


def load_splits(conn: sqlite3.Connection) -> dict[str, dict[tuple[int, Optional[int]], str]]:
    """Manual same-name splits: ``{name_key: {(season, team|None): person}}``."""
    out: dict[str, dict[tuple[int, Optional[int]], str]] = {}
    for row in conn.execute("SELECT * FROM player_splits"):
        out.setdefault(row["name"], {})[(row["season_id"], row["team_id"])] = row["person_key"]
    return out


def format_item(row: sqlite3.Row, *, verbose: bool = False) -> str:
    """Render one item for the terminal."""
    evidence = json.loads(row["evidence"] or "{}")
    confidence = "" if row["confidence"] is None else f" ({row['confidence']:.0%} sure)"
    lines = [f"[{row['item_id']}] {row['kind']}{confidence}: {row['subject']}"]
    if row["applied"]:
        lines.append(f"      did: {row['applied']}")
    if row["suggestion"]:
        lines.append(f"      ask: {row['suggestion']}")
    if verbose:
        for name in evidence.get("names", []):
            lines.append(f"      name: {name}")
        for line in evidence.get("detail", []):
            lines.append(f"      {line}")
    return "\n".join(lines)
