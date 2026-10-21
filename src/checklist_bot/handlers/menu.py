"""Личка с ботом: /start, регистрация сотрудников и главное меню."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..access import all_admin_ids, is_admin, is_staff
from ..config import Config
from ..db import DB
from ..engine import Engine

BTN_SHIFT = "📅 Смена"
BTN_TODAY = "📋 Сегодня"
BTN_TASKS = "🗂 Задачи"
BTN_NEW_TASK = "➕ Новая задача"
BTN_STAFF = "👥 Сотрудники"

router = Router(name="menu")
router.message.filter(F.chat.type == "private")


class StaffOnly(Filter):
    """Пропускает только главных админов и старших смены."""

    async def __call__(self, event: Message | CallbackQuery, db: DB, cfg: Config) -> bool:
        return is_staff(db, cfg, event.from_user.id)


class AdminOnly(Filter):
    async def __call__(self, event: Message | CallbackQuery, db: DB, cfg: Config) -> bool:
        return is_admin(db, cfg, event.from_user.id)


def main_menu(admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SHIFT), KeyboardButton(text=BTN_TODAY)],
        [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_NEW_TASK)],
    ]
    if admin:
        rows.append([KeyboardButton(text=BTN_STAFF)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def show_menu(message: Message, db: DB, cfg: Config, intro: str = "Главное меню.") -> None:
    await message.answer(intro, reply_markup=main_menu(is_admin(db, cfg, message.from_user.id)))


@router.message(CommandStart())
async def start(
    message: Message, command: CommandObject, db: DB, cfg: Config, engine: Engine, state: FSMContext
) -> None:
    await state.clear()
    user = message.from_user

    # Первый вход главного админа по коду из установки.
    if command.args and cfg.setup_code and command.args.strip() == cfg.setup_code:
        db.add_admin(user.id)
        await show_menu(message, db, cfg, "✅ Вы назначены главным админом бота.\n\n" + HELP_ADMIN)
        return

    if is_admin(db, cfg, user.id):
        await show_menu(message, db, cfg, HELP_ADMIN)
        return

    emp, created = db.register_employee(user.id, user.full_name, user.username, engine.now())
    if emp["status"] == "active":
        if emp["is_lead"]:
            await show_menu(message, db, cfg, "Вы старший смены. " + HELP_LEAD)
        else:
            await message.answer(
                "Вы в списке сотрудников. Задачи приходят в общий чат; чтобы отметить выполнение, "
                "ответьте на сообщение задачи фото или текстом. Сюда буду присылать напоминания."
            )
        return
    if emp["status"] == "inactive":
        await message.answer("Ваш доступ отключён. Если это ошибка, обратитесь к администратору.")
        return

    await message.answer("Заявка отправлена администратору. Я напишу, когда вас добавят.")
    if created:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Сотрудник", callback_data=f"emp_approve:{emp['id']}:0")
        kb.button(text="⭐ Старший смены", callback_data=f"emp_approve:{emp['id']}:1")
        kb.button(text="✖️ Отклонить", callback_data=f"emp_reject:{emp['id']}")
        kb.adjust(2, 1)
        username = f" (@{escape(user.username)})" if user.username else ""
        for admin_id in all_admin_ids(db, cfg):
            await engine.send_private(
                admin_id,
                f"Новый сотрудник просит доступ: <b>{escape(user.full_name)}</b>{username}",
                reply_markup=kb.as_markup(),
            )


HELP_ADMIN = (
    "Что здесь есть:\n"
    f"{BTN_SHIFT}: отметить, кто сегодня (или завтра) на смене.\n"
    f"{BTN_TODAY}: задачи на сегодня, закрыть или отменить вручную.\n"
    f"{BTN_TASKS}: список задач, пауза и удаление.\n"
    f"{BTN_NEW_TASK}: создать задачу.\n"
    f"{BTN_STAFF}: сотрудники и старшие смены.\n\n"
    "Чтобы привязать общий чат, добавьте меня туда админом и отправьте в чате /bind."
)
HELP_LEAD = f"{BTN_SHIFT}, {BTN_TODAY}, {BTN_TASKS} и {BTN_NEW_TASK} доступны в меню ниже."


@router.message(F.text == "/cancel")
async def cancel_any(message: Message, state: FSMContext, db: DB, cfg: Config) -> None:
    await state.clear()
    if is_staff(db, cfg, message.from_user.id):
        await show_menu(message, db, cfg, "Отменено.")
