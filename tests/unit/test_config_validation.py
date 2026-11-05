from __future__ import annotations

from pathlib import Path

import pytest

from geoimport.config.loader import ConfigError, expand_env, load_config
from tests.conftest import CONFIG, load_yaml, write_yaml


def test_repo_config_is_valid() -> None:
    cfg = load_config(CONFIG)
    ids = [s.id for s in cfg.enabled_sources()]
    assert ids == ["afs_bb", "geobasis_bb_gemeinden", "inkar_gemeinden"]  # V10: 1 WFS + INKAR + AfS
    assert {s.id for s in cfg.sources if not s.enabled} == {"TODO_TH_wfs_quelle_2", "TODO_TH_wfs_quelle_3"}
    assert cfg.target.has_placeholders()  # TH-Modell noch nicht bekannt
    assert any("license" in w for w in cfg.warnings)
    assert cfg.config_hash.startswith("sha256:")


def test_demo_target_config_is_valid() -> None:
    cfg = load_config(CONFIG, target_file="target.demo.yaml")
    assert cfg.target.adapter == "demo_reference"
    assert not cfg.target.has_placeholders()


def test_example_source_validates(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    write_yaml(cfg / "target.yaml", load_yaml(CONFIG / "target.demo.yaml"))
    write_yaml(cfg / "sources" / "kreise.yaml", load_yaml(CONFIG / "examples" / "geobasis_bb_kreise.yaml"))
    assert load_config(cfg).source("geobasis_bb_kreise").wfs.type_name == "app:krs"


def _base(tmp_path: Path, source: dict) -> Path:
    cfg = tmp_path / "config"
    write_yaml(cfg / "target.yaml", load_yaml(CONFIG / "target.demo.yaml"))
    write_yaml(cfg / "sources" / "s.yaml", source)
    return cfg


def _wfs(**over: object) -> dict:
    src = load_yaml(CONFIG / "sources" / "geobasis_bb_gemeinden.yaml")
    src.update(over)
    return src


def test_placeholder_requires_disabled(tmp_path: Path) -> None:
    src = _wfs()
    src["wfs"]["url"] = "TODO_TH_URL"
    with pytest.raises(ConfigError, match="enabled: false"):
        load_config(_base(tmp_path, src))
    src["enabled"] = False
    load_config(_base(tmp_path, src))


def test_unknown_field_is_reported_in_german(tmp_path: Path) -> None:
    src = _wfs()
    src["wfs"]["typename"] = "app:gem"  # Tippfehler
    with pytest.raises(ConfigError) as exc:
        load_config(_base(tmp_path, src))
    msg = str(exc.value)
    assert "s.yaml" in msg and "wfs.typename" in msg and "unbekanntes Feld" in msg


def test_missing_required_field(tmp_path: Path) -> None:
    src = _wfs()
    del src["business_key"]
    with pytest.raises(ConfigError, match="business_key: Pflichtfeld fehlt"):
        load_config(_base(tmp_path, src))


def _inkar_src() -> dict:
    src = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    src["geometry_join"] = None
    return src


def test_business_key_muss_im_datensatz_vorkommen(tmp_path: Path) -> None:
    src = _inkar_src()
    src["business_key"] = ["region_key", "gibt_es_nicht"]
    with pytest.raises(ConfigError, match="business_key-Feld.*gibt_es_nicht.*kommen im Datensatz nicht vor"):
        load_config(_base(tmp_path, src))


def test_business_key_leer_doppelt_und_normalisierung(tmp_path: Path) -> None:
    for key, normalize, msg in (
        ([], {}, "business_key"),
        (["ags", "ags"], {}, "mehrfach"),
        (["ags"], {"krss": {"zfill": 5}}, "außerhalb von business_key"),
    ):
        src = _wfs()
        src["business_key"], src["key_normalize"] = key, normalize
        with pytest.raises(ConfigError, match=msg):
            load_config(_base(tmp_path, src))


def test_zusammengesetzter_business_key(tmp_path: Path) -> None:
    cfg = load_config(_base(tmp_path, _inkar_src()))
    key = cfg.source("inkar_gemeinden").key
    assert key.fields == ["region_key", "domain", "indicator_code", "indicator", "period"]


def test_invalid_id(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="id"):
        load_config(_base(tmp_path, _wfs(id="Gemeinden-BB")))


def test_duplicate_dataset(tmp_path: Path) -> None:
    cfg = _base(tmp_path, _wfs())
    write_yaml(cfg / "sources" / "t.yaml", _wfs(id="zweite_quelle"))
    with pytest.raises(ConfigError, match="denselben Datensatz"):
        load_config(cfg)


def test_geometry_join_must_reference_known_source(tmp_path: Path) -> None:
    csv = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    csv["geometry_join"]["source"] = "gibt_es_nicht"
    with pytest.raises(ConfigError, match="gibt_es_nicht"):
        load_config(_base(tmp_path, csv))


def test_csv_location_exactly_one(tmp_path: Path) -> None:
    csv = load_yaml(CONFIG / "sources" / "inkar_gemeinden.yaml")
    csv["csv"]["location"]["path"] = "/data/x.zip"
    csv["geometry_join"] = None
    with pytest.raises(ConfigError, match="genau eines"):
        load_config(_base(tmp_path, csv))


def test_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TOKEN", "geheim")
    missing: list[str] = []
    assert expand_env({"a": "k=${X_TOKEN}", "b": ["${NOPE:-std}"], "c": "${FEHLT}"}, missing) == {
        "a": "k=geheim",
        "b": ["std"],
        "c": "${FEHLT}",
    }
    assert missing == ["FEHLT"]


def test_missing_env_in_enabled_source(tmp_path: Path) -> None:
    src = _wfs()
    src["wfs"]["params"] = {"apikey": "${GEOIMPORT_TEST_KEY_FEHLT}"}
    with pytest.raises(ConfigError, match="GEOIMPORT_TEST_KEY_FEHLT"):
        load_config(_base(tmp_path, src))


def test_target_scope_requires_fk(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    target = load_yaml(CONFIG / "target.demo.yaml")
    del target["tables"]["geometries"]["fk_metadata"]
    write_yaml(cfg / "target.yaml", target)
    with pytest.raises(ConfigError, match="fk_metadata"):
        load_config(cfg)


def test_no_password_defaults_in_repo() -> None:
    for path in [*CONFIG.rglob("*.yaml"), ROOT_ENV]:
        text = path.read_text(encoding="utf-8").lower()
        for line in text.splitlines():
            if "password" in line and "=" in line and not line.strip().startswith("#"):
                assert line.split("=", 1)[1].split("#")[0].strip() == "", line


ROOT_ENV = Path(__file__).resolve().parents[2] / ".env.example"
