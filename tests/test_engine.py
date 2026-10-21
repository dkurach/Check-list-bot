from datetime import datetime

from checklist_bot.engine import (
    REPLY_ALREADY_CLOSED,
    REPLY_DONE,
    REPLY_NOT_ASSIGNEE,
    REPLY_NOT_EMPLOYEE,
    REPLY_NOT_TASK,
)

from .conftest import ADMIN, GROUP, TZ, add_employee

CREATED = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)


def add_times_task(db, times="09:00, 13:00", assignee_id=None, due_min=30):
    return db.add_template(
        CREATED,
        title="Фото витрины",
        assignee_id=assignee_id,
        kind="times",
        times=times,
        weekdays="1234567",
        due_min=due_min,
    )


def group_messages(bot):
    return bot.to(GROUP)


async def test_publishes_at_time_to_shift(engine, db, bot, clock):
    ivan = add_employee(db, 10, "Иван")
    add_employee(db, 11, "Пётр")
    db.toggle_shift("2026-10-20", ivan)
    add_times_task(db)

    clock.set(8, 59)
    await engine.tick()
    assert group_messages(bot) == []

    clock.set(9, 0)
    await engine.tick()
    await engine.tick()  # повторный тик не дублирует задачу
    msgs = group_messages(bot)
    assert len(msgs) == 1
    assert "Фото витрины" in msgs[0]["text"]
    assert "tg://user?id=10" in msgs[0]["text"]
    assert "tg://user?id=11" not in msgs[0]["text"]
    assert "до 09:30" in msgs[0]["text"]


async def test_reply_by_assignee_closes_task(engine, db, bot, clock):
    ivan = add_employee(db, 10, "Иван")
    add_employee(db, 11, "Пётр")
    db.toggle_shift("2026-10-20", ivan)
    add_times_task(db)
    clock.set(9, 0)
    await engine.tick()
    msg_id = group_messages(bot)[0]["message_id"]

    assert await engine.handle_reply(GROUP, 999, 10, "Иван", "ok", None) == REPLY_NOT_TASK
    assert await engine.handle_reply(GROUP, msg_id, 11, "Пётр", "я", None) == REPLY_NOT_ASSIGNEE
    assert await engine.handle_reply(GROUP, msg_id, 77, "Чужой", "я", None) == REPLY_NOT_EMPLOYEE

    clock.set(9, 12)
    assert await engine.handle_reply(GROUP, msg_id, 10, "Иван", None, "photo-id") == REPLY_DONE
    inst = db.find_instance_by_message(GROUP, msg_id)
    assert inst["status"] == "done"
    assert inst["done_photo"] == "photo-id"
    assert "Выполнено: Иван, 09:12" in bot.edited[-1]["text"]
    assert bot.reactions == [{"chat_id": GROUP, "message_id": msg_id}]

    assert await engine.handle_reply(GROUP, msg_id, 10, "Иван", "ещё", None) == REPLY_ALREADY_CLOSED


async def test_anyone_on_shift_can_close_shared_task(engine, db, bot, clock):
    ivan = add_employee(db, 10, "Иван")
    petr = add_employee(db, 11, "Пётр")
    db.toggle_shift("2026-10-20", ivan)
    db.toggle_shift("2026-10-20", petr)
    add_times_task(db, times="09:00")
    clock.set(9, 0)
    await engine.tick()
    msg_id = group_messages(bot)[0]["message_id"]
    assert await engine.handle_reply(GROUP, msg_id, 11, "Пётр", "готово", None) == REPLY_DONE


