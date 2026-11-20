import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import TZ

# Часовой пояс репетитора, который сейчас пользуется ботом (ставится в middleware).
CURRENT_TZ: ContextVar[ZoneInfo] = ContextVar("CURRENT_TZ", default=TZ)

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_SLOT_RE = re.compile(r"([а-яё]+)\.?\s+(?:в\s+)?(\d{1,2})[:.](\d{2})")

# Начала слов, по которым узнаём день недели: «сб», «суб», «субботу», «Суббота» и т.д.
_WEEKDAY_PREFIXES = [("пн", "пон"), ("вт",), ("ср",), ("чт", "чет"), ("пт", "пят"), ("сб", "суб"), ("вс", "вос")]


def parse_weekday(word: str) -> int | None:
    w = word.lower().rstrip(".")
    for i, prefixes in enumerate(_WEEKDAY_PREFIXES):
        if w.startswith(prefixes):
            return i
    return None


def now() -> datetime:
    """Текущее время в часовом поясе репетитора."""
    return datetime.now(CURRENT_TZ.get())


# Часовые пояса для кнопок: ключ -> (флаг, название, зона)
TIMEZONES = {
    "kgd": ("🇷🇺", "Калининград", "Europe/Kaliningrad"),
    "msk": ("🇷🇺", "Москва", "Europe/Moscow"),
    "sam": ("🇷🇺", "Самара", "Europe/Samara"),
    "ekb": ("🇷🇺", "Екатеринбург", "Asia/Yekaterinburg"),
    "oms": ("🇷🇺", "Омск", "Asia/Omsk"),
    "nsk": ("🇷🇺", "Новосибирск", "Asia/Novosibirsk"),
    "krs": ("🇷🇺", "Красноярск", "Asia/Krasnoyarsk"),
    "irk": ("🇷🇺", "Иркутск", "Asia/Irkutsk"),
    "yak": ("🇷🇺", "Якутск", "Asia/Yakutsk"),
    "vla": ("🇷🇺", "Владивосток", "Asia/Vladivostok"),
    "by": ("🇧🇾", "Беларусь", "Europe/Minsk"),
    "ua": ("🇺🇦", "Украина", "Europe/Kyiv"),
    "ge": ("🇬🇪", "Грузия", "Asia/Tbilisi"),
    "am": ("🇦🇲", "Армения", "Asia/Yerevan"),
    "az": ("🇦🇿", "Азербайджан", "Asia/Baku"),
    "kz": ("🇰🇿", "Казахстан", "Asia/Almaty"),
    "uz": ("🇺🇿", "Узбекистан", "Asia/Tashkent"),
    "kg": ("🇰🇬", "Киргизия", "Asia/Bishkek"),
    "tj": ("🇹🇯", "Таджикистан", "Asia/Dushanbe"),
}


def get_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else TZ
    except Exception:  # неизвестная зона — не падаем, берём по умолчанию
        return TZ


