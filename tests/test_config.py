"""Config loading: rejecting foreign config.yaml files.

Regression cover for v0.1.1, where a double-clicked GT7Coach.exe adopted
whatever ``config.yaml`` sat in its working directory — a Home Assistant
configuration, in the report — and died on HA's ``!include`` tag.
"""

from __future__ import annotations

import pytest

from gt7coach import config

# --- foreign config.yaml (regression: v0.1.1 crashed on Home Assistant's) ---

HA_CONFIG = """\
default_config:

homeassistant:
  name: Home
  latitude: 47.35
  longitude: 19.09

automation: !include automations.yaml
script: !include scripts.yaml
"""


def test_foreign_yaml_tag_does_not_crash_discovery(tmp_path):
    """Home Assistant's !include tag is unknown to safe_load. Auto-discovery
    must answer 'not mine', not raise."""
    p = tmp_path / "config.yaml"
    p.write_text(HA_CONFIG, encoding="utf-8")
    assert config.looks_like_gt7_config(p) is False


def test_explicit_foreign_config_raises_clean_error(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(HA_CONFIG, encoding="utf-8")
    with pytest.raises(config.ConfigError) as exc:
        config.load(p)
    assert "not valid YAML" in str(exc.value)


def test_valid_yaml_from_another_program_is_rejected(tmp_path):
    """Parses fine, but has none of our sections — e.g. a Compose file."""
    p = tmp_path / "config.yaml"
    p.write_text("services:\n  web:\n    image: nginx\n", encoding="utf-8")
    assert config.looks_like_gt7_config(p) is False
    with pytest.raises(config.ConfigError) as exc:
        config.load(p)
    assert "another program" in str(exc.value)


def test_our_own_config_is_recognised(tmp_path):
    p = tmp_path / "config.yaml"
    config.save(config.default_config(), p)
    assert config.looks_like_gt7_config(p) is True
    assert config.load(p).voice.speed == config.default_config().voice.speed


def test_partial_config_with_one_known_section_is_ours(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("voice:\n  speed: 180\n", encoding="utf-8")
    assert config.looks_like_gt7_config(p) is True
    assert config.load(p).voice.speed == 180


def test_empty_and_nonmapping_configs(tmp_path):
    empty = tmp_path / "config.yaml"
    empty.write_text("", encoding="utf-8")
    assert config.load(empty).voice.speed == config.default_config().voice.speed

    listy = tmp_path / "list.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(listy)


def test_main_ignores_foreign_cwd_config(tmp_path, monkeypatch, capsys):
    """The actual v0.1.1 failure: double-clicked exe, cwd had HA's config."""
    (tmp_path / "config.yaml").write_text(HA_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from gt7coach.main import main as coach_main

    # main() owns the root logger and drops pre-existing handlers, so caplog
    # can't see it — read the stderr handler it installs instead.
    rc = coach_main(["--source", "does-not-exist.csv", "--voice", "null"])
    err = capsys.readouterr().err
    assert "doesn't look like a gt7coach config" in err
    # It must fail on the missing key/source, not on the foreign config.
    assert rc != 0
    assert "ConstructorError" not in err


def test_lap_announce_defaults_to_single_utterance():
    """'both' spoke a best-lap callout AND an AI line every lap, which reads
    as a double announcement. Default to one; 'both' stays available."""
    assert config.default_config().session.lap_announce_mode == "recommendation"


def test_lap_announce_mode_still_configurable(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("session:\n  lap_announce_mode: both\n", encoding="utf-8")
    assert config.load(p).session.lap_announce_mode == "both"
