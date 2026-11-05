"""HTTP-Zugriff mit Timeouts, Wiederholversuchen und gestreamtem Download.

Wiederholt wird bei vorübergehenden Netzwerkfehlern, Timeouts und den Statuscodes aus
``http.retry_on_status`` (Standard 429, 500, 502, 503, 504) mit exponentiellem
Backoff und Zufallsanteil; ein ``Retry-After`` des Dienstes hat Vorrang (gedeckelt). Andere 4xx-Antworten
und bleibende Fehler (TLS-Prüfung, ungültige URL, nicht unterstütztes Protokoll) werden nicht wiederholt.

Schutz vor feindlichen Diensten: Antworten werden gestreamt und die *entpackten* Bytes gezählt
(``max_response_mb``, ``max_download_mb``, ``max_decompression_ratio`` → ``too_large``); jede Anfrage
hat eine Gesamtfrist (``deadline_s``), auch wenn der Dienst Daten nur tröpfeln lässt.

Umleitungen werden verfolgt, aber nie von https auf http (``insecure_redirect``): Ein Angreifer im Netz
könnte sonst Antworten unterschieben.
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential, wait_random

from geoimport.config.models import HttpSettings
from geoimport.logging import mask_secrets

log = logging.getLogger(__name__)

MIB = 1024 * 1024
_RATIO_MIN_BYTES = 16 * MIB  # Verhältnis erst ab dieser Größe prüfen (kleine XML-Antworten packen sehr gut)


class HttpError(Exception):
    """Dienst nicht erreichbar oder Fehlerantwort.

    ``kind``: http | timeout | network | insecure_redirect | too_large | tls | config.
    """

    def __init__(self, kind: str, message: str, url: str, status: int | None = None, attempts: int = 1) -> None:
        super().__init__(message)
        self.kind = kind
        self.url = mask_secrets(url)
        self.status = status
        self.attempts = attempts


def _block_downgrade(response: httpx.Response) -> None:
    """Event-Hook: Umleitung von https auf http abweisen, bevor sie verfolgt wird."""
    if not response.has_redirect_location or response.request.url.scheme != "https":
        return
    target = response.request.url.join(response.headers["Location"])
    if target.scheme != "https":
        raise HttpError(
            "insecure_redirect",
            f"Umleitung von https auf {target.scheme} abgewiesen: {mask_secrets(str(target))}",
            str(response.request.url),
            response.status_code,
        )


def merge_query(url: str, params: dict[str, Any] | None) -> httpx.QueryParams | None:
    """Query-String der URL mit ``params`` zusammenführen (httpx ersetzt ihn sonst).

    ``params`` gewinnt bei gleichem Namen, ohne Groß-/Kleinschreibung (OGC-KVP: ``service`` = ``SERVICE``).
    """
    if params is None:
        return None
    override = {str(k).upper() for k in params}
    kept = [(k, v) for k, v in httpx.URL(url).params.multi_items() if k.upper() not in override]
    return httpx.QueryParams(kept + [(str(k), v) for k, v in params.items()])


def _retry_after(response: httpx.Response) -> float | None:
    """``Retry-After`` in Sekunden (Zahl oder HTTP-Datum), ``None`` wenn fehlend/unlesbar."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():  # isdigit() allein lässt z. B. „²“ durch
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _tls_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def _permanent(exc: BaseException) -> str | None:
    """Art eines bleibenden Fehlers, bei dem eine Wiederholung nichts ändert (sonst ``None``)."""
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.InvalidURL)):
        return "config"
    if isinstance(exc, httpx.ConnectError) and _tls_failure(exc):
        return "tls"
    return None


def _detached(response: httpx.Response, body: bytes) -> httpx.Response:
    """Vollständig gelesene Antwort ohne offenen Stream (Body bereits entpackt)."""
    drop = {"content-encoding", "content-length", "transfer-encoding"}
    headers = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in drop]
    out = httpx.Response(response.status_code, headers=headers, content=body, request=response.request)
    out.history = response.history
    return out


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


@dataclass
class DownloadResult:
    """Ergebnis eines Downloads; ``not_modified`` bei HTTP 304."""

    path: Path | None
    not_modified: bool
    sha256: str | None
    size: int
    etag: str | None
    last_modified: str | None
    url: str


