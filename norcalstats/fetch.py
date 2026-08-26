"""Polite HTTP fetching with an on-disk raw-page archive.

Uses only the standard library so the Pi needs nothing but ``python3``.

Two properties matter for an unattended seasonal crawler:

* **Politeness.** One request at a time, a configurable delay between them,
  exponential backoff on 5xx/timeouts, and a hard per-run request ceiling so a
  bug cannot turn into a hammering loop.
* **Re-parsability.** Every page fetched is archived gzipped on disk. When the
  parser improves, the whole archive can be re-parsed offline -- no refetching,
  no extra load on the site.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A page could not be retrieved after all retries."""


class RequestCeilingReached(FetchError):
    """The per-run request limit was hit.

    Distinct from an ordinary failure because it says nothing about the page
    being fetched: the run should stop and be resumed, not record every
    remaining item as broken.
    """


class RateLimited(FetchError):
    """The site returned 429 and kept returning it through the retries.

    Distinct from an ordinary failure, like the request ceiling: it says stop
    and come back later, not that this page is broken. A run that hits it should
    keep what it has and resume next time, leaving the unfetched pages to be
    tried again -- never recording them as errors, which would skip them.
    """


@dataclass
class Page:
    url: str
    html: str
    sha256: str
    from_cache: bool = False
    status: int = 200


@dataclass
class BinaryPage:
    url: str
    payload: bytes
    sha256: str
    from_cache: bool = False
    status: int = 200