def tz_label(zone_name: str) -> str:
    """'Asia/Almaty' -> '🇰🇿 Казахстан (UTC+5)'"""
    for flag, title, zone in TIMEZONES.values():
        if zone == zone_name:
            break
    else:
        flag, title = "🌍", zone_name
    off = datetime.now(get_zone(zone_name)).utcoffset()
    hours = int(off.total_seconds() // 3600)
    mins = int(off.total_seconds() % 3600 // 60)
    utc = f"UTC{hours:+d}" + (f":{mins:02d}" if mins else "")
    return f"{flag} {title} ({utc})"


def now_key(dt: datetime | None = None) -> str:
    """Момент времени в формате, который сравнивается с day||' '||time в БД."""
    return (dt or now()).strftime("%Y-%m-%d %H:%M")


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def parse_time(text: str) -> str | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_slots(text: str) -> list[tuple[int, str]] | None:
    """'вт 17:00, чт 18:30' -> [(1, '17:00'), (3, '18:30')]"""
    found = _SLOT_RE.findall(text.lower())
    if not found:
        return None
    result = []
    for word, h, mi in found:
        wd = parse_weekday(word)
        t = parse_time(f"{h}:{mi}")
        if wd is None or t is None:
            return None
        result.append((wd, t))
    return result


def fmt_slots(slots) -> str:
    if not slots:
        return "нет регулярных уроков"
    return ", ".join(f"{WEEKDAYS[s['weekday']]} {s['time']}" for s in slots)


def parse_day(word: str, base: date) -> date | None:
    w = word.lower()
    if w == "сегодня":
        return base
    if w == "завтра":
        return base + timedelta(days=1)
    wd = parse_weekday(w)
    if wd is not None:
        return base + timedelta(days=(wd - base.weekday()) % 7)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", w)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (int(y) + 2000 if len(y) == 2 else int(y)) if y else base.year
    try:
        result = date(year, mo, d)
        if not y and result < base:  # «25.01» в декабре — значит, следующий год
            result = date(year + 1, mo, d)
    except ValueError:
        return None
    return result


def parse_when(text: str, base: date) -> tuple[date, str] | None:
    """'завтра 18:00', 'пт 17:30', '25.11 16:00' -> (date, 'HH:MM')"""
    parts = text.split()
    if len(parts) != 2:
        return None
    d = parse_day(parts[0], base)
    t = parse_time(parts[1])
    if d is None or t is None:
        return None
    return d, t


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_balance(balance: int, price: int) -> str:
    if balance > 0:
        return f"оплачено вперёд: {balance} {plural(balance, 'урок', 'урока', 'уроков')}"
    if balance == 0:
        return "оплаченные уроки закончились"
    debt = -balance
    money = f" ({debt * price} ₽)" if price else ""
    return f"долг: {debt} {plural(debt, 'урок', 'урока', 'уроков')}{money}"


def whatsapp_phone(contact: str | None) -> str | None:
    """Достаёт из контакта номер для ссылки wa.me (8XXXXXXXXXX -> 7XXXXXXXXXX)."""
    if not contact:
        return None
    digits = re.sub(r"\D", "", contact)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else None


# Валюты для кнопок: код -> (символ, флаг, название). Порядок = порядок кнопок.
CURRENCIES = {
    "RUB": ("₽", "🇷🇺", "Рубль"),
    "KZT": ("₸", "🇰🇿", "Тенге"),
    "BYN": ("Br", "🇧🇾", "Бел. рубль"),
    "UZS": ("сум", "🇺🇿", "Сум"),
    "KGS": ("сом", "🇰🇬", "Сом"),
    "TJS": ("смн", "🇹🇯", "Сомони"),
    "AMD": ("֏", "🇦🇲", "Драм"),
    "AZN": ("₼", "🇦🇿", "Манат"),
    "GEL": ("₾", "🇬🇪", "Лари"),
    "UAH": ("₴", "🇺🇦", "Гривна"),
    "USD": ("$", "🇺🇸", "Доллар"),
    "EUR": ("€", "🇪🇺", "Евро"),
}
DEFAULT_CURRENCY = "RUB"


def fmt_money(n: int, currency: str = DEFAULT_CURRENCY) -> str:
    """1200, 'KZT' -> '1 200 ₸' (неразрывные пробелы)"""
    symbol = CURRENCIES[currency][0] if currency in CURRENCIES else currency
    return f"{n:,}".replace(",", "\u00a0") + "\u00a0" + symbol


def fmt_totals(totals: dict[str, int]) -> str:
    """{'RUB': 12000, 'KZT': 16000} -> '12 000 ₽ + 16 000 ₸'"""
    order = list(CURRENCIES)
    items = sorted(totals.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    return " + ".join(fmt_money(v, k) for k, v in items if v)


def fmt_phone(contact: str | None) -> str:
    """'79537581257' -> '+7 953 758-12-57'; всё остальное (например, @ник) — как есть."""
    if not contact:
        return "—"
    digits = re.sub(r"\D", "", contact)
    if len(digits) == 11 and digits[0] in "78" and not contact.startswith("@"):
        d = "7" + digits[1:]
        return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}"
    return contact


def day_title(d: date, today: date) -> str:
    """'Сегодня · пятница, 20 ноября' / 'Завтра · …' / 'Понедельник, 23 ноября'"""
    rel = {0: "Сегодня", 1: "Завтра", -1: "Вчера"}.get((d - today).days)
    return f"{rel} · {fmt_day(d)}" if rel else fmt_day(d).capitalize()