class HttpClient:
    """Dünne Hülle um ``httpx.Client`` mit einheitlicher Fehlerbehandlung."""

    def __init__(self, settings: HttpSettings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_s, connect=min(settings.timeout_s, 30)),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            event_hooks={"response": [_block_downgrade]},
            transport=transport,
        )
        self.sleep: Callable[[float], None] = time.sleep  # austauschbar für Tests

    def close(self) -> None:
        """Schließt die Verbindungen."""
        self.client.close()

    # ------------------------------------------------------------------ intern
    def _retrying(self) -> Retrying:
        def is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, _RetryableStatus):
                return True
            return isinstance(exc, httpx.TransportError) and _permanent(exc) is None

        def before_sleep(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            log.warning(
                "HTTP-Versuch %s fehlgeschlagen (%s) – neuer Versuch", state.attempt_number, mask_secrets(str(exc))
            )

        base = self.settings.backoff_base_s
        backoff = wait_exponential(multiplier=base, max=60) + wait_random(0, base / 2)

        def wait(state: RetryCallState) -> float:
            exc = state.outcome.exception() if state.outcome else None
            if isinstance(exc, _RetryableStatus):
                after = _retry_after(exc.response)
                if after is not None:
                    return min(after, self.settings.max_retry_after_s)
            return float(backoff(state))

        return Retrying(
            stop=stop_after_attempt(self.settings.retries + 1),
            wait=wait,
            retry=retry_if_exception(is_retryable),
            before_sleep=before_sleep,
            sleep=lambda seconds: self.sleep(seconds),
            reraise=True,
        )

    def _raise_for(self, exc: BaseException, url: str, attempts: int) -> HttpError:
        if isinstance(exc, _RetryableStatus):
            r = exc.response
            return HttpError(
                "http", f"HTTP {r.status_code} nach {attempts} Versuchen", str(r.request.url), r.status_code, attempts
            )
        if isinstance(exc, httpx.TimeoutException):
            return HttpError("timeout", f"Zeitüberschreitung nach {attempts} Versuchen", url, None, attempts)
        permanent = _permanent(exc)
        if permanent == "tls":
            return HttpError("tls", f"TLS-Prüfung fehlgeschlagen (nicht wiederholt): {exc}", url, None, attempts)
        if permanent == "config":
            return HttpError("config", f"Anfrage nicht möglich (nicht wiederholt): {exc}", url, None, attempts)
        if isinstance(exc, httpx.DecodingError):
            return HttpError("http", f"Antwort nicht dekodierbar: {exc}", url, None, attempts)
        return HttpError("network", f"Netzwerkfehler nach {attempts} Versuchen: {exc}", url, None, attempts)

    def _body(self, response: httpx.Response, limit: int, deadline: float, url: str) -> Iterator[bytes]:
        """Entpackte Bytes der Antwort, mit Größen-, Verhältnis- und Fristprüfung."""
        codings = [
            c.strip().lower()
            for c in response.headers.get_list("content-encoding", split_commas=True)
            if c.strip().lower() not in ("", "identity")
        ]
        if len(codings) > 1:
            # httpx entpackt verschachtelte Kodierungen ohne Längengrenze: wenige KiB können vor der ersten
            # Prüfung Gigabytes ergeben. Kein bekannter Dienst liefert so etwas.
            raise HttpError(
                "too_large",
                f"Mehrfache Content-Encoding ({', '.join(codings)}) abgelehnt – vermutlich Kompressionsbombe",
                url,
                response.status_code,
            )
        ratio = self.settings.max_decompression_ratio
        total = 0
        for chunk in response.iter_bytes():  # ohne chunk_size: nicht bis 1 MiB puffern (Frist!)
            total += len(chunk)
            if total > limit:
                raise HttpError(
                    "too_large",
                    f"Antwort größer als {limit // MIB} MiB (entpackt) – abgebrochen",
                    url,
                    response.status_code,
                )
            if ratio and total > _RATIO_MIN_BYTES and total > ratio * max(response.num_bytes_downloaded, 1):
                raise HttpError(
                    "too_large",
                    f"Verhältnis entpackt/übertragen über {ratio:g}:1 ({total // MIB} MiB aus "
                    f"{response.num_bytes_downloaded // 1024} KiB) – vermutlich Kompressionsbombe, abgebrochen",
                    url,
                    response.status_code,
                )
            if time.monotonic() > deadline:
                raise HttpError(
                    "timeout", f"Gesamtfrist {self.settings.deadline_s:g} s überschritten – abgebrochen", url
                )
            yield chunk

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.status_code in self.settings.retry_on_status:
            raise _RetryableStatus(response)
        return response

    # ----------------------------------------------------------------- öffentlich
    def get(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """GET mit Wiederholversuchen. Fehlerstatus (außer 304) → ``HttpError``."""
        query = merge_query(url, params)
        limit = self.settings.max_response_mb * MIB
        attempts = 0
        try:
            for attempt in self._retrying():
                with attempt:
                    attempts += 1
                    deadline = time.monotonic() + self.settings.deadline_s
                    with self.client.stream("GET", url, params=query, headers=headers) as streamed:
                        self._check(streamed)
                        if streamed.status_code >= 400:
                            raise HttpError(
                                "http", f"HTTP {streamed.status_code}", str(streamed.request.url), streamed.status_code
                            )
                        body = bytearray()
                        for chunk in self._body(streamed, limit, deadline, str(streamed.request.url)):
                            body += chunk
                        response = _detached(streamed, bytes(body))
        except (httpx.HTTPError, httpx.InvalidURL, _RetryableStatus) as exc:
            raise self._raise_for(exc, url, attempts) from exc
        return response

    def head_ok(self, url: str, params: dict[str, Any] | None = None) -> bool:
        """True, wenn die URL mit 200 antwortet (HEAD, ohne Wiederholung bei 404)."""
        query = merge_query(url, params)
        try:
            for attempt in self._retrying():
                with attempt:
                    response = self._check(self.client.head(url, params=query))
        except (httpx.HTTPError, httpx.InvalidURL, _RetryableStatus):
            return False
        except HttpError as exc:
            log.warning("%s", exc)
            return False
        return response.status_code == 200

    def download(
        self,
        url: str,
        dest: Path,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> DownloadResult:
        """Lädt ``url`` gestreamt nach ``dest`` (nicht in den Speicher).

        Mit ``etag``/``last_modified`` wird bedingt angefragt; bei HTTP 304 bleibt ``dest`` unberührt.
        ``max_bytes`` (entpackt) ersetzt ``http.max_download_mb``; Überschreitung → ``HttpError("too_large")``.
        ``params`` werden an die Abfrage angehängt (z. B. Token), stehen aber nicht in ``url`` des Ergebnisses.
        """
        limit = max_bytes or self.settings.max_download_mb * MIB
        query = merge_query(url, params)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        attempts = 0
        try:
            for attempt in self._retrying():
                with attempt:
                    attempts += 1
                    deadline = time.monotonic() + self.settings.deadline_s
                    with self.client.stream("GET", url, params=query, headers=headers) as response:
                        self._check(response)
                        if response.status_code == 304:
                            return DownloadResult(None, True, None, 0, etag, last_modified, url)
                        if response.status_code >= 400:
                            raise HttpError(
                                "http", f"HTTP {response.status_code}", str(response.request.url), response.status_code
                            )
                        digest = hashlib.sha256()
                        size = 0
                        with tmp.open("wb") as fh:
                            for chunk in self._body(response, limit, deadline, str(response.request.url)):
                                fh.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                        tmp.replace(dest)
                        return DownloadResult(
                            dest,
                            False,
                            digest.hexdigest(),
                            size,
                            response.headers.get("etag"),
                            response.headers.get("last-modified"),
                            url,
                        )
        except (httpx.HTTPError, httpx.InvalidURL, _RetryableStatus) as exc:
            raise self._raise_for(exc, url, attempts) from exc
        finally:
            tmp.unlink(missing_ok=True)  # nach Erfolg schon umbenannt; sonst keine GB-großen Reste liegen lassen
        raise AssertionError("unreachable")  # pragma: no cover


def sha256_file(path: Path) -> str:
    """SHA-256 einer Datei (blockweise)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
