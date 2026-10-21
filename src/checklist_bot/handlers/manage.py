"""Меню управления: смена, задачи на сегодня, список задач, сотрудники."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts, updater
from ..config import Config
from ..db import DB, OPEN_STATUSES
from ..engine import Engine
from ..schedule import describe
from .menu import BTN_SHIFT, BTN_STAFF, BTN_TASKS, BTN_TODAY, BTN_UPDATE, AdminOnly, StaffOnly

router = Router(name="manage")
router.message.filter(F.chat.type == "private", StaffOnly())
router.callback_query.filter(StaffOnly())


def short(text: str, limit: int = 28) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- смена ---


def shift_view(db: DB, day: date, today: date) -> tuple[str, object]:
    day_key = day.isoformat()
    on_shift = db.shift_employee_ids(day_key)
    employees = db.list_employees("active")
    label = "сегодня" if day == today else "завтра"
    kb = InlineKeyboardBuilder()
    for emp in employees:
        mark = "✅" if emp["id"] in on_shift else "▫️"
        star = " ⭐" if emp["is_lead"] else ""
        kb.button(text=f"{mark} {emp['full_name']}{star}", callback_data=f"shift:{day_key}:{emp['id']}")
    other = today + timedelta(days=1) if day == today else today
    kb.button(text="➡️ Завтра" if day == today else "⬅️ Сегодня", callback_data=f"shift_day:{other.isoformat()}")
    kb.adjust(*([1] * len(employees)), 1)
    if employees:
        text = f"Смена на {label}, {day.strftime('%d.%m')}. Нажмите на сотрудника, чтобы отметить."
    else:
        text = "Пока нет активных сотрудников. Пусть напишут боту /start, а вы подтвердите заявки."
    return text, kb.as_markup()


@router.message(F.text == BTN_SHIFT)
async def shift_menu(message: Message, db: DB, engine: Engine) -> None:
    today = engine.business_date()
    text, markup = shift_view(db, today, today)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("shift_day:"))
async def shift_switch_day(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    day = date.fromisoformat(callback.data.split(":", 1)[1])
    text, markup = shift_view(db, day, engine.business_date())
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shift:"))
async def shift_toggle(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    _, day_key, emp_id = callback.data.split(":")
    db.toggle_shift(day_key, int(emp_id))
    text, markup = shift_view(db, date.fromisoformat(day_key), engine.business_date())
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --- задачи на сегодня ---


def today_view(db: DB, engine: Engine) -> tuple[str, object]:
    tz = engine.cfg.tz
    instances = db.list_instances(engine.today())
    # Просроченные со вчера и раньше тоже показываем, их ещё можно закрыть.
    older = [inst for inst in db.open_instances() if inst["day"] < engine.today()]
    items = older + instances
    if not items:
        return "На сегодня задач пока нет.", None
    lines = [texts.instance_line(inst, tz) for inst in items]
    done = sum(1 for inst in instances if inst["status"] == "done")
    header = f"Задачи на сегодня: выполнено {done} из {len(instances)}.\n"
    kb = InlineKeyboardBuilder()
    open_items = [inst for inst in items if inst["status"] in OPEN_STATUSES]
    for inst in open_items:
        title = f"✅ {texts.hhmm(inst['scheduled_at'], tz)} {short(inst['title'])}"
        kb.button(text=title, callback_data=f"inst_done:{inst['id']}")
        kb.button(text="✖️", callback_data=f"inst_cancel:{inst['id']}")
    kb.adjust(*([2] * len(open_items)))
    footer = "\n\nКнопки ниже закрывают открытые задачи вручную (✅) или отменяют их (✖️)." if open_items else ""
    return header + "\n".join(lines) + footer, kb.as_markup() if open_items else None


@router.message(F.text == BTN_TODAY)
async def today_menu(message: Message, db: DB, engine: Engine) -> None:
    text, markup = today_view(db, engine)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("inst_done:") | F.data.startswith("inst_cancel:"))
async def instance_action(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    action, inst_id = callback.data.split(":")
    if action == "inst_done":
        ok = await engine.close_manually(int(inst_id), callback.from_user.id, callback.from_user.full_name)
    else:
        ok = await engine.cancel(int(inst_id))
    text, markup = today_view(db, engine)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Готово" if ok else "Задача уже закрыта")


# --- список задач ---


def tasks_view(db: DB, engine: Engine) -> tuple[str, object]:
    templates = db.list_templates()
    if not templates:
        return "Задач пока нет. Создайте первую кнопкой «➕ Новая задача».", None
    lines = []
    kb = InlineKeyboardBuilder()
    for tpl in templates:
        if tpl["assignee_id"]:
            emp = db.get_employee(tpl["assignee_id"])
            who = escape(emp["full_name"]) if emp else "?"
        else:
            who = "кто на смене"
        pause = " <i>(на паузе)</i>" if not tpl["active"] else ""
        lines.append(
            f"<b>{escape(tpl['title'])}</b>{pause}\n   {describe(tpl, engine.cfg.tz)}; срок {tpl['due_min']} мин; {who}"
        )
        toggle = "▶️" if not tpl["active"] else "⏸"
        kb.button(text=f"{toggle} {short(tpl['title'])}", callback_data=f"tpl_toggle:{tpl['id']}")
        kb.button(text="🗑", callback_data=f"tpl_del:{tpl['id']}")
    kb.adjust(*([2] * len(templates)))
    hint = "\n\n⏸ ставит задачу на паузу, ▶️ возобновляет, 🗑 удаляет."
    return "\n".join(lines) + hint, kb.as_markup()


@router.message(F.text == BTN_TASKS)
async def tasks_menu(message: Message, db: DB, engine: Engine) -> None:
    text, markup = tasks_view(db, engine)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("tpl_toggle:"))
async def task_toggle(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    tpl = db.get_template(int(callback.data.split(":")[1]))
    if tpl:
        db.set_template_active(tpl["id"], not tpl["active"])
    text, markup = tasks_view(db, engine)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("tpl_del:"))
async def task_delete_ask(callback: CallbackQuery, db: DB) -> None:
    tpl = db.get_template(int(callback.data.split(":")[1]))
    if not tpl:
        await callback.answer("Задача не найдена")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"tpl_del_yes:{tpl['id']}")
    kb.button(text="Нет", callback_data="tpl_list")
    await callback.message.edit_text(
        f"Удалить задачу «{escape(tpl['title'])}»? История выполнения сохранится.", reply_markup=kb.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tpl_del_yes:") | (F.data == "tpl_list"))
async def task_delete_confirm(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    if callback.data.startswith("tpl_del_yes:"):
        db.archive_template(int(callback.data.split(":")[1]))
    text, markup = tasks_view(db, engine)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --- сотрудники (только главный админ) ---

STATUS_LABELS = {"pending": "ждёт подтверждения", "active": "активен", "inactive": "отключён"}


def staff_view(db: DB) -> tuple[str, object]:
    employees = db.list_employees()
    if not employees:
        return "Сотрудников пока нет. Попросите их написать боту /start в личку.", None
    kb = InlineKeyboardBuilder()
    for emp in employees:
        icon = {"pending": "🆕", "active": "✅", "inactive": "⛔"}[emp["status"]]
        star = " ⭐" if emp["is_lead"] else ""
        kb.button(text=f"{icon} {emp['full_name']}{star}", callback_data=f"emp:{emp['id']}")
    kb.adjust(1)
    return "Сотрудники (⭐ старший смены). Нажмите, чтобы изменить:", kb.as_markup()


def employee_card(emp) -> tuple[str, object]:
    username = f"@{escape(emp['username'])}" if emp["username"] else "без username"
    role = "старший смены" if emp["is_lead"] else "сотрудник"
    text = f"<b>{escape(emp['full_name'])}</b> ({username})\nСтатус: {STATUS_LABELS[emp['status']]}\nРоль: {role}"
    kb = InlineKeyboardBuilder()
    if emp["status"] == "active":
        kb.button(text="⛔ Отключить", callback_data=f"emp_set:{emp['id']}:inactive")
    else:
        kb.button(text="✅ Активировать", callback_data=f"emp_set:{emp['id']}:active")
    if emp["is_lead"]:
        kb.button(text="Снять старшего", callback_data=f"emp_lead:{emp['id']}:0")
    else:
        kb.button(text="⭐ Сделать старшим", callback_data=f"emp_lead:{emp['id']}:1")
    kb.button(text="⬅️ Назад", callback_data="emp_list")
    kb.adjust(2, 1)
    return text, kb.as_markup()


@router.message(F.text == BTN_STAFF, AdminOnly())
async def staff_menu(message: Message, db: DB) -> None:
    text, markup = staff_view(db)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "emp_list", AdminOnly())
async def staff_list(callback: CallbackQuery, db: DB) -> None:
    text, markup = staff_view(db)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("emp:"), AdminOnly())
async def staff_card(callback: CallbackQuery, db: DB) -> None:
    emp = db.get_employee(int(callback.data.split(":")[1]))
    text, markup = employee_card(emp)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("emp_set:") | F.data.startswith("emp_lead:"), AdminOnly())
async def staff_change(callback: CallbackQuery, db: DB, engine: Engine) -> None:
    action, emp_id, value = callback.data.split(":")
    emp_id = int(emp_id)
    before = db.get_employee(emp_id)
    if action == "emp_set":
        db.set_employee_status(emp_id, value)
    else:
        db.set_employee_lead(emp_id, value == "1")
    emp = db.get_employee(emp_id)
    if before["status"] != "active" and emp["status"] == "active":
        await notify_approved(engine, emp)
    text, markup = employee_card(emp)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def notify_approved(engine: Engine, emp) -> None:
    text = "✅ Вас добавили в список сотрудников. Задачи приходят в общий чат."
    if emp["is_lead"]:
        text += " Вы старший смены: нажмите /start, чтобы открыть меню."
    await engine.send_private(emp["tg_id"], text)


@router.callback_query(F.data.startswith("emp_approve:") | F.data.startswith("emp_reject:"), AdminOnly())
async def staff_approve(callback: CallbackQuery, db: DB, engine: Engine, cfg: Config) -> None:
    parts = callback.data.split(":")
    emp = db.get_employee(int(parts[1]))
    if emp is None:
        await callback.answer("Сотрудник не найден")
        return
    if emp["status"] != "pending":
        await callback.answer("Заявка уже обработана")
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    if parts[0] == "emp_approve":
        db.set_employee_status(emp["id"], "active")
        db.set_employee_lead(emp["id"], parts[2] == "1")
        emp = db.get_employee(emp["id"])
        await notify_approved(engine, emp)
        role = "старший смены" if emp["is_lead"] else "сотрудник"
        result = f"✅ {escape(emp['full_name'])} добавлен(а): {role}."
    else:
        db.set_employee_status(emp["id"], "inactive")
        result = f"✖️ Заявка {escape(emp['full_name'])} отклонена."
    await callback.message.edit_text(result)
    await callback.answer()


# --- обновление (только главный админ) ---


@router.message(F.text == BTN_UPDATE, AdminOnly())
async def update_menu(message: Message, engine: Engine) -> None:
    cfg = engine.cfg
    current = updater.current_version(cfg.data_dir)
    if not updater.auto_update_allowed(cfg.home):
        await message.answer(f"Версия: {current}.\nЭто рабочая копия git, обновляйте её через git pull.")
        return
    try:
        release = await asyncio.to_thread(updater.fetch_latest, cfg.update_repo, cfg.github_token)
    except (updater.UpdateError, OSError) as exc:
        await message.answer(f"Версия: {current}.\nНе удалось проверить обновления: {escape(str(exc))}")
        return
    if release is None:
        hint = "" if cfg.github_token else " Возможно, не указан токен GitHub: запустите установку ещё раз."
        await message.answer(f"Версия: {current}.\nРелизов на GitHub не найдено.{hint}")
        return
    if not updater.is_newer(release.tag, current):
        await message.answer(f"Версия: {current}. Это последняя версия.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Обновить до {release.tag}", callback_data=f"upd:{release.tag}")
    await message.answer(
        f"Версия: {current}.\nДоступна новая: <b>{escape(release.tag)}</b>.\n"
        "Бот перезапустится примерно за минуту, задачи при этом не потеряются.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("upd:"), AdminOnly())
async def update_now(callback: CallbackQuery, engine: Engine) -> None:
    cfg = engine.cfg
    await callback.message.edit_text("Скачиваю обновление…")
    await callback.answer()
    try:
        release = await asyncio.to_thread(updater.fetch_latest, cfg.update_repo, cfg.github_token)
        if release is None:
            raise updater.UpdateError("релиз не найден")
        await callback.message.edit_text(f"Устанавливаю {escape(release.tag)} и перезапускаюсь. Напишу, когда закончу.")
        await engine.install_update(release)
    except (updater.UpdateError, OSError) as exc:
        await callback.message.edit_text(f"Не удалось скачать обновление: {escape(str(exc))}")
