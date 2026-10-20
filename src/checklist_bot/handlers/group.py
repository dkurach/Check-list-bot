"""Общий чат: привязка группы и приём ответов на задачи."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from ..access import is_admin
from ..config import Config
from ..db import DB
from ..engine import REPLY_DONE, REPLY_NOT_ASSIGNEE, REPLY_NOT_EMPLOYEE, Engine

router = Router(name="group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(Command("bind"))
async def bind_group(message: Message, db: DB, cfg: Config) -> None:
    if not is_admin(db, cfg, message.from_user.id):
        await message.reply("Привязать чат может только главный админ бота.")
        return
    db.set_setting("group_chat_id", str(message.chat.id))
    await message.reply(
        "✅ Этот чат привязан. Задачи будут приходить сюда.\n"
        "Сотрудникам нужно один раз написать боту /start в личку: так бот сможет присылать напоминания."
    )


@router.message(F.migrate_to_chat_id)
async def group_migrated(message: Message, db: DB) -> None:
    # Telegram меняет id, когда группа становится супергруппой.
    if db.group_chat_id() == message.chat.id:
        db.set_setting("group_chat_id", str(message.migrate_to_chat_id))


@router.message(F.reply_to_message.from_user.is_bot)
async def task_reply(message: Message, engine: Engine) -> None:
    photo = message.photo[-1].file_id if message.photo else None
    if photo is None and message.document and (message.document.mime_type or "").startswith("image/"):
        photo = message.document.file_id
    result = await engine.handle_reply(
        chat_id=message.chat.id,
        reply_to_message_id=message.reply_to_message.message_id,
        tg_id=message.from_user.id,
        full_name=message.from_user.full_name,
        text=message.text or message.caption,
        photo_file_id=photo,
    )
    if result == REPLY_DONE:
        return
    if result == REPLY_NOT_EMPLOYEE:
        await message.reply("Вас ещё нет в списке сотрудников. Напишите боту /start в личку.")
    elif result == REPLY_NOT_ASSIGNEE:
        await message.reply("Эта задача назначена другому сотруднику.")
