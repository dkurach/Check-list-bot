"""Тексты сообщений бота (HTML-разметка Telegram)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo


def mention(emp: sqlite3.Row) -> str:
    return f'<a href="tg://user?id={emp["tg_id"]}">{escape(emp["full_name"])}</a>'


def hhmm(value: str | datetime, tz: ZoneInfo) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(tz).strftime("%H:%M")


STATUS_ICONS = {
    "pending": "🕓",
    "overdue": "⏰",
    "done": "✅",
    "cancelled": "✖️",
    "missed": "💤",
}


def task_message(inst: sqlite3.Row, assignees: Sequence[sqlite3.Row], tz: ZoneInfo) -> str:
    lines = [f"📌 <b>{escape(inst['title'])}</b>"]
    if assignees:
        lines.append("👤 " + ", ".join(mention(emp) for emp in assignees))
    else:
        lines.append("👤 Исполнитель не назначен: на смене никого нет, закрыть может любой сотрудник")
    lines.append(f"⏳ Срок: до {hhmm(inst['due_at'], tz)}")

    status = inst["status"]
    if status == "done":
        lines.append("")
        lines.append(f"✅ Выполнено: {escape(inst['done_by_name'] or '')}, {hhmm(inst['done_at'], tz)}")
    elif status == "cancelled":
        lines.append("")
        lines.append("✖️ Задача отменена")
    else:
        if status == "overdue":
            lines.append("")
            lines.append("⏰ <b>Просрочено</b>")
        lines.append("")
        lines.append("<i>Чтобы отметить выполнение, ответьте на это сообщение фото или текстом.</i>")
    return "\n".join(lines)


def instance_line(inst: sqlite3.Row, tz: ZoneInfo) -> str:
    line = f"{STATUS_ICONS.get(inst['status'], '')} {hhmm(inst['scheduled_at'], tz)} {escape(inst['title'])}"
    if inst["status"] == "done":
        line += f" ({escape(inst['done_by_name'] or '')}, {hhmm(inst['done_at'], tz)})"
    elif inst["status"] == "missed":
        line += " (пропущено, бот был выключен)"
    return line