async def test_overdue_reminds_then_escalates(engine, db, bot, clock):
    ivan = add_employee(db, 10, "Иван")
    lead = add_employee(db, 20, "Старший", lead=True)
    db.toggle_shift("2026-10-20", ivan)
    db.toggle_shift("2026-10-20", lead)
    add_times_task(db, times="09:00", assignee_id=ivan)
    clock.set(9, 0)
    await engine.tick()
    msg_id = group_messages(bot)[0]["message_id"]

    clock.set(9, 30)
    await engine.tick()
    inst = db.find_instance_by_message(GROUP, msg_id)
    assert inst["status"] == "overdue"
    assert "Просрочено" in bot.edited[-1]["text"]
    assert len(bot.to(10)) == 1
    assert bot.to(20) == [] and bot.to(ADMIN) == []

    clock.set(10, 0)
    await engine.tick()
    await engine.tick()
    assert len(bot.to(20)) == 1 and len(bot.to(ADMIN)) == 1
    assert "не выполнена" in bot.to(ADMIN)[0]["text"]

    # Просроченную задачу всё ещё можно закрыть ответом.
    assert await engine.handle_reply(GROUP, msg_id, 10, "Иван", "сделал", None) == REPLY_DONE


async def test_missed_while_offline(engine, db, bot, clock):
    add_times_task(db, times="09:00, 13:00", due_min=30)
    clock.set(13, 10)  # бот включили после 9:30, но до 13:30
    await engine.tick()
    day = db.list_instances("2026-10-20")
    assert [inst["status"] for inst in day] == ["missed", "pending"]
    assert len(group_messages(bot)) == 1
    assert any("бот был выключен" in m["text"] for m in bot.to(ADMIN))


async def test_no_one_on_shift_warns_admin(engine, db, bot, clock):
    add_times_task(db, times="09:00")
    clock.set(9, 0)
    await engine.tick()
    assert "Исполнитель не назначен" in group_messages(bot)[0]["text"]
    assert "никого нет" in bot.to(ADMIN)[0]["text"]


async def test_template_created_later_does_not_backfill(engine, db, bot, clock):
    clock.set(14, 0)
    db.add_template(clock(), title="Позже", kind="times", times="09:00, 15:00", weekdays="1234567", due_min=30)
    await engine.tick()
    assert db.list_instances("2026-10-20") == []
    clock.set(15, 0)
    await engine.tick()
    assert len(group_messages(bot)) == 1


async def test_nothing_published_until_group_bound(engine, db, bot, clock):
    db.set_setting("group_chat_id", "")
    add_times_task(db, times="09:00")
    clock.set(9, 0)
    await engine.tick()
    assert bot.sent == []


async def test_manual_close_and_cancel(engine, db, bot, clock):
    add_times_task(db, times="09:00, 10:00", due_min=120)
    clock.set(10, 0)
    await engine.tick()
    first, second = db.list_instances("2026-10-20")
    assert await engine.close_manually(first["id"], ADMIN, "Админ")
    assert await engine.cancel(second["id"])
    assert not await engine.cancel(second["id"])
    statuses = [inst["status"] for inst in db.list_instances("2026-10-20")]
    assert statuses == ["done", "cancelled"]


async def test_daily_backup(engine, cfg, clock):
    await engine.tick()
    assert (cfg.backups_dir / "bot-2026-10-20.db").exists()


async def test_night_shift_after_midnight(engine, db, bot, clock):
    # Заведение работает до 3 ночи: задача в 00:30 вторника относится к смене понедельника.
    ivan = add_employee(db, 10, "Иван")
    db.toggle_shift("2026-10-19", ivan)
    db.add_template(
        datetime(2026, 10, 19, 12, 0, tzinfo=TZ),
        title="Ночной обход",
        kind="interval",
        start_time="14:45",
        end_time="02:45",
        interval_min=45,
        weekdays="1",
        due_min=30,
    )
    clock.set(0, 40, day=20)
    assert engine.today() == "2026-10-19"
    await engine.tick()
    msgs = group_messages(bot)
    assert len(msgs) == 1 and "tg://user?id=10" in msgs[0]["text"]
    # Всё, что прошло, пока бот был выключен, помечено пропущенным в те же сутки.
    statuses = [inst["status"] for inst in db.list_instances("2026-10-19")]
    assert statuses.count("pending") == 1 and statuses.count("missed") == 13

    clock.set(5, 0, day=20)  # наступили новые рабочие сутки (вторник), окна для вторника нет
    await engine.tick()
    assert db.list_instances("2026-10-20") == []
