from datetime import date, datetime, timedelta

import aiosqlite

from config import DEFAULT_DIGEST_TIME, GENERATE_DAYS
from utils import get_zone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tutors (
    user_id      INTEGER PRIMARY KEY,
    digest_time  TEXT NOT NULL DEFAULT '08:00',
    last_digest  TEXT,                     -- дата последней утренней сводки
    digest_on    INTEGER NOT NULL DEFAULT 1,
    tz           TEXT NOT NULL DEFAULT 'Europe/Moscow',  -- часовой пояс репетитора
    blocked      INTEGER NOT NULL DEFAULT 0              -- 1 = заблокировал бота, сводки не шлём
);

CREATE TABLE IF NOT EXISTS students (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tutor_id  INTEGER NOT NULL REFERENCES tutors(user_id),
    name      TEXT NOT NULL,
    contact   TEXT,
    price     INTEGER NOT NULL DEFAULT 0,  -- цена одного урока
    currency  TEXT NOT NULL DEFAULT 'RUB', -- валюта цены: RUB, KZT, USD...
    active    INTEGER NOT NULL DEFAULT 1
);

-- Регулярное расписание: «каждый вторник в 17:00»
CREATE TABLE IF NOT EXISTS slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL,           -- 0 = понедельник
    time       TEXT NOT NULL               -- 'HH:MM'
);

-- Конкретные уроки. Регулярные создаются из slots на GENERATE_DAYS вперёд,
-- разовые добавляются вручную (slot_id = NULL).
-- orig_day нужен, чтобы перенесённый урок не создавался заново в старую дату.
CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    slot_id    INTEGER REFERENCES slots(id) ON DELETE SET NULL,
    orig_day   TEXT,
    day        TEXT NOT NULL,              -- 'YYYY-MM-DD'
    time       TEXT NOT NULL,              -- 'HH:MM'
    status     TEXT NOT NULL DEFAULT 'planned',   -- planned | cancelled
    UNIQUE (slot_id, orig_day)
);
CREATE INDEX IF NOT EXISTS idx_lessons_day ON lessons(day);

