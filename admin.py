"""Команды только для владельца бота (ADMIN_ID)."""
import logging

from aiogram import F, Router
from aiogram.filters import Command
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
