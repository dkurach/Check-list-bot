"""Автообновление из релизов GitHub.

Только стандартная библиотека: модуль работает до загрузки aiogram, чтобы `uv sync`
мог заменять зависимости, которые иначе были бы заняты запущенным процессом (Windows).

Порядок:
1. Работающий бот находит новый релиз, скачивает архив и распаковывает его в data/update/staged.
2. Бот перезапускается. Новый процесс до старта применяет подготовленную версию:
   сохраняет текущие файлы в data/update/previous, кладёт новые, выполняет `uv sync`.
3. Если что-то пошло не так, прежние файлы возвращаются, бот работает на старой версии.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__

DEFAULT_REPO = "dkurach/check-list-bot"
# То, что принадлежит конкретной установке, при обновлении не трогаем.
PRESERVED = {".env", "data", ".venv", ".git"}


class UpdateError(Exception):
    pass


@dataclass
class Release:
    tag: str
    zip_url: str


def parse_version(tag: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", tag)
    if not numbers:
        raise UpdateError(f"Не понимаю версию «{tag}»")
    return tuple(int(n) for n in numbers)


def update_dir(data_dir: Path) -> Path:
    return data_dir / "update"


def _state_path(data_dir: Path) -> Path:
    return update_dir(data_dir) / "state.json"


def load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, **changes) -> None:
    state = load_state(data_dir) | changes
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def current_version(data_dir: Path) -> str:
    """Тег установленного релиза, а если бот ставили не через автообновление, то версия из кода."""
    return load_state(data_dir).get("installed") or f"v{__version__}"


def is_newer(tag: str, current: str) -> bool:
    return parse_version(tag) > parse_version(current)


def _request(url: str, token: str | None) -> urllib.request.Request:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "checklist-bot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def check_access(repo: str, token: str | None, timeout: float = 15) -> bool:
    """Видно ли репозиторий с этим токеном (без токена - только публичный)."""
    try:
        with urllib.request.urlopen(_request(f"https://api.github.com/repos/{repo}", token), timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return False
        raise


def fetch_latest(repo: str, token: str | None, timeout: float = 30) -> Release | None:
    """Последний опубликованный релиз или None, если релизов нет."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with urllib.request.urlopen(_request(url, token), timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Нет релизов, либо репозиторий приватный и токен не подходит.
            return None
        raise UpdateError(f"GitHub ответил {exc.code}") from exc
    return Release(tag=data["tag_name"], zip_url=data["zipball_url"])


def available_update(home: Path, repo: str, token: str | None) -> Release | None:
    """Релиз, на который стоит обновиться, или None."""
    data_dir = home / "data"
    release = fetch_latest(repo, token)
    if release is None or not is_newer(release.tag, current_version(data_dir)):
        return None
    if load_state(data_dir).get("failed") == release.tag:
        return None  # уже пробовали и откатились, ждём следующий релиз
    return release


def staged_tag(data_dir: Path) -> str | None:
    marker = update_dir(data_dir) / "staged.json"
    try:
        return json.loads(marker.read_text(encoding="utf-8"))["tag"]
    except (OSError, ValueError, KeyError):
        return None


def stage_archive(archive: Path, tag: str, data_dir: Path) -> Path:
    """Распаковывает архив релиза в data/update/staged."""
    staged = update_dir(data_dir) / "staged"
    shutil.rmtree(staged, ignore_errors=True)
    (update_dir(data_dir) / "staged.json").unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=update_dir(data_dir)) as tmp:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        # Архив GitHub содержит одну папку верхнего уровня вида owner-repo-sha.
        root = roots[0] if len(roots) == 1 else Path(tmp)
        if not (root / "pyproject.toml").exists() or not (root / "src" / "checklist_bot").is_dir():
            raise UpdateError("В архиве релиза нет кода бота")
        shutil.copytree(root, staged)
    (update_dir(data_dir) / "staged.json").write_text(json.dumps({"tag": tag}), encoding="utf-8")
    return staged


