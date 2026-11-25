"""Лог действий пользователей: кто, что написал или какую кнопку нажал."""
import logging
import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from utils import CURRENT_TZ, get_zone

log = logging.getLogger("actions")


def who(user) -> str:
    nick = f"@{user.username}" if user.username else "без ника"
    return f"{user.full_name} ({nick}, id={user.id})"


def pressed_button(cb: CallbackQuery) -> str | None:
    """Текст нажатой кнопки — ищем его в клавиатуре сообщения."""
    markup = cb.message.reply_markup if cb.message else None
    if not markup:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data == cb.data:
                return button.text
    return None


def describe(event) -> str | None:
    if isinstance(event, Message):
        if event.text:
            return f"написал: «{event.text[:200]}»"
        return f"прислал: {event.content_type}"
    if isinstance(event, CallbackQuery):
        text = pressed_button(event)
        return f"нажал [{text}] ({event.data})" if text else f"нажал кнопку ({event.data})"
    return None


class ActionLogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        action = describe(event)
        user = getattr(event, "from_user", None)
        if action is None or user is None:
            return await handler(event, data)

        step = data.get("raw_state")  # шаг диалога, например AddStudent:name
        if step:
            action += f" | шаг: {step}"

        started = time.monotonic()
        try:
            result = await handler(event, data)
        except Exception:
            log.exception("%s — %s — ОШИБКА", who(user), action)
            raise
        log.info("%s — %s (%.0f мс)", who(user), action, (time.monotonic() - started) * 1000)
        return result


class TutorTimezoneMiddleware(BaseMiddleware):
    """На время обработки ставит часовой пояс репетитора — now() в хендлерах становится местным."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        db = data.get("db")
        if user is None or db is None:
            return await handler(event, data)
        tutor = await db.tutor(user.id)
        if tutor and tutor["blocked"]:
            # человек снова пишет боту — значит, разблокировал: возвращаем сводки
            await db.set_blocked(user.id, False)
            log.info("%s снова пользуется ботом — снята отметка «заблокировал»", who(user))
        token = CURRENT_TZ.set(get_zone(tutor["tz"] if tutor else None))
        try:
            return await handler(event, data)
        finally:
            CURRENT_TZ.reset(token)


class MenuUpdateMiddleware(BaseMiddleware):
    """После обработки события один раз отправляет обновлённое главное меню тем,
    у кого в Telegram осталась клавиатура старой версии (Telegram хранит её до замены ботом)."""
    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        user = getattr(event, "from_user", None)
        db = data.get("db")
        if user is None or db is None:
            return result
        from handlers import BTN_SCHEDULE, MENU_VERSION, main_kb  # импорт здесь: handlers загружается позже
        tutor = await db.tutor(user.id)
        if tutor and tutor["kb_version"] < MENU_VERSION:
            await db.set_kb_version(user.id, MENU_VERSION)
            try:
                await data["bot"].send_message(
                    user.id,
                    f"🆕 В меню появилась кнопка «{BTN_SCHEDULE}»: там все уроки на неделю "
                    "и «➕ Добавить урок».",
                    reply_markup=main_kb(),
                )
            except Exception:
                log.exception("Не удалось отправить обновлённое меню %s", who(user))
        return result
