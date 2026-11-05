"""Kommandozeile ``pipeline`` (Typer).

Beispiele::

    pipeline validate-config
    pipeline check-target
    pipeline run                                  # alle aktiven Quellen
    pipeline run --source geobasis_bb_gemeinden --dry-run
    pipeline report --format md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from geoimport import __version__
from geoimport.config.loader import ConfigError, LoadedConfig, json_schema, load_config
from geoimport.exitcodes import ExitCode
from geoimport.logging import setup_logging

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Geodaten-Import-Pipeline (WFS/CSV → PostgreSQL/PostGIS)"
)

ConfigDir = Annotated[Path | None, typer.Option("--config-dir", envvar="CONFIG_DIR", help="Konfigurationsverzeichnis")]


def _load(config_dir: Path | None) -> LoadedConfig:
    try:
        return load_config(config_dir)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.CONFIG) from exc


def _fail(msg: str, code: int = ExitCode.CONFIG) -> NoReturn:
    typer.echo(msg, err=True)
    raise typer.Exit(code)


def _fail_db(exc: ConfigError) -> NoReturn:
    """Verbindungsfehler: Datenbank nicht erreichbar (Exit 5) oder Verbindungsangaben fehlen (Exit 3)."""
    from geoimport.db import DatabaseUnavailable

    _fail(str(exc), ExitCode.DB_UNAVAILABLE if isinstance(exc, DatabaseUnavailable) else ExitCode.CONFIG)


@app.callback()
def main(
    log_level: Annotated[str | None, typer.Option(envvar="LOG_LEVEL", help="DEBUG, INFO, WARNING")] = None,
    log_format: Annotated[str | None, typer.Option(envvar="LOG_FORMAT", help="json oder text")] = None,
) -> None:
    """Gemeinsame Optionen."""
    for stream in (sys.stdout, sys.stderr):  # Windows-Konsolen: Umlaute nicht verlieren
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    setup_logging(log_level, log_format)


@app.command()
def version() -> None:
    """Version ausgeben."""
    typer.echo(__version__)


@app.command("validate-config")
def validate_config(
    config_dir: ConfigDir = None,
    schema: Annotated[Path | None, typer.Option(help="JSON-Schema der Konfiguration in diese Datei schreiben")] = None,
) -> None:
    """YAML-Dateien gegen das Schema prüfen (inkl. Platzhalterregel)."""
    if schema:
        schema.write_text(json.dumps(json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"Schema geschrieben: {schema}")
        return
    cfg = _load(config_dir)
    typer.echo(
        f"Konfiguration gültig: {cfg.config_dir} ({len(cfg.sources)} Quellen, davon {len(cfg.enabled_sources())} aktiv)"
    )
    for s in cfg.sources:
        typer.echo(f"  {'[x]' if s.enabled else '[ ]'} {s.id:<32} {s.type:<4} {s.title}")
    for w in cfg.warnings:
        typer.echo(f"  Hinweis: {w}")


@app.command("check-target")
def check_target(
    config_dir: ConfigDir = None,
    quick: Annotated[bool, typer.Option(help="Nur Verbindung prüfen (Healthcheck)")] = False,
    snapshot: Annotated[
        Path | None, typer.Option(help="Schema-Schnappschuss des Zielschemas als JSON speichern")
    ] = None,
) -> None:
    """Zieltabellen gegen target.yaml prüfen – nur lesend, es wird nichts angelegt oder geändert."""
    from geoimport.db import connect
    from geoimport.target.adapter import make_adapter
    from geoimport.target.introspect import schema_snapshot

    cfg = _load(config_dir)
    try:
        conn = connect(autocommit=True, settings=cfg.pipeline.db)
    except ConfigError as exc:
        _fail_db(exc)
    with conn:
        if quick:
            conn.execute("SELECT 1")
            typer.echo("Datenbank erreichbar")
            return
        issues = make_adapter(cfg.target).check(conn)
        if snapshot:
            snapshot.write_text(
                json.dumps(schema_snapshot(conn, cfg.target.schema_name), indent=2, default=str), encoding="utf-8"
            )
            typer.echo(f"Schnappschuss gespeichert: {snapshot}")
    errors = [i for i in issues if i.level == "error"]
    for i in issues:
        typer.echo(f"  {'FEHLER' if i.level == 'error' else 'Hinweis'}: {i.message}")
    if errors:
        _fail(f"Zielschema passt nicht zu {cfg.target_file.name} ({len(errors)} Fehler)")
    typer.echo(f"Zielschema passt zu {cfg.target_file.name} ({cfg.target.schema_name}, Adapter {cfg.target.adapter})")


@app.command("init-meta")
def init_meta(config_dir: ConfigDir = None) -> None:
    """Schema pipeline_meta anlegen bzw. auf die aktuelle Version bringen (bei Adapter demo_reference
    zusätzlich das Demo-Referenzmodell). Ohne ausstehende Änderung wird nichts ausgeführt."""
    from geoimport.db import connect
    from geoimport.meta.repository import MetaRepository, MetaSchemaError
    from geoimport.target.adapter import make_adapter
    from geoimport.target.demo_reference import DemoReferenceAdapter

    cfg = _load(config_dir)
    m = cfg.pipeline.meta
    try:
        if m.location == "database":
            _ensure_database(m.database)
        conn = connect(None if m.location == "schema" else m.database, autocommit=True, settings=cfg.pipeline.db)
    except ConfigError as exc:
        _fail_db(exc)
    with conn:
        repo = MetaRepository(conn, m.schema_name)
        try:
            applied = repo.migrate(__version__)
            repo.check_schema()
        except MetaSchemaError as exc:
            _fail(str(exc), ExitCode.META_SCHEMA)
        where = "eigene Datenbank " + m.database if m.location == "database" else "Zieldatenbank"
        done = f"Migration(en) {', '.join(map(str, applied))} angewendet" if applied else "keine Änderung nötig"
        typer.echo(f"Schema {m.schema_name} bereit, Version {repo.schema_version()} ({where}; {done})")
    adapter = make_adapter(cfg.target)
    if isinstance(adapter, DemoReferenceAdapter):
        with connect(autocommit=True, settings=cfg.pipeline.db) as conn:
            try:
                adapter.ensure_model(conn)
            except RuntimeError as exc:
                _fail(str(exc))
        typer.echo("Demo-Referenzmodell (Schema ref) bereit – NUR DEMO, nicht das Datenmodell der TH")


def _ensure_database(name: str) -> None:
    from psycopg import sql

    from geoimport.db import connect

    with connect(autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            return
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except Exception as exc:
            raise ConfigError(f"Datenbank {name} fehlt und kann nicht angelegt werden: {exc}") from exc


@app.command()
def run(
    config_dir: ConfigDir = None,
    source: Annotated[list[str] | None, typer.Option("--source", "-s", help="nur diese Quelle(n)")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="alles außer Schreiben in die Zieltabellen")] = False,
    force_replace: Annotated[
        list[str] | None, typer.Option("--force-replace", help="Schutzregeln für diese Quelle übersteuern")
    ] = None,
    adopt: Annotated[
        list[str] | None,
        typer.Option(
            "--adopt",
            help="bestehende, nicht von der Pipeline angelegte Metadatenzeile dieser Quelle übernehmen (einmalig)",
        ),
    ] = None,
    full: Annotated[bool, typer.Option("--full", help="auch unveränderte Quellen neu einlesen")] = False,
    trigger: Annotated[str, typer.Option(help="manual | schedule | test")] = "manual",
) -> None:
    """Import ausführen. Exit 0 ok, 1 teilweise, 2 alle fehlgeschlagen, 3 Konfiguration/Zielschema, 4 Lauf aktiv,
    5 Datenbank nicht erreichbar, 6 pipeline_meta fehlt/veraltet (init-meta), 7 interner Fehler."""
    from geoimport.orchestrator import RunOptions

    if trigger not in ("manual", "schedule", "test"):
        _fail("--trigger muss manual, schedule oder test sein")
    cfg = _load(config_dir)
    opts = RunOptions(
        source_ids=source or None,
        dry_run=dry_run,
        force_replace=set(force_replace or []),
        adopt=set(adopt or []),
        trigger=trigger,
        full=full,
    )
    _execute(cfg, opts)


def _execute(cfg: LoadedConfig, opts: Any) -> None:
    import logging

    from geoimport.db import DatabaseUnavailable
    from geoimport.orchestrator import RunLocked, Terminated, run_pipeline

    try:
        outcome = run_pipeline(cfg, opts)
    except RunLocked as exc:
        _fail(str(exc), ExitCode.LOCKED)
    except DatabaseUnavailable as exc:
        _fail(str(exc), ExitCode.DB_UNAVAILABLE)
    except ConfigError as exc:
        _fail(str(exc), ExitCode.CONFIG)
    except Terminated:
        _fail("Lauf durch SIGTERM beendet", ExitCode.TERMINATED)
    except KeyboardInterrupt:
        _fail("Lauf abgebrochen (Strg-C)", ExitCode.INTERRUPTED)
    except Exception as exc:  # nichts darf als „teilweise“ (Exit 1) enden
        logging.getLogger(__name__).exception("Unerwarteter Fehler")
        _fail(f"Interner Fehler: {type(exc).__name__}: {exc}", ExitCode.INTERNAL)
    s = outcome.report["summary"]
    typer.echo(
        f"Lauf {outcome.report['run']['status']}: {s['ok']} ok, {s['failed']} fehlgeschlagen, {s['skipped']} nicht ersetzt, "
        f"{s['unchanged']} unverändert – neu {s['added']}, geändert {s['changed']}, gelöscht {s['deleted']}, Fehler {s['errors']}"
    )
    typer.echo(f"Bericht: {outcome.json_path} / {outcome.md_path}")
    raise typer.Exit(outcome.exit_code)


@app.command()
def crontab(
    config_dir: ConfigDir = None,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Datei schreiben statt ausgeben")] = None,
) -> None:
    """Crontab für supercronic aus dem Feld ``schedule`` der aktiven Quellen erzeugen."""
    from geoimport.schedule import render_crontab

    text = render_crontab(_load(config_dir).sources)
    if output is None:
        typer.echo(text, nl=False)
    else:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"Crontab geschrieben: {output}")


@app.command()
def sources(config_dir: ConfigDir = None) -> None:
    """Quellen mit Status des letzten Laufs."""
    from geoimport.db import connect
    from geoimport.meta.repository import MetaRepository

    cfg = _load(config_dir)
    last: dict[str, Any] = {}
    try:
        m = cfg.pipeline.meta
        with connect(None if m.location == "schema" else m.database, autocommit=True) as conn:
            repo = MetaRepository(conn, m.schema_name)
            if repo.exists():
                last = repo.last_results()
    except ConfigError as exc:
        typer.echo(f"(Lauf-Historie nicht lesbar: {exc})", err=True)
    for s in cfg.sources:
        r = last.get(s.id) or {}
        when = r.get("finished_at")
        status = f"{r.get('status', ''):<14} {when:%d.%m.%Y %H:%M}" if when else "noch kein Lauf"
        typer.echo(f"{'[x]' if s.enabled else '[ ]'} {s.id:<30} {s.type:<4} {s.schedule:<10} {status}")


@app.command()
def report(
    config_dir: ConfigDir = None,
    run_id: Annotated[str | None, typer.Option("--run", help="Lauf-ID (Standard: letzter Lauf)")] = None,
    fmt: Annotated[str, typer.Option("--format", help="md oder json")] = "md",
) -> None:
    """Bericht anzeigen (aus pipeline_meta, sonst reports/latest.*)."""
    from geoimport.db import connect
    from geoimport.meta.repository import MetaRepository
    from geoimport.report.render_md import render_markdown

    cfg = _load(config_dir)
    data = None
    try:
        m = cfg.pipeline.meta
        with connect(None if m.location == "schema" else m.database, autocommit=True) as conn:
            repo = MetaRepository(conn, m.schema_name)
            data = repo.report(run_id) if repo.exists() else None
    except ConfigError:
        pass
    if data is None and run_id is None:
        latest = cfg.pipeline.storage.report_dir / "latest.json"
        if latest.exists():
            data = json.loads(latest.read_text(encoding="utf-8"))
    if data is None:
        _fail("Kein Bericht gefunden", 1)
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) if fmt == "json" else render_markdown(data))
    sys.stdout.write("\n")


@app.command()
def probe(
    source: Annotated[str, typer.Option("--source", "-s", help="Quell-ID")],
    config_dir: ConfigDir = None,
) -> None:
    """Erreichbarkeit und Eckdaten einer Quelle prüfen (Capabilities bzw. HEAD), ohne zu importieren."""
    import shutil
    import tempfile

    from geoimport.http import HttpClient
    from geoimport.sources import source_class
    from geoimport.sources.base import SourceContext, SourceFailure, StateStore

    cfg = _load(config_dir)
    try:
        src = cfg.source(source)
    except ConfigError as exc:
        _fail(str(exc))
    http = HttpClient(cfg.pipeline.http)
    tmp = Path(tempfile.mkdtemp(prefix="geoimport-probe-"))
    ctx = SourceContext(cfg.pipeline, http, "probe", tmp, StateStore(tmp, src.id))
    try:
        info = source_class(src.type)(src, ctx).probe()
    except SourceFailure as exc:
        _fail(f"Quelle nicht erreichbar ({exc.kind}): {exc}", 1)
    finally:
        http.close()
        shutil.rmtree(tmp, ignore_errors=True)
    typer.echo(json.dumps(info, ensure_ascii=False, indent=2, default=str))


@app.command()
def demo(
    changes: Annotated[bool, typer.Option("--changes", help="Lauf mit gezielt veränderten Daten (SIMULATION)")] = False,
    reset: Annotated[bool, typer.Option("--reset", help="Stand aus den echten Rohdaten wiederherstellen")] = False,
    source: Annotated[list[str] | None, typer.Option("--source", "-s")] = None,
    config_dir: ConfigDir = None,
) -> None:
    """Demo-Modus – nur mit PIPELINE_ENV=demo, Adapter demo_reference und einer Datenbank *_demo."""
    from geoimport.demo import demo_options, purge_simulated, require_demo

    if changes == reset:
        _fail("Genau eine Option angeben: --changes oder --reset")
    cfg = _load(config_dir)
    try:
        require_demo(cfg)
        opts = demo_options(cfg, changes=changes, source_ids=source)
        if reset:
            purge_simulated(cfg, source)
    except ConfigError as exc:
        _fail_db(exc)
    _execute(cfg, opts)


if __name__ == "__main__":  # pragma: no cover
    app()
