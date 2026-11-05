"""CSV-Ressourcengrenzen (SECURITY_REVIEW S3): Zeilenlänge, entpackte Größe, Packverhältnis, Fußzeilen."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path
from typing import Any

import pytest

import geoimport.sources.csv_source as csv_source
from geoimport.config.models import CsvLimits
from geoimport.orchestrator import INCOMPLETE_KINDS
from geoimport.sources.base import SourceFailure
from geoimport.sources.csv_source import open_table
from tests.unit.test_csv import _plugin

MIB = 1024 * 1024


class _Counting:
    """Zählt, wie viele entpackte Bytes aus dem GZ-Strom gelesen wurden."""

    def __init__(self, inner: Any, counter: list[int]) -> None:
        self._inner, self._counter = inner, counter

    def read(self, n: int = -1) -> bytes:
        data = self._inner.read(n)
        self._counter[0] += len(data)
        return bytes(data)

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def readable(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _Counting:
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.close()


@pytest.fixture
def gz_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    counter = [0]
    real_open = gzip.open

    def counting_open(*args: Any, **kwargs: Any) -> _Counting:
        return _Counting(real_open(*args, **kwargs), counter)

    monkeypatch.setattr(csv_source.gzip, "open", counting_open)
    return counter


def _src(path: Path, **csv: Any) -> dict:
    return {
        "id": "grenzen",
        "type": "csv",
        "title": "Test",
        "license": "synthetisch",
        "csv": {"location": {"path": str(path)}, **csv},
        "columns": {"a": {"to": "region_key"}, "b": {"to": "indicator"}, "c": {"to": "value", "type": "float"}},
        "record": {"period": None},
        "business_key": ["region_key", "indicator"],
        "target": {"dataset": "grenzen"},
    }


def _gz(path: Path, blocks: int, block: bytes, head: bytes = b"") -> Path:
    with gzip.open(path, "wb", compresslevel=1) as g:
        g.write(head)
        for _ in range(blocks):
            g.write(block)
    return path


def test_lange_zeile_in_gz_bricht_vor_dem_kopf_ab(tmp_path: Path, gz_counter: list[int]) -> None:
    # Nachbau von t_line.py: kleine GZ, darin eine einzige Zeile ohne Zeilenumbruch (hier 64 MB statt 400 MB)
    path = _gz(tmp_path / "bombe.csv.gz", 64, b"A" * MIB)
    assert path.stat().st_size < 1 * MIB
    p = _plugin(tmp_path, _src(path))
    with pytest.raises(SourceFailure, match="Zeile") as exc:
        p.parse(p.fetch())
    assert exc.value.kind in INCOMPLETE_KINDS
    # Abbruch nach höchstens Zeilengrenze + ein Leseblock – nicht nach der ganzen Zeile
    assert gz_counter[0] <= CsvLimits().max_line_bytes + 3 * 64 * 1024


def test_zeilengrenze_konfigurierbar_auch_bei_fussnotenmarke(tmp_path: Path) -> None:
    path = tmp_path / "lang.csv"
    path.write_bytes(b"a;b;c\n1;x;2\n" + b"9;" + b"y" * 5000 + b";1\n__________\nFussnote\n")
    p = _plugin(tmp_path, _src(path, footer_marker="__________", limits={"max_line_bytes": 2048}))
    with pytest.raises(SourceFailure, match="max_line_bytes") as exc:
        p.parse(p.fetch())
    assert exc.value.kind in INCOMPLETE_KINDS
    # Gleiche Datei mit Standardgrenze liest sauber
    p = _plugin(tmp_path, _src(path, footer_marker="__________"))
    assert len(p.parse(p.fetch()).values) == 2  # type: ignore[arg-type]


def test_gz_bombe_kurzer_zeilen_scheitert_am_packverhaeltnis(
    tmp_path: Path, gz_counter: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csv_source, "RATIO_FLOOR_BYTES", 1 * MIB)  # Untergrenze für den Test klein
    path = _gz(tmp_path / "kurz.csv.gz", 200, b"1;x;2\n" * (MIB // 6), head=b"a;b;c\n")
    compressed = path.stat().st_size
    limit = 100 * compressed  # Standard max_compression_ratio
    with pytest.raises(SourceFailure, match="max_compression_ratio") as exc, open_table(path, None) as (fh, _):
        while fh.read(MIB):
            pass
    assert exc.value.kind in INCOMPLETE_KINDS
    assert gz_counter[0] <= limit + 3 * 64 * 1024 < 200 * MIB


def test_entpackte_groesse_begrenzt_auch_ueber_pandas(tmp_path: Path) -> None:
    path = tmp_path / "gross.csv"
    path.write_bytes(b"a;b;c\n" + b"1;x;2\n" * 400_000)  # 2,4 MB
    p = _plugin(tmp_path, _src(path, limits={"max_uncompressed_bytes": MIB}))
    with pytest.raises(SourceFailure, match="max_uncompressed_bytes") as exc:
        p.parse(p.fetch())
    assert exc.value.kind in INCOMPLETE_KINDS


def test_zip_mitglied_mit_langer_zeile(tmp_path: Path) -> None:
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("daten.csv", b"a;b;c\n" + b"B" * (3 * MIB))
    with pytest.raises(SourceFailure, match="Zeile") as exc, open_table(z, None) as (fh, _):
        fh.read()
    assert exc.value.kind in INCOMPLETE_KINDS


def test_fussnotenzeilen_gestreamt(tmp_path: Path) -> None:
    # footer_rows ohne Python-Engine: Blöcke wie sonst, die letzten N nicht leeren Zeilen entfallen
    path = tmp_path / "fuss.csv"
    rows = "".join(f"{i};x{i};{i},5\n" for i in range(1, 351))
    path.write_text("a;b;c\n" + rows + "Quelle: synthetisch\n\nStand: 2026\n\n", encoding="utf-8")
    p = _plugin(tmp_path, _src(path, footer_rows=2, chunksize=100))
    parsed = p.parse(p.fetch())
    v = parsed.values
    assert v is not None and len(v) == 350 and not parsed.errors
    assert v["value"].iloc[-1] == pytest.approx(350.5)


# ---------------------------------------------------------------- Zeilenenden in Fußzeilen-Filtern
@pytest.mark.parametrize(
    ("data", "n", "expected"),
    [
        (b"h\rd1\rd2\rF1\rF2\r", 2, b"h\rd1\rd2\r"),  # nur CR (ältere Mac-Exporte)
        (b"h\r\nd1\r\nF\r\n", 1, b"h\r\nd1\r\n"),
        (b"h\nd1\nF", 1, b"h\nd1\n"),  # ohne abschließenden Umbruch
        (b"h\nd1\nF\n\n\n", 1, b"h\nd1\n"),  # Leerzeilen nach der Fußzeile
        (b"h\n", 3, b""),  # weniger Zeilen als footer_rows
    ],
)
def test_footer_rows_zeilenenden(data: bytes, n: int, expected: bytes) -> None:
    import io

    assert io.BufferedReader(csv_source.SkipLastLines(io.BytesIO(data), n)).read() == expected


def test_crlf_ueber_blockgrenze() -> None:
    import io

    data = b"a" * (csv_source.READ_BLOCK - 1) + b"\r\nb\rc\n"
    lines = csv_source._Lines(io.BytesIO(data))
    assert [lines.readline() for _ in range(4)] == [b"a" * (csv_source.READ_BLOCK - 1) + b"\r\n", b"b\r", b"c\n", b""]


def test_footer_marker_nur_cr() -> None:
    import io

    stream = csv_source.UntilMarker(io.BytesIO(b"h\rd1\r___ Fussnote\rmehr\r"), b"___")
    assert io.BufferedReader(stream).read() == b"h\rd1\r" and stream.found