-- Оплаты пакетами: «8000 ₽ за 8 уроков»
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    day        TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    lessons    INTEGER NOT NULL
);
"""

# Урок считается проведённым, если он не отменён и его время уже прошло.
HELD = "l.status = 'planned' AND l.day || ' ' || l.time < ?"


def _key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self):
        """Добавляет новые колонки в базу, созданную старой версией бота."""
        cols = {r["name"] for r in await self._all("PRAGMA table_info(tutors)")}
        if "digest_on" not in cols:
            await self.conn.execute("ALTER TABLE tutors ADD COLUMN digest_on INTEGER NOT NULL DEFAULT 1")
        if "tz" not in cols:
            await self.conn.execute("ALTER TABLE tutors ADD COLUMN tz TEXT NOT NULL DEFAULT 'Europe/Moscow'")
        if "blocked" not in cols:
            await self.conn.execute("ALTER TABLE tutors ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in await self._all("PRAGMA table_info(students)")}
        if "currency" not in cols:
            await self.conn.execute("ALTER TABLE students ADD COLUMN currency TEXT NOT NULL DEFAULT 'RUB'")

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _all(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _one(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    # ---------- репетиторы ----------

    async def ensure_tutor(self, user_id: int, today: date):
        # last_digest = сегодня, чтобы новичку не прилетела сводка сразу после /start
        await self.conn.execute(
            "INSERT OR IGNORE INTO tutors(user_id, digest_time, last_digest) VALUES (?, ?, ?)",
            (user_id, DEFAULT_DIGEST_TIME, today.isoformat()),
        )
        await self.conn.commit()

    async def tutor(self, user_id: int):
        return await self._one("SELECT * FROM tutors WHERE user_id = ?", (user_id,))

    async def set_digest(self, user_id: int, at: datetime, *, time: str | None = None, on: bool | None = None):
        """Меняет время и/или включает-выключает сводку.
        Если новое время сегодня уже прошло — считаем сегодняшнюю сводку отправленной,
        чтобы она не пришла сразу после изменения настроек вечером."""
        if time is not None:
            await self.conn.execute("UPDATE tutors SET digest_time = ? WHERE user_id = ?", (time, user_id))
        if on is not None:
            await self.conn.execute("UPDATE tutors SET digest_on = ? WHERE user_id = ?", (int(on), user_id))
        t = await self.tutor(user_id)
        if t and t["digest_time"] <= at.strftime("%H:%M"):
            await self.conn.execute("UPDATE tutors SET last_digest = ? WHERE user_id = ? "
                                    "AND (last_digest IS NULL OR last_digest < ?)",
                                    (at.date().isoformat(), user_id, at.date().isoformat()))
        await self.conn.commit()

    async def set_blocked(self, user_id: int, blocked: bool):
        await self.conn.execute("UPDATE tutors SET blocked = ? WHERE user_id = ?", (int(blocked), user_id))
        await self.conn.commit()

    async def stats(self) -> dict:
        row = await self._one(
            "SELECT (SELECT COUNT(*) FROM tutors), (SELECT COUNT(*) FROM tutors WHERE blocked = 1), "
            "(SELECT COUNT(*) FROM students WHERE active = 1)"
        )
        return {"tutors": row[0], "blocked": row[1], "students": row[2]}

    async def tutor_tz(self, user_id: int) -> str:
        row = await self._one("SELECT tz FROM tutors WHERE user_id = ?", (user_id,))
        return row["tz"] if row else "Europe/Moscow"

    async def set_tz(self, user_id: int, zone: str):
        await self.conn.execute("UPDATE tutors SET tz = ? WHERE user_id = ?", (zone, user_id))
        await self.conn.commit()
        # по новому местному времени: если сводка сегодня уже «прошла» — не присылать её сразу
        await self.set_digest(user_id, datetime.now(get_zone(zone)))

    async def tutors_due_digest(self, at: datetime) -> list[tuple[int, datetime]]:
        """Кому пора прислать сводку: [(user_id, местное время репетитора)]."""
        due = []
        for t in await self._all("SELECT * FROM tutors WHERE digest_on = 1 AND blocked = 0"):
            local = at.astimezone(get_zone(t["tz"]))
            if t["digest_time"] <= local.strftime("%H:%M") and (
                    t["last_digest"] is None or t["last_digest"] < local.date().isoformat()):
                due.append((t["user_id"], local))
        return due

    async def mark_digest_sent(self, user_id: int, today: date):
        await self.conn.execute("UPDATE tutors SET last_digest = ? WHERE user_id = ?",
                                (today.isoformat(), user_id))
        await self.conn.commit()

    # ---------- ученики ----------

    async def add_student(self, tutor_id: int, name: str, contact: str | None, price: int,
                          currency: str = "RUB") -> int:
        cur = await self.conn.execute(
            "INSERT INTO students(tutor_id, name, contact, price, currency) VALUES (?, ?, ?, ?, ?)",
            (tutor_id, name, contact, price, currency),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def student(self, student_id: int, tutor_id: int):
        """Всегда проверяем владельца: репетитор видит только своих учеников."""
        return await self._one(
            "SELECT * FROM students WHERE id = ? AND tutor_id = ?", (student_id, tutor_id)
        )

    async def students_with_balance(self, tutor_id: int, at: datetime):
        return await self._all(
            f"""
            SELECT s.*,
                   COALESCE((SELECT SUM(p.lessons) FROM payments p WHERE p.student_id = s.id), 0)
                 - (SELECT COUNT(*) FROM lessons l WHERE l.student_id = s.id AND {HELD}) AS balance
            FROM students s
            WHERE s.tutor_id = ? AND s.active = 1
            ORDER BY s.name COLLATE NOCASE
            """,
            (_key(at), tutor_id),
        )

    async def balance(self, student_id: int, at: datetime) -> int:
        row = await self._one(
            f"""
            SELECT COALESCE((SELECT SUM(lessons) FROM payments WHERE student_id = ?), 0)
                 - (SELECT COUNT(*) FROM lessons l WHERE l.student_id = ? AND {HELD})
            """,
            (student_id, student_id, _key(at)),
        )
        return row[0]

    async def set_contact(self, student_id: int, contact: str | None):
        await self.conn.execute("UPDATE students SET contact = ? WHERE id = ?", (contact, student_id))
        await self.conn.commit()

    async def set_name(self, student_id: int, name: str):
        await self.conn.execute("UPDATE students SET name = ? WHERE id = ?", (name, student_id))
        await self.conn.commit()

    async def set_price(self, student_id: int, price: int, currency: str | None = None):
        if currency:
            await self.conn.execute("UPDATE students SET price = ?, currency = ? WHERE id = ?",
                                    (price, currency, student_id))
        else:
            await self.conn.execute("UPDATE students SET price = ? WHERE id = ?", (price, student_id))
        await self.conn.commit()

    async def archive_student(self, student_id: int, at: datetime):
        await self.conn.execute("UPDATE students SET active = 0 WHERE id = ?", (student_id,))
        await self.conn.execute(
            "DELETE FROM lessons WHERE student_id = ? AND status = 'planned' AND day || ' ' || time >= ?",
            (student_id, _key(at)),
        )
        await self.conn.commit()

    # ---------- расписание ----------

    async def slots_of(self, student_id: int):
        return await self._all(
            "SELECT * FROM slots WHERE student_id = ? ORDER BY weekday, time", (student_id,)
        )

    async def replace_slots(self, student_id: int, slots: list[tuple[int, str]], at: datetime):
        """Новое регулярное расписание: будущие регулярные уроки пересоздаются, прошлые остаются."""
        await self.conn.execute(
            "DELETE FROM lessons WHERE student_id = ? AND slot_id IS NOT NULL "
            "AND status = 'planned' AND day || ' ' || time >= ?",
            (student_id, _key(at)),
        )
        await self.conn.execute("DELETE FROM slots WHERE student_id = ?", (student_id,))
        await self.conn.executemany(
            "INSERT INTO slots(student_id, weekday, time) VALUES (?, ?, ?)",
            [(student_id, wd, t) for wd, t in slots],
        )
        await self.conn.commit()
        await self.generate_lessons(at)

    async def add_slot(self, student_id: int, weekday: int, time: str, at: datetime) -> bool:
        """Добавляет один регулярный урок. False, если такой уже есть."""
        if await self._one("SELECT 1 FROM slots WHERE student_id = ? AND weekday = ? AND time = ?",
                           (student_id, weekday, time)):
            return False
        await self.conn.execute("INSERT INTO slots(student_id, weekday, time) VALUES (?, ?, ?)",
                                (student_id, weekday, time))
        await self.conn.commit()
        await self.generate_lessons(at)
        return True

    async def delete_slot(self, slot_id: int, student_id: int, at: datetime):
        """Удаляет регулярный урок; будущие уроки по нему удаляются, прошедшие остаются."""
        await self.conn.execute(
            "DELETE FROM lessons WHERE slot_id = ? AND student_id = ? AND status = 'planned' "
            "AND day || ' ' || time >= ?",
            (slot_id, student_id, _key(at)),
        )
        await self.conn.execute("DELETE FROM slots WHERE id = ? AND student_id = ?", (slot_id, student_id))
        await self.conn.commit()

    async def generate_lessons(self, at: datetime, days: int = GENERATE_DAYS):
        """Разворачивает регулярные слоты в конкретные уроки. Безопасно вызывать сколько угодно раз."""
        slots = await self._all(
            "SELECT sl.*, t.tz FROM slots sl JOIN students s ON s.id = sl.student_id "
            "JOIN tutors t ON t.user_id = s.tutor_id WHERE s.active = 1"
        )
        rows = []
        for sl in slots:
            local = at.astimezone(get_zone(sl["tz"]))  # время и даты уроков — местные для репетитора
            start = local.date()
            end = start + timedelta(days=days)
            now_k = _key(local)
            d = start + timedelta(days=(sl["weekday"] - start.weekday()) % 7)
            while d < end:
                if f"{d.isoformat()} {sl['time']}" >= now_k:  # прошлое не создаём
                    rows.append((sl["student_id"], sl["id"], d.isoformat(), d.isoformat(), sl["time"]))
                d += timedelta(days=7)
        await self.conn.executemany(
            "INSERT OR IGNORE INTO lessons(student_id, slot_id, orig_day, day, time) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.conn.commit()

    # ---------- уроки ----------

    async def lessons_on(self, tutor_id: int, day: date):
        return await self._all(
            "SELECT l.*, s.name, s.contact, s.price, sl.time AS slot_time FROM lessons l "
            "JOIN students s ON s.id = l.student_id LEFT JOIN slots sl ON sl.id = l.slot_id "
            "WHERE s.tutor_id = ? AND s.active = 1 AND l.day = ? ORDER BY l.time",
            (tutor_id, day.isoformat()),
        )

    async def lesson(self, lesson_id: int, tutor_id: int):
        return await self._one(
            "SELECT l.*, s.name, s.contact, s.price, sl.time AS slot_time FROM lessons l "
            "JOIN students s ON s.id = l.student_id LEFT JOIN slots sl ON sl.id = l.slot_id "
            "WHERE l.id = ? AND s.tutor_id = ?",
            (lesson_id, tutor_id),
        )

    async def set_lesson_status(self, lesson_id: int, status: str):
        await self.conn.execute("UPDATE lessons SET status = ? WHERE id = ?", (status, lesson_id))
        await self.conn.commit()

    async def move_lesson(self, lesson_id: int, day: date, time: str):
        await self.conn.execute(
            "UPDATE lessons SET day = ?, time = ?, status = 'planned' WHERE id = ?",
            (day.isoformat(), time, lesson_id),
        )
        await self.conn.commit()

    async def add_extra_lesson(self, student_id: int, day: date, time: str):
        await self.conn.execute(
            "INSERT INTO lessons(student_id, day, time) VALUES (?, ?, ?)",
            (student_id, day.isoformat(), time),
        )
        await self.conn.commit()

    # ---------- оплаты и статистика ----------

    async def add_payment(self, student_id: int, amount: int, lessons: int, day: date):
        await self.conn.execute(
            "INSERT INTO payments(student_id, day, amount, lessons) VALUES (?, ?, ?, ?)",
            (student_id, day.isoformat(), amount, lessons),
        )
        await self.conn.commit()

    async def month_stats(self, tutor_id: int, at: datetime):
        month = at.strftime("%Y-%m")
        held = await self._one(
            f"SELECT COUNT(*), COALESCE(SUM(s.price), 0) FROM lessons l JOIN students s ON s.id = l.student_id "
            f"WHERE s.tutor_id = ? AND l.day LIKE ? AND {HELD}",
            (tutor_id, month + "%", _key(at)),
        )
        paid = await self._one(
            "SELECT COALESCE(SUM(p.amount), 0) FROM payments p JOIN students s ON s.id = p.student_id "
            "WHERE s.tutor_id = ? AND p.day LIKE ?",
            (tutor_id, month + "%"),
        )
        return {"lessons": held[0], "earned": held[1], "received": paid[0]}
