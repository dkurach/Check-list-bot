"""Хранилище на SQLite. Все времена хранятся строками ISO с часовым поясом."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Каждый элемент списка - одна миграция. Новые добавлять только в конец.
MIGRATIONS = [
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE employees (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        username TEXT,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending / active / inactive
        is_lead INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE shifts (
        day TEXT NOT NULL,
        employee_id INTEGER NOT NULL REFERENCES employees(id),
        PRIMARY KEY (day, employee_id)
    );
    CREATE TABLE templates (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        assignee_id INTEGER REFERENCES employees(id),  -- NULL = все, кто на смене
        kind TEXT NOT NULL,                             -- once / times / interval / count
        once_at TEXT,
        times TEXT,
        start_time TEXT,
        end_time TEXT,
        interval_min INTEGER,
        count INTEGER,
        weekdays TEXT NOT NULL DEFAULT '1234567',
        due_min INTEGER NOT NULL DEFAULT 30,
        active INTEGER NOT NULL DEFAULT 1,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE instances (
        id INTEGER PRIMARY KEY,
        template_id INTEGER NOT NULL REFERENCES templates(id),
        day TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        due_at TEXT NOT NULL,
        title TEXT NOT NULL,
        assignees TEXT NOT NULL DEFAULT '[]',
        chat_id INTEGER,
        message_id INTEGER,
        status TEXT NOT NULL,          -- pending / overdue / done / cancelled / missed
        reminded INTEGER NOT NULL DEFAULT 0,
        escalated INTEGER NOT NULL DEFAULT 0,
        done_by_tg INTEGER,
        done_by_name TEXT,
        done_at TEXT,
        done_text TEXT,
        done_photo TEXT,
        UNIQUE (template_id, scheduled_at)
    );
    CREATE INDEX ix_instances_message ON instances (chat_id, message_id);
    CREATE INDEX ix_instances_day ON instances (day);
    CREATE INDEX ix_instances_status ON instances (status);
    """,
]

OPEN_STATUSES = ("pending", "overdue")


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            self.conn.executescript(MIGRATIONS[index])
            self.conn.execute(f"PRAGMA user_version = {index + 1}")
        self.conn.commit()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    # --- настройки ---

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def group_chat_id(self) -> int | None:
        value = self.get_setting("group_chat_id")
        return int(value) if value else None

    def admin_ids(self) -> set[int]:
        return set(json.loads(self.get_setting("admin_ids") or "[]"))

    def add_admin(self, tg_id: int) -> None:
        ids = self.admin_ids() | {tg_id}
        self.set_setting("admin_ids", json.dumps(sorted(ids)))

    # --- сотрудники ---

    def register_employee(
        self, tg_id: int, full_name: str, username: str | None, now: datetime
    ) -> tuple[sqlite3.Row, bool]:
        """Создаёт заявку сотрудника или обновляет имя. Возвращает (сотрудник, создан_ли_новый)."""
        existing = self.get_employee_by_tg(tg_id)
        if existing:
            self._write(
                "UPDATE employees SET full_name = ?, username = ? WHERE id = ?",
                (full_name, username, existing["id"]),
            )
            return self.get_employee(existing["id"]), False
        emp_id = self._write(
            "INSERT INTO employees (tg_id, full_name, username, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, full_name, username, now.isoformat()),
        )
        return self.get_employee(emp_id), True

    def get_employee(self, emp_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM employees WHERE id = ?", (emp_id,))

    def get_employee_by_tg(self, tg_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM employees WHERE tg_id = ?", (tg_id,))

    def list_employees(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._all("SELECT * FROM employees WHERE status = ? ORDER BY full_name", (status,))
        return self._all(
            "SELECT * FROM employees ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, full_name"
        )

    def set_employee_status(self, emp_id: int, status: str) -> None:
        self._write("UPDATE employees SET status = ? WHERE id = ?", (status, emp_id))

    def set_employee_lead(self, emp_id: int, is_lead: bool) -> None:
        self._write("UPDATE employees SET is_lead = ? WHERE id = ?", (int(is_lead), emp_id))

    def active_leads(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM employees WHERE status = 'active' AND is_lead = 1")

    # --- смены ---

    def shift_employee_ids(self, day: str) -> set[int]:
        return {row[0] for row in self._all("SELECT employee_id FROM shifts WHERE day = ?", (day,))}

    def toggle_shift(self, day: str, emp_id: int) -> bool:
        """Добавляет или убирает сотрудника из смены. Возвращает True, если теперь он на смене."""
        if emp_id in self.shift_employee_ids(day):
            self._write("DELETE FROM shifts WHERE day = ? AND employee_id = ?", (day, emp_id))
            return False
        self._write("INSERT INTO shifts (day, employee_id) VALUES (?, ?)", (day, emp_id))
        return True

    def on_shift(self, day: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT e.* FROM employees e JOIN shifts s ON s.employee_id = e.id "
            "WHERE s.day = ? AND e.status = 'active' ORDER BY e.full_name",
            (day,),
        )

    # --- шаблоны задач ---

    def add_template(self, now: datetime, **fields: Any) -> int:
        fields["created_at"] = now.isoformat()
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self._write(f"INSERT INTO templates ({columns}) VALUES ({marks})", tuple(fields.values()))

    def get_template(self, tpl_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM templates WHERE id = ?", (tpl_id,))

    def list_templates(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM templates WHERE archived = 0"
        if active_only:
            sql += " AND active = 1"
        return self._all(sql + " ORDER BY id")

    def set_template_active(self, tpl_id: int, active: bool) -> None:
        self._write("UPDATE templates SET active = ? WHERE id = ?", (int(active), tpl_id))

    def archive_template(self, tpl_id: int) -> None:
        self._write("UPDATE templates SET archived = 1, active = 0 WHERE id = ?", (tpl_id,))

    # --- экземпляры задач ---

    def instance_exists(self, tpl_id: int, scheduled_at: datetime) -> bool:
        row = self._one(
            "SELECT 1 FROM instances WHERE template_id = ? AND scheduled_at = ?",
            (tpl_id, scheduled_at.isoformat()),
        )
        return row is not None

    def create_instance(
        self,
        tpl: sqlite3.Row,
        scheduled_at: datetime,
        due_at: datetime,
        assignees: list[int],
        status: str = "pending",
        day: str | None = None,
    ) -> int:
        return self._write(
            "INSERT INTO instances (template_id, day, scheduled_at, due_at, title, assignees, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tpl["id"],
                day or scheduled_at.date().isoformat(),
                scheduled_at.isoformat(),
                due_at.isoformat(),
                tpl["title"],
                json.dumps(assignees),
                status,
            ),
        )

    def get_instance(self, inst_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM instances WHERE id = ?", (inst_id,))

    def find_instance_by_message(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM instances WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    def list_instances(self, day: str) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM instances WHERE day = ? ORDER BY scheduled_at, id", (day,))

    def open_instances(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM instances WHERE status IN (?, ?) ORDER BY scheduled_at", OPEN_STATUSES)

    def update_instance(self, inst_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._write(f"UPDATE instances SET {assignments} WHERE id = ?", (*fields.values(), inst_id))

    # --- обслуживание ---

    def backup(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()


def assignee_ids(inst: sqlite3.Row) -> list[int]:
    return json.loads(inst["assignees"])