def download_and_stage(release: Release, data_dir: Path, token: str | None, timeout: float = 120) -> Path:
    update_dir(data_dir).mkdir(parents=True, exist_ok=True)
    archive = update_dir(data_dir) / "release.zip"
    with urllib.request.urlopen(_request(release.zip_url, token), timeout=timeout) as resp, open(archive, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    try:
        return stage_archive(archive, release.tag, data_dir)
    finally:
        archive.unlink(missing_ok=True)


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if os.name == "nt" else "uv"
    for candidate in (Path.home() / ".local" / "bin" / name, Path.home() / ".cargo" / "bin" / name):
        if candidate.exists():
            return str(candidate)
    return None


def uv_sync(home: Path) -> None:
    uv = find_uv()
    if uv is None:
        raise UpdateError("Не найден uv, нужен для установки зависимостей")
    result = subprocess.run([uv, "sync", "--no-dev", "--frozen"], cwd=home, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise UpdateError(f"uv sync завершился с ошибкой: {result.stderr.strip()[-500:]}")


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _replace_entries(source: Path, home: Path, names: list[str]) -> None:
    for name in names:
        _remove(home / name)
        if (source / name).exists():
            _copy(source / name, home / name)


def apply_staged(home: Path, sync: Callable[[Path], None] = uv_sync) -> str:
    """Ставит подготовленную версию. Возвращает её тег. При ошибке откатывает файлы и бросает UpdateError."""
    data_dir = home / "data"
    tag = staged_tag(data_dir)
    staged = update_dir(data_dir) / "staged"
    if tag is None or not staged.is_dir():
        raise UpdateError("Нет подготовленного обновления")

    new_names = sorted(p.name for p in staged.iterdir() if p.name not in PRESERVED)
    previous = update_dir(data_dir) / "previous"
    shutil.rmtree(previous, ignore_errors=True)
    previous.mkdir(parents=True)
    existed = [name for name in new_names if (home / name).exists()]
    for name in existed:
        _copy(home / name, previous / name)
    (previous / "manifest.json").write_text(
        json.dumps({"version": current_version(data_dir), "existed": existed, "replaced": new_names}),
        encoding="utf-8",
    )

    try:
        _replace_entries(staged, home, new_names)
        sync(home)
    except Exception as exc:
        try:
            restore_previous(home, sync)
        except Exception as restore_exc:  # noqa: BLE001 - сообщаем админу обе ошибки
            exc = UpdateError(f"{exc}; откат тоже не удался: {restore_exc}")
        save_state(data_dir, failed=tag, notice=f"⚠️ Не удалось обновиться до {tag}: {exc}. Работает прежняя версия.")
        _clear_staged(data_dir)
        raise UpdateError(str(exc)) from exc

    save_state(data_dir, installed=tag, failed=None, notice=f"✅ Бот обновлён до версии {tag}.")
    _clear_staged(data_dir)
    return tag


def restore_previous(home: Path, sync: Callable[[Path], None] = uv_sync) -> str:
    """Возвращает файлы версии, которая стояла до последнего обновления."""
    data_dir = home / "data"
    previous = update_dir(data_dir) / "previous"
    try:
        manifest = json.loads((previous / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError("Нет сохранённой предыдущей версии") from exc
    for name in manifest["replaced"]:
        _remove(home / name)
    for name in manifest["existed"]:
        _copy(previous / name, home / name)
    sync(home)
    save_state(data_dir, installed=manifest["version"])
    return manifest["version"]


def _clear_staged(data_dir: Path) -> None:
    shutil.rmtree(update_dir(data_dir) / "staged", ignore_errors=True)
    (update_dir(data_dir) / "staged.json").unlink(missing_ok=True)


def pop_notice(data_dir: Path) -> str | None:
    notice = load_state(data_dir).get("notice")
    if notice:
        save_state(data_dir, notice=None)
    return notice


def auto_update_allowed(home: Path) -> bool:
    """В рабочей копии git (у разработчика) автообновление выключено, чтобы не затереть код."""
    return not (home / ".git").exists()
