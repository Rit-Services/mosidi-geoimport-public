"""CSV-Quellen: lokale Datei oder Download per URL, optional in ZIP/GZ.

Generalisiert die INKAR-Vorarbeit (``inkar_import``):

- ``url_template`` + ``year: latest`` sucht wie das Vorarbeiten-Skript die neueste Jahresdatei per HEAD,
  beginnend beim nächsten Jahr rückwärts (plattformneutral, ohne undefinierte Variablen).
- ``archive.member_glob`` ersetzt die Dateimuster aus ``filenames_in_folder``.
- ``columns`` ersetzt das Spaltenmapping aus ``config.xml``.
- Die Formatprüfung (``inkar_version_comparison``) wird zum generischen ``schema_drift``-Check.

Die INKAR-ZIP (2025) ist mit Deflate64 komprimiert, das Pythons ``zipfile`` nicht entpacken kann;
``zipfile-deflate64`` (Apache-2.0) ergänzt das Verfahren. Die CSV wird gestreamt gelesen
(entpackt rund 6,8 GB), nicht vollständig in den Speicher geladen.
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import fnmatch
import gzip
import io
import json
import logging
import re
import zlib
from collections import deque
from collections.abc import Iterator
from datetime import date
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast
from urllib.parse import urlparse

import pandas as pd
import zipfile_deflate64 as zipfile
from shapely import points

from geoimport.config.models import ColumnSpec, CsvLimits, CsvSource
from geoimport.http import HttpError, sha256_file
from geoimport.logging import event, mask_secrets
from geoimport.sources import register_source
from geoimport.sources.base import (
    VALUE_COLUMNS,
    FetchResult,
    ParseResult,
    PipelineWarning,
    RawArtifact,
    RecordError,
    Source,
    SourceFailure,
)
from geoimport.transform.crs import normalize_crs
from geoimport.transform.keys import KEY_SEP, normalize_series
from geoimport.transform.mapping import with_number_format
from geoimport.transform.validate import RawFeature, prepare_features

log = logging.getLogger(__name__)

MAX_STORED_ERRORS = 10_000
SAMPLE_BYTES = 2 * 1024 * 1024
READ_BLOCK = 64 * 1024
RATIO_FLOOR_BYTES = 256 * 1024 * 1024  # kleine Archive dürfen unabhängig vom Packverhältnis so viel entpacken


def _match_members(names: list[str], pattern: str) -> list[str]:
    pat = pattern.lower()
    out = []
    for n in names:
        p = PurePosixPath(n)
        # macOS-Zusatzdaten (__MACOSX/, AppleDouble „._x.csv“) sind keine Tabellen, passen aber auf „*.csv“
        if n.endswith("/") or "__MACOSX" in p.parts or p.name.startswith("._"):
            continue
        if fnmatch.fnmatchcase(p.name.lower(), pat):
            out.append(n)
    return out


def _de(n: int) -> str:
    """Zahl mit Tausenderpunkt für Meldungen."""
    return f"{n:,}".replace(",", ".")


class LimitedReader(io.RawIOBase):
    """Liest einen (entpackten) Binärstrom und bricht ab, wenn eine Zeile oder die Gesamtmenge zu groß wird.

    Liest höchstens ``min(READ_BLOCK, max_line)`` Bytes je Aufruf; so liegt der Abbruch spätestens einen Block
    nach Überschreiten der Grenze, und ``readline()``/pandas halten nie mehr als Zeilengrenze + Block im Speicher.
    """

    def __init__(self, raw: IO[bytes], name: str, max_line: int, max_total: int, total_hint: str) -> None:
        self._raw, self._name, self._max_line, self._max_total = raw, name, max_line, max_total
        self._total_hint = total_hint
        self._total = 0
        self._line = 0  # Länge der aktuell offenen Zeile

    def readable(self) -> bool:  # noqa: D102
        return True

    def readinto(self, b: Any) -> int:  # noqa: D102
        data = self._raw.read(min(len(b), READ_BLOCK, self._max_line))
        n = len(data)
        if not n:
            return 0
        self._total += n
        if self._total > self._max_total:
            raise SourceFailure(
                "incomplete",
                f"{self._name}: mehr als {_de(self._max_total)} Bytes entpackt – Lesen abgebrochen "
                f"({self._total_hint}; Archivbombe oder Grenze zu niedrig)",
            )
        last = max(data.rfind(b"\n"), data.rfind(b"\r"))
        if last < 0:
            self._line += n
        else:
            # Ein Block ist nie länger als die Grenze; prüfen genügt für das Stück bis zum ersten Umbruch
            first = min(i for i in (data.find(b"\n"), data.find(b"\r")) if i >= 0)
            self._check_line(self._line + first)
            self._line = n - last - 1
        self._check_line(self._line)
        b[:n] = data
        return n

    def _check_line(self, length: int) -> None:
        if length > self._max_line:
            raise SourceFailure(
                "parse",
                f"{self._name}: Zeile länger als {_de(self._max_line)} Bytes (csv.limits.max_line_bytes) – "
                "keine CSV-Datei, falsches Zeilenende oder beschädigt",
            )


def _limit_total(path: Path, archived: bool, limits: CsvLimits) -> tuple[int, str]:
    """Höchstmenge entpackter Bytes und welche Einstellung sie setzt."""
    if archived and limits.max_compression_ratio is not None:
        by_ratio = max(int(limits.max_compression_ratio * path.stat().st_size), RATIO_FLOOR_BYTES)
        if by_ratio < limits.max_uncompressed_bytes:
            return by_ratio, f"csv.limits.max_compression_ratio = {limits.max_compression_ratio:g}"
    return limits.max_uncompressed_bytes, "csv.limits.max_uncompressed_bytes"


@contextlib.contextmanager
def open_table(path: Path, member_glob: str | None, limits: CsvLimits | None = None) -> Iterator[tuple[IO[bytes], str]]:
    """Öffnet CSV, CSV in GZ oder genau eine passende Datei in einem ZIP (binär, mit Lesegrenzen)."""
    limits = limits or CsvLimits()
    with _open_raw(path, member_glob) as (raw, member):
        max_total, hint = _limit_total(path, path.suffix.lower() in (".zip", ".gz"), limits)
        reader = LimitedReader(raw, member, limits.max_line_bytes, max_total, hint)
        yield cast(IO[bytes], io.BufferedReader(reader)), member


@contextlib.contextmanager
def _open_raw(path: Path, member_glob: str | None) -> Iterator[tuple[IO[bytes], str]]:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:  # z. B. abgebrochener Download: Inhaltsverzeichnis am Dateiende fehlt
            raise SourceFailure("incomplete", f"Archiv {path.name} unvollständig oder beschädigt: {exc}") from exc
        with zf:
            names = zf.namelist()
            matches = _match_members(names, member_glob or "*.csv")
            if len(matches) != 1:
                raise SourceFailure(
                    "archive",
                    f"Archiv {path.name}: Muster '{member_glob or '*.csv'}' trifft {len(matches)} Dateien "
                    f"(genau eine erwartet; Inhalt: {', '.join(names[:10])})",
                )
            with zf.open(matches[0]) as fh:
                yield fh, matches[0]
    elif suffix == ".gz":
        with gzip.open(path, "rb") as gz:
            yield cast(IO[bytes], gz), path.stem
    else:
        with path.open("rb") as fh:
            yield fh, path.name


_LINE_END = re.compile(rb"\r\n?|\n")


class _Lines:
    """Zeilenweise lesen mit ``\n``, ``\r\n`` oder ``\r`` als Zeilenende (``readline()`` kennt nur ``\n``).

    Die Zeilenlänge begrenzt bereits ``LimitedReader`` (``csv.limits.max_line_bytes``).
    """

    def __init__(self, raw: IO[bytes]) -> None:
        self._raw, self._buf, self._eof = raw, b"", False

    def readline(self) -> bytes:
        start = 0
        while True:
            m = _LINE_END.search(self._buf, start)
            # \r am Pufferende: erst weiterlesen, ob \n folgt
            if m and not (m.group() == b"\r" and m.end() == len(self._buf) and not self._eof):
                line, self._buf = self._buf[: m.end()], self._buf[m.end() :]
                return line
            if self._eof:
                line, self._buf = self._buf, b""
                return line
            start = max(len(self._buf) - 1, 0)
            chunk = self._raw.read(READ_BLOCK)
            if chunk:
                self._buf += chunk
            else:
                self._eof = True


class UntilMarker(io.RawIOBase):
    """Liest einen Binärstrom bis vor die erste Zeile, die mit ``marker`` beginnt (Fußnotenblock).

    ``found`` zeigt danach, ob die Marke erreicht wurde – fehlt sie, ist die Datei vermutlich abgeschnitten.
    """

    def __init__(self, raw: IO[bytes], marker: bytes) -> None:
        self._lines, self._marker, self._buf, self.found = _Lines(raw), marker, b"", False

    def readable(self) -> bool:  # noqa: D102
        return True

    def readinto(self, b: Any) -> int:  # noqa: D102
        while not self._buf and not self.found:
            line = self._lines.readline()
            if not line:
                break
            if line.startswith(self._marker):
                self.found = True
                break
            self._buf = line
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


class SkipLastLines(io.RawIOBase):
    """Gibt einen Binärstrom ohne seine letzten ``n`` nicht leeren Zeilen weiter (``footer_rows``).

    Gestreamt statt ``skipfooter`` (Python-Engine, ganze Datei im Speicher): zurückgehalten werden nur die
    jeweils letzten ``n`` Zeilen, je höchstens ``max_line_bytes`` lang.
    """

    def __init__(self, raw: IO[bytes], n: int) -> None:
        self._lines, self._n = _Lines(raw), n
        self._held: deque[bytes] = deque()
        self._count = 0  # nicht leere Zeilen in _held
        self._out = b""
        self._eof = False

    def readable(self) -> bool:  # noqa: D102
        return True

    def readinto(self, b: Any) -> int:  # noqa: D102
        while not self._out and not self._eof:
            line = self._lines.readline()
            if not line:
                self._eof = True  # was noch in _held liegt, ist die Fußzeile
                break
            if not self._held and not line.strip():
                self._out = line
                continue
            self._held.append(line)
            self._count += bool(line.strip())
            out = []
            while self._count > self._n:
                out.append(self._held.popleft())
                self._count -= 1
                while self._held and not self._held[0].strip():
                    out.append(self._held.popleft())
            self._out = b"".join(out)
        n = min(len(b), len(self._out))
        b[:n] = self._out[:n]
        self._out = self._out[n:]
        return n


@contextlib.contextmanager
def _limited(
    fh: IO[bytes], marker: str | None, encoding: str, footer_rows: int = 0
) -> Iterator[tuple[IO[bytes], UntilMarker | None]]:
    stream = None
    if marker:
        stream = UntilMarker(fh, marker.encode(encoding.replace("-sig", "")))
        fh = cast(IO[bytes], io.BufferedReader(stream))
    if footer_rows:
        fh = cast(IO[bytes], io.BufferedReader(SkipLastLines(fh, footer_rows)))
    yield fh, stream


def last_line_complete(path: Path, encoding: str, delimiter: str, quotechar: str, n_columns: int) -> bool:
    """Hat die letzte Zeile einer unkomprimierten CSV so viele Felder wie die Kopfzeile?

    Eine mitten in einer Zeile abgeschnittene Datei liefert sonst eine Zeile mit leeren Werten, die
    pandas stillschweigend übernimmt. ZIP/GZ erkennen Abbrüche selbst (Prüfsumme bzw. Dateiende).
    """
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 65536))
        tail = fh.read()
    lines = [ln for ln in tail.decode(encoding, errors="replace").splitlines() if ln.strip()]
    if not lines:
        return True
    fields = next(csv.reader([lines[-1]], delimiter=delimiter, quotechar=quotechar))
    return len(fields) >= n_columns


CP1252_FALLBACK = "geoimport_cp1252_fallback"
_fallback_hits = 0  # Zähler je Lauf (die Pipeline liest Quellen nacheinander)


def _cp1252_fallback(exc: UnicodeError) -> tuple[str, int]:
    """Fehlerbehandlung beim UTF-8-Lesen: ungültige Bytes als cp1252 deuten statt abzubrechen.

    Nur für Dateien, deren Stichprobe reines ASCII ist: Ob der Rest UTF-8 oder cp1252 ist, zeigt
    sich erst später – ohne Rückfall bräche ein INKAR-Lauf nach Gigabytes am ersten Umlaut ab.
    """
    global _fallback_hits
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    _fallback_hits += 1
    return exc.object[exc.start : exc.end].decode("cp1252", errors="replace"), exc.end


codecs.register_error(CP1252_FALLBACK, _cp1252_fallback)


def detect_encoding(sample: bytes) -> str:
    """Zeichenkodierung aus einer Stichprobe: BOM → ``utf-8-sig``, gültiges UTF-8 → ``utf-8``, sonst cp1252.

    Bewusst ohne statistische Erkennung: Bei kurzen deutschen Stichproben riet sie exotische Codepages
    (cp775, cp1006, cp932) und verstümmelte Umlaute still („Müncheberg“ → „M³ncheberg“). Deutsche
    Statistikdateien sind UTF-8 oder cp1252; latin-1 nur, wenn cp1252 ein Byte nicht kennt (0x81, 0x8D …).
    """
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    cut = sample[: sample.rfind(b"\n") + 1] or sample
    for enc in ("utf-8", "cp1252"):
        try:
            cut.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _to_number(s: pd.Series, decimal: str, thousands: str | None) -> tuple[pd.Series, pd.Series]:
    """Text → Zahl. Liefert (Zahlen, Maske nicht umwandelbarer Werte).

    Ist das Dezimaltrennzeichen nicht der Punkt, ist ein verbleibender Punkt ein Fehler: „1.234“ ist in
    deutschen Dateien meist ein Tausenderpunkt und würde sonst still zu 1,234 (Faktor 1000).
    """
    cleaned = s
    if thousands:
        cleaned = cleaned.str.replace(thousands, "", regex=False)
    stray_dot = pd.Series(False, index=s.index)
    if decimal != ".":
        stray_dot = cleaned.str.contains(".", regex=False).fillna(False).astype(bool)
        cleaned = cleaned.str.replace(decimal, ".", regex=False)
    num = pd.to_numeric(cleaned, errors="coerce").mask(stray_dot)
    return num, num.isna() & s.notna()


def required_value_fields(c: CsvSource) -> list[str]:
    """Pflichtfelder einer Kennzahl-Quelle (Bieterantwort 63): Raumbezug, Wert, Zeitbezug und ``guards.required``.

    Raumbezug, Wert und (falls die Quelle einen hat) Zeitbezug sind immer Pflicht, auch wenn sie in
    ``guards.required`` fehlen. Fehlt einer, wird die Zeile übersprungen (``missing_required_field``).
    """
    r = c.record
    implicit = [r.object_key, r.value, *([r.period] if r.period else [])]
    return list(dict.fromkeys([*implicit, *c.guards.required]))


def overflow_by_reason(reasons: pd.Series, stored: list[RecordError]) -> dict[str, int]:
    """Nicht einzeln gespeicherte Fehler je Grund: alle Gründe abzüglich der gespeicherten."""
    counts = {str(k): int(v) for k, v in reasons.value_counts().items()}
    for e in stored:
        if e.reason_code in counts:
            counts[e.reason_code] -= 1
    return {k: v for k, v in counts.items() if v > 0}


def convert_column(
    series: pd.Series, spec: ColumnSpec, decimal: str, thousands: str | None
) -> tuple[pd.Series, pd.Series]:
    """Typumwandlung einer Spalte. Liefert (Werte, Maske ungültiger Werte)."""
    s = series.astype("string")
    if spec.normalize:
        s = normalize_series(s, spec.normalize)
    else:
        s = s.str.strip().mask(lambda x: x == "")
    if spec.type == "str":
        return s, pd.Series(False, index=s.index)
    if spec.type in ("int", "float"):
        num, bad = _to_number(s, decimal, thousands)
        if spec.type == "int":
            frac = num.notna() & (num % 1 != 0)
            bad |= frac
            return num.where(~bad).astype("Int64"), bad
        return num.astype("Float64"), bad
    parsed = pd.to_datetime(s, format=spec.date_format, errors="coerce")
    bad = parsed.isna() & s.notna()
    return parsed.dt.strftime("%Y-%m-%d").astype("string"), bad


@register_source("csv", CsvSource)
class CsvSourcePlugin(Source):
    """Plugin für ``type: csv``."""

    cfg: CsvSource

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resolved_url: str | None = None

    # ------------------------------------------------------------------ Ort
    def location_url(self) -> str | None:  # noqa: D102
        loc = self.cfg.csv.location
        return mask_secrets(self._resolved_url or loc.url or loc.url_template or loc.path or "") or None

    def resolve_url(self) -> str:
        """URL der Datei; bei ``year: latest`` die neueste vorhandene Jahresdatei."""
        loc = self.cfg.csv.location
        if loc.url:
            return loc.url
        assert loc.url_template
        if isinstance(loc.year, int):
            return loc.url_template.format(year=loc.year)
        this_year = date.today().year
        tried = []
        for year in range(this_year + 1, this_year - loc.search_years_back - 1, -1):
            url = loc.url_template.format(year=year)
            tried.append(str(year))
            if self.ctx.http.head_ok(url, loc.params):
                event(log, "csv_latest", f"Neueste Datei: {url}", year=year)
                return url
        raise SourceFailure("not_found", f"Keine Datei für die Jahre {', '.join(tried)} gefunden ({loc.url_template})")

    # ------------------------------------------------------------------ Plugin-API
    def probe(self) -> dict[str, Any]:  # noqa: D102
        loc = self.cfg.csv.location
        if loc.path:
            p = Path(loc.path)
            return {"pfad": str(p), "vorhanden": p.exists(), "groesse_bytes": p.stat().st_size if p.exists() else None}
        url = self.resolve_url()
        try:
            r = self.ctx.http.client.head(url)
        except Exception as exc:  # Netzwerkfehler werden nur gemeldet
            return {"url": url, "erreichbar": False, "fehler": str(exc)}
        return {
            "url": url,
            "erreichbar": r.status_code == 200,
            "status": r.status_code,
            "groesse_bytes": r.headers.get("content-length"),
            "last_modified": r.headers.get("last-modified"),
            "etag": r.headers.get("etag"),
        }

    def fetch(self) -> FetchResult:  # noqa: D102
        loc = self.cfg.csv.location
        state = self.ctx.state.load()
        skip = self.cfg.skip_if_unchanged and self.ctx.use_conditional_requests
        if loc.path:
            path = Path(loc.path)
            if not path.is_file():
                raise SourceFailure("file_missing", f"Datei nicht gefunden: {path}")
            sha = sha256_file(path)
            result = FetchResult([RawArtifact(path, sha, None, {"local": True})], info={"path": str(path)})
            result.state_updates = {"raw_sha256": sha}
            result.unchanged = skip and state.get("raw_sha256") == sha
            return result

        url = self.resolve_url()
        shown = mask_secrets(url)  # so in Bericht, Zustand und Metadaten; Token in der URL bleiben dort nicht stehen
        self._resolved_url = shown
        name = PurePosixPath(urlparse(url).path).name or "download.csv"
        dest = self.ctx.raw_dir / name
        same_url = state.get("url") == shown
        try:
            dl = self.ctx.http.download(
                url,
                dest,
                params=loc.params,
                etag=state.get("etag") if skip and same_url else None,
                last_modified=state.get("last_modified") if skip and same_url else None,
            )
        except HttpError as exc:
            if loc.fallback_path and Path(loc.fallback_path).is_file():
                return self._fallback(Path(loc.fallback_path), exc)
            raise SourceFailure(exc.kind, str(exc), exc.status, exc.url) from exc
        if dl.not_modified:
            return FetchResult([], unchanged=True, info={"url": shown, "not_modified": True})
        assert dl.path and dl.sha256
        result = FetchResult(
            [RawArtifact(dl.path, dl.sha256, shown, {"size": dl.size})],
            info={"url": shown, "size_bytes": dl.size, "last_modified": dl.last_modified},
        )
        result.state_updates = {
            "url": shown,
            "etag": dl.etag,
            "last_modified": dl.last_modified,
            "raw_sha256": dl.sha256,
        }
        result.unchanged = skip and same_url and state.get("raw_sha256") == dl.sha256
        return result

    def _fallback(self, path: Path, exc: HttpError) -> FetchResult:
        """Download gescheitert: Ersatzdatei lesen (Bieterantwort V10, AfS darf aus einer Datei kommen)."""
        sha = sha256_file(path)
        result = FetchResult(
            [RawArtifact(path, sha, None, {"local": True, "fallback": True})],
            info={"path": str(path), "fallback_for": exc.url},
            warnings=[PipelineWarning("fallback_file", f"Download fehlgeschlagen ({exc}); Ersatzdatei {path} gelesen")],
        )
        result.state_updates = {"raw_sha256": sha}
        return result

    # ------------------------------------------------------------------ Lesen
    def _source_columns(self) -> set[str]:
        c = self.cfg
        cols = set(c.columns)
        cols |= set(c.csv.row_filter)
        if c.csv.region_filter:
            cols.add(c.csv.region_filter.column)
        if c.coordinates:
            cols |= {c.coordinates.x, c.coordinates.y}
        if c.reshape.wide_to_long:
            cols |= set(c.reshape.wide_to_long.id_columns)
            cols.discard(c.reshape.wide_to_long.var_name)
            cols.discard(c.reshape.wide_to_long.value_name)
        return cols

    def _read_kwargs(self, encoding: str, ascii_sample: bool = False) -> dict[str, Any]:
        c = self.cfg.csv
        return {
            "sep": c.delimiter,
            "quotechar": c.quotechar,
            "encoding": encoding,
            "encoding_errors": CP1252_FALLBACK if ascii_sample and encoding == "utf-8" else "strict",
            "header": None if c.column_names else c.header_row,
            "names": c.column_names,
            "skiprows": c.skip_rows or None,
            "dtype": str,
            "keep_default_na": False,
            "na_values": [*c.na_values, ""],
            "on_bad_lines": "error",
        }

    def parse(self, fetched: FetchResult) -> ParseResult:  # noqa: D102
        if not fetched.artifacts:
            raise SourceFailure("parse", "Keine Rohdatei vorhanden")
        c = self.cfg
        path = fetched.artifacts[0].path
        member_glob = c.csv.archive.member_glob if c.csv.archive else None
        warnings: list[PipelineWarning] = []

        limits = c.csv.limits
        ascii_sample = False
        with open_table(path, member_glob, limits) as (fh, member):
            if c.csv.encoding == "auto":
                sample = fh.read(SAMPLE_BYTES)
                encoding, ascii_sample = detect_encoding(sample), sample.isascii()
                if encoding not in ("utf-8", "utf-8-sig"):
                    warnings.append(
                        PipelineWarning(
                            "encoding",
                            f"Kodierung automatisch als {encoding} erkannt (kein gültiges UTF-8) – "
                            "bei falschen Sonderzeichen csv.encoding festlegen",
                            {"encoding": encoding},
                        )
                    )
            else:
                encoding = c.csv.encoding
        marker = c.csv.footer_marker
        with open_table(path, member_glob, limits) as (raw_fh, member), _limited(raw_fh, marker, encoding) as (fh, _):
            try:
                header = pd.read_csv(
                    fh, nrows=0 if not c.csv.column_names else 1, **self._read_kwargs(encoding, ascii_sample)
                )
            except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError, ValueError) as exc:
                raise SourceFailure("parse", f"{member}: Kopfzeile nicht lesbar ({encoding}): {exc}") from exc
        columns = [str(col) for col in header.columns]
        if c.csv.column_names and len(header) and header.iloc[0].isna().all():
            raise SourceFailure("schema_mismatch", f"{member}: erste Datenzeile passt nicht zu column_names")
        if path.suffix.lower() not in (".zip", ".gz") and not c.csv.footer_rows and not marker:
            if not last_line_complete(path, encoding, c.csv.delimiter, c.csv.quotechar, len(columns)):
                raise SourceFailure(
                    "incomplete", f"{member}: letzte Zeile hat weniger Felder als die Kopfzeile – Datei abgeschnitten?"
                )

        needed = self._source_columns()
        missing = sorted(col for col in needed if col not in columns)
        if missing:
            raise SourceFailure(
                "schema_mismatch",
                f"Spalten fehlen in {member}: {', '.join(missing)} (vorhanden: {', '.join(columns[:30])})",
            )
        if c.guards.expected_columns:
            absent = [col for col in c.guards.expected_columns if col not in columns]
            if absent:
                warnings.append(PipelineWarning("schema_drift", f"Erwartete Spalten fehlen: {', '.join(absent)}"))
        old_cols = self.ctx.state.load().get("columns")
        if old_cols and old_cols != columns:
            new = [x for x in columns if x not in old_cols]
            gone = [x for x in old_cols if x not in columns]
            msg = "; ".join(
                filter(None, [f"neu: {', '.join(new)}" if new else "", f"entfallen: {', '.join(gone)}" if gone else ""])
            )
            warnings.append(PipelineWarning("schema_drift", f"Spalten geändert ({msg or 'Reihenfolge'})"))

        usecols = None if c.reshape.wide_to_long else sorted(needed)
        frames: list[pd.DataFrame] = []
        error_rows: list[RecordError] = []
        overflow = 0
        overflow_reasons: dict[str, int] = {}
        rows_read = 0
        rows_filtered = 0
        kwargs = self._read_kwargs(encoding, ascii_sample)
        global _fallback_hits
        _fallback_hits = 0
        with (
            open_table(path, member_glob, limits) as (raw_fh, member),
            _limited(raw_fh, marker, encoding, c.csv.footer_rows) as (fh, limiter),
        ):
            try:
                for chunk in pd.read_csv(fh, usecols=usecols, chunksize=c.csv.chunksize, **kwargs):
                    rows_read += len(chunk)
                    chunk = self._filter(chunk)
                    if chunk.empty:
                        continue
                    chunk = self._reshape(chunk)
                    rows_filtered += len(chunk)
                    out, bad_rows, not_stored = self._convert(chunk, MAX_STORED_ERRORS - len(error_rows))
                    error_rows += [RecordError(None, "invalid_value", reason, None, raw) for raw, reason in bad_rows]
                    overflow += not_stored
                    if not_stored:
                        overflow_reasons["invalid_value"] = overflow_reasons.get("invalid_value", 0) + not_stored
                    frames.append(out)
            except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
                raise SourceFailure("parse", f"{member}: CSV nicht lesbar ({encoding}): {exc}") from exc
            except (zipfile.BadZipFile, EOFError, zlib.error) as exc:  # Prüfsumme falsch bzw. Datei endet zu früh
                raise SourceFailure("incomplete", f"{member}: Datei unvollständig oder beschädigt: {exc}") from exc

        if limiter is not None and not limiter.found:
            raise SourceFailure("incomplete", f"{member}: Fußnotenmarke {marker!r} fehlt – Datei abgeschnitten?")
        if _fallback_hits:
            warnings.append(
                PipelineWarning(
                    "encoding",
                    f"Stichprobe war reines ASCII, die Datei enthält aber {_fallback_hits} Nicht-UTF-8-Zeichen; "
                    "als cp1252 gelesen – Kodierung bitte in csv.encoding festlegen",
                )
            )
        targets = [spec.to for spec in c.columns.values()]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=targets)
        info = {
            **fetched.info,
            "member": member,
            "detected_encoding": encoding,
            "rows_read": rows_read,
            "rows_after_filter": rows_filtered,
        }
        event(log, "csv_read", f"{rows_read} Zeilen gelesen, {rows_filtered} nach Filter", rows=rows_read)
        state_updates = {"columns": columns}

        if c.coordinates:
            result = self._as_features(df)
        else:
            result = self._as_values(df)
        result.errors = error_rows + result.errors
        # nicht überschreiben: _as_values zählt seine eigenen, nicht gespeicherten Fehler schon mit
        result.invalid_overflow += overflow
        for reason, n in overflow_reasons.items():
            result.overflow_reasons[reason] = result.overflow_reasons.get(reason, 0) + n
        result.fetched = rows_filtered
        result.warnings = warnings + result.warnings
        result.info = info
        result.state_updates = state_updates
        return result

    def _filter(self, chunk: pd.DataFrame) -> pd.DataFrame:
        c = self.cfg.csv
        mask = pd.Series(True, index=chunk.index)
        for col, val in c.row_filter.items():
            values = val if isinstance(val, list) else [val]
            mask &= chunk[col].isin(values)
        if c.region_filter:
            p = c.region_filter.prefix
            prefixes = tuple(p) if isinstance(p, list) else (p,)
            mask &= chunk[c.region_filter.column].fillna("").str.strip().str.startswith(prefixes)
        return chunk[mask]

    def _reshape(self, chunk: pd.DataFrame) -> pd.DataFrame:
        w = self.cfg.reshape.wide_to_long
        if not w:
            return chunk
        value_cols = w.value_columns or [x for x in chunk.columns if x not in w.id_columns]
        long = chunk.melt(id_vars=w.id_columns, value_vars=value_cols, var_name=w.var_name, value_name=w.value_name)
        if w.indicator:
            long["__indicator__"] = w.indicator
        return long

    def _convert(
        self, chunk: pd.DataFrame, max_rows: int
    ) -> tuple[pd.DataFrame, list[tuple[dict[str, Any], str]], int]:
        """Spalten umwandeln. Liefert gültige Zeilen, höchstens ``max_rows`` Fehlerzeilen und die Zahl der übrigen."""
        c = self.cfg
        out = pd.DataFrame(index=chunk.index)
        bad_any = pd.Series(False, index=chunk.index)
        reasons: dict[int, str] = {}
        specs = dict(c.columns)
        w = c.reshape.wide_to_long
        if w:
            # Nach wide→long gibt es die Spalten var_name/value_name; ohne eigenes Mapping gleichnamig übernehmen
            for col in (w.var_name, w.value_name):
                specs.setdefault(col, ColumnSpec(to=col))
            if w.indicator:
                specs.setdefault("__indicator__", ColumnSpec(to=c.record.name))
        for src, spec in specs.items():
            if src not in chunk.columns:
                continue
            values, bad = convert_column(chunk[src], spec, c.csv.decimal, c.csv.thousands)
            out[spec.to] = values
            for idx in bad[bad].index:
                reasons.setdefault(int(idx), f"Spalte '{src}': Wert {chunk.at[idx, src]!r} ist kein {spec.type}")
            bad_any |= bad
        if c.coordinates:
            for col in (c.coordinates.x, c.coordinates.y):
                num, _ = _to_number(chunk[col].astype("string").str.strip(), c.csv.decimal, c.csv.thousands)
                out[f"__{col}__"] = num
                bad = num.isna()
                for idx in bad[bad].index:
                    reasons.setdefault(int(idx), f"Koordinate '{col}' fehlt oder ist keine Zahl")
                bad_any |= bad
        bad_idx = bad_any[bad_any].index
        keep = max(max_rows, 0)
        # Nur die gespeicherten Fehlerzeilen aufbauen; bei falscher Konfiguration wären es sonst Millionen
        bad_rows = [
            ({k: (None if pd.isna(v) else str(v)) for k, v in chunk.loc[idx].items()}, reasons[int(idx)])
            for idx in bad_idx[:keep]
        ]
        return out[~bad_any], bad_rows, max(len(bad_idx) - keep, 0)

    def _as_values(self, df: pd.DataFrame) -> ParseResult:
        c = self.cfg
        r = c.record
        errors: list[RecordError] = []
        for f in [r.object_key, r.name, r.value, *([r.period] if r.period else [])]:
            if f not in df.columns:
                raise SourceFailure("config", f"Zielfeld '{f}' (record) wird von keiner Spalte erzeugt")
        key_parts = []
        for f in c.key.fields:
            if f not in df.columns:
                raise SourceFailure("config", f"Schlüsselfeld '{f}' wird von keiner Spalte erzeugt")
            key_parts.append(
                normalize_series(df[f], c.key.normalize.get(f)) if f in c.key.normalize else df[f].astype("string")
            )
        key = key_parts[0].copy()
        for part in key_parts[1:]:
            key = key.str.cat(part, sep=KEY_SEP)
        bad = key.isna()
        reason = pd.Series("", index=df.index, dtype="object")
        reason[bad] = "missing_key"
        required = required_value_fields(c)
        for f in required:
            if f in df.columns:
                miss = df[f].isna() & ~bad
                reason[miss] = "missing_required_field"
                bad |= miss
        dup = key.duplicated(keep=False) & ~bad
        reason[dup] = "duplicate_key"
        bad |= dup

        texts = {
            "missing_key": f"Schlüsselfeld(er) {c.key.fields} leer",
            "missing_required_field": "Pflichtfeld(er) leer: {fields}",
            "duplicate_key": "Schlüssel mehrfach geliefert",
        }
        for idx in bad[bad].index[:MAX_STORED_ERRORS]:
            row = {k: (None if pd.isna(v) else str(v)) for k, v in df.loc[idx].items()}
            k = None if pd.isna(key.at[idx]) else str(key.at[idx])
            text = texts[reason.at[idx]].format(
                fields=", ".join(f for f in required if f in df and pd.isna(df.at[idx, f]))
            )
            errors.append(RecordError(k, reason.at[idx], text, None, row))
        overflow = max(0, int(bad.sum()) - MAX_STORED_ERRORS)
        overflow_reasons = overflow_by_reason(reason[bad], errors)

        good = df[~bad]
        record_fields = {r.object_key, r.name, r.value, *([r.period] if r.period else [])}
        extra_cols = [spec.to for spec in c.columns.values() if spec.keep and spec.to not in record_fields]
        extra_cols = [x for x in dict.fromkeys(extra_cols) if x in good.columns]
        if extra_cols:
            extra = [
                json.dumps(dict(zip(extra_cols, row, strict=True)), ensure_ascii=False, default=str, sort_keys=True)
                for row in good[extra_cols]
                .astype("object")
                .where(good[extra_cols].notna(), None)
                .itertuples(index=False)
            ]
        else:
            extra = [None] * len(good)  # type: ignore[list-item]
        values = pd.DataFrame(
            {
                "key": key[~bad].astype("string"),
                "object_key": good[r.object_key].astype("string"),
                "name": good[r.name].astype("string"),
                "value": good[r.value],
                "period": good[r.period].astype("string")
                if r.period
                else pd.Series(pd.NA, index=good.index, dtype="string"),
                "extra": pd.Series(extra, index=good.index, dtype="string"),
            }
        ).reset_index(drop=True)
        from geoimport.diff import value_hashes

        values["hash"] = value_hashes(values)
        res = ParseResult(values=values[VALUE_COLUMNS], errors=errors)
        res.invalid_overflow = overflow
        res.overflow_reasons = overflow_reasons
        return res

    def _as_features(self, df: pd.DataFrame) -> ParseResult:
        c = self.cfg
        assert c.coordinates
        crs = normalize_crs(c.coordinates.crs)
        xcol, ycol = f"__{c.coordinates.x}__", f"__{c.coordinates.y}__"
        geoms = points(df[xcol].to_numpy(dtype=float), df[ycol].to_numpy(dtype=float))
        props_df = df.drop(columns=[xcol, ycol]).astype("object").where(df.drop(columns=[xcol, ycol]).notna(), None)
        raw = [
            RawFeature(dict(zip(props_df.columns, row, strict=True)), g)
            for row, g in zip(props_df.itertuples(index=False), geoms, strict=True)
        ]
        features, errors = prepare_features(
            raw,
            key=c.key,
            mapping=with_number_format(c.mapping, c.csv.decimal, c.csv.thousands),
            required=c.guards.required,
            geometry_types=c.guards.geometry_types,
            src_crs=crs,
            pipeline=self.ctx.pipeline,
        )
        return ParseResult(features=features, errors=errors)
