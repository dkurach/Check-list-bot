"""Журнал работы бота (только стандартная библиотека: нужен ещё до загрузки aiogram)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Config


def setup_logging(cfg: Config) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(cfg.logs_dir / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    ]
    if sys.stderr is not None:  # у pythonw.exe на Windows консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True
    )
