from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from checklist_bot.config import Config
from checklist_bot.db import DB
from checklist_bot.engine import Engine

TZ = ZoneInfo("Europe/Moscow")


class FakeBot:
    """Записывает вызовы вместо обращения к Telegram."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.reactions: list[dict] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next_id, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.reactions.append({"chat_id": chat_id, "message_id": message_id})

    def to(self, chat_id):
        return [m for m in self.sent if m["chat_id"] == chat_id]


@dataclass
class Clock:
    value: datetime = field(default_factory=lambda: datetime(2026, 10, 20, 8, 0, tzinfo=TZ))

    def __call__(self):
        return self.value

    def set(self, hour, minute=0, day=20):
        self.value = datetime(2026, 10, day, hour, minute, tzinfo=TZ)


GROUP = -1001
ADMIN = 1


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(home=tmp_path, token="x", tz=TZ, admin_ids={ADMIN}, escalate_after_min=30, auto_update=False)


@pytest.fixture
def db() -> DB:
    database = DB(":memory:")
    database.migrate()
    database.set_setting("group_chat_id", str(GROUP))
    return database


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def engine(db, bot, cfg, clock) -> Engine:
    return Engine(db, bot, cfg, clock=clock)


def add_employee(db: DB, tg_id: int, name: str, lead: bool = False) -> int:
    emp, _ = db.register_employee(tg_id, name, None, datetime(2026, 10, 1, tzinfo=TZ))
    db.set_employee_status(emp["id"], "active")
    db.set_employee_lead(emp["id"], lead)
    return emp["id"]
