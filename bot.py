import asyncio
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile

from admin import router as admin_router
from backup import has_backup_for, make_backup
from config import ADMIN_ID, BACKUP_TIME, BOT_TOKEN, DB_PATH, TZ
from db import DB
from handlers import render_digest, router
from middlewares import ActionLogMiddleware, MenuUpdateMiddleware, TutorTimezoneMiddleware

LOG_FILE = Path(__file__).with_name("logs") / "bot.log"


def setup_logging():
    """Логи в консоль и в logs/bot.log (до 5 файлов по 5 МБ, старые удаляются сами)."""
    LOG_FILE.parent.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    file = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (console, file):
        h.setFormatter(fmt)
        root.addHandler(h)
    # aiogram пишет строку на каждый апдейт; действия уже пишет ActionLogMiddleware, поэтому от aiogram — только ошибки
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def send_backup(bot: Bot, path, caption: str):
    """Отправляет бэкап админу, чтобы копия базы была не только на этом компьютере."""
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задан — бэкап сохранён только локально: %s", path)
        return
    try:
        await bot.send_document(ADMIN_ID, FSInputFile(path), caption=caption)
    except Exception:
        logging.exception("Не удалось отправить бэкап админу")


async def daily_backup(bot: Bot):
    local = datetime.now(TZ)
    today = local.strftime("%Y-%m-%d")
    if local.strftime("%H:%M") < BACKUP_TIME or has_backup_for(today):
        return
    path = await make_backup(today)
    logging.getLogger("backup").info("Бэкап готов: %s (%.1f КБ)", path.name, path.stat().st_size / 1024)
    await send_backup(bot, path, f"💾 Ежедневный бэкап базы · {today}")


async def send_digest(bot: Bot, db: DB, tutor_id: int, local: datetime):
    log = logging.getLogger("digest")
    try:
        await bot.send_message(tutor_id, await render_digest(db, tutor_id, local.date()))
        log.info("Сводка отправлена: id=%s, местное время %s", tutor_id, local.strftime("%H:%M"))
    except TelegramForbiddenError:
        # бот заблокирован или аккаунт удалён — больше не пытаемся, пока человек не вернётся
        await db.set_blocked(tutor_id, True)
        log.info("id=%s заблокировал бота — сводки приостановлены", tutor_id)
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            await db.set_blocked(tutor_id, True)
            log.info("id=%s: чат не найден — сводки приостановлены", tutor_id)
        else:
            log.exception("Не удалось отправить сводку id=%s", tutor_id)
    except Exception:
        log.exception("Не удалось отправить сводку id=%s", tutor_id)
    # отмечаем в любом случае, чтобы при ошибке не пытаться каждые 20 секунд весь день
    await db.mark_digest_sent(tutor_id, local.date())


async def scheduler(bot: Bot, db: DB):
    """Раз в 20 секунд: раз в час продлевает расписание, рассылает сводки по местному времени,
    раз в сутки делает бэкап базы."""
    last_generated = None
    while True:
        try:
            at = datetime.now(timezone.utc)
            hour = at.strftime("%Y-%m-%d %H")
            if last_generated != hour:
                await db.generate_lessons(at)
                last_generated = hour

            for tutor_id, local in await db.tutors_due_digest(at):
                await send_digest(bot, db, tutor_id, local)
                await asyncio.sleep(0.05)  # не упираемся в лимиты Telegram при большой рассылке

            await daily_backup(bot)
        except Exception:
            logging.exception("Ошибка в планировщике")
        await asyncio.sleep(20)


async def main():
    setup_logging()
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")

    db = DB(DB_PATH)
    await db.connect()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)  # админские команды — раньше основного роутера
    dp.include_router(router)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(ActionLogMiddleware())
        observer.outer_middleware(TutorTimezoneMiddleware())
        observer.outer_middleware(MenuUpdateMiddleware())
    logging.info("Бот запущен, база: %s, логи: %s", DB_PATH, LOG_FILE)
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задан в .env — бэкапы не будут присылаться в Telegram")

    task = asyncio.create_task(scheduler(bot, db))
    try:
        # db=db попадает в аргументы всех хендлеров
        await dp.start_polling(bot, db=db)
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
