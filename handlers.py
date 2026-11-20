from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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

from db import DB
from utils import (CURRENT_TZ, CURRENCIES, TIMEZONES, WEEKDAYS, tz_label, WEEKDAYS_FULL, day_title, now_key, fmt_day, fmt_money, fmt_phone, plural, now, parse_slots,
                   parse_time, whatsapp_phone)

router = Router()

# ================= клавиатуры и callback-данные =================

BTN_TODAY, BTN_TOMORROW, BTN_WEEK = "📅 Сегодня", "📆 Завтра", "🗓 Неделя"
BTN_STUDENTS, BTN_ADD, BTN_SETTINGS = "👥 Ученики", "➕ Новый ученик", "⚙️ Настройки"
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_STUDENTS, BTN_ADD, BTN_SETTINGS}


def main_kb():
    kb = ReplyKeyboardBuilder()
    for text in (BTN_TODAY, BTN_TOMORROW, BTN_WEEK, BTN_STUDENTS, BTN_ADD, BTN_SETTINGS):
        kb.button(text=text)
    kb.adjust(3, 3)
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
    contact = State()
    price = State()
    currency = State()


class Ask(StatesGroup):
    """Одношаговые вопросы; что делать с ответом — лежит в data['target']."""
    slots = State()
    price = State()
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


