"""Прогон настоящих обработчиков aiogram через диспетчер с подменённой сетью."""

from __future__ import annotations

from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from checklist_bot.app import build_dispatcher
from checklist_bot.engine import Engine

from .conftest import ADMIN, GROUP, TZ, add_employee

BOT_ID = 999


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._next_id = 500

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            self._next_id += 1
            return Message(
                message_id=self._next_id,
                date=datetime.now(TZ),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def sent(self, chat_id=None):
        return [c for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]


@pytest.fixture
def env(db, cfg, clock):
    session = FakeSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    engine = Engine(db, bot, cfg, clock=clock)
    dp = build_dispatcher(db, cfg, engine)
    return dp, bot, session, engine


_update_id = 0


def user(tg_id, name="User"):
    return User(id=tg_id, is_bot=False, first_name=name)


def private_message(tg_id, text, name="User"):
    return Message(
        message_id=1, date=datetime.now(TZ), chat=Chat(id=tg_id, type="private"), from_user=user(tg_id, name), text=text
    )


def group_message(tg_id, text, reply_to_id=None, name="User"):
    reply = None
    if reply_to_id:
        reply = Message(
            message_id=reply_to_id,
            date=datetime.now(TZ),
            chat=Chat(id=GROUP, type="supergroup"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
            text="task",
        )
    return Message(
        message_id=2,
        date=datetime.now(TZ),
        chat=Chat(id=GROUP, type="supergroup"),
        from_user=user(tg_id, name),
        text=text,
        reply_to_message=reply,
    )


def callback(tg_id, data):
    msg = Message(
        message_id=3,
        date=datetime.now(TZ),
        chat=Chat(id=tg_id, type="private"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
        text="menu",
    )
    return CallbackQuery(id="cb", from_user=user(tg_id), chat_instance="ci", message=msg, data=data)


async def feed(dp, bot, **kwargs):
    global _update_id
    _update_id += 1
    await dp.feed_update(bot, Update(update_id=_update_id, **kwargs))


async def test_employee_signup_and_approval(env, db):
    dp, bot, session, _ = env
    await feed(dp, bot, message=private_message(10, "/start", "Иван"))
    emp = db.get_employee_by_tg(10)
    assert emp["status"] == "pending"
    admin_note = session.sent(ADMIN)[-1]
    assert "Иван" in admin_note.text and admin_note.reply_markup is not None

    await feed(dp, bot, callback_query=callback(ADMIN, f"emp_approve:{emp['id']}:1"))
    emp = db.get_employee_by_tg(10)
    assert emp["status"] == "active" and emp["is_lead"] == 1
    assert "добавили" in session.sent(10)[-1].text


async def test_non_staff_cannot_use_menu(env, db):
    dp, bot, session, _ = env
    add_employee(db, 10, "Иван")
    await feed(dp, bot, callback_query=callback(10, "tpl_list"))
    await feed(dp, bot, message=private_message(10, "🗂 Задачи"))
    assert session.sent(10) == []


async def test_setup_code_makes_admin(env, db, cfg):
    dp, bot, session, _ = env
    cfg.setup_code = "abc123"
    await feed(dp, bot, message=private_message(55, "/start abc123"))
    assert 55 in db.admin_ids()
    assert "главным админом" in session.sent(55)[-1].text


async def test_bind_and_reply_flow(env, db, clock):
    dp, bot, session, engine = env
    db.set_setting("group_chat_id", "")
    await feed(dp, bot, message=group_message(ADMIN, "/bind"))
    assert db.group_chat_id() == GROUP

    ivan = add_employee(db, 10, "Иван")
    db.toggle_shift("2026-10-20", ivan)
    db.add_template(
        datetime(2026, 10, 20, 7, tzinfo=TZ),
        title="Полить цветы",
        kind="times",
        times="09:00",
        weekdays="1234567",
        due_min=30,
    )
    clock.set(9, 0)
    await engine.tick()
    task_msg_id = session._next_id
    assert "Полить цветы" in session.sent(GROUP)[-1].text

    await feed(dp, bot, message=group_message(10, "готово", reply_to_id=task_msg_id, name="Иван"))
    inst = db.find_instance_by_message(GROUP, task_msg_id)
    assert inst["status"] == "done" and inst["done_text"] == "готово"


async def test_new_task_wizard(env, db):
    dp, bot, session, _ = env
    for text in ("➕ Новая задача", "Фото витрины"):
        await feed(dp, bot, message=private_message(ADMIN, text))
    await feed(dp, bot, callback_query=callback(ADMIN, "nt_as:0"))
    await feed(dp, bot, callback_query=callback(ADMIN, "nt_kind:times"))
    await feed(dp, bot, message=private_message(ADMIN, "18:00, 9:00"))
    await feed(dp, bot, callback_query=callback(ADMIN, "nt_days:12345"))
    await feed(dp, bot, message=private_message(ADMIN, "45"))
    await feed(dp, bot, callback_query=callback(ADMIN, "nt_save"))
    [tpl] = db.list_templates()
    assert (tpl["title"], tpl["times"], tpl["weekdays"], tpl["due_min"]) == (
        "Фото витрины",
        "09:00, 18:00",
        "12345",
        45,
    )
    assert tpl["assignee_id"] is None


async def test_shift_toggle_via_menu(env, db):
    dp, bot, session, _ = env
    ivan = add_employee(db, 10, "Иван")
    await feed(dp, bot, message=private_message(ADMIN, "📅 Смена"))
    assert "Иван" in str(session.sent(ADMIN)[-1].reply_markup)
    await feed(dp, bot, callback_query=callback(ADMIN, f"shift:2026-10-20:{ivan}"))
    assert db.shift_employee_ids("2026-10-20") == {ivan}


def test_dispatcher_can_be_rebuilt(db, cfg, clock):
    # После сбоя бот пересоздаёт диспетчер в том же процессе.
    bot = Bot("42:TEST", session=FakeSession())
    engine = Engine(db, bot, cfg, clock=clock)
    build_dispatcher(db, cfg, engine)
    build_dispatcher(db, cfg, engine)
