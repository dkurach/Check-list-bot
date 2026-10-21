import json
import zipfile

import pytest

from checklist_bot import updater
from checklist_bot.engine import Engine

from .conftest import ADMIN


def make_release_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(f"dkurach-check-list-bot-abc123/{name}", content)
    return path


RELEASE_FILES = {
    "pyproject.toml": "new",
    "src/checklist_bot/__init__.py": "new code",
    "README.md": "new readme",
    "CHANGELOG.md": "added in release",
}


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "bot"
    (root / "src" / "checklist_bot").mkdir(parents=True)
    (root / "src" / "checklist_bot" / "__init__.py").write_text("old code")
    (root / "src" / "checklist_bot" / "stale.py").write_text("removed in release")
    (root / "pyproject.toml").write_text("old")
    (root / "README.md").write_text("old readme")
    (root / ".env").write_text("BOT_TOKEN=secret")
    (root / "data").mkdir()
    (root / "data" / "bot.db").write_text("db")
    return root


def stage(home, tmp_path, files=RELEASE_FILES, tag="v0.3.0"):
    archive = make_release_zip(tmp_path / "release.zip", files)
    updater.update_dir(home / "data").mkdir(parents=True, exist_ok=True)
    return updater.stage_archive(archive, tag, home / "data")


def test_versions():
    assert updater.is_newer("v0.10.0", "v0.9.1")
    assert not updater.is_newer("v0.2.0", "0.2.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("latest")


def test_apply_replaces_code_and_keeps_data(home, tmp_path):
    stage(home, tmp_path)
    assert updater.staged_tag(home / "data") == "v0.3.0"
    synced = []
    assert updater.apply_staged(home, sync=synced.append) == "v0.3.0"

    assert synced == [home]
    assert (home / "src" / "checklist_bot" / "__init__.py").read_text() == "new code"
    assert not (home / "src" / "checklist_bot" / "stale.py").exists()
    assert (home / "CHANGELOG.md").exists()
    assert (home / ".env").read_text() == "BOT_TOKEN=secret"
    assert (home / "data" / "bot.db").read_text() == "db"
    assert updater.current_version(home / "data") == "v0.3.0"
    assert updater.staged_tag(home / "data") is None
    assert "v0.3.0" in updater.pop_notice(home / "data")
    assert updater.pop_notice(home / "data") is None

    # Откат возвращает прежние файлы и убирает добавленные релизом.
    assert updater.restore_previous(home, sync=lambda h: None) == f"v{updater.__version__}"
    assert (home / "src" / "checklist_bot" / "__init__.py").read_text() == "old code"
    assert (home / "src" / "checklist_bot" / "stale.py").exists()
    assert not (home / "CHANGELOG.md").exists()


def test_failed_sync_rolls_back(home, tmp_path):
    stage(home, tmp_path)

    def broken_sync(path):
        if (path / "pyproject.toml").read_text() == "new":
            raise updater.UpdateError("нет сети")

    with pytest.raises(updater.UpdateError):
        updater.apply_staged(home, sync=broken_sync)
    assert (home / "pyproject.toml").read_text() == "old"
    assert (home / "src" / "checklist_bot" / "__init__.py").read_text() == "old code"
    state = updater.load_state(home / "data")
    assert state["failed"] == "v0.3.0"
    assert "Не удалось" in state["notice"]


def test_archive_without_bot_is_rejected(home, tmp_path):
    with pytest.raises(updater.UpdateError):
        stage(home, tmp_path, files={"README.md": "x"})
    assert updater.staged_tag(home / "data") is None


def test_available_update(home, monkeypatch):
    release = updater.Release(tag="v9.0.0", zip_url="https://example/zip")
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, token: release)
    assert updater.available_update(home, "o/r", None) == release
    updater.save_state(home / "data", failed="v9.0.0")
    assert updater.available_update(home, "o/r", None) is None
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, token: updater.Release("v0.0.1", "u"))
    assert updater.available_update(home, "o/r", None) is None
    monkeypatch.setattr(updater, "fetch_latest", lambda repo, token: None)
    assert updater.available_update(home, "o/r", None) is None


def test_auto_update_skipped_in_git_checkout(tmp_path):
    assert updater.auto_update_allowed(tmp_path)
    (tmp_path / ".git").mkdir()
    assert not updater.auto_update_allowed(tmp_path)


async def test_engine_checks_once_a_day_and_requests_restart(db, bot, cfg, clock, monkeypatch):
    cfg.auto_update = True
    checks, staged, restarts = [], [], []
    release = updater.Release(tag="v9.0.0", zip_url="u")

    def fake_available(home, repo, token):
        checks.append(repo)
        return release

    monkeypatch.setattr(updater, "available_update", fake_available)
    monkeypatch.setattr(updater, "download_and_stage", lambda rel, data_dir, token: staged.append(rel.tag))
    engine = Engine(db, bot, cfg, clock=clock)

    async def on_restart():
        restarts.append(True)

    engine.on_restart = on_restart
    await engine.tick()
    await engine.tick()
    assert checks == [cfg.update_repo]
    assert staged == ["v9.0.0"] and restarts == [True]
    assert engine.restart_requested


async def test_engine_sends_update_notice_once(db, bot, cfg, clock):
    updater.save_state(cfg.data_dir, notice="✅ Бот обновлён до версии v9.0.0.")
    engine = Engine(db, bot, cfg, clock=clock)
    await engine.tick()
    await engine.tick()
    notes = [m for m in bot.to(ADMIN) if "обновлён" in m["text"]]
    assert len(notes) == 1
    assert json.loads((cfg.data_dir / "update" / "state.json").read_text())["notice"] is None