def parse_price(text: str) -> int | None:
    """'1500', '1 500', '1500 ₽', '1500р' -> 1500; '-' -> 0; иначе None"""
    t = text.strip().lower()
    if t in SKIP:
        return 0
    # убираем валюту в любом виде («₽», «руб.», «тенге», «$»…) и пробелы-разделители тысяч
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
        name = escape(l["name"])
        if l["status"] == "cancelled":
            lines.append(f"<s>{l['time']}  {name}</s>  <i>отменён</i>")
        else:
            moved = "  🔁 <i>перенесён</i>" if is_moved(l) else ""
            lines.append(f"<b>{l['time']}</b>  {name}{moved}")
        mark = "❌ " if l["status"] == "cancelled" else "🔁 " if is_moved(l) else ""
        # слева — сам урок (отмена/перенос), справа — регулярное расписание ученика
        kb.button(text=f"{mark}{l['time']}  {l['name']}", callback_data=LessonCb(action="open", id=l["id"]))
        kb.button(text="🕒", callback_data=StudentCb(action="slots", id=l["student_id"]))
    kb.button(text="◀️", callback_data=DayCb(day=(d - timedelta(days=1)).isoformat()))
    kb.button(text="▶️", callback_data=DayCb(day=(d + timedelta(days=1)).isoformat()))
    kb.adjust(*([2] * len(lessons)), 2)
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
    text = (f"👤 <b>{escape(l['name'])}</b>\n\n"
            f"📅 {day_title(d, now().date())}\n"
            f"⏰ <b>{l['time']}</b>\n\n"
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


def slot_lines(slots) -> list[str]:
    return [f"<b>{WEEKDAYS[x['weekday']].capitalize()}</b>  {x['time']}" for x in slots]


async def render_student(db: DB, tutor_id: int, student_id: int):
    s = await db.student(student_id, tutor_id)
    if not s or not s["active"]:
        return None, None
    slots = await db.slots_of(student_id)
    phone = fmt_phone(s["contact"])
    contact = f"<code>{escape(phone)}</code>" if s["contact"] else "—"
    price = f"{fmt_money(s['price'], s['currency'])} за урок" if s["price"] else "стоимость не указана"
    lines = [
        f"👤 <b>{escape(s['name'])}</b>",
        "",
        f"📞 {contact}",
        f"💵 {price}",
        "",
        f"🗓 <b>Расписание</b>",
    ]
    if slots:
        lines += slot_lines(slots)
        n = len(slots)
        week = f"{n} {plural(n, 'урок', 'урока', 'уроков')} в неделю"
        if s["price"]:
            week += f" · {fmt_money(n * s['price'], s['currency'])}"
        lines += ["", HINT.format(week)]
    else:
        lines.append("<i>регулярных уроков нет</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="🕒 Расписание", callback_data=SlotCb(action="edit", sid=student_id))
    kb.button(text="💵 Стоимость", callback_data=StudentCb(action="price", id=student_id))
    kb.button(text="📞 Телефон", callback_data=StudentCb(action="contact", id=student_id))
    kb.button(text="✏️ Имя", callback_data=StudentCb(action="name", id=student_id))
    kb.button(text="🗑 Удалить", callback_data=StudentCb(action="archive", id=student_id))
    kb.button(text="◀️ Все ученики", callback_data=StudentCb(action="list", id=0))
    kb.adjust(2, 2, 2)
    return "\n".join(lines), kb.as_markup()


async def render_slot_editor(db: DB, tutor_id: int, sid: int):
    s = await db.student(sid, tutor_id)
    if not s or not s["active"]:
        return None, None
    slots = await db.slots_of(sid)
    lines = [f"🕒 <b>Расписание: {escape(s['name'])}</b>", ""]
    if slots:
        lines += slot_lines(slots)
        lines += ["", HINT.format("✖️ — удалить урок, ➕ — добавить новый")]
    else:
        lines += ["Регулярных уроков пока нет.", "", HINT.format("Нажми «➕ Добавить»")]
    kb = InlineKeyboardBuilder()
    for x in slots:
        kb.button(text=f"✖️ {WEEKDAYS[x['weekday']]} {x['time']}",
                  callback_data=SlotCb(action="del", sid=sid, slot=x["id"]))
    kb.button(text="➕ Добавить", callback_data=SlotCb(action="add", sid=sid))
    kb.button(text="⌨️ Текстом", callback_data=StudentCb(action="slots_text", id=sid))
    kb.button(text="✅ Готово", callback_data=StudentCb(action="open", id=sid))
    n = len(slots)
    kb.adjust(*([2] * (n // 2)), *([1] if n % 2 else []), 1, 2)  # уроки парами, «Добавить», «Текстом | Готово»
    return "\n".join(lines), kb.as_markup()


def render_picker(name: str, cd: SlotCb):
    """Шаги добавления: день недели -> час -> минуты."""
    kb = InlineKeyboardBuilder()
    head = f"🕒 <b>{escape(name)}</b> · новый урок\n\n"
    if cd.action == "add":
        text = head + "Шаг 1 из 3: <b>день недели</b>"
        for i, wd in enumerate(WEEKDAYS):
            kb.button(text=wd.capitalize(), callback_data=SlotCb(action="wd", sid=cd.sid, wd=i))
        kb.button(text="◀️ Назад", callback_data=SlotCb(action="edit", sid=cd.sid))
        kb.adjust(4, 3, 1)
    elif cd.action == "wd":
        text = head + f"{WEEKDAYS_FULL[cd.wd].capitalize()}\n\nШаг 2 из 3: <b>час</b>"
        for h in HOURS:
            kb.button(text=f"{h:02d}:__", callback_data=SlotCb(action="h", sid=cd.sid, wd=cd.wd, h=h))
        kb.button(text="◀️ Назад", callback_data=SlotCb(action="add", sid=cd.sid))
        kb.adjust(*([4] * ((len(HOURS) + 3) // 4)), 1)
    else:  # "h"
        text = head + f"{WEEKDAYS_FULL[cd.wd].capitalize()}, {cd.h:02d}:__\n\nШаг 3 из 3: <b>минуты</b>"
        for m in MINUTES:
            kb.button(text=f"{cd.h:02d}:{m:02d}",
                      callback_data=SlotCb(action="m", sid=cd.sid, wd=cd.wd, h=cd.h, m=m))
        kb.button(text="◀️ Назад", callback_data=SlotCb(action="wd", sid=cd.sid, wd=cd.wd))
        kb.adjust(4, 1)
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
        lines.append(f"<b>{escape(s['name'])}</b> — {info}")
        kb.button(text=s["name"], callback_data=StudentCb(action="open", id=s["id"]))
    kb.adjust(2)

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
    lines += [f"<b>{l['time']}</b>  {escape(l['name'])}" for l in lessons]
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
        f"3. Готово! Расписание — в «{BTN_TODAY}» и «{BTN_WEEK}».\n\n"
        f"{digest}\n"
        f"🌍 Часовой пояс: {tz_label(t['tz'])}\n"
        f"<i>Сводка и часовой пояс настраиваются в «{BTN_SETTINGS}».</i>",
        reply_markup=main_kb(),
    )


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


@router.message(F.text.in_({BTN_TODAY, BTN_TOMORROW}))
async def menu_day(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    d = now().date() + timedelta(days=0 if msg.text == BTN_TODAY else 1)
    text, kb = await render_day(db, msg.from_user.id, d)
    await msg.answer(text, reply_markup=kb)


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
        lines += [f"     {l['time']}  {escape(l['name'])}" for l in lessons] or ["     <i>нет уроков</i>"]
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


@router.message(F.text == BTN_STUDENTS)
async def menu_students(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    text, kb = await render_students(db, msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


@router.message(F.text == BTN_SETTINGS)
async def menu_settings(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    await db.ensure_tutor(msg.from_user.id, now().date())
    text, kb = await render_settings(db, msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


# ================= добавление ученика =================

@router.message(F.text == BTN_ADD)
async def add_start(msg: Message, db: DB, state: FSMContext):
    await state.clear()
    await db.ensure_tutor(msg.from_user.id, now().date())
    await state.set_state(AddStudent.name)
    await msg.answer("Как зовут ученика?")


@router.message(AddStudent.name, F.text)
async def add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip()[:64])
    await state.set_state(AddStudent.contact)
    await msg.answer("Контакт: телефон (для кнопки WhatsApp) или @ник. Если не нужно — «-»")


@router.message(AddStudent.contact, F.text)
async def add_contact(msg: Message, state: FSMContext):
    contact = None if msg.text.strip() in SKIP else msg.text.strip()[:64]
    await state.update_data(contact=contact)
    await state.set_state(AddStudent.price)
    await msg.answer("Стоимость одного урока — просто число, например <code>1500</code>. "
                     "Валюту выберешь на следующем шаге.\nЕсли не нужно — «-».")


@router.message(AddStudent.price, F.text)
async def add_price(msg: Message, db: DB, state: FSMContext):
    price = parse_price(msg.text)
    if price is None:
        await msg.answer("Нужно число, например <code>1500</code>, или «-».")
        return
    if price:
        await state.update_data(price=price)
        await state.set_state(AddStudent.currency)
        await msg.answer(f"Стоимость: <b>{price:,}</b>. В какой валюте?".replace(",", "\u00a0"), reply_markup=currency_kb("new"))
        return
    data = await state.get_data()
    await state.clear()
    sid = await db.add_student(msg.from_user.id, data["name"], data["contact"], 0)
    text, kb = await render_slot_editor(db, msg.from_user.id, sid)
    await msg.answer(ADDED + text, reply_markup=kb)


@router.message(AddStudent.currency)
async def add_currency_text(msg: Message):
    await msg.answer("Выбери валюту кнопкой в сообщении выше 👆 (или /cancel — отмена).")


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
        await state.clear()
        sid = await db.add_student(cb.from_user.id, data["name"], data["contact"], data["price"], cd.code)
        text, kb = await render_slot_editor(db, cb.from_user.id, sid)
        await safe_edit(cb, ADDED + text, reply_markup=kb)
        await cb.answer(f"Стоимость: {fmt_money(data['price'], cd.code)}")
        return

    # mode == "edit": новая цена уже в кнопке
    s = await db.student(cd.sid, cb.from_user.id)
    if not s or not s["active"]:
        await cb.answer("Ученик не найден", show_alert=True)
        return
    await db.set_price(s["id"], cd.price, cd.code)
    text, kb = await render_student(db, cb.from_user.id, s["id"])
    await safe_edit(cb, "✅ Стоимость обновлена\n\n" + text, reply_markup=kb)
    await cb.answer()


# ================= навигация по дням и урокам =================

@router.callback_query(DayCb.filter())
async def cb_day(cb: CallbackQuery, callback_data: DayCb, db: DB):
    text, kb = await render_day(db, cb.from_user.id, date.fromisoformat(callback_data.day))
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer()


@router.callback_query(LessonCb.filter())
async def cb_lesson(cb: CallbackQuery, callback_data: LessonCb, db: DB, state: FSMContext):
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
    action, sid = callback_data.action, callback_data.id

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
    elif action == "slots_text":
        await state.set_state(Ask.slots)
        await state.update_data(target=sid)
        slots = await db.slots_of(sid)
        current = ", ".join(f"{WEEKDAYS[x['weekday']]} {x['time']}" for x in slots)
        current_line = (f"Сейчас: <code>{current}</code>\n(нажми, чтобы скопировать, и поправь)\n\n"
                        if current else "Регулярных уроков сейчас нет.\n\n")
        await cb.message.answer(f"Расписание: <b>{name}</b>\n{current_line}"
                                "Отправь новое целиком, например <code>вт 17:00, сб 12:00</code>. "
                                "«-» — без регулярных уроков.\n"
                                "Прошедшие уроки не изменятся. /cancel — отмена.")
    elif action == "price":
        await state.set_state(Ask.price)
        await state.update_data(target=sid)
        await cb.message.answer(f"Стоимость урока для <b>{name}</b> сейчас: "
                                f"{fmt_price(s['price'], s['currency'])}.\n\n"
                                "Напиши новую — просто число, например <code>1500</code>, "
                                "валюту выберешь следующим шагом.\n«-» — убрать стоимость. /cancel — отмена.")
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
async def cb_slots(cb: CallbackQuery, callback_data: SlotCb, db: DB):
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

    text, kb = await render_slot_editor(db, cb.from_user.id, cd.sid)
    await safe_edit(cb, text, reply_markup=kb)
    await cb.answer(note)


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
async def cb_settings(cb: CallbackQuery, callback_data: SetCb, db: DB):
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
async def cb_move(cb: CallbackQuery, callback_data: MoveCb, db: DB):
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

    # action == "m": переносим
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


@router.message(Ask.slots, F.text)
async def ask_slots(msg: Message, db: DB, state: FSMContext):
    s = await _target_student(msg, db, state)
    text = msg.text.strip()
    slots = [] if text in SKIP else parse_slots(text)
    if not s or slots is None:
        await msg.answer("Не понял. Формат: <code>вт 17:00, чт 18:30</code>. /cancel — отмена.")
        return
    await state.clear()
    await db.replace_slots(s["id"], slots, now())
    text, kb = await render_student(db, msg.from_user.id, s["id"])
    await msg.answer("✅ Расписание обновлено\n\n" + text, reply_markup=kb)


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

@router.message()
async def fallback(msg: Message):
    """Неизвестный текст (например, кнопка из старой версии меню) — присылаем актуальное меню."""
    await msg.answer("Не понял 🤔 Выбери действие в меню ниже.", reply_markup=main_kb())
