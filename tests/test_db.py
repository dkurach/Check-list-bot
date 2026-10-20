from datetime import datetime

from checklist_bot.config import read_env, write_env
from checklist_bot.db import DB

from .conftest import TZ, add_employee


def test_migrate_is_idempotent(tmp_path):
    db = DB(tmp_path / "data" / "bot.db")
    db.migrate()
    db.migrate()
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_register_and_shift(db):
    now = datetime(2026, 10, 20, tzinfo=TZ)
    emp, created = db.register_employee(5, "Анна", "anna", now)
    assert created and emp["status"] == "pending"
    emp, created = db.register_employee(5, "Анна К.", "anna", now)
    assert not created and emp["full_name"] == "Анна К."

    assert db.toggle_shift("2026-10-20", emp["id"]) is True
    assert db.on_shift("2026-10-20") == []  # ещё не подтверждена
    db.set_employee_status(emp["id"], "active")
    assert [e["full_name"] for e in db.on_shift("2026-10-20")] == ["Анна К."]
    assert db.toggle_shift("2026-10-20", emp["id"]) is False


def test_admins_and_archive(db):
    db.add_admin(42)
    db.add_admin(42)
    assert db.admin_ids() == {42}
    emp_id = add_employee(db, 7, "Иван")
    tpl_id = db.add_template(
        datetime(2026, 10, 20, tzinfo=TZ),
        title="X",
        assignee_id=emp_id,
        kind="times",
        times="09:00",
        weekdays="1234567",
        due_min=30,
    )
    db.archive_template(tpl_id)
    assert db.list_templates() == []


def test_env_roundtrip(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\nBOT_TOKEN="1:abc"\nTIMEZONE=Europe/Moscow\n', encoding="utf-8")
    env = read_env(path)
    assert env == {"BOT_TOKEN": "1:abc", "TIMEZONE": "Europe/Moscow"}
    write_env(path, env)
    assert read_env(path) == env


def test_instance_lock(tmp_path):
    from checklist_bot.service import InstanceLock, is_running

    data = tmp_path / "data"
    lock = InstanceLock(data)
    assert lock.acquire()
    assert is_running(data)
    assert not InstanceLock(data).acquire()
    lock.release()
    assert not is_running(data)
