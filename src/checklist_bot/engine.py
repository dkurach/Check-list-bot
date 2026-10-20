"""Логика задач: публикация по расписанию, приём ответов, просрочки, бэкап."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ReactionTypeEmoji

from . import texts
from .access import all_admin_ids
from .config import Config
from .db import DB, OPEN_STATUSES, assignee_ids
from .schedule import occurrences

log = logging.getLogger(__name__)

TICK_SECONDS = 20
BACKUPS_TO_KEEP = 14

# Результаты обработки ответа на задачу
REPLY_NOT_TASK = "not_task"
REPLY_DONE = "done"
REPLY_ALREADY_CLOSED = "already_closed"
REPLY_NOT_EMPLOYEE = "not_employee"
REPLY_NOT_ASSIGNEE = "not_assignee"


class Engine:
    def __init__(self, db: DB, bot, cfg: Config, clock: Callable[[], datetime] | None = None):
        self.db = db
        self.bot = bot
        self.cfg = cfg
        self._clock = clock or (lambda: datetime.now(cfg.tz))
        self._last_backup_day: date | None = None

    def now(self) -> datetime:
        return self._clock()

    def today(self) -> str:
        return self.now().date().isoformat()

    # --- цикл ---

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в цикле задач")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        await self.publish_due()
        await self.check_deadlines()
        self.daily_backup()

    # --- публикация ---

    def resolve_assignees(self, tpl: sqlite3.Row, day: str) -> list[int]:
        if tpl["assignee_id"]:
            emp = self.db.get_employee(tpl["assignee_id"])
            if emp and emp["status"] == "active":
                return [emp["id"]]
        return [emp["id"] for emp in self.db.on_shift(day)]

    async def publish_due(self) -> None:
        chat_id = self.db.group_chat_id()
        if chat_id is None:
            return
        now = self.now()
        day = now.date()
        missed: list[sqlite3.Row] = []

        for tpl in self.db.list_templates(active_only=True):
            created_at = datetime.fromisoformat(tpl["created_at"])
            for at in occurrences(tpl, day, self.cfg.tz):
                # Не создаём задачи из будущего и из времени до появления шаблона.
                if at > now or at < created_at.replace(second=0, microsecond=0):
                    continue
                if self.db.instance_exists(tpl["id"], at):
                    continue
                due = at + timedelta(minutes=tpl["due_min"])
                if due <= now:
                    # Бот был выключен всё время, пока задача была актуальна.
                    inst_id = self.db.create_instance(tpl, at, due, [], status="missed")
                    missed.append(self.db.get_instance(inst_id))
                    continue
                inst_id = self.db.create_instance(tpl, at, due, self.resolve_assignees(tpl, day.isoformat()))
                inst = self.db.get_instance(inst_id)
                if not inst["assignees"] or inst["assignees"] == "[]":
                    await self.notify_admins(
                        f"⚠️ Задача «{escape(tpl['title'])}» опубликована без исполнителя: "
                        "на смене никого нет. Отметьте смену в меню «📅 Смена»."
                    )

        # Отправляем всё, что ещё не дошло до чата (в том числе после сбоя сети).
        for inst in self.db.open_instances():
            if inst["message_id"] is None:
                await self.send_instance(inst, chat_id)

        if missed:
            lines = [texts.instance_line(inst, self.cfg.tz) for inst in missed]
            await self.notify_admins("Пока бот был выключен, прошло время этих задач:\n" + "\n".join(lines))

    async def send_instance(self, inst: sqlite3.Row, chat_id: int) -> None:
        try:
            msg = await self.bot.send_message(chat_id=chat_id, text=self.render(inst))
        except TelegramAPIError:
            log.exception("Не удалось отправить задачу %s в чат", inst["id"])
            return
        self.db.update_instance(inst["id"], chat_id=chat_id, message_id=msg.message_id)

    def render(self, inst: sqlite3.Row) -> str:
        emps = [self.db.get_employee(emp_id) for emp_id in assignee_ids(inst)]
        return texts.task_message(inst, [emp for emp in emps if emp], self.cfg.tz)

    async def refresh_message(self, inst_id: int) -> None:
        inst = self.db.get_instance(inst_id)
        if inst is None or inst["message_id"] is None:
            return
        try:
            await self.bot.edit_message_text(
                text=self.render(inst), chat_id=inst["chat_id"], message_id=inst["message_id"]
            )
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                log.warning("Не удалось обновить сообщение задачи %s: %s", inst_id, exc)
        except TelegramAPIError:
            log.exception("Не удалось обновить сообщение задачи %s", inst_id)

    # --- выполнение ---

    async def handle_reply(
        self,
        chat_id: int,
        reply_to_message_id: int,
        tg_id: int,
        full_name: str,
        text: str | None,
        photo_file_id: str | None,
    ) -> str:
        inst = self.db.find_instance_by_message(chat_id, reply_to_message_id)
        if inst is None:
            return REPLY_NOT_TASK
        if inst["status"] not in OPEN_STATUSES:
            return REPLY_ALREADY_CLOSED
        emp = self.db.get_employee_by_tg(tg_id)
        if emp is None or emp["status"] != "active":
            return REPLY_NOT_EMPLOYEE
        assigned = assignee_ids(inst)
        if assigned and emp["id"] not in assigned:
            return REPLY_NOT_ASSIGNEE

        self.db.update_instance(
            inst["id"],
            status="done",
            done_by_tg=tg_id,
            done_by_name=emp["full_name"] or full_name,
            done_at=self.now().isoformat(),
            done_text=text,
            done_photo=photo_file_id,
        )
        await self.refresh_message(inst["id"])
        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id, message_id=reply_to_message_id, reaction=[ReactionTypeEmoji(emoji="👍")]
            )
        except TelegramAPIError:
            log.info("Не удалось поставить реакцию на задачу %s", inst["id"])
        return REPLY_DONE

    async def close_manually(self, inst_id: int, by_tg: int, by_name: str) -> bool:
        inst = self.db.get_instance(inst_id)
        if inst is None or inst["status"] not in OPEN_STATUSES:
            return False
        self.db.update_instance(
            inst_id,
            status="done",
            done_by_tg=by_tg,
            done_by_name=f"{by_name} (вручную)",
            done_at=self.now().isoformat(),
        )
        await self.refresh_message(inst_id)
        return True

    async def cancel(self, inst_id: int) -> bool:
        inst = self.db.get_instance(inst_id)
        if inst is None or inst["status"] not in OPEN_STATUSES:
            return False
        self.db.update_instance(inst_id, status="cancelled")
        await self.refresh_message(inst_id)
        return True

    # --- сроки ---

    async def check_deadlines(self) -> None:
        now = self.now()
        escalate_after = timedelta(minutes=self.cfg.escalate_after_min)
        for inst in self.db.open_instances():
            if inst["message_id"] is None:
                continue
            due = datetime.fromisoformat(inst["due_at"])
            if inst["status"] == "pending" and due <= now:
                self.db.update_instance(inst["id"], status="overdue", reminded=1)
                await self.refresh_message(inst["id"])
                await self.remind_assignees(self.db.get_instance(inst["id"]))
            elif inst["status"] == "overdue" and not inst["escalated"] and due + escalate_after <= now:
                self.db.update_instance(inst["id"], escalated=1)
                await self.escalate(inst)

    async def remind_assignees(self, inst: sqlite3.Row) -> None:
        text = (
            f"⏰ Срок задачи «{escape(inst['title'])}» вышел. "
            "Ответьте на сообщение задачи в общем чате фото или текстом, когда сделаете."
        )
        for emp_id in assignee_ids(inst):
            emp = self.db.get_employee(emp_id)
            if emp:
                await self.send_private(emp["tg_id"], text)

    async def escalate(self, inst: sqlite3.Row) -> None:
        names = [self.db.get_employee(emp_id) for emp_id in assignee_ids(inst)]
        who = ", ".join(escape(emp["full_name"]) for emp in names if emp) or "никто не назначен"
        text = (
            f"🚨 Задача «{escape(inst['title'])}» не выполнена "
            f"(срок был до {texts.hhmm(inst['due_at'], self.cfg.tz)}). Исполнитель: {who}."
        )
        for tg_id in self.escalation_recipients():
            await self.send_private(tg_id, text)

    def escalation_recipients(self) -> set[int]:
        """Главные админы и старшие, которые сегодня на смене (если таких нет, то все старшие)."""
        leads = [emp for emp in self.db.on_shift(self.today()) if emp["is_lead"]] or self.db.active_leads()
        return all_admin_ids(self.db, self.cfg) | {emp["tg_id"] for emp in leads}

    async def notify_admins(self, text: str) -> None:
        for tg_id in all_admin_ids(self.db, self.cfg):
            await self.send_private(tg_id, text)

    async def send_private(self, tg_id: int, text: str, **kwargs) -> None:
        try:
            await self.bot.send_message(chat_id=tg_id, text=text, **kwargs)
        except TelegramAPIError as exc:
            # Чаще всего человек ещё не нажал /start у бота.
            log.warning("Не удалось написать в личку %s: %s", tg_id, exc)

    # --- бэкап ---

    def daily_backup(self) -> None:
        today = self.now().date()
        if self._last_backup_day == today:
            return
        self._last_backup_day = today
        try:
            self.db.backup(self.cfg.backups_dir / f"bot-{today.isoformat()}.db")
            old = sorted(self.cfg.backups_dir.glob("bot-*.db"))[:-BACKUPS_TO_KEEP]
            for path in old:
                path.unlink()
        except OSError:
            log.exception("Не удалось сделать бэкап базы")
