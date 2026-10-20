"""Настройки бота из файла .env в папке установки."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ENV_FILE = ".env"


def read_env(path: Path) -> dict[str, str]:
    """Простейший парсер .env: KEY=VALUE, комментарии через #, кавычки снимаются."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_ids(value: str) -> set[int]:
    return {int(part) for part in value.replace(";", ",").split(",") if part.strip()}


@dataclass
class Config:
    home: Path
    token: str
    tz: ZoneInfo
    admin_ids: set[int] = field(default_factory=set)
    setup_code: str | None = None
    escalate_after_min: int = 30

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


class ConfigError(Exception):
    pass


def load_config(home: Path) -> Config:
    env = read_env(home / ENV_FILE)
    token = env.get("BOT_TOKEN", "")
    if not token:
        raise ConfigError(f"Нет BOT_TOKEN в {home / ENV_FILE}. Запустите установку (install).")
    return Config(
        home=home,
        token=token,
        tz=ZoneInfo(env.get("TIMEZONE", "Europe/Moscow")),
        admin_ids=parse_ids(env.get("ADMIN_IDS", "")),
        setup_code=env.get("SETUP_CODE") or None,
        escalate_after_min=int(env.get("ESCALATE_AFTER_MIN", "30")),
    )
