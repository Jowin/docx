import json

import pytest

from extractor_service.config_store import ConfigStore
from extractor_service.errors import ConfigError


def test_everything_defaulted(config_root):
    cfg = ConfigStore(config_root).resolve()
    assert (cfg.client, cfg.usecase, cfg.version) == ("default", "invoice", "1.0.0")
    assert cfg.resolved_by == {"client": "default", "usecase": "default", "version": "default"}
    assert [s.name for s in cfg.skills] == ["field-extraction", "table-extraction"]
    assert cfg.dictionary.get("total_amount").critical


def test_client_only_takes_its_only_usecase_and_latest_version(config_root):
    cfg = ConfigStore(config_root).resolve(client="acme")
    assert (cfg.usecase, cfg.version) == ("ap-invoices", "1.1.0")
    assert cfg.resolved_by == {"client": "request", "usecase": "only", "version": "latest"}


def test_explicit_and_latest_versions(config_root):
    store = ConfigStore(config_root)
    assert store.resolve("acme", "ap-invoices", "1.0.0").version == "1.0.0"
    assert store.resolve("acme", "ap-invoices", "latest").version == "1.1.0"
    assert store.resolve("acme", "ap-invoices", "1.1.0").dictionary.get("payment_reference")


def test_semver_order_is_numeric(config_root):
    src = config_root / "acme" / "ap-invoices" / "1.1.0"
    import shutil
    shutil.copytree(src, src.parent / "1.10.0")
    shutil.copytree(src, src.parent / "1.9.0")
    assert ConfigStore(config_root).resolve("acme", "ap-invoices").version == "1.10.0"


def test_not_found_lists_what_exists(config_root):
    with pytest.raises(ConfigError) as e:
        ConfigStore(config_root).resolve("nobody", "invoice", "1.0.0")
    assert e.value.code == "config_not_found" and e.value.status == 404
    assert {c["client"] for c in e.value.detail["available"]} == {"acme", "default"}


@pytest.mark.parametrize("bad", ["../etc", "a/b", ".hidden", ""])
def test_folder_names_cannot_escape(config_root, bad):
    with pytest.raises(ConfigError) as e:
        ConfigStore(config_root).resolve(bad or "x y", "invoice", "1.0.0")
    assert e.value.code in ("config_invalid_name", "config_not_found")


def test_broken_schema_reported_at_startup(config_root):
    p = config_root / "acme" / "ap-invoices" / "1.0.0" / "schema.json"
    s = json.loads(p.read_text())
    s["fields"][0]["type"] = "money"
    p.write_text(json.dumps(s))
    problems = ConfigStore(config_root).validate_all()
    assert problems and problems[0]["config"] == "acme/ap-invoices/1.0.0"
    assert problems[0]["error"] == "schema_invalid"


def test_config_hash_changes_with_any_file(config_root):
    store = ConfigStore(config_root)
    before = store.resolve().sha256
    skill = config_root / "default" / "invoice" / "1.0.0" / "skills" / "field-extraction.md"
    skill.write_text(skill.read_text() + "\nOne more rule.\n")
    assert store.resolve().sha256 != before
