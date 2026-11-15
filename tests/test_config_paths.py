"""Configuration imports use an isolated project and fictional credentials only."""
import os
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


_SECRET = "test-only-config-paths-secret-key-with-at-least-32-characters"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    project = tmp_path / "cursor-project"
    storage = project / "storage"
    storage.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "storage" / "config.py"
    entry = storage / "config.py"
    entry.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    other_cwd = tmp_path / "different-working-directory"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    def load(values=None, *, environment=None, encoding="utf-8"):
        # Copy the module, not the real .env: importing it cannot read private
        # credentials or inherit real integration flags from the parent process.
        settings = dict(values or {})
        settings.setdefault("SECRET_KEY", _SECRET)
        (project / ".env").write_text(
            "\n".join(f"{key}={value}" for key, value in settings.items()) + "\n",
            encoding=encoding,
        )
        monkeypatch.setattr(os, "environ", dict(environment or {}))
        return SimpleNamespace(**runpy.run_path(str(entry)))

    return project, other_cwd, load


def test_relative_paths_use_project_and_accept_windows_utf8_bom(isolated_config):
    project, other_cwd, load = isolated_config
    config = load(
        {
            "GATEBOT_DATA_DIR": "./private-state",
            "GATEBOT_LOG_DIR": "./private-logs",
            "MAX_CA_BUNDLE": "./certificates/max.pem",
        },
        encoding="utf-8-sig",
    )
    assert config.ENV_FILE == project / ".env"
    assert Path(config.DATA_DIR) == project / "private-state"
    assert Path(config.LOG_DIR) == project / "private-logs"
    assert Path(config.MAX_CA_BUNDLE) == project / "certificates" / "max.pem"
    assert not (other_cwd / "private-state").exists()


def test_process_environment_takes_precedence_over_dotenv(isolated_config):
    project, _, load = isolated_config
    config = load(
        {
            "GATEBOT_DATA_DIR": "./from-dotenv",
            "GATEBOT_LOG_DIR": "./dotenv-logs",
            "MAX_CA_BUNDLE": "./dotenv.pem",
            "BOT_ENABLED": "true",
        },
        environment={
            "GATEBOT_DATA_DIR": "./from-process",
            "GATEBOT_LOG_DIR": "./process-logs",
            "MAX_CA_BUNDLE": "./process.pem",
            "BOT_ENABLED": "false",
            "SECRET_KEY": _SECRET + "-process",
        },
    )
    assert Path(config.DATA_DIR) == project / "from-process"
    assert Path(config.LOG_DIR) == project / "process-logs"
    assert Path(config.MAX_CA_BUNDLE) == project / "process.pem"
    assert config.BOT_ENABLED is False
    assert config.SECRET_KEY == _SECRET + "-process"


def test_absolute_paths_keep_their_locations(isolated_config, tmp_path):
    _, _, load = isolated_config
    state = tmp_path / "state-outside-project"
    logs = tmp_path / "logs-outside-project"
    certificate = tmp_path / "certificates-outside-project" / "max.pem"
    config = load(environment={
        "GATEBOT_DATA_DIR": str(state),
        "GATEBOT_LOG_DIR": str(logs),
        "MAX_CA_BUNDLE": str(certificate),
    })
    assert Path(config.DATA_DIR) == state
    assert Path(config.LOG_DIR) == logs
    assert Path(config.MAX_CA_BUNDLE) == certificate


@pytest.mark.parametrize("values", [{}, {"GATEBOT_DATA_DIR": "./custom-state"}])
def test_default_logs_follow_data_and_empty_ca_remains_unset(isolated_config, values):
    project, _, load = isolated_config
    config = load({**values, "MAX_CA_BUNDLE": ""})
    expected_state = project / ("custom-state" if values else "data")
    assert Path(config.DATA_DIR) == expected_state
    assert Path(config.LOG_DIR) == expected_state / "logs"
    assert config.MAX_CA_BUNDLE == ""
