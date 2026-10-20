"""Кто есть кто: главные админы, старшие смены, сотрудники."""

from __future__ import annotations

from .config import Config
from .db import DB


def is_admin(db: DB, cfg: Config, tg_id: int) -> bool:
    return tg_id in cfg.admin_ids or tg_id in db.admin_ids()


def is_lead(db: DB, tg_id: int) -> bool:
    emp = db.get_employee_by_tg(tg_id)
    return bool(emp and emp["status"] == "active" and emp["is_lead"])


def is_staff(db: DB, cfg: Config, tg_id: int) -> bool:
    """Имеет доступ к меню управления: главный админ или старший смены."""
    return is_admin(db, cfg, tg_id) or is_lead(db, tg_id)


def all_admin_ids(db: DB, cfg: Config) -> set[int]:
    return cfg.admin_ids | db.admin_ids()
