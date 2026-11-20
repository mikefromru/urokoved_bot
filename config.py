import os
from pathlib import Path
from zoneinfo import ZoneInfo

# Подгружаем .env рядом с config.py (переменные окружения, если заданы, важнее)
_env = Path(__file__).with_name(".env")
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Moscow"))
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).with_name("tutor.db")))

# На сколько дней вперёд «разворачивать» регулярное расписание в конкретные уроки
GENERATE_DAYS = 21
DEFAULT_DIGEST_TIME = "08:00"

# Твой Telegram id: сюда приходят бэкапы, и только тебе доступна команда /backup.
# Узнать id можно в логе бота (строки вида «… id=123456789 …»).
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

# Бэкапы базы: каждый день в BACKUP_TIME (по часовому поясу BOT_TZ), хранятся последние BACKUP_KEEP
BACKUP_DIR = Path(__file__).with_name("backups")
BACKUP_TIME = os.environ.get("BACKUP_TIME", "03:30")
BACKUP_KEEP = 14
