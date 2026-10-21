"""Запуск бота: Telegram-поллинг и цикл задач в одном процессе."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, ErrorEvent

from .config import Config
from .db import DB
from .engine import Engine
from .handlers import group, manage, menu, new_task

log = logging.getLogger(__name__)


def setup_logging(cfg: Config) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(cfg.logs_dir / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    ]
    if sys.stderr is not None:  # у pythonw.exe на Windows консоли нет
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def keep_awake() -> None:
    """На Windows не даём системе уснуть, пока бот работает (экран при этом гаснуть может)."""
    if os.name != "nt":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)


def build_dispatcher(db: DB, cfg: Config, engine: Engine) -> Dispatcher:
    dp = Dispatcher()
    dp["db"] = db
    dp["cfg"] = cfg
    dp["engine"] = engine
    routers = (group.router, menu.router, manage.router, new_task.router)
    for router in routers:
        # Роутеры живут на уровне модулей; при перезапуске после сбоя отвязываем их от старого диспетчера.
        router._parent_router = None
    dp.include_routers(*routers)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        if isinstance(event.exception, TelegramBadRequest) and "not modified" in str(event.exception):
            if event.update.callback_query:
                await event.update.callback_query.answer()
            return True
        log.exception("Ошибка при обработке обновления", exc_info=event.exception)
        return True

    return dp


async def run(cfg: Config) -> None:
    db = DB(cfg.db_path)
    db.migrate()
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode="HTML"))
    engine = Engine(db, bot, cfg)
    dp = build_dispatcher(db, cfg, engine)

    me = await bot.get_me()
    log.info("Бот @%s запущен, данные в %s", me.username, cfg.data_dir)
    await bot.set_my_commands(
        [BotCommand(command="start", description="Меню"), BotCommand(command="cancel", description="Отмена")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    keep_awake()
    loop_task = asyncio.create_task(engine.run_forever())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        loop_task.cancel()
        await bot.session.close()
        db.close()
