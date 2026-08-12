"""WATCH_DETAIL resolution and frame_cap mapping."""
from __future__ import annotations

import config


def test_default_detail_is_balanced(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_DETAIL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "balanced"


def test_env_overrides_detail(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_DETAIL", "efficient")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "efficient"


def test_invalid_detail_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_DETAIL", "bogus")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    assert config.get_config()["detail"] == "balanced"


def test_get_config_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("WATCH_DETAIL", raising=False)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "missing.env")
    cfg = config.get_config()
    assert set(cfg) == {"detail", "config_file"}


def test_frame_cap_mapping():
    assert config.frame_cap("efficient") == 50
    assert config.frame_cap("balanced") == 100
    assert config.frame_cap("token-burner") is None
    assert config.frame_cap("transcript") is None
    assert config.frame_cap("anything-else") == 100


# --- read_setting: one resolution order for every consumer ----------------------


def test_read_setting_env_beats_config_beats_cwd(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text("K=from-config\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("K=from-cwd\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)

    monkeypatch.setenv("K", "from-env")
    assert config.read_setting("K") == "from-env"
    monkeypatch.delenv("K")
    assert config.read_setting("K") == "from-config"
    (cfg / ".env").write_text("OTHER=x\n", encoding="utf-8")
    assert config.read_setting("K") == "from-cwd"
    assert config.read_setting("K", include_cwd=False) is None


def test_export_prefixed_lines_parse(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'export GROQ_API_KEY=sk-abc\nexport\tWATCH_DETAIL=efficient\nVALUE="export inside"\n',
        encoding="utf-8",
    )
    values = config.read_env_file(env)
    assert values["GROQ_API_KEY"] == "sk-abc"
    assert values["WATCH_DETAIL"] == "efficient"
    assert values["VALUE"] == "export inside"
