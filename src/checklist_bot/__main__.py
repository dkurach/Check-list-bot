"""Командная строка: python -m checklist_bot {run,setup,start,stop,restart,status,uninstall}."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import service
from .config import ENV_FILE, ConfigError, load_config, read_env, write_env
from .db import DB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checklist_bot", description="Telegram-бот для задач в общем чате")
    parser.add_argument(
        "command",
        choices=["run", "setup", "start", "stop", "restart", "status", "uninstall"],
        help="run - запустить в этом окне; setup - установка; start/stop/restart - фоновый бот",
    )
    parser.add_argument("--home", type=Path, default=Path.cwd(), help="папка установки (где лежит .env)")
    args = parser.parse_args(argv)
    home: Path = args.home.resolve()

    if args.command == "setup":
        return setup(home)
    if args.command == "run":
        return run(home)
    if args.command == "start":
        return start(home)
    if args.command == "stop":
        print("Бот остановлен." if service.stop(home) else "Бот не был запущен.")
        return 0
    if args.command == "restart":
        service.stop(home)
        wait_stopped(home)
        return start(home)
    if args.command == "status":
        print("Бот работает." if service.is_running(home / "data") else "Бот не запущен.")
        return 0
    if args.command == "uninstall":
        service.stop(home)
        service.remove_autostart()
        print("Автозапуск отключён. Данные остались в папке data, её можно удалить вручную.")
        return 0
    return 1


def run(home: Path) -> int:
    from aiogram.exceptions import TelegramUnauthorizedError

    from .app import run as run_bot
    from .app import setup_logging

    try:
        cfg = load_config(home)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    lock = service.InstanceLock(cfg.data_dir)
    if not lock.acquire():
        print("Бот уже запущен из этой папки.", file=sys.stderr)
        return 3
    setup_logging(cfg)
    log = logging.getLogger("checklist_bot")
    try:
        # Перезапускаем при неожиданных сбоях, чтобы бот не падал насовсем.
        while True:
            try:
                asyncio.run(run_bot(cfg))
                return 0
            except KeyboardInterrupt:
                return 0
            except TelegramUnauthorizedError:
                log.error("Telegram не принимает токен бота. Проверьте BOT_TOKEN в .env и запустите установку снова.")
                return 2
            except Exception:
                log.exception("Бот упал, перезапуск через 15 секунд")
                time.sleep(15)
    finally:
        lock.release()


def start(home: Path) -> int:
    if service.is_running(home / "data"):
        print("Бот уже работает.")
        return 0
    service.start(home)
    for _ in range(20):
        time.sleep(0.5)
        if service.is_running(home / "data"):
            print("Бот запущен в фоне.")
            return 0
    print(f"Бот не запустился. Смотрите журнал: {home / 'data' / 'logs'}")
    return 1


def wait_stopped(home: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and service.is_running(home / "data"):
        time.sleep(0.3)


# --- установка ---


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default


def check_token(token: str) -> str | None:
    """Возвращает username бота или None, если токен не подошёл."""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError:
        return None
    return data["result"]["username"] if data.get("ok") else None


def setup(home: Path) -> int:
    print("=== Установка бота ===")
    print(f"Папка: {home}\n")
    if service.is_macos() and any(part in home.parts for part in ("Desktop", "Documents", "Downloads")):
        print(
            "Внимание: на macOS фоновые службы не имеют доступа к папкам Рабочий стол, Документы и Загрузки.\n"
            "Перенесите папку бота, например, в домашнюю папку (~/check-list-bot) и запустите установку снова.\n"
        )
        return 1
    env = read_env(home / ENV_FILE)

    while True:
        token = ask("Токен бота от @BotFather", env.get("BOT_TOKEN"))
        try:
            username = check_token(token)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Не удалось проверить токен (нет интернета?): {exc}")
            username = None
            if ask("Продолжить без проверки? (да/нет)", "нет").lower().startswith("д"):
                break
            continue
        if username:
            print(f"Бот найден: @{username}")
            break
        print("Telegram не принял этот токен. Скопируйте его из @BotFather ещё раз.")

    while True:
        tz_name = ask("Часовой пояс", env.get("TIMEZONE", "Europe/Moscow"))
        try:
            ZoneInfo(tz_name)
            break
        except (ZoneInfoNotFoundError, ValueError):
            print("Не знаю такого пояса. Примеры: Europe/Moscow, Europe/Kaliningrad, Asia/Yekaterinburg.")

    env["BOT_TOKEN"] = token
    env["TIMEZONE"] = tz_name
    env.setdefault("ADMIN_IDS", "")
    env.setdefault("ESCALATE_AFTER_MIN", "30")
    env["SETUP_CODE"] = env.get("SETUP_CODE") or secrets.token_hex(4)
    write_env(home / ENV_FILE, env)

    db = DB(home / "data" / "bot.db")
    db.migrate()
    db.close()

    service.stop(home)
    wait_stopped(home)
    try:
        where = service.install_autostart(home)
        print(f"Автозапуск: {where}")
    except Exception as exc:  # noqa: BLE001 - установка должна продолжиться и без автозапуска
        print(f"Не удалось включить автозапуск: {exc}")
    start(home)

    code = env["SETUP_CODE"]
    link = f"https://t.me/{username}?start={code}" if username else None
    print("\n=== Готово ===")
    print("1. Главный администратор открывает ссылку и нажимает «Старт»:")
    print(f"   {link}" if link else f"   напишите боту: /start {code}")
    print("2. Добавьте бота в общий чат, сделайте администратором и отправьте в чате: /bind")
    print("3. Сотрудники пишут боту /start, вы подтверждаете их в меню.")
    print("Не отправляйте эту ссылку никому, кроме администраторов: она даёт полный доступ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
