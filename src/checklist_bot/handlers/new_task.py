"""Пошаговое создание задачи в личке с ботом."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import DB
from ..engine import Engine
from ..schedule import (
    ALL_DAYS,
    KIND_COUNT,
    KIND_INTERVAL,
    KIND_ONCE,
    KIND_TIMES,
    ScheduleError,
    describe,
    fmt_hhmm,
    parse_hhmm,
    parse_times,
    parse_weekdays,
    parse_window,
)
from .menu import BTN_NEW_TASK, StaffOnly

router = Router(name="new_task")
router.message.filter(F.chat.type == "private", StaffOnly())
router.callback_query.filter(StaffOnly())


class NewTask(StatesGroup):
    title = State()
    assignee = State()
    kind = State()
    params = State()
    weekdays = State()
    due = State()
    confirm = State()


KIND_PROMPTS = {
    KIND_ONCE: "Во сколько опубликовать? Например: 15:30.\nЕсли это время сегодня уже прошло, задача выйдет завтра.",
    KIND_TIMES: "Перечислите время публикаций через запятую. Например: 09:00, 13:00, 18:00",
    KIND_INTERVAL: "Интервал в минутах и окно дня. Например: <code>120 10:00-20:00</code> (каждые 2 часа)",
    KIND_COUNT: "Сколько раз и в каком окне, равномерно. Например: <code>4 09:00-21:00</code>",
}


def cancel_button(kb: InlineKeyboardBuilder) -> InlineKeyboardBuilder:
    kb.button(text="Отмена", callback_data="nt_cancel")
    return kb


@router.message(F.text == BTN_NEW_TASK)
async def new_task_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewTask.title)
    await message.answer(
        "Как назвать задачу? Этот текст увидят в чате. Например: «Фото витрины после выкладки».\n"
        "Отменить можно командой /cancel.",
    )


@router.callback_query(F.data == "nt_cancel")
async def new_task_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Создание задачи отменено.")
    await callback.answer()


@router.message(NewTask.title, F.text)
async def got_title(message: Message, state: FSMContext, db: DB) -> None:
    title = message.text.strip()
    if not title or title.startswith("/"):
        await message.answer("Напишите название задачи текстом.")
        return
    await state.update_data(title=title[:200])
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Кто на смене", callback_data="nt_as:0")
    for emp in db.list_employees("active"):
        kb.button(text=emp["full_name"], callback_data=f"nt_as:{emp['id']}")
    cancel_button(kb).adjust(1)
    await state.set_state(NewTask.assignee)
    await message.answer(
        "Кому назначать?\n«Кто на смене» значит всем, кто отмечен в смене на момент публикации; "
        "закроет первый ответивший.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewTask.assignee, F.data.startswith("nt_as:"))
async def got_assignee(callback: CallbackQuery, state: FSMContext) -> None:
    emp_id = int(callback.data.split(":")[1])
    await state.update_data(assignee_id=emp_id or None)
    kb = InlineKeyboardBuilder()
    kb.button(text="Один раз", callback_data=f"nt_kind:{KIND_ONCE}")
    kb.button(text="В заданное время", callback_data=f"nt_kind:{KIND_TIMES}")
    kb.button(text="Каждые N минут", callback_data=f"nt_kind:{KIND_INTERVAL}")
    kb.button(text="N раз за день", callback_data=f"nt_kind:{KIND_COUNT}")
    cancel_button(kb).adjust(2, 2, 1)
    await state.set_state(NewTask.kind)
    await callback.message.edit_text("Как часто публиковать?", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(NewTask.kind, F.data.startswith("nt_kind:"))
async def got_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":")[1]
    await state.update_data(kind=kind)
    await state.set_state(NewTask.params)
    await callback.message.edit_text(KIND_PROMPTS[kind])
    await callback.answer()


@router.message(NewTask.params, F.text)
async def got_params(message: Message, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    kind = data["kind"]
    try:
        if kind == KIND_ONCE:
            at = parse_hhmm(message.text)
            now = engine.now()
            once_at = datetime.combine(now.date(), at, tzinfo=engine.cfg.tz)
            if once_at <= now:
                once_at += timedelta(days=1)
            await state.update_data(once_at=once_at.isoformat())
        elif kind == KIND_TIMES:
            times = parse_times(message.text)
            await state.update_data(times=", ".join(fmt_hhmm(t) for t in times))
        else:
            number, start, end = parse_window(message.text)
            fields = {"start_time": fmt_hhmm(start), "end_time": fmt_hhmm(end)}
            fields["interval_min" if kind == KIND_INTERVAL else "count"] = number
            await state.update_data(**fields)
    except ScheduleError as exc:
        await message.answer(f"{exc}\nПопробуйте ещё раз или /cancel.")
        return

    if kind == KIND_ONCE:
        await state.update_data(weekdays=ALL_DAYS)
        await ask_due(message, state)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Каждый день", callback_data="nt_days:1234567")
    kb.button(text="Будни", callback_data="nt_days:12345")
    kb.button(text="Выходные", callback_data="nt_days:67")
    cancel_button(kb).adjust(3, 1)
    await state.set_state(NewTask.weekdays)
    await message.answer(
        "В какие дни? Выберите кнопкой или напишите цифрами (1 - понедельник), например: 1,3,5",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewTask.weekdays, F.data.startswith("nt_days:"))
async def got_weekdays_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(weekdays=callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=None)
    await ask_due(callback.message, state)
    await callback.answer()


@router.message(NewTask.weekdays, F.text)
async def got_weekdays_text(message: Message, state: FSMContext) -> None:
    try:
        weekdays = parse_weekdays(message.text)
    except ScheduleError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(weekdays=weekdays)
    await ask_due(message, state)


async def ask_due(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    for minutes, label in ((15, "15 мин"), (30, "30 мин"), (60, "1 час"), (120, "2 часа")):
        kb.button(text=label, callback_data=f"nt_due:{minutes}")
    cancel_button(kb).adjust(4, 1)
    await state.set_state(NewTask.due)
    await message.answer(
        "Сколько времени на выполнение после публикации? Кнопкой или числом минут.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewTask.due, F.data.startswith("nt_due:"))
async def got_due_button(callback: CallbackQuery, state: FSMContext, db: DB, engine: Engine) -> None:
    await state.update_data(due_min=int(callback.data.split(":")[1]))
    await callback.message.edit_reply_markup(reply_markup=None)
    await ask_confirm(callback.message, state, db, engine)
    await callback.answer()


@router.message(NewTask.due, F.text)
async def got_due_text(message: Message, state: FSMContext, db: DB, engine: Engine) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 24 * 60:
        await message.answer("Нужно число минут от 1 до 1440.")
        return
    await state.update_data(due_min=int(text))
    await ask_confirm(message, state, db, engine)


TEMPLATE_FIELDS = (
    "title",
    "assignee_id",
    "kind",
    "once_at",
    "times",
    "start_time",
    "end_time",
    "interval_min",
    "count",
    "weekdays",
    "due_min",
)


async def ask_confirm(message: Message, state: FSMContext, db: DB, engine: Engine) -> None:
    data = await state.get_data()
    tpl = {key: data.get(key) for key in TEMPLATE_FIELDS}
    if tpl["assignee_id"]:
        emp = db.get_employee(tpl["assignee_id"])
        who = escape(emp["full_name"]) if emp else "?"
    else:
        who = "кто на смене"
    kb = InlineKeyboardBuilder()
    kb.button(text="💾 Сохранить", callback_data="nt_save")
    cancel_button(kb).adjust(2)
    await state.set_state(NewTask.confirm)
    await message.answer(
        f"<b>{escape(tpl['title'])}</b>\n"
        f"Когда: {describe(tpl, engine.cfg.tz)}\n"
        f"Срок: {tpl['due_min']} мин после публикации\n"
        f"Исполнитель: {who}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NewTask.confirm, F.data == "nt_save")
async def save_task(callback: CallbackQuery, state: FSMContext, db: DB, engine: Engine) -> None:
    data = await state.get_data()
    fields = {key: data.get(key) for key in TEMPLATE_FIELDS}
    db.add_template(engine.now(), **fields)
    await state.clear()
    note = "" if db.group_chat_id() else "\n\n⚠️ Общий чат ещё не привязан: отправьте /bind в группе."
    await callback.message.edit_text(f"✅ Задача «{escape(fields['title'])}» сохранена.{note}")
    await callback.answer()
