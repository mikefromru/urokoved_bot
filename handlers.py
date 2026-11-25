from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re
from html import escape
from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config import ADMIN_ID
from db import DB
from utils import (CURRENT_TZ, CURRENCIES, TIMEZONES, WEEKDAYS, tz_label, WEEKDAYS_FULL, day_title, now_key, fmt_day, fmt_money, fmt_phone, plural, now,
                   parse_time, whatsapp_phone)

router = Router()

# ================= клавиатуры и callback-данные =================

BTN_TODAY, BTN_TOMORROW, BTN_WEEK = "📅 Сегодня", "📆 Завтра", "🗓 Неделя"
BTN_STUDENTS, BTN_ADD, BTN_SETTINGS = "👥 Ученики", "➕ Новый ученик", "⚙️ Настройки"
BTN_SCHEDULE = "🕒 Расписание"
# Версия главного меню: при её увеличении пользователи один раз получают обновлённую клавиатуру
MENU_VERSION = 1
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_SCHEDULE, BTN_STUDENTS, BTN_ADD, BTN_SETTINGS}


def main_kb():
    kb = ReplyKeyboardBuilder()
    for text in (BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_SCHEDULE, BTN_STUDENTS, BTN_ADD, BTN_SETTINGS):
        kb.button(text=text)
    kb.adjust(3, 2, 2)  # дни | расписание и ученики | новый ученик и настройки
    return kb.as_markup(resize_keyboard=True)


class DayCb(CallbackData, prefix="day"):
    day: str  # YYYY-MM-DD


class LessonCb(CallbackData, prefix="les"):
    action: str
    id: int


class StudentCb(CallbackData, prefix="st"):
    action: str
    id: int


class SlotCb(CallbackData, prefix="sl"):
    """Редактор расписания на кнопках."""
    action: str        # edit | del | add | wd | h | m
    sid: int           # ученик
    slot: int = 0      # id слота (для удаления)
    wd: int = -1       # день недели
    h: int = -1        # час
    m: int = -1        # минуты
    ret: str = ""      # куда вернуться после добавления: "" — редактор, "sched" — расписание, дата — день


class PickCb(CallbackData, prefix="pk"):
    """Выбор ученика для нового регулярного урока."""
    ret: str           # "sched" или дата YYYY-MM-DD экрана дня
    wd: int = -1       # день недели, если он уже известен (добавление из экрана дня)


class SchedCb(CallbackData, prefix="sch"):
    """Открыть экран «Расписание на неделю»."""
    v: int = 0


class MoveCb(CallbackData, prefix="mv"):
    """Перенос урока на кнопках."""
    action: str        # days | day | h | m
    id: int            # урок
    day: str = ""      # YYYY-MM-DD
    h: int = -1
    m: int = -1
    page: int = 0      # листание дней по 2 недели


class CurCb(CallbackData, prefix="cur"):
    """Выбор валюты ученика."""
    mode: str          # new — при добавлении ученика, edit — при смене стоимости
    code: str
    sid: int = 0
    price: int = 0


class DurCb(CallbackData, prefix="dur"):
    """Выбор длительности урока."""
    mode: str          # new — при добавлении ученика, edit — в карточке ученика
    minutes: int       # 0 = длительность не указана
    sid: int = 0


class SkipCb(CallbackData, prefix="skip"):
    """Кнопка «Пропустить» на необязательных шагах добавления ученика."""
    step: str          # subject | contact | price


class SetCb(CallbackData, prefix="set"):
    """Настройки."""
    action: str        # open | on | off | time | h | m | tz | tzset
    h: int = -1
    m: int = -1
    tz: str = ""       # ключ из TIMEZONES


DIGEST_HOURS = range(5, 13)
MOVE_DAYS = 14
HOURS = range(7, 23)
MINUTES = (0, 15, 30, 45)


class AddStudent(StatesGroup):
    name = State()
    subject = State()
    contact = State()
    price = State()
    currency = State()
    duration = State()


class Ask(StatesGroup):
    """Одношаговые вопросы; id ученика, к которому относится ответ, хранится в data['target']."""
    price = State()
    subject = State()
    contact = State()
    name = State()


SKIP = {"-", "—", "нет"}


def fmt_price(price: int, currency: str) -> str:
    return fmt_money(price, currency) if price else "не указана"


def currency_kb(mode: str, current: str | None = None, sid: int = 0, price: int = 0):
    kb = InlineKeyboardBuilder()
    for code, (symbol, flag, title) in CURRENCIES.items():
        mark = "✅ " if code == current else ""
        sym = "" if symbol.isalpha() else f" {symbol}"  # «Сум сум» не пишем дважды
        kb.button(text=f"{mark}{flag} {title}{sym}",
                  callback_data=CurCb(mode=mode, code=code, sid=sid, price=price))
    if mode == "edit":
        kb.button(text="◀️ Назад", callback_data=StudentCb(action="open", id=sid))
    kb.adjust(*([2] * 6), 1)
    return kb.as_markup()


DURATIONS = (30, 45, 60, 90, 120)


def fmt_duration(minutes: int) -> str:
    """45 -> '45 мин', 60 -> '1 ч', 90 -> '1 ч 30 мин'."""
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} мин"
    return f"{h} ч" + (f" {m} мин" if m else "")


def time_range(start: str, minutes: int) -> str:
    """'08:00', 45 -> '08:00–08:45'; без длительности — только время начала."""
    if not minutes:
        return start
    h, m = map(int, start.split(":"))
    end = (h * 60 + m + minutes) % (24 * 60)
    return f"{start}–{end // 60:02d}:{end % 60:02d}"


def lesson_line(l, strike: bool = False) -> str:
    """Строка урока для дня, недели и сводки: время (с концом урока), имя и предмет."""
    when = time_range(l["time"], l["duration"])
    who = escape(l["name"])
    subject = f" · <i>{escape(l['subject'])}</i>" if l["subject"] else ""
    if strike:
        return f"<s>{when}  {who}</s>{subject}  <i>отменён</i>"
    return f"<b>{when}</b>  {who}{subject}"


def duration_kb(mode: str, current: int | None = None, sid: int = 0):
    kb = InlineKeyboardBuilder()
    for m in DURATIONS:
        mark = "✅ " if m == current else ""
        kb.button(text=f"{mark}{fmt_duration(m)}", callback_data=DurCb(mode=mode, minutes=m, sid=sid))
    mark = "✅ " if current == 0 and mode == "edit" else ""
    kb.button(text=f"{mark}Не указывать", callback_data=DurCb(mode=mode, minutes=0, sid=sid))
    if mode == "edit":
        kb.button(text="◀️ Назад", callback_data=StudentCb(action="open", id=sid))
    kb.adjust(3, 2, 1, 1)
    return kb.as_markup()