class Fetcher:
    def __init__(
        self,
        base_url: str,
        *,
        delay: float = 1.5,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 4.0,
        user_agent: str = "norcal-hockey-stats/2.0",
        raw_dir: Optional[Path] = None,
        keep_raw: bool = True,
        max_requests: int = 20000,
        offline: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.user_agent = user_agent
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.keep_raw = keep_raw and self.raw_dir is not None
        self.max_requests = max_requests
        #: When true, only the raw archive is read; nothing hits the network.
        self.offline = offline

        self.requests_made = 0
        self.cache_hits = 0
        self._last_request = 0.0

    # -- url helpers -----------------------------------------------------
    def url_for(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _raw_path(self, url: str, key: Optional[str],
                  ext: str = "html") -> Optional[Path]:
        if not self.raw_dir:
            return None
        if key:
            safe = key.replace("/", "_")
        else:
            parts = urlsplit(url)
            safe = hashlib.sha1(
                f"{parts.path}?{parts.query}".encode()
            ).hexdigest()[:20]
        return self.raw_dir / f"{safe}.{ext}.gz"

    # -- archive ---------------------------------------------------------
    def read_raw(self, key: str) -> Optional[str]:
        """Return archived HTML for ``key``, or None when not archived."""
        path = self._raw_path("", key)
        if path and path.is_file():
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                    return fh.read()
            except OSError as exc:  # truncated/corrupt archive entry
                log.warning("unreadable archive %s: %s", path, exc)
        return None

    def _write_raw(self, key: Optional[str], url: str, html: str) -> None:
        if not self.keep_raw:
            return
        path = self._raw_path(url, key)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(html)
        tmp.replace(path)

    def read_raw_bytes(self, key: str, ext: str) -> Optional[bytes]:
        """Return an archived binary payload (e.g. a PDF), or None."""
        path = self._raw_path("", key, ext)
        if path and path.is_file():
            try:
                with gzip.open(path, "rb") as fh:
                    return fh.read()
            except OSError as exc:
                log.warning("unreadable archive %s: %s", path, exc)
        return None

    def _write_raw_bytes(self, key: Optional[str], url: str,
                         payload: bytes, ext: str) -> None:
        if not self.keep_raw:
            return
        path = self._raw_path(url, key, ext)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wb") as fh:
            fh.write(payload)
        tmp.replace(path)

    # -- fetching --------------------------------------------------------
    def get(
        self,
        path: str,
        *,
        key: Optional[str] = None,
        use_cache: bool = False,
        force: bool = False,
    ) -> Page:
        """Fetch ``path``.

        ``key`` names the archive entry (e.g. ``"s31/game/50647"``).
        ``use_cache`` serves from the archive when present, avoiding the network
        entirely -- used for offline re-parsing and for backfill resume.
        """
        url = self.url_for(path)

        if (use_cache or self.offline) and not force and key:
            cached = self.read_raw(key)
            if cached is not None:
                self.cache_hits += 1
                return Page(url, cached, _sha(cached), from_cache=True)

        if self.offline:
            raise FetchError(f"offline and not archived: {key or url}")

        if self.requests_made >= self.max_requests:
            raise RequestCeilingReached(
                f"request ceiling reached ({self.max_requests}); "
                "raise max_requests, or re-run to continue where this left off"
            )

        html = self._get_with_retries(url)
        self._write_raw(key, url, html)
        return Page(url, html, _sha(html))

    def get_bytes(
        self,
        path: str,
        *,
        key: str,
        ext: str,
        use_cache: bool = False,
        force: bool = False,
    ) -> "BinaryPage":
        """Fetch a binary payload (a PDF), archiving it gzipped under ``ext``.

        Mirrors :meth:`get` but keeps the bytes intact -- decoding a PDF to text
        would corrupt it -- and requires a ``key`` so the archive entry is
        stable and re-parsable offline.
        """
        url = self.url_for(path)

        if (use_cache or self.offline) and not force:
            cached = self.read_raw_bytes(key, ext)
            if cached is not None:
                self.cache_hits += 1
                return BinaryPage(url, cached, _sha_bytes(cached), from_cache=True)

        if self.offline:
            raise FetchError(f"offline and not archived: {key}")

        if self.requests_made >= self.max_requests:
            raise RequestCeilingReached(
                f"request ceiling reached ({self.max_requests}); "
                "raise max_requests, or re-run to continue where this left off"
            )

        payload = self._get_bytes_with_retries(url)
        self._write_raw_bytes(key, url, payload, ext)
        return BinaryPage(url, payload, _sha_bytes(payload))

    def _get_bytes_with_retries(self, url: str) -> bytes:
        return self._with_retries(url, self._get_bytes_once)

    def _get_bytes_once(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip",
                "Connection": "close",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return raw

    def _get_with_retries(self, url: str) -> str:
        return self._with_retries(url, self._get_once)

    def _with_retries(self, url: str, fetch_fn):
        """Retry ``fetch_fn(url)`` on 5xx/429/network errors, with backoff.

        A 429 that survives every retry raises :class:`RateLimited` rather than
        a plain :class:`FetchError`, so a caller can stop and resume instead of
        recording the page as broken -- and it backs off far harder than a 5xx,
        since the site is explicitly asking for a pause.
        """
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                self.requests_made += 1
                return fetch_fn(url)
            except urllib.error.HTTPError as exc:
                last = exc
                # 4xx other than 429 will not improve on retry.
                if exc.code < 500 and exc.code != 429:
                    raise FetchError(f"HTTP {exc.code} for {url}") from exc
                log.warning("HTTP %s for %s (attempt %d/%d)",
                            exc.code, url, attempt, self.retries)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                log.warning("network error for %s (attempt %d/%d): %s",
                            url, attempt, self.retries, exc)
            if attempt < self.retries:
                time.sleep(self._retry_delay(attempt, last))
        if isinstance(last, urllib.error.HTTPError) and last.code == 429:
            raise RateLimited(f"rate limited on {url} after {self.retries} tries")
        raise FetchError(f"giving up on {url}: {last}")

    def _retry_delay(self, attempt: int, exc: Optional[Exception]) -> float:
        """How long to wait before the next attempt.

        A 429 gets a long cooldown -- the server's ``Retry-After`` if it gave
        one, else several times the ordinary backoff -- because retrying a rate
        limit quickly just earns another. Capped so a run cannot hang for
        minutes on a single page.
        """
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after and str(retry_after).strip().isdigit():
                return min(int(retry_after), 120)
            return min(self.backoff * attempt * 4, 120)
        return self.backoff * attempt

    def _get_once(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                # The site answers 406 to a restrictive Accept (it rejects a
                # bare "text/html,application/xhtml+xml"), so the wildcard must
                # be present.
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip",
                "Connection": "close",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
