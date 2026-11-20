"""Бэкапы базы: безопасная копия SQLite (можно делать на работающем боте) + gzip + ротация."""
import asyncio
import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from config import BACKUP_DIR, BACKUP_KEEP, DB_PATH


def _make_backup(stamp: str) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    raw = BACKUP_DIR / f"tutor-{stamp}.db"
    gz = raw.with_name(raw.name + ".gz")

    # backup API SQLite корректно копирует базу, даже если в неё в этот момент пишут
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    with open(raw, "rb") as f_in, gzip.open(gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink()

    # оставляем только последние BACKUP_KEEP файлов
    old = sorted(BACKUP_DIR.glob("tutor-*.db.gz"))[:-BACKUP_KEEP]
    for f in old:
        f.unlink()
    return gz


async def make_backup(stamp: str | None = None) -> Path:
    """Делает бэкап в отдельном потоке, чтобы не тормозить бота. Возвращает путь к .gz."""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d_%H%M")
    return await asyncio.to_thread(_make_backup, stamp)


def has_backup_for(day: str) -> bool:
    """Есть ли уже автоматический бэкап за этот день (чтобы не делать повторно после перезапуска)."""
    return any(BACKUP_DIR.glob(f"tutor-{day}*.db.gz")) if BACKUP_DIR.exists() else False