def skip_kb(step: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить ▶️", callback_data=SkipCb(step=step))
    return kb.as_markup()


def parse_price(text: str) -> int | None:
    """'1500', '1 500', '1500 ₽', '1500р' -> 1500; '-' -> 0; иначе None"""
    t = text.strip().lower()
    if t in SKIP:
        return 0
    # из ввода удаляются обозначения валюты («₽», «руб.», «тенге», «$»…) и пробелы-разделители тысяч
    digits = re.sub(r"[^\d\s]", "", t).replace(" ", "").replace("\u00a0", "")
    return int(digits) if digits.isdigit() and int(digits) <= 1_000_000 else None

# ================= рендеринг =================


async def safe_edit(cb: CallbackQuery, text: str, reply_markup=None):
    """edit_text, который не падает, если сообщение не изменилось."""
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


HINT = "<i>{}</i>"  # подсказки внизу сообщений — курсивом, чтобы не мешали


def is_moved(l) -> bool:
    """Регулярный урок, который перенесли на другой день или время."""
    return bool(l["slot_id"]) and (l["day"] != l["orig_day"] or l["time"] != l["slot_time"])


async def render_day(db: DB, tutor_id: int, d: date):
    lessons = await db.lessons_on(tutor_id, d)
    planned = [l for l in lessons if l["status"] == "planned"]
    lines = [f"📅 <b>{day_title(d, now().date())}</b>"]
    if planned:
        lines.append(f"{len(planned)} {plural(len(planned), 'урок', 'урока', 'уроков')}")
    lines.append("")
    kb = InlineKeyboardBuilder()
    if not lessons:
        lines.append("Уроков нет 🎉")
    for l in lessons:
        if l["status"] == "cancelled":
            lines.append(lesson_line(l, strike=True))
        else:
            moved = "  🔁 <i>перенесён</i>" if is_moved(l) else ""
            lines.append(lesson_line(l) + moved)
        mark = "❌ " if l["status"] == "cancelled" else "🔁 " if is_moved(l) else ""
        # слева кнопка самого урока (отмена/перенос), справа — регулярное расписание ученика
        kb.button(text=f"{mark}{l['time']}  {l['name']}", callback_data=LessonCb(action="open", id=l["id"]))
        kb.button(text="🕒", callback_data=StudentCb(action="slots", id=l["student_id"]))
    kb.button(text="◀️", callback_data=DayCb(day=(d - timedelta(days=1)).isoformat()))
    kb.button(text="➕ Урок", callback_data=PickCb(ret=d.isoformat(), wd=d.weekday()))
    kb.button(text="▶️", callback_data=DayCb(day=(d + timedelta(days=1)).isoformat()))
    kb.adjust(*([2] * len(lessons)), 3)
    if lessons:
        lines += ["", HINT.format("Урок — отменить или перенести\n🕒 — расписание ученика")]
    return "\n".join(lines), kb.as_markup()


async def render_lesson(db: DB, tutor_id: int, lesson_id: int):
    l = await db.lesson(lesson_id, tutor_id)
    if not l:
        return None, None
    d = date.fromisoformat(l["day"])
    if l["status"] == "cancelled":
        status = "❌ <b>Отменён</b>"
    elif is_moved(l):
        orig = date.fromisoformat(l["orig_day"])
        status = f"🔁 <i>Перенесён. Изначально был: {day_title(orig, now().date()).lower()}, {l['slot_time']}</i>"
    elif l["slot_id"]:
        status = "<i>Регулярный урок</i>"
    else:
        status = "<i>Разовый урок</i>"
    subject = f"📚 {escape(l['subject'])}\n" if l["subject"] else ""
    text = (f"👤 <b>{escape(l['name'])}</b>\n{subject}\n"
            f"📅 {day_title(d, now().date())}\n"
            f"⏰ <b>{time_range(l['time'], l['duration'])}</b>\n\n"
            f"{status}")
    kb = InlineKeyboardBuilder()
    if l["status"] == "planned":
        kb.button(text="❌ Отменить", callback_data=LessonCb(action="cancel", id=lesson_id))
        kb.button(text="🔁 Перенести", callback_data=MoveCb(action="days", id=lesson_id))
        kb.button(text="✉️ Напоминание", callback_data=LessonCb(action="remind", id=lesson_id))
    else:
        kb.button(text="↩️ Вернуть", callback_data=LessonCb(action="restore", id=lesson_id))
    kb.button(text="👤 Ученик", callback_data=StudentCb(action="open", id=l["student_id"]))
    kb.button(text="◀️ К дню", callback_data=DayCb(day=l["day"]))
    kb.adjust(2, 1, 2) if l["status"] == "planned" else kb.adjust(1, 2)
    return text, kb.as_markup()


def slot_lines(slots, duration: int = 0) -> list[str]:
    return [f"<b>{WEEKDAYS[x['weekday']].capitalize()}</b>  {time_range(x['time'], duration)}" for x in slots]


async def render_student(db: DB, tutor_id: int, student_id: int):
    s = await db.student(student_id, tutor_id)
    if not s or not s["active"]:
        return None, None
    slots = await db.slots_of(student_id)
    phone = fmt_phone(s["contact"])
    contact = f"<code>{escape(phone)}</code>" if s["contact"] else "—"
    price = f"{fmt_money(s['price'], s['currency'])} за урок" if s["price"] else "стоимость не указана"
    lines = [f"👤 <b>{escape(s['name'])}</b>"]
    if s["subject"]:
        lines.append(f"📚 {escape(s['subject'])}")
    lines += [
        "",
        f"📞 {contact}",
        f"💵 {price}",
        f"⏱ {fmt_duration(s['duration']) if s['duration'] else 'длительность не указана'}",
        "",
        f"🗓 <b>Расписание</b>",
    ]
    if slots:
        lines += slot_lines(slots, s["duration"])
        n = len(slots)
        week = f"{n} {plural(n, 'урок', 'урока', 'уроков')} в неделю"
        if s["price"]:
            week += f" · {fmt_money(n * s['price'], s['currency'])}"
        lines += ["", HINT.format(week)]
    else:
        lines.append("<i>регулярных уроков нет</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="🕒 Расписание", callback_data=SlotCb(action="edit", sid=student_id))
    kb.button(text="📚 Предмет", callback_data=StudentCb(action="subject", id=student_id))
    kb.button(text="💵 Стоимость", callback_data=StudentCb(action="price", id=student_id))
    kb.button(text="⏱ Длительность", callback_data=StudentCb(action="duration", id=student_id))
    kb.button(text="📞 Телефон", callback_data=StudentCb(action="contact", id=student_id))
    kb.button(text="✏️ Имя", callback_data=StudentCb(action="name", id=student_id))
    kb.button(text="🗑 Удалить", callback_data=StudentCb(action="archive", id=student_id))
    kb.button(text="◀️ Все ученики", callback_data=StudentCb(action="list", id=0))
    kb.adjust(2, 2, 2, 2)
    return "\n".join(lines), kb.as_markup()


async def render_slot_editor(db: DB, tutor_id: int, sid: int):
    s = await db.student(sid, tutor_id)
    if not s or not s["active"]:
        return None, None
    slots = await db.slots_of(sid)
    lines = [f"🕒 <b>Расписание: {escape(s['name'])}</b>", ""]
    if slots:
        lines += slot_lines(slots, s["duration"])
        lines += ["", HINT.format("✖️ — удалить урок, ➕ — добавить новый")]
    else:
        lines += ["Регулярных уроков пока нет.", "", HINT.format("Нажми «➕ Добавить»")]
    kb = InlineKeyboardBuilder()
    for x in slots:
        kb.button(text=f"✖️ {WEEKDAYS[x['weekday']]} {x['time']}",
                  callback_data=SlotCb(action="del", sid=sid, slot=x["id"]))
    kb.button(text="➕ Добавить", callback_data=SlotCb(action="add", sid=sid))
    kb.button(text="✅ Готово", callback_data=StudentCb(action="open", id=sid))
    n = len(slots)
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 2)  # уроки парами, затем «Добавить | Готово»
    return "\n".join(lines), kb.as_markup()


EVERY_WEEKDAY = ["Каждый понедельник", "Каждый вторник", "Каждую среду", "Каждый четверг",
                 "Каждую пятницу", "Каждую субботу", "Каждое воскресенье"]
WEEKDAYS_PLURAL_DAT = ["понедельникам", "вторникам", "средам", "четвергам",
                       "пятницам", "субботам", "воскресеньям"]


def return_cb(cd):
    """Кнопка «Назад» к экрану, с которого началось добавление урока."""
    if cd.ret == "sched":
        return SchedCb()
    if cd.ret:
        return DayCb(day=cd.ret)
    return SlotCb(action="edit", sid=cd.sid)


def render_picker(name: str, cd: SlotCb):
    """Шаги добавления регулярного урока: день недели -> час -> минуты."""
    kb = InlineKeyboardBuilder()
    head = f"🕒 <b>{escape(name)}</b> · новый урок\n\n"
    if cd.action == "add":
        text = head + "Шаг 1 из 3: <b>день недели</b>"
        for i, wd in enumerate(WEEKDAYS):
            kb.button(text=wd.capitalize(), callback_data=SlotCb(action="wd", sid=cd.sid, wd=i, ret=cd.ret))
        kb.button(text="◀️ Назад", callback_data=return_cb(cd))
        kb.adjust(4, 3, 1)
    elif cd.action == "wd":
        text = head + f"{EVERY_WEEKDAY[cd.wd]}\n\nШаг 2 из 3: <b>час</b>"
        for h in HOURS:
            kb.button(text=f"{h:02d}:__", callback_data=SlotCb(action="h", sid=cd.sid, wd=cd.wd, h=h, ret=cd.ret))
        # из экрана дня день недели уже выбран, поэтому «Назад» ведёт к выбору ученика
        if cd.ret and cd.ret != "sched":
            back = PickCb(ret=cd.ret, wd=cd.wd)
        else:
            back = SlotCb(action="add", sid=cd.sid, ret=cd.ret)
        kb.button(text="◀️ Назад", callback_data=back)
        kb.adjust(*([4] * ((len(HOURS) + 3) // 4)), 1)
    else:  # "h"
        text = head + f"{WEEKDAYS_FULL[cd.wd].capitalize()}, {cd.h:02d}:__\n\nШаг 3 из 3: <b>минуты</b>"
        for m in MINUTES:
            kb.button(text=f"{cd.h:02d}:{m:02d}",
                      callback_data=SlotCb(action="m", sid=cd.sid, wd=cd.wd, h=cd.h, m=m, ret=cd.ret))
        kb.button(text="◀️ Назад", callback_data=SlotCb(action="wd", sid=cd.sid, wd=cd.wd, ret=cd.ret))
        kb.adjust(4, 1)
    return text, kb.as_markup()


async def render_schedule(db: DB, tutor_id: int):
    """Экран «Расписание на неделю»: все регулярные уроки по дням и кнопки для изменения."""
    rows = await db.week_template(tutor_id)
    students = await db.students_with_balance(tutor_id, now())
    lines = ["🕒 <b>Расписание на неделю</b>", HINT.format("Регулярные уроки — повторяются каждую неделю"), ""]
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить урок", callback_data=PickCb(ret="sched"))
    if not students:
        lines.append("Учеников пока нет. Сначала добавь ученика, потом — его уроки.")
    elif not rows:
        lines.append("Регулярных уроков пока нет.\n\nНажми «➕ Добавить урок»: выбери ученика, день и время.")
    else:
        for wd in range(7):
            day_rows = [r for r in rows if r["weekday"] == wd]
            if not day_rows:
                continue
            lines.append(f"<b>{WEEKDAYS_FULL[wd].capitalize()}</b>")
            lines += [f"   {lesson_line(r)}" for r in day_rows]
            lines.append("")
        lines.append(HINT.format("🕒 Имя — изменить или удалить уроки ученика"))
    with_slots = list(dict.fromkeys((r["student_id"], r["name"]) for r in rows))
    for sid, name in with_slots:
        kb.button(text=f"🕒 {name}", callback_data=SlotCb(action="edit", sid=sid))
    kb.button(text="➕ Новый ученик", callback_data=StudentCb(action="new", id=0))
    n = len(with_slots)
    kb.adjust(1, *([2] * (n // 2)), *([1] if n % 2 else []), 1)
    return "\n".join(lines).rstrip(), kb.as_markup()


async def render_pick(db: DB, tutor_id: int, cd: PickCb):
    """Выбор ученика для нового регулярного урока."""
    students = await db.students_with_balance(tutor_id, now())
    kb = InlineKeyboardBuilder()
    if cd.wd >= 0:
        text = f"➕ <b>Новый урок по {WEEKDAYS_PLURAL_DAT[cd.wd]}</b>\n\n"
    else:
        text = "➕ <b>Новый урок</b>\n\n"
    if students:
        text += "Для какого ученика?"
        for st in students:
            action = "wd" if cd.wd >= 0 else "add"
            kb.button(text=st["name"], callback_data=SlotCb(action=action, sid=st["id"], wd=cd.wd, ret=cd.ret))
    else:
        text += "Учеников пока нет — сначала добавь ученика."
    kb.button(text="➕ Новый ученик", callback_data=StudentCb(action="new", id=0))
    kb.button(text="◀️ Назад", callback_data=SchedCb() if cd.ret == "sched" else DayCb(day=cd.ret))
    n = len(students)
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1, 1)
    return text, kb.as_markup()


async def render_students(db: DB, tutor_id: int):
    students = await db.students_with_balance(tutor_id, now())
    if not students:
        return f"Учеников пока нет. Нажми «{BTN_ADD}».", None
    lines = [f"👥 <b>Ученики</b> · {len(students)}", ""]
    totals: dict[str, list[int]] = {}   # валюта -> [сумма, уроков]
    not_counted = []                    # ученики, которые не попали в итог, и почему
    kb = InlineKeyboardBuilder()
    for s in students:
        n = len(await db.slots_of(s["id"]))
        if n and s["price"]:
            t = totals.setdefault(s["currency"], [0, 0])
            t[0] += n * s["price"]
            t[1] += n
        elif n and not s["price"]:
            not_counted.append(f"{escape(s['name'])} — не указана стоимость")
        elif s["price"]:
            not_counted.append(f"{escape(s['name'])} — нет регулярных уроков")
        info = f"{n} {plural(n, 'урок', 'урока', 'уроков')}/нед" if n else "без расписания"
        if s["price"]:
            info += f" · {fmt_money(s['price'], s['currency'])}"
        subject = f" · <i>{escape(s['subject'])}</i>" if s["subject"] else ""
        lines.append(f"<b>{escape(s['name'])}</b>{subject} — {info}")
        kb.button(text=s["name"], callback_data=StudentCb(action="open", id=s["id"]))
    kb.button(text="➕ Новый ученик", callback_data=StudentCb(action="new", id=0))
    n = len(students)
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1)

    if totals:
        lines += ["", "💰 <b>За неделю по расписанию</b>"]
        order = list(CURRENCIES)
        for code, (amount, n) in sorted(totals.items(),
                                        key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
            lines.append(f"{fmt_money(amount, code)} · {n} {plural(n, 'урок', 'урока', 'уроков')}")
    if not_counted:
        lines += ["", HINT.format("Не вошли в итог:\n" + "\n".join(not_counted))]
    return "\n".join(lines), kb.as_markup()


def reminder_text(name: str, d: date, t: str) -> str:
    today = now().date()
    if d == today:
        when = "сегодня"
    elif d == today + timedelta(days=1):
        when = "завтра"
    else:
        when = fmt_day(d)
    return f"Здравствуйте, {name}! Напоминаю, что {when} в {t} у нас урок. До встречи!"


async def render_digest(db: DB, tutor_id: int, d: date) -> str:
    """Утренняя сводка: уроки на день."""
    lessons = [l for l in await db.lessons_on(tutor_id, d) if l["status"] == "planned"]
    head = f"☀️ <b>Доброе утро!</b>\n{fmt_day(d).capitalize()}"
    if not lessons:
        return head + "\n\nСегодня уроков нет 🎉"
    n = len(lessons)
    lines = [head, f"Сегодня {n} {plural(n, 'урок', 'урока', 'уроков')}:", ""]
    lines += [lesson_line(l) for l in lessons]
    return "\n".join(lines)


# ================= команды и главное меню =================

@router.message(CommandStart())
async def cmd_start(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    await db.ensure_tutor(msg.from_user.id, now().date())
    t = await db.tutor(msg.from_user.id)
    digest = (f"☀️ Каждое утро в <b>{t['digest_time']}</b> я пришлю список уроков на день."
              if t["digest_on"] else "🔕 Утренняя сводка сейчас выключена.")
    await msg.answer(
        "👋 <b>Привет! Я помогу вести расписание уроков.</b>\n\n"
        "<b>С чего начать:</b>\n"
        f"1. Нажми «{BTN_ADD}» и добавь ученика.\n"
        "2. Отметь кнопками, в какие дни и во сколько у вас уроки.\n"
        f"3. Готово! Все уроки — в «{BTN_SCHEDULE}», ближайшие — в «{BTN_TODAY}» и «{BTN_WEEK}».\n\n"
        f"{digest}\n"
        f"🌍 Часовой пояс: {tz_label(t['tz'])}\n"
        f"<i>Сводка и часовой пояс настраиваются в «{BTN_SETTINGS}». Подсказки — /help</i>",
        reply_markup=main_kb(),
    )
    await db.set_kb_version(msg.from_user.id, MENU_VERSION)


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=main_kb())


@router.message(Command("digest"))
async def cmd_digest(msg: Message, command: CommandObject, db: DB):
    t = parse_time(command.args or "")
    if not t:
        await msg.answer("Укажи время, например: <code>/digest 07:30</code>")
        return
    await db.ensure_tutor(msg.from_user.id, now().date())
    await db.set_digest(msg.from_user.id, now(), time=t, on=True)
    await msg.answer(f"Готово, сводка будет приходить в {t}.")


@router.message(Command("today"))
async def cmd_today(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_day(db, msg.from_user.id, now().date())
    await msg.answer(text, reply_markup=kb)


@router.message(F.text.in_({BTN_TODAY, BTN_TOMORROW}))
async def menu_day(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    d = now().date() + timedelta(days=0 if msg.text == BTN_TODAY else 1)
    text, kb = await render_day(db, msg.from_user.id, d)
    await msg.answer(text, reply_markup=kb)


# Декораторы стоят друг над другом: одна функция отвечает и на кнопку, и на команду из меню
@router.message(Command("week"))
@router.message(F.text == BTN_WEEK)
async def menu_week(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    start = now().date()
    lines, kb = [f"🗓 <b>Неделя</b>", ""], InlineKeyboardBuilder()
    students = {}  # id -> имя, ученики этой недели в порядке появления
    for i in range(7):
        d = start + timedelta(days=i)
        lessons = [l for l in await db.lessons_on(msg.from_user.id, d) if l["status"] == "planned"]
        lines.append(f"<b>{day_title(d, start)}</b>")
        lines += [f"     {lesson_line(l)}" for l in lessons] or ["     <i>нет уроков</i>"]
        lines.append("")
        for l in lessons:
            students.setdefault(l["student_id"], l["name"])
        kb.button(text=f"{WEEKDAYS[d.weekday()]} {d.day}", callback_data=DayCb(day=d.isoformat()))
    for sid, name in students.items():
        kb.button(text=f"🕒 {name}", callback_data=StudentCb(action="slots", id=sid))
    kb.adjust(4, 3, *([2] * ((len(students) + 1) // 2)))
    if students:
        lines.append(HINT.format("Дни — открыть день\n🕒 — расписание ученика"))
    await msg.answer("\n".join(lines).rstrip(), reply_markup=kb.as_markup())


@router.message(Command("students"))
@router.message(F.text == BTN_STUDENTS)
async def menu_students(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_students(db, msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


@router.message(Command("schedule"))
@router.message(F.text == BTN_SCHEDULE)
async def menu_schedule(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_schedule(db, msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(HELP_TEXT, reply_markup=help_kb())


@router.message(Command("about"))
async def cmd_about(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(ABOUT_TEXT, reply_markup=help_kb())


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def menu_settings(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    await db.ensure_tutor(msg.from_user.id, now().date())
    text, kb = await render_settings(db, msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


# ================= добавление ученика =================

@router.message(Command("add"))
@router.message(F.text == BTN_ADD)
async def add_start(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    await db.ensure_tutor(msg.from_user.id, now().date())
    await state.set_state(AddStudent.name)
    await msg.answer("Как зовут ученика?")


@router.message(AddStudent.name, F.text)
async def add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip()[:64])
    await state.set_state(AddStudent.subject)
    await msg.answer("📚 Какой предмет? Например, <i>английский</i> или <i>казахская литература</i>.\n"
                     "Можно пропустить — предмет добавляется и позже, в карточке ученика.",
                     reply_markup=skip_kb("subject"))


async def to_contact(target: Message, state: FSMContext):
    await state.set_state(AddStudent.contact)
    await target.answer("📞 Контакт: телефон (для кнопки WhatsApp) или @ник.", reply_markup=skip_kb("contact"))


async def to_price(target: Message, state: FSMContext):
    await state.set_state(AddStudent.price)
    await target.answer("💵 Стоимость одного урока — просто число, например <code>1500</code>. "
                        "Валюта выбирается на следующем шаге.", reply_markup=skip_kb("price"))


async def to_duration(target: Message, state: FSMContext, edit_cb: CallbackQuery | None = None):
    """Шаг «длительность урока»: новым сообщением или правкой сообщения с кнопками валют."""
    await state.set_state(AddStudent.duration)
    text = "⏱ Сколько длится один урок?"
    if edit_cb:
        await safe_edit(edit_cb, text, reply_markup=duration_kb("new"))
    else:
        await target.answer(text, reply_markup=duration_kb("new"))


async def after_price(target: Message, state: FSMContext, price: int):
    """После цены: если цена указана — выбор валюты, иначе сразу длительность."""
    await state.update_data(price=price)
    if price:
        await state.set_state(AddStudent.currency)
        await target.answer(f"Стоимость: <b>{price:,}</b>. В какой валюте?".replace(",", "\u00a0"),
                            reply_markup=currency_kb("new"))
    else:
        await to_duration(target, state)


@router.message(AddStudent.subject, F.text)
async def add_subject(msg: Message, state: FSMContext):
    text = msg.text.strip()
    await state.update_data(subject=None if text in SKIP else text[:64])
    await to_contact(msg, state)


@router.message(AddStudent.contact, F.text)
async def add_contact(msg: Message, state: FSMContext):
    text = msg.text.strip()
    await state.update_data(contact=None if text in SKIP else text[:64])
    await to_price(msg, state)


@router.message(AddStudent.price, F.text)
async def add_price(msg: Message, state: FSMContext):
    price = parse_price(msg.text)
    if price is None:
        await msg.answer("Нужно число, например <code>1500</code>, или кнопка «Пропустить».",
                         reply_markup=skip_kb("price"))
        return
    await after_price(msg, state, price)


@router.message(AddStudent.currency)
async def add_currency_text(msg: Message):
    await msg.answer("Выбери валюту кнопкой в сообщении выше 👆 (или /cancel — отмена).")


@router.message(AddStudent.duration)
async def add_duration_text(msg: Message):
    await msg.answer("Выбери длительность кнопкой в сообщении выше 👆 (или /cancel — отмена).")


@router.callback_query(SkipCb.filter())
async def cb_skip(cb: CallbackQuery, callback_data: SkipCb, state: FSMContext):
    step = callback_data.step
    if await state.get_state() != getattr(AddStudent, step).state:
        await cb.answer("Эта кнопка устарела", show_alert=False)
        return
    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # кнопка «Пропустить» больше не нужна
    except TelegramBadRequest:
        pass
    if step == "subject":
        await state.update_data(subject=None)
        await to_contact(cb.message, state)
    elif step == "contact":
        await state.update_data(contact=None)
        await to_price(cb.message, state)
    elif step == "price":
        await after_price(cb.message, state, 0)
    await cb.answer("Пропущено")


ADDED = "✅ Ученик добавлен. Теперь добавь регулярные уроки (если их нет — просто нажми «Готово»).\n\n"


@router.callback_query(CurCb.filter())
async def cb_currency(cb: CallbackQuery, callback_data: CurCb, db: DB, state: FSMContext):
    cd = callback_data
    if cd.code not in CURRENCIES:
        await cb.answer()
        return

    if cd.mode == "new":
        if await state.get_state() != AddStudent.currency.state:
            await cb.answer("Эта кнопка устарела — начни добавление заново", show_alert=True)
            return
        data = await state.get_data()
        await state.update_data(currency=cd.code)
        await to_duration(cb.message, state, edit_cb=cb)
        await cb.answer(f"Стоимость: {fmt_money(data['price'], cd.code)}")
        return

    # mode == "edit": новая цена передаётся в самой кнопке
    await state.clear()
    s = await db.student(cd.sid, cb.from_user.id)
    if not s or not s["active"]:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await db.set_price(s["id"], cd.price, cd.code)
    text, kb = await render_student(db, cb.from_user.id, s["id"])
    await safe_edit(cb, "✅ Стоимость обновлена\n\n" + text, reply_markup=kb)
    await cb.answer()


@router.callback_query(DurCb.filter())
async def cb_duration(cb: CallbackQuery, callback_data: DurCb, db: DB, state: FSMContext):
    cd = callback_data
    minutes = cd.minutes if cd.minutes in DURATIONS else 0

    if cd.mode == "new":
        if await state.get_state() != AddStudent.duration.state:
            await cb.answer("Эта кнопка устарела — начни добавление заново", show_alert=True)
            return
        data = await state.get_data()
        await state.clear()
        sid = await db.add_student(cb.from_user.id, data["name"], data.get("contact"), data.get("price", 0),
                                   data.get("currency", "RUB"), data.get("subject"), minutes)
        text, kb = await render_slot_editor(db, cb.from_user.id, sid)
        await safe_edit(cb, ADDED + text, reply_markup=kb)
        await cb.answer()
        return

    # mode == "edit"
    await state.clear()
    s = await db.student(cd.sid, cb.from_user.id)
    if not s or not s["active"]:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await db.set_duration(s["id"], minutes)
    text, kb = await render_student(db, cb.from_user.id, s["id"])
    await safe_edit(cb, "✅ Длительность обновлена\n\n" + text, reply_markup=kb)
    await cb.answer()


# ================= навигация по дням и урокам =================

@router.callback_query(DayCb.filter())
async def cb_day(cb: CallbackQuery, callback_data: DayCb, db: DB, state: FSMContext):
    await state.clear()  # нажатие кнопки отменяет незаконченный ввод текста
    text, kb = await render_day(db, cb.from_user.id, date.fromisoformat(callback_data.day))
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


@router.callback_query(LessonCb.filter())
async def cb_lesson(cb: CallbackQuery, callback_data: LessonCb, db: DB, state: FSMContext):
    await state.clear()
    lesson = await db.lesson(callback_data.id, cb.from_user.id)
    if not lesson:
        await cb.answer("Урок не найден", show_alert=True)
        return
    action = callback_data.action

    if action == "cancel":
        await db.set_lesson_status(lesson["id"], "cancelled")
    elif action == "restore":
        await db.set_lesson_status(lesson["id"], "planned")
    elif action == "remind":
        d = date.fromisoformat(lesson["day"])
        text = reminder_text(lesson["name"], d, lesson["time"])
        kb = None
        phone = whatsapp_phone(lesson["contact"])
        if phone:
            b = InlineKeyboardBuilder()
            b.button(text="Отправить в WhatsApp", url=f"https://wa.me/{phone}?text={quote(text)}")
            kb = b.as_markup()
        await cb.message.answer(f"Нажми на текст, чтобы скопировать:\n\n<code>{escape(text)}</code>",
                                reply_markup=kb)
        await cb.answer()
        return

    text, kb = await render_lesson(db, cb.from_user.id, lesson["id"])
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


# ================= карточка ученика =================

@router.callback_query(StudentCb.filter())
async def cb_student(cb: CallbackQuery, callback_data: StudentCb, db: DB, state: FSMContext):
    await state.clear()  # ветки ниже при необходимости задают новый шаг ввода
    action, sid = callback_data.action, callback_data.id

    if action == "new":
        await db.ensure_tutor(cb.from_user.id, now().date())
        await state.set_state(AddStudent.name)
        await cb.message.answer("Как зовут ученика?")
        await cb.answer()
        return

    if action == "list":
        text, kb = await render_students(db, cb.from_user.id)
        await safe_edit(cb, text, reply_markup=kb)
        await cb.answer()
        return

    s = await db.student(sid, cb.from_user.id)
    if not s or not s["active"]:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    name = escape(s["name"])

    if action == "slots":
        text, kb = await render_slot_editor(db, cb.from_user.id, sid)
        await cb.message.answer(text, reply_markup=kb)
    elif action == "price":
        await state.set_state(Ask.price)
        await state.update_data(target=sid)
        await cb.message.answer(f"Стоимость урока для <b>{name}</b> сейчас: "
                                f"{fmt_price(s['price'], s['currency'])}.\n\n"
                                "Напиши новую — просто число, например <code>1500</code>, "
                                "валюту выберешь следующим шагом.\n«-» — убрать стоимость. /cancel — отмена.")
    elif action == "subject":
        await state.set_state(Ask.subject)
        await state.update_data(target=sid)
        current = f"<b>{escape(s['subject'])}</b>" if s["subject"] else "не указан"
        await cb.message.answer(f"📚 Предмет у <b>{name}</b> сейчас: {current}\n\n"
                                "Напиши предмет, например <i>английский</i> или <i>казахская литература</i>.\n"
                                "«-» — убрать предмет. /cancel — отмена.")
    elif action == "duration":
        current = fmt_duration(s["duration"]) if s["duration"] else "не указана"
        await safe_edit(cb, f"⏱ Длительность урока у <b>{name}</b> сейчас: {current}\n\nВыбери новую:",
                        reply_markup=duration_kb("edit", s["duration"], sid))
    elif action == "contact":
        await state.set_state(Ask.contact)
        await state.update_data(target=sid)
        current = f"<code>{escape(fmt_phone(s['contact']))}</code>" if s["contact"] else "не указан"
        await cb.message.answer(f"📞 Контакт <b>{name}</b> сейчас: {current}\n\n"
                                "Напиши новый номер телефона (для кнопки WhatsApp) или @ник.\n"
                                "«-» — удалить контакт. /cancel — отмена.")
    elif action == "name":
        await state.set_state(Ask.name)
        await state.update_data(target=sid)
        await cb.message.answer(f"✏️ Сейчас ученик записан как <b>{name}</b>.\n\n"
                                "Напиши новое имя. /cancel — отмена.")
    elif action == "archive":
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Да, удалить", callback_data=StudentCb(action="archive_yes", id=sid))
        kb.button(text="◀️ Назад", callback_data=StudentCb(action="open", id=sid))
        await safe_edit(cb, f"Удалить ученика <b>{name}</b>?\n\n"
                            "Он пропадёт из списка учеников и из расписания.",
                                   reply_markup=kb.as_markup())
    elif action == "archive_yes":
        await db.archive_student(sid, now())
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Все ученики", callback_data=StudentCb(action="list", id=0))
        await safe_edit(cb, f"🗑 Ученик <b>{name}</b> удалён.", reply_markup=kb.as_markup())
    else:  # open
        text, kb = await render_student(db, cb.from_user.id, sid)
        await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


# ================= редактор расписания на кнопках =================

@router.callback_query(SlotCb.filter())
async def cb_slots(cb: CallbackQuery, callback_data: SlotCb, db: DB, state: FSMContext):
    await state.clear()
    cd = callback_data
    s = await db.student(cd.sid, cb.from_user.id)
    if not s or not s["active"]:
        await cb.answer("Ученик не найден", show_alert=True)
        return

    if cd.action in ("add", "wd", "h"):
        text, kb = render_picker(s["name"], cd)
        await safe_edit(cb, text, reply_markup=kb)
        await cb.answer()
        return

    note = None
    if cd.action == "del":
        await db.delete_slot(cd.slot, cd.sid, now())
        note = "Удалено"
    elif cd.action == "m":
        t = f"{cd.h:02d}:{cd.m:02d}"
        added = await db.add_slot(cd.sid, cd.wd, t, now())
        note = f"Добавлено: {WEEKDAYS[cd.wd]} {t}" if added else "Такой урок уже есть"

    if cd.action == "m" and cd.ret == "sched":
        text, kb = await render_schedule(db, cb.from_user.id)
    elif cd.action == "m" and cd.ret:
        text, kb = await render_day(db, cb.from_user.id, date.fromisoformat(cd.ret))
    else:
        text, kb = await render_slot_editor(db, cb.from_user.id, cd.sid)
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer(note)


@router.callback_query(PickCb.filter())
async def cb_pick(cb: CallbackQuery, callback_data: PickCb, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_pick(db, cb.from_user.id, callback_data)
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


@router.callback_query(SchedCb.filter())
async def cb_schedule(cb: CallbackQuery, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_schedule(db, cb.from_user.id)
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


# ================= настройки =================

async def render_settings(db: DB, tutor_id: int):
    t = await db.tutor(tutor_id)
    kb = InlineKeyboardBuilder()
    lines = ["⚙️ <b>Настройки</b>", ""]
    if t["digest_on"]:
        lines += ["☀️ <b>Утренняя сводка: включена</b>",
                  f"Приходит каждый день в <b>{t['digest_time']}</b>"]
        kb.button(text="🕗 Время сводки", callback_data=SetCb(action="time"))
        kb.button(text="🔕 Выключить", callback_data=SetCb(action="off"))
    else:
        lines += ["🔕 <b>Утренняя сводка: выключена</b>",
                  HINT.format(f"Если включить, каждый день в {t['digest_time']} "
                              "я буду присылать список уроков на день.")]
        kb.button(text="🔔 Включить сводку", callback_data=SetCb(action="on"))
    lines += ["", f"🌍 <b>Часовой пояс:</b> {tz_label(t['tz'])}",
              f"Сейчас у тебя: <b>{now().strftime('%H:%M')}</b>",
              HINT.format("Если время не совпадает с твоими часами — поменяй пояс.")]
    kb.button(text="🌍 Часовой пояс", callback_data=SetCb(action="tz"))
    kb.adjust(2, 1) if t["digest_on"] else kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


def render_tz_picker(current_zone: str):
    kb = InlineKeyboardBuilder()
    for key, (flag, title, zone) in TIMEZONES.items():
        local = datetime.now(ZoneInfo(zone)).strftime("%H:%M")
        mark = "✅ " if zone == current_zone else ""
        kb.button(text=f"{mark}{flag} {title} · {local}", callback_data=SetCb(action="tzset", tz=key))
    kb.button(text="◀️ Назад", callback_data=SetCb(action="open"))
    n = len(TIMEZONES)
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1)
    text = ("🌍 <b>Часовой пояс</b>\n\n"
            "Выбери, где ты находишься. Рядом с каждым вариантом — время там прямо сейчас: "
            "выбирай то, что совпадает с твоими часами.\n\n"
            + HINT.format("По этому времени приходит утренняя сводка и считаются «сегодня» и «завтра»."))
    return text, kb.as_markup()


def render_digest_time_picker(cd: SetCb, current: str):
    kb = InlineKeyboardBuilder()
    head = f"🕗 <b>Время утренней сводки</b>\nСейчас: {current}\n\n"
    if cd.action == "time":
        text = head + "Шаг 1 из 2: <b>час</b>"
        for h in DIGEST_HOURS:
            kb.button(text=f"{h:02d}:__", callback_data=SetCb(action="h", h=h))
        kb.button(text="◀️ Назад", callback_data=SetCb(action="open"))
        kb.adjust(4, 4, 1)
    else:  # "h"
        text = head + f"{cd.h:02d}:__\n\nШаг 2 из 2: <b>минуты</b>"
        for m in MINUTES:
            kb.button(text=f"{cd.h:02d}:{m:02d}", callback_data=SetCb(action="m", h=cd.h, m=m))
        kb.button(text="◀️ Назад", callback_data=SetCb(action="time"))
        kb.adjust(4, 1)
    return text, kb.as_markup()


@router.callback_query(SetCb.filter())
async def cb_settings(cb: CallbackQuery, callback_data: SetCb, db: DB, state: FSMContext):
    await state.clear()
    cd = callback_data
    await db.ensure_tutor(cb.from_user.id, now().date())
    note = None
    if cd.action == "tz":
        text, kb = render_tz_picker(await db.tutor_tz(cb.from_user.id))
        await safe_edit(cb, text, reply_markup=kb)
        await cb.answer()
        return
    if cd.action == "tzset" and cd.tz in TIMEZONES:
        flag, title, zone = TIMEZONES[cd.tz]
        await db.set_tz(cb.from_user.id, zone)
        CURRENT_TZ.set(ZoneInfo(zone))  # чтобы экран настроек сразу показал новое время
        note = f"Часовой пояс: {title}"
    if cd.action in ("time", "h"):
        t = await db.tutor(cb.from_user.id)
        text, kb = render_digest_time_picker(cd, t["digest_time"])
        await safe_edit(cb, text, reply_markup=kb)
        await cb.answer()
        return
    elif cd.action == "on":
        await db.set_digest(cb.from_user.id, now(), on=True)
        note = "Сводка включена 🔔"
    elif cd.action == "off":
        await db.set_digest(cb.from_user.id, now(), on=False)
        note = "Сводка выключена 🔕"
    elif cd.action == "m":
        t = f"{cd.h:02d}:{cd.m:02d}"
        await db.set_digest(cb.from_user.id, now(), time=t)
        note = f"Сводка будет приходить в {t}"
    text, kb = await render_settings(db, cb.from_user.id)
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer(note)


# ================= перенос урока на кнопках =================

def render_move_picker(lesson, cd: MoveCb):
    today = now().date()
    old_day = date.fromisoformat(lesson["day"])
    head = (f"🔁 <b>Перенос урока: {escape(lesson['name'])}</b>\n"
            f"Сейчас: {day_title(old_day, today)}, {lesson['time']}\n\n")
    kb = InlineKeyboardBuilder()

    if cd.action == "days":
        text = head + "Шаг 1 из 3: <b>новый день</b>"
        first = today + timedelta(days=cd.page * MOVE_DAYS)
        for i in range(MOVE_DAYS):
            d = first + timedelta(days=i)
            rel = {0: "Сегодня", 1: "Завтра"}.get((d - today).days)
            label = rel or f"{WEEKDAYS[d.weekday()].capitalize()} {d.day:02d}.{d.month:02d}"
            if d == old_day:
                label = f"• {label} •"
            kb.button(text=label, callback_data=MoveCb(action="day", id=cd.id, day=d.isoformat()))
        nav = 0
        if cd.page > 0:
            kb.button(text="◀️ Раньше", callback_data=MoveCb(action="days", id=cd.id, page=cd.page - 1))
            nav += 1
        if cd.page < 4:
            kb.button(text="Позже ▶️", callback_data=MoveCb(action="days", id=cd.id, page=cd.page + 1))
            nav += 1
        kb.button(text="◀️ Назад", callback_data=LessonCb(action="open", id=cd.id))
        kb.adjust(3, 3, 3, 3, 2, *([nav] if nav else []), 1)

    elif cd.action == "day":
        d = date.fromisoformat(cd.day)
        text = head + f"Новый день: <b>{day_title(d, today)}</b>\n\nШаг 2 из 3: <b>время</b>"
        oh, om = map(int, lesson["time"].split(":"))
        kb.button(text=f"✅ В то же время — {lesson['time']}",
                  callback_data=MoveCb(action="m", id=cd.id, day=cd.day, h=oh, m=om))
        for h in HOURS:
            kb.button(text=f"{h:02d}:__", callback_data=MoveCb(action="h", id=cd.id, day=cd.day, h=h))
        kb.button(text="◀️ Назад", callback_data=MoveCb(action="days", id=cd.id))
        kb.adjust(1, *([4] * ((len(HOURS) + 3) // 4)), 1)

    else:  # "h"
        d = date.fromisoformat(cd.day)
        text = (head + f"Новый день: <b>{day_title(d, today)}</b>, {cd.h:02d}:__\n\n"
                "Шаг 3 из 3: <b>минуты</b>")
        for m in MINUTES:
            kb.button(text=f"{cd.h:02d}:{m:02d}",
                      callback_data=MoveCb(action="m", id=cd.id, day=cd.day, h=cd.h, m=m))
        kb.button(text="◀️ Назад", callback_data=MoveCb(action="day", id=cd.id, day=cd.day))
        kb.adjust(4, 1)
    return text, kb.as_markup()


@router.callback_query(MoveCb.filter())
async def cb_move(cb: CallbackQuery, callback_data: MoveCb, db: DB, state: FSMContext):
    await state.clear()
    cd = callback_data
    lesson = await db.lesson(cd.id, cb.from_user.id)
    if not lesson:
        await cb.answer("Урок не найден", show_alert=True)
        return

    if cd.action in ("days", "day", "h"):
        text, kb = render_move_picker(lesson, cd)
        await safe_edit(cb, text, reply_markup=kb)
        await cb.answer()
        return

    # action == "m": перенос урока на выбранные день и время
    d = date.fromisoformat(cd.day)
    t = f"{cd.h:02d}:{cd.m:02d}"
    if f"{cd.day} {t}" < now_key():
        await cb.answer("Это время уже прошло — выбери другое", show_alert=True)
        return
    busy = [l["name"] for l in await db.lessons_on(cb.from_user.id, d)
            if l["time"] == t and l["status"] == "planned" and l["id"] != lesson["id"]]
    await db.move_lesson(lesson["id"], d, t)
    note = f"Перенесено: {WEEKDAYS[d.weekday()]} {d.day:02d}.{d.month:02d}, {t}"
    if busy:
        note += f"\n\n⚠️ В это время уже есть урок: {', '.join(busy)}"
    text, kb = await render_lesson(db, cb.from_user.id, lesson["id"])
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer(note, show_alert=bool(busy))


# ================= ответы на вопросы (Ask) =================

async def _target_student(msg: Message, db: DB, state: FSMContext):
    data = await state.get_data()
    return await db.student(data.get("target", 0), msg.from_user.id)


@router.message(Ask.price, F.text)
async def ask_price(msg: Message, db: DB, state: FSMContext):
    s = await _target_student(msg, db, state)
    price = parse_price(msg.text)
    if not s or price is None:
        await msg.answer("Нужно число, например <code>1500</code>, или «-». /cancel — отмена.")
        return
    await state.clear()
    if price:
        await msg.answer(f"Стоимость: <b>{price:,}</b>. В какой валюте?".replace(",", "\u00a0"),
                         reply_markup=currency_kb("edit", s["currency"], s["id"], price))
        return
    await db.set_price(s["id"], 0)
    text, kb = await render_student(db, msg.from_user.id, s["id"])
    await msg.answer("✅ Стоимость убрана\n\n" + text, reply_markup=kb)


@router.message(Ask.subject, F.text)
async def ask_subject(msg: Message, db: DB, state: FSMContext):
    s = await _target_student(msg, db, state)
    if not s:
        await state.clear()
        await msg.answer("Ученик не найден.")
        return
    text = msg.text.strip()
    subject = None if text in SKIP else text[:64]
    await state.clear()
    await db.set_subject(s["id"], subject)
    card, kb = await render_student(db, msg.from_user.id, s["id"])
    await msg.answer(("✅ Предмет сохранён" if subject else "✅ Предмет убран") + "\n\n" + card, reply_markup=kb)


@router.message(Ask.contact, F.text)
async def ask_contact(msg: Message, db: DB, state: FSMContext):
    s = await _target_student(msg, db, state)
    if not s:
        await state.clear()
        await msg.answer("Ученик не найден.")
        return
    text = msg.text.strip()
    contact = None if text in SKIP else text[:64]
    await state.clear()
    await db.set_contact(s["id"], contact)
    card, kb = await render_student(db, msg.from_user.id, s["id"])
    note = "✅ Контакт обновлён" if contact else "✅ Контакт удалён"
    if contact and not contact.startswith("@") and not whatsapp_phone(contact):
        note += "\n<i>Похоже, это не номер телефона — кнопка WhatsApp для него работать не будет.</i>"
    await msg.answer(f"{note}\n\n{card}", reply_markup=kb)


@router.message(Ask.name, F.text)
async def ask_name(msg: Message, db: DB, state: FSMContext):
    s = await _target_student(msg, db, state)
    new_name = msg.text.strip()[:64]
    if not s or not new_name or new_name in SKIP:
        await msg.answer("Напиши имя ученика. /cancel — отмена.")
        return
    await state.clear()
    await db.set_name(s["id"], new_name)
    card, kb = await render_student(db, msg.from_user.id, s["id"])
    await msg.answer("✅ Имя изменено\n\n" + card, reply_markup=kb)


# ================= всё остальное =================

# Команды для кнопки «Меню» слева от поля ввода (устанавливаются при запуске бота в bot.py)
BOT_COMMANDS = [
    ("start", "Главное меню"),
    ("schedule", "Расписание на неделю"),
    ("today", "Уроки на сегодня"),
    ("week", "Уроки на неделю"),
    ("students", "Ученики"),
    ("add", "Новый ученик"),
    ("settings", "Утренняя сводка и часовой пояс"),
    ("help", "Как пользоваться ботом"),
    ("about", "О боте"),
]
# Дополнительные команды, которые видит только владелец бота
ADMIN_COMMANDS = [
    ("stats", "Статистика"),
    ("backup", "Бэкап базы"),
    ("reply", "Ответить пользователю: /reply id текст"),
]

ABOUT_TEXT = (
    "📅 <b>Уроковед</b> — расписание уроков для репетиторов прямо в Telegram.\n\n"
    "• регулярные уроки по дням недели, с предметом и длительностью\n"
    "• перенос и отмена урока в пару нажатий\n"
    "• утренняя сводка по вашему часовому поясу\n"
    "• напоминания ученикам, в том числе через WhatsApp\n"
    "• стоимость урока в валютах СНГ\n\n"
    "Ваших учеников и расписание видите только вы. Удалить ученика можно в его карточке.\n\n"
    "Бот бесплатный и сейчас в бета-тесте. Вопрос или пожелание можно написать прямо сюда — "
    "сообщение получит разработчик."
)

HELP_TEXT = (
    "❓ <b>Как пользоваться ботом</b>\n\n"
    f"<b>{BTN_SCHEDULE}</b> — все регулярные уроки на неделю. Чтобы добавить урок: "
    "«➕ Добавить урок» → ученик → день → время.\n\n"
    f"<b>{BTN_TODAY}</b>, <b>{BTN_TOMORROW}</b>, <b>{BTN_WEEK}</b> — ближайшие уроки. "
    "Нажми на урок, чтобы перенести, отменить его или отправить напоминание ученику. "
    "«➕ Урок» в экране дня добавляет регулярный урок в этот день недели.\n\n"
    f"<b>{BTN_ADD}</b> — добавить ученика: имя, предмет, контакт, стоимость, длительность урока.\n\n"
    f"<b>{BTN_STUDENTS}</b> — карточки учеников: расписание, предмет, стоимость, телефон.\n\n"
    f"<b>{BTN_SETTINGS}</b> — утренняя сводка и часовой пояс."
)


def help_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_SCHEDULE, callback_data=SchedCb())
    kb.button(text=BTN_ADD, callback_data=StudentCb(action="new", id=0))
    kb.adjust(2)
    return kb.as_markup()


async def forward_to_admin(msg: Message):
    """Пересылка непонятого сообщения владельцу бота вместе с готовой командой для ответа."""
    if not ADMIN_ID or msg.from_user.id == ADMIN_ID:
        return
    u = msg.from_user
    nick = f"@{u.username}" if u.username else "без ника"
    body = escape(msg.text) if msg.text else f"<i>{msg.content_type}</i>"
    try:
        await msg.bot.send_message(
            ADMIN_ID,
            f"✉️ <b>Сообщение от пользователя</b>\n{escape(u.full_name)} ({nick}, id={u.id})\n\n{body}\n\n"
            f"Ответить: <code>/reply {u.id} </code>",
        )
        if not msg.text:
            await msg.copy_to(ADMIN_ID)  # фото, голосовое и т.п. — копия оригинала
    except Exception:
        logging.getLogger("actions").exception("Не удалось переслать сообщение владельцу")


@router.message()
async def fallback(msg: Message):
    """Ответ на сообщение, которое бот не распознал: подсказка с кнопками и пересылка владельцу."""
    await msg.answer(
        "Я понимаю кнопки 🙂 Главное:\n\n"
        f"<b>{BTN_SCHEDULE}</b> — все уроки на неделю, там же «➕ Добавить урок».\n"
        f"<b>{BTN_TODAY}</b> / <b>{BTN_WEEK}</b> — ближайшие уроки: нажми на урок, чтобы перенести или отменить.\n\n"
        "Все подсказки — /help\n"
        + (HINT.format("Сообщение передано разработчику.") if ADMIN_ID else ""),
        reply_markup=help_kb(),
    )
    await forward_to_admin(msg)
