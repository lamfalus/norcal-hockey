"""Tolerant HTML table extractor built on the standard library.

timetoscore.com serves hand-rolled 1990s-era markup: cells opened with ``<td>``
and closed with ``</th>``, stray ``</tr>`` between tables, cells that are never
closed, and inconsistent tag case. Rather than depend on lxml/bs4 -- which would
have to keep working untouched on a Raspberry Pi for a whole season -- we parse
the only subset we need (nested tables of text) with ``html.parser``.

The result is a tree of :class:`Table`, each holding rows of cell text plus the
tables nested inside them, so callers can walk the layout the way a browser
would.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional, Union

#: Tags whose text content is never table data.
_IGNORED_CONTENT = {"script", "style"}

_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    """Collapse whitespace (including ``&nbsp;``) and trim."""
    return _WS.sub(" ", text.replace("\xa0", " ")).strip()


@dataclass
class Cell:
    text: str = ""
    colspan: int = 1
    rowspan: int = 1
    is_header: bool = False
    #: Tables nested inside this cell, in document order.
    tables: list["Table"] = field(default_factory=list)
    #: hrefs of links inside this cell, in document order.
    links: list[str] = field(default_factory=list)
    #: ``<td>`` attributes -- the scoresheet's shot grid encodes save vs goal
    #: purely as ``bgcolor``.
    attrs: dict[str, str] = field(default_factory=dict)
    _buf: list[str] = field(default_factory=list, repr=False)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.text


@dataclass
class Row:
    cells: list[Cell] = field(default_factory=list)
    #: ``<tr>`` attributes -- the season page encodes its division tree in
    #: ``data-id`` / ``data-parent`` here.
    attrs: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cells)

    def __iter__(self):
        return iter(self.cells)

    def __getitem__(self, i: int) -> Cell:
        return self.cells[i]

    def text(self, i: int, default: str = "") -> str:
        """Text of cell ``i``, or ``default`` when the row is too short."""
        return self.cells[i].text if 0 <= i < len(self.cells) else default

    def int(self, i: int, default: int = 0) -> int:
        """Cell ``i`` parsed as an int, tolerating blanks and stray symbols."""
        return to_int(self.text(i), default)

    @property
    def texts(self) -> list[str]:
        return [c.text for c in self.cells]

    @property
    def joined(self) -> str:
        return " ".join(c.text for c in self.cells if c.text)

    @property
    def links(self) -> list[str]:
        out: list[str] = []
        for cell in self.cells:
            out.extend(cell.links)
        return out


@dataclass
class Table:
    rows: list[Row] = field(default_factory=list)
    attrs: dict[str, str] = field(default_factory=dict)
    #: Tables nested directly inside this one, in document order.
    children: list["Table"] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, i: int) -> Row:
        return self.rows[i]

    @property
    def text(self) -> str:
        return " ".join(r.joined for r in self.rows)

    def head(self, rows: int = 2) -> str:
        """Text of the first ``rows`` rows -- used to identify a table."""
        return " ".join(r.joined for r in self.rows[:rows])

    def descendants(self) -> list["Table"]:
        """Nested tables, depth-first, excluding this one."""
        out: list[Table] = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        return out

    def data_rows(self, skip: int = 0) -> list[Row]:
        """Rows after ``skip`` leading header rows, ignoring all-header rows."""
        return [r for r in self.rows[skip:] if not all(c.is_header for c in r.cells)]


def to_int(text: str, default: int = 0) -> int:
    """Parse an int out of scoresheet text, tolerating ``''`` and ``'12*'``."""
    match = re.search(r"-?\d+", text or "")
    return int(match.group()) if match else default


def to_float(text: str, default: Optional[float] = None) -> Optional[float]:
    match = re.search(r"-?\d*\.?\d+", (text or "").replace(",", ""))
    return float(match.group()) if match else default


# A ``None`` on the cell stack is a table barrier: it stops cell-closing from
# escaping out of a nested table into its enclosing cell.
_StackEntry = Union[Cell, None]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []      # top-level tables, document order
        self.every: list[Table] = []       # every table, document order
        self._open_tables: list[Table] = []
        self._stack: list[_StackEntry] = []
        self._ignore_depth = 0

    # -- cell stack helpers ---------------------------------------------
    @property
    def _cell(self) -> Optional[Cell]:
        """The cell currently collecting text, if any."""
        if self._stack and isinstance(self._stack[-1], Cell):
            return self._stack[-1]
        return None

    def _close_cell(self) -> None:
        cell = self._cell
        if cell is not None:
            cell.text = clean("".join(cell._buf))
            self._stack.pop()

    # -- HTMLParser hooks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _IGNORED_CONTENT:
            self._ignore_depth += 1
            return
        if self._ignore_depth:
            return

        attr = {k.lower(): (v or "") for k, v in attrs}

        if tag == "table":
            parent = self._cell
            table = Table(attrs=attr)
            if self._open_tables:
                self._open_tables[-1].children.append(table)
            else:
                self.tables.append(table)
            if parent is not None:
                parent.tables.append(table)
            self.every.append(table)
            self._open_tables.append(table)
            self._stack.append(None)  # barrier
        elif tag == "tr":
            self._close_cell()
            if self._open_tables:
                self._open_tables[-1].rows.append(Row(attrs=attr))
        elif tag in ("td", "th"):
            # An unclosed previous cell is implicitly closed here.
            self._close_cell()
            if not self._open_tables:
                return
            table = self._open_tables[-1]
            if not table.rows:
                # Cells outside any <tr> do occur on this site.
                table.rows.append(Row())
            cell = Cell(
                is_header=(tag == "th"),
                colspan=_int(attr.get("colspan"), 1),
                rowspan=_int(attr.get("rowspan"), 1),
                attrs=attr,
            )
            table.rows[-1].cells.append(cell)
            self._stack.append(cell)
        elif tag == "a":
            cell = self._cell
            if cell is not None and attr.get("href"):
                cell.links.append(attr["href"])
        elif tag in ("br", "p", "div", "li"):
            cell = self._cell
            if cell is not None:
                cell._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _IGNORED_CONTENT:
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth:
            return

        # </td> and </th> are interchangeable here: the site mixes them.
        if tag in ("td", "th", "tr"):
            self._close_cell()
        elif tag == "table":
            self._close_cell()
            # Unwind to and including this table's barrier.
            while self._stack:
                entry = self._stack.pop()
                if entry is None:
                    break
                entry.text = clean("".join(entry._buf))
            if self._open_tables:
                self._open_tables.pop()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth:
            return
        cell = self._cell
        if cell is not None:
            cell._buf.append(data)

    def finish(self) -> None:
        """Commit any cells left open by unclosed tags at end of document."""
        while self._stack:
            entry = self._stack.pop()
            if entry is not None:
                entry.text = clean("".join(entry._buf))


def _int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse(html: str) -> _TableParser:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    return parser


def parse_tables(html: str) -> list[Table]:
    """Top-level tables in ``html``, each carrying its nested tables."""
    return _parse(html).tables


def all_tables(html: str) -> list[Table]:
    """Every table in ``html``, in opening-tag order.

    This mirrors ``document.querySelectorAll('table')`` in the browser, which is
    the indexing the original JS scrapers relied on.
    """
    return _parse(html).every


def find_table(tables: list[Table], *needles: str, rows: int = 2) -> Optional[Table]:
    """First table whose leading ``rows`` mention every needle, case-insensitively."""
    lowered = [n.lower() for n in needles]
    for table in tables:
        head = table.head(rows).lower()
        if all(n in head for n in lowered):
            return table
    return None


def find_tables(tables: list[Table], *needles: str, rows: int = 2) -> list[Table]:
    """All tables whose leading ``rows`` mention every needle."""
    lowered = [n.lower() for n in needles]
    return [t for t in tables if all(n in t.head(rows).lower() for n in lowered)]
