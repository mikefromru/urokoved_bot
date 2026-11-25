"""Команды только для владельца бота (ADMIN_ID)."""
import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from backup import make_backup
from config import ADMIN_ID
from db import DB

router = Router()
router.message.filter(F.from_user.id == ADMIN_ID)  # для остальных этих команд как будто нет


@router.message(Command("backup"))
async def cmd_backup(msg: Message):
    path = await make_backup()
    logging.getLogger("backup").info("Бэкап по команде: %s", path.name)
    await msg.answer_document(FSInputFile(path), caption=f"💾 Бэкап базы · {path.name}")


@router.message(Command("stats"))
async def cmd_stats(msg: Message, db: DB):
    s = await db.stats()
    await msg.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Репетиторов: <b>{s['tutors']}</b> (заблокировали бота: {s['blocked']})\n"
        f"Учеников: <b>{s['students']}</b>"
    )


@router.message(Command("reply", "replay"))
async def cmd_reply(msg: Message, command: CommandObject):
    """/reply <id> <текст> — ответ пользователю от имени бота."""
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await msg.answer("Формат: <code>/reply 123456789 текст ответа</code>")
        return
    user_id, text = int(parts[0]), parts[1]
    try:
        await msg.bot.send_message(user_id, f"💬 <b>Ответ разработчика</b>\n\n{escape(text)}")
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        await msg.answer(f"Не удалось отправить: {e}")
        return
    logging.getLogger("actions").info("Ответ разработчика отправлен: id=%s", user_id)
    await msg.answer(f"✅ Отправлено пользователю id={user_id}")
