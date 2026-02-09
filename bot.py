import asyncio

import sqlite3
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional
from dotenv import load_dotenv
import os
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import timedelta, date as dt_date
from aiogram.exceptions import TelegramRetryAfter



from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

import logging
import traceback
import asyncio
from logging import Handler
from collections import deque
from datetime import datetime

logging.info("Logger initialized")



# ---------- ПАГИНАЦИЯ ----------

PAGE_SIZE = 20  # Элементов на странице


class RescheduleStates(StatesGroup):
    choosing_student = State()
    choosing_lesson = State()
    entering_date = State()
    entering_time = State()
    confirming = State()

class Paginator:
    """Утилита для работы с пагинацией"""

    @staticmethod
    def parse_callback_data(callback_data: str):
        ...
        # у тебя уже есть

    @staticmethod
    def get_page(items, page: int = 0, page_size: int = PAGE_SIZE):
        """Возвращает (page_items, current_page, total_pages, page_size)"""
        if items is None:
            items = []

        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))

        start = page * page_size
        end = start + page_size
        return items[start:end], page, total_pages, page_size

    @staticmethod
    def create_pagination_keyboard(
        current_page: int,
        total_pages: int,
        prefix: str,
        data: str = "",
        show_info: bool = True
    ):
        """Клавиатура навигации страницами: <prefix>_page_{page}_{data}"""
        if total_pages <= 1:
            return None

        builder = InlineKeyboardBuilder()
        row = []

        if current_page > 0:
            cb = f"{prefix}_page_{current_page - 1}"
            if data:
                cb += f"_{data}"
            row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=cb))

        if show_info:
            row.append(InlineKeyboardButton(
                text=f"{current_page + 1}/{total_pages}",
                callback_data="page_info"
            ))

        if current_page < total_pages - 1:
            cb = f"{prefix}_page_{current_page + 1}"
            if data:
                cb += f"_{data}"
            row.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=cb))

        builder.row(*row)
        return builder.as_markup()


load_dotenv()


API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

# Несколько преподавателей / админов
TEACHER_IDS = {
    # 814870211, # твой ID
    5629840688,
}


logging.basicConfig(level=logging.INFO)

# ===== In-memory log buffer (last N lines) =====
LOG_BUFFER = deque(maxlen=400)          # сколько строк храним
LOG_LEVEL_FOR_BUFFER = logging.INFO     # что складываем в буфер
LOG_TAIL_ENABLED = False                # слать новые логи в TG в реальном времени или нет


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;")
              .replace("<", "&lt;")
              .replace(">", "&gt;"))


class BufferingTelegramHandler(Handler):
    """
    1) Всегда пишет логи в кольцевой буфер (если record.levelno >= LOG_LEVEL_FOR_BUFFER)
    2) Опционально "tail": шлёт новые записи в TG всем админам из TEACHER_IDS
    """
    def __init__(self, bot: Bot, admin_ids: set[int], level=logging.DEBUG):
        super().__init__(level)
        self.bot = bot
        self.admin_ids = list(admin_ids)
        self._sending = False  # защита от рекурсии

    async def _send_to_admins(self, text: str):
        # ограничение телеграма — режем
        if len(text) > 3500:
            text = text[:3500] + "\n…(truncated)"
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    f"🧾 <b>LOG TAIL</b>\n<pre>{_escape_html(text)}</pre>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    def emit(self, record: logging.LogRecord):
        global LOG_BUFFER, LOG_LEVEL_FOR_BUFFER, LOG_TAIL_ENABLED

        try:
            msg = self.format(record)
        except Exception:
            return

        # 1) всегда в буфер (с нужного уровня)
        if record.levelno >= LOG_LEVEL_FOR_BUFFER:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            LOG_BUFFER.append(f"{ts} | {record.levelname} | {record.name}\n{msg}")

        # 2) tail по желанию
        if LOG_TAIL_ENABLED and not self._sending:
            try:
                self._sending = True
                asyncio.create_task(self._send_to_admins(msg))
            finally:
                self._sending = False


def setup_buffered_logging(bot: Bot):
    root = logging.getLogger()

    h = BufferingTelegramHandler(bot, TEACHER_IDS, level=logging.DEBUG)
    h.setFormatter(logging.Formatter("%(message)s"))

    root.addHandler(h)
    # общий уровень логгера можешь оставить INFO
    root.setLevel(logging.INFO)

class TelegramLogHandler(Handler):
    """
    Лог-хэндлер, который отправляет сообщения в Telegram всем админам из TEACHER_IDS.
    Работает в event loop через asyncio.create_task.
    """
    def __init__(self, bot: Bot, admin_ids: set[int], level=logging.ERROR):
        super().__init__(level)
        self.bot = bot
        self.admin_ids = list(admin_ids)
        self._sending = False  # защита от рекурсии

    async def _send(self, text: str):
        # режем слишком длинные сообщения
        if len(text) > 3500:
            text = text[:3500] + "\n…(truncated)"

        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    f"🐞 <b>BOT LOG</b>\n<pre>{text}</pre>",
                    parse_mode="HTML"
                )
            except Exception:
                # тут не логируем, чтобы не уйти в рекурсию
                pass

    def emit(self, record: logging.LogRecord):
        if self._sending:
            return
        try:
            msg = self.format(record)
            self._sending = True
            asyncio.create_task(self._send(msg))
        finally:
            self._sending = False


def setup_telegram_logging(bot: Bot):
    # root logger
    root = logging.getLogger()
    tg_handler = TelegramLogHandler(bot, TEACHER_IDS, level=logging.ERROR)
    tg_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s\n%(message)s"
    ))
    root.addHandler(tg_handler)
    root.setLevel(logging.INFO)



bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
setup_buffered_logging(bot)
logging.info("🧾 Buffered logging ENABLED")



DB_PATH = "data/LEA_it_bot.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

BACK_TEXT = "⬅️ Назад"
YES_TEXT = "✅ Да"
FEEDBACK_TEXT = "💡 Предложения и исправления"
ADMIN_FEEDBACK_TEXT = "🛠️ Замечания"
PAY_PREFIX = "pay_"
BACK_CALLBACK = "back_to_history"
DELETE_SLOT_PREFIX = "delete_slot_"
DELETE_SLOT_CONFIRM_PREFIX = "delete_confirm_"
DONE_LESSON_PREFIX = "done_lesson_"
CANCEL_LESSON_PREFIX = "cancel_lesson_"
DELETE_STUDENT_PREFIX = "delete_student_"
CONFIRM_DELETE_STUDENT_PREFIX = "confirm_delete_student_"
APPROVE_REQUEST_PREFIX = "approve_req_"
REJECT_REQUEST_PREFIX = "reject_req_"
DISPUTE_PREFIX = "dispute_"
EDIT_OVERRIDE_PREFIX = "edit_override_"
DELETE_OVERRIDE_PREFIX = "delete_override_"
RESCHEDULE_OVERRIDE_PREFIX = "reschedule_override_"
EDIT_HISTORY_PREFIX = "edit_history_"
DELETE_HISTORY_PREFIX = "delete_history_"
EDIT_HISTORY_FIELD_PREFIX = "edit_field_"
ADMIN_HW_STUDENT_PREFIX = "adminhw_student_"
ADMIN_HW_PAGE_PREFIX = "adminhw_page_"
ADMIN_HW_PICK_PREFIX = "adminhw_pick_"          # pick homework_id
ADMIN_HW_TOGGLE_PREFIX = "adminhw_toggle_"      # toggle homework_id
ADMIN_HW_DELETE_PREFIX = "adminhw_delete_"      # delete homework_id
ADMIN_HW_EDIT_PREFIX = "adminhw_edit_"          # edit homework_id
ADMIN_HW_BACK_TO_LIST = "adminhw_back_list_"    # back to list for student_id
TOPIC_DELETE_PREFIX = "topic_delete_"
SET_TOPIC_WRITE_PREFIX = "set_topic_write_"
SET_TOPIC_DEL_PREFIX = "set_topic_del_"
SET_TOPIC_DEL_OK_PREFIX = "set_topic_del_ok_"
SET_TOPIC_DEL_NO_PREFIX = "set_topic_del_no_"
SET_TOPICS_BACK = "set_topics_back"




DAY_NAMES = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

# Константы для callback_data

class AdminHomeworkStates(StatesGroup):
    choosing_student = State()
    choosing_homework = State()
    editing_text = State()

class SetTopicStates(StatesGroup):
    waiting_topic = State()
    selecting_lesson = State()

class SetPriceStates(StatesGroup):
    choosing_student = State()
    waiting_price = State()

class SetSlotStates(StatesGroup):
    waiting_user = State()      # выбор ученика (inline)
    waiting_weekday = State()   # ввод дня недели (1-7)
    waiting_time = State()      # ввод времени (HH:MM)


# В начало кода добавляем
USER_PAGE_SIZES = {}  # telegram_id -> page_size

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import date, timedelta, datetime, time as dtime

from aiogram.fsm.state import StatesGroup, State


class MassCancelAllStates(StatesGroup):
    entering_start_date = State()
    entering_end_date = State()
    confirming = State()


class MassCancelAllStates(StatesGroup):
    choosing_student = State()
    choosing_lesson = State()
    entering_start_date = State()
    entering_end_date = State()
    confirming = State()


class DeleteUserStates(StatesGroup):
    choosing_kind = State()     # кого удаляем: ученик / родитель
    choosing_student = State()  # выбор ученика
    choosing_parent = State()   # выбор родителя
    confirming = State()        # подтверждение удаления


# --- Клавиатура "какое занятие отменяем" ---
def build_cancel_lessons_keyboard(lessons):
    """
    lessons: список словарей вида
      {"kind": "weekly", "weekly_lesson_id": int, "date": date, "time": "HH:MM", "label": str}
      {"kind": "extra",  "extra_lesson_id": int, "date": date, "time": "HH:MM", "label": str}
    """
    kb = InlineKeyboardBuilder()
    for it in lessons:
        kb.button(text=it["label"], callback_data=it["cb"])
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_from_cancel"))
    return kb.as_markup()


def _collect_upcoming_lessons_for_cancel(student_id: int, days_ahead: int = 45):
    """
    Собираем ближайшие занятия, чтобы было что отменять.
    Если у тебя уже есть готовая функция "get_upcoming_lessons_for_student" — лучше используй её.
    """
    result = []
    today = date.today()

    # 1) Регулярные слоты -> считаем ближайшие даты на горизонте days_ahead
    weekly = get_weekly_lessons_for_student(student_id, active_only=True)
    for w in weekly:
        hh, mm = map(int, w["time"].split(":"))
        t = dtime(hh, mm)

        for d in range(0, days_ahead + 1):
            dt = today + timedelta(days=d)
            if dt.weekday() == w["weekday"]:
                # проверим, нет ли уже override cancel на эту дату
                # (если есть — пропускаем)
                cur = conn.cursor()
                cur.execute(
                    "SELECT change_kind FROM lesson_overrides WHERE weekly_lesson_id = ? AND date = ?",
                    (w["id"], dt.isoformat())
                )
                ov = cur.fetchone()
                if ov and ov["change_kind"] == "cancel":
                    continue

                label = f"❌ {dt.strftime('%d.%m.%Y')} {w['time']} (слот)"
                result.append({
                    "kind": "weekly",
                    "weekly_lesson_id": w["id"],
                    "date": dt,
                    "time": w["time"],
                    "cb": f"cancel_pick_weekly_{w['id']}_{dt.isoformat()}",
                    "label": label
                })
                break  # берём ближайшую дату по этому слоту

    # 2) Доп. занятия (если такая таблица есть)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, date, time
            FROM extra_lessons
            WHERE student_id = ?
              AND date >= ?
            ORDER BY date, time
            LIMIT 30
            """,
            (student_id, today.isoformat())
        )
        extras = cur.fetchall()
        for e in extras:
            label = f"❌ {date.fromisoformat(e['date']).strftime('%d.%m.%Y')} {e['time']} (доп.)"
            result.append({
                "kind": "extra",
                "extra_lesson_id": e["id"],
                "date": date.fromisoformat(e["date"]),
                "time": e["time"],
                "cb": f"cancel_pick_extra_{e['id']}",
                "label": label
            })
    except Exception:
        pass

    # сортировка
    def key(x):
        return (x["date"], parse_time_str(x["time"]))

    result.sort(key=key)

    return result

def delete_user_completely(telegram_id: int):
    """
    Полное удаление пользователя:
    - его роль (user_roles)
    - его заявки родителя (parent_requests)
    - его привязки родителя (parent_links)
    - если он был учеником: удалить ученика и всё по student_id (через delete_student_by_id)
    """
    cur = conn.cursor()

    # 1) Если это ученик - найдём его student_id и удалим как ученика
    cur.execute("SELECT id FROM students WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row:
        student_id = row["id"]
        delete_student_by_id(student_id)  # сейчас допилим её ниже

    # 2) Если это родитель - убрать все привязки/заявки
    cur.execute("DELETE FROM parent_links WHERE parent_telegram_id = ?", (telegram_id,))
    cur.execute("DELETE FROM parent_requests WHERE parent_telegram_id = ?", (telegram_id,))

    # 3) Самое главное: стереть роль
    cur.execute("DELETE FROM user_roles WHERE telegram_id = ?", (telegram_id,))

    conn.commit()


def get_parent_students(parent_tg_id: int):
    cur = conn.cursor()
    cur.execute("""
        SELECT s.*
        FROM parent_links pl
        JOIN students s ON s.id = pl.student_id
        WHERE pl.parent_telegram_id = ? AND pl.is_active = 1
        ORDER BY s.full_name, s.username
    """, (parent_tg_id,))
    return cur.fetchall()

def parent_menu_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📅 Расписание ученика")],
        [KeyboardButton(text="📚 Домашка ученика")],
        [KeyboardButton(text="🧾 История занятий ученика")],
        [KeyboardButton(text=FEEDBACK_TEXT)],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)



def get_main_menu(message: Message) -> ReplyKeyboardMarkup:
    if is_teacher(message):
        return main_menu_keyboard(True)
    if is_parent(message):
        return parent_menu_keyboard()
    return main_menu_keyboard(False)

def get_main_menu_for_user_id(user_id: int) -> ReplyKeyboardMarkup:
    if user_id in TEACHER_IDS:
        return main_menu_keyboard(True)
    if len(get_parent_students(user_id)) > 0:  # parent
        return parent_menu_keyboard()
    return main_menu_keyboard(False)  # student


@router.message(Command("bind_parent"))
async def cmd_bind_parent(message: Message):
    if not is_teacher(message):
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /bind_parent <родитель> <ученик>\nНапр: /bind_parent @mama @petya")
        return

    parent_key = parts[1]
    student_key = parts[2]

    parent = get_student_by_user_key(parent_key)  # у тебя уже есть функция для @username/telegram_id
    student = get_student_by_user_key(student_key)

    # ВАЖНО: parent у тебя сейчас ищется в students — это не подходит для родителей.
    # Лучше сделать отдельный поиск по telegram_id/username через Telegram нельзя.
    # Поэтому практично: привязывать по числовому telegram_id родителя.

    await message.answer("Сделаем правильно: привязку родителя лучше делать по parent_telegram_id (числом).")


def is_parent(message: Message) -> bool:
    return len(get_parent_students(message.from_user.id)) > 0


# --- Пагинация списка учеников (если используешь cancel_page_) ---
@router.callback_query(lambda c: c.data.startswith("cancel_page_"))
async def cancel_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("cancel_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    # важно: совпадает с callback_data cancel_student_{id}_{page}
    keyboard, _ = create_cancel_students_keyboard(students, page=page)  # :contentReference[oaicite:1]{index=1}
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    if message.from_user.id not in TEACHER_IDS:
        return

    parts = (message.text or "").split()
    n = 80
    if len(parts) > 1 and parts[1].isdigit():
        n = max(1, min(300, int(parts[1])))

    lines = list(LOG_BUFFER)[-n:]
    text = "\n\n".join(lines) if lines else "Лог-буфер пуст."

    # телега ограничивает размер — режем
    if len(text) > 3500:
        text = text[-3500:]
        text = "…(tail)\n" + text

    await message.answer(f"🧾 <b>Последние логи ({len(lines)})</b>\n<pre>{_escape_html(text)}</pre>",
                         parse_mode="HTML")


@router.message(Command("loglevel"))
async def cmd_loglevel(message: Message):
    global LOG_LEVEL_FOR_BUFFER
    if message.from_user.id not in TEACHER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: /loglevel DEBUG|INFO|WARNING|ERROR")
        return

    lvl = parts[1].upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    if lvl not in mapping:
        await message.answer("Неверный уровень. DEBUG|INFO|WARNING|ERROR")
        return

    LOG_LEVEL_FOR_BUFFER = mapping[lvl]
    await message.answer(f"✅ Теперь в буфер складываем начиная с уровня: {lvl}")


@router.message(Command("logtail"))
async def cmd_logtail(message: Message):
    global LOG_TAIL_ENABLED
    if message.from_user.id not in TEACHER_IDS:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await message.answer("Использование: /logtail on|off")
        return

    LOG_TAIL_ENABLED = (parts[1].lower() == "on")
    await message.answer(f"✅ LOG TAIL: {'включен' if LOG_TAIL_ENABLED else 'выключен'}")


# --- Выбор ученика для отмены ---
@router.callback_query(lambda c: c.data.startswith("cancel_student_"))
async def cancel_select_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    await state.update_data(cancel_student_id=student_id)

    lessons = _collect_upcoming_lessons_for_cancel(student_id)
    if not lessons:
        await callback_query.message.edit_text("Не нашёл ближайших занятий для отмены.")
        await callback_query.answer()
        return

    await callback_query.message.edit_text(
        "❌ <b>Отмена занятия</b>\n\nВыберите, какое занятие отменяем:",
        parse_mode="HTML",
        reply_markup=build_cancel_lessons_keyboard(lessons)
    )
    await callback_query.answer()


# --- Отмена регулярного (разово) через override cancel ---
@router.callback_query(lambda c: c.data.startswith("cancel_pick_weekly_"))
async def cancel_pick_weekly(callback_query: CallbackQuery):
    # cancel_pick_weekly_{weekly_id}_{YYYY-MM-DD}
    parts = callback_query.data.split("_")
    weekly_id = int(parts[3])
    target_date = date.fromisoformat(parts[4])

    wl = get_weekly_lesson_by_id(weekly_id)
    if not wl:
        await callback_query.answer("Слот не найден")
        return

    hh, mm = map(int, wl["time"].split(":"))
    normal_time = dtime(hh, mm)

    # create_lesson_override уже поддерживает change_kind="cancel" (см. логику approve) :contentReference[oaicite:2]{index=2}
    create_lesson_override(
        weekly_lesson_id=weekly_id,
        override_date=target_date,
        new_time=normal_time,
        change_kind="cancel",
        original_date=None,
        original_time=None
    )

    await callback_query.message.edit_text(
        f"✅ Занятие {target_date.strftime('%d.%m.%Y')} в {wl['time']} отменено (разово)."
    )
    await callback_query.answer("Отменено")


# --- Отмена доп. занятия (если есть таблица extra_lessons) ---
@router.callback_query(lambda c: c.data.startswith("cancel_pick_extra_"))
async def cancel_pick_extra(callback_query: CallbackQuery):
    extra_id = int(callback_query.data.split("_")[3])

    cur = conn.cursor()
    cur.execute("DELETE FROM extra_lessons WHERE id = ?", (extra_id,))
    conn.commit()

    await callback_query.message.edit_text("✅ Дополнительное занятие отменено.")
    await callback_query.answer("Отменено")


@router.message(Command("set_page_size"))
async def cmd_set_page_size(message: Message, state: FSMContext):
    """Настройка размера страницы для пользователя"""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    await message.answer(
        "📏 <b>Настройка размера страниц</b>\n\n"
        "Сколько элементов показывать на одной странице?\n"
        "Доступные варианты:\n"
        "• 5 - для мобильных устройств\n"
        "• 10 - по умолчанию (рекомендуется)\n"
        "• 15 - для десктопов\n"
        "• 20 - максимум\n\n"
        "Отправьте число от 5 до 20:",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )

    await state.set_state("waiting_page_size")

def add_history_time_keyboard_17_23() -> ReplyKeyboardMarkup:
    times = [f"{h:02d}:00" for h in range(12, 24)]  # 17:00 ... 23:00

    rows = []
    row = []
    for t in times:
        row.append(KeyboardButton(text=t))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([KeyboardButton(text=BACK_TEXT)])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def add_history_date_keyboard_last14(days: int = 14) -> ReplyKeyboardMarkup:
    today = date.today()
    buttons = [KeyboardButton(text=(today - timedelta(days=i)).strftime("%d.%m.%Y")) for i in range(days)]

    rows = []
    row_size = 4  # 4 кнопки в ряд (можешь поставить 3, если хочешь крупнее)
    for i in range(0, len(buttons), row_size):
        rows.append(buttons[i:i + row_size])

    rows.append([KeyboardButton(text=BACK_TEXT)])  # назад отдельной строкой

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(lambda message: message.text and message.text.isdigit() and 5 <= int(message.text) <= 20,
                StateFilter("waiting_page_size"))
async def set_page_size_handler(message: Message, state: FSMContext):
    """Обработчик установки размера страницы"""
    page_size = int(message.text)
    USER_PAGE_SIZES[message.from_user.id] = page_size

    await message.answer(
        f"✅ Размер страницы установлен на {page_size} элементов.",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )
    await state.clear()




def main_menu_keyboard(is_teacher_flag: bool) -> ReplyKeyboardMarkup:
    if is_teacher_flag:
        buttons = [
            [
                KeyboardButton(text="👥 Расписание"),
                KeyboardButton(text="📚 Указать темы"),
                KeyboardButton(text="➕ Добавить занятие"),
            ],
            [
                KeyboardButton(text="✏️ Задать домашку"),
                KeyboardButton(text="❌ Отменить занятие"),
                KeyboardButton(text="📚 Домашки учеников"),

            ],
            [
                KeyboardButton(text="📢 Объявление"),
                KeyboardButton(text="🧾 История ученика"),
                KeyboardButton(text="📝 Добавить занятие в историю"),
            ],


            [
                KeyboardButton(text="📌 Переносы/отмены"),
                KeyboardButton(text="✏️ Редактировать историю"),
            ],
            [



            ],
            [
                KeyboardButton(text="📅 Массовая отмена"),
                KeyboardButton(text="🔄 Перенести занятие"),

            ],
            [
                KeyboardButton(text="💵 Ставка ученика"),
                KeyboardButton(text="🔗 Ссылки ученика"),
            ],

            [
                KeyboardButton(text="🗑️ Удалить слот"),
                KeyboardButton(text="🗑️ Удалить пользователя"),

            ],
            [
                KeyboardButton(text="📜 Запросы"),
                KeyboardButton(text=ADMIN_FEEDBACK_TEXT),
            ],
            # [KeyboardButton(text="👋 Тест: привет")],

        ]
    else:
        buttons = [
            [
                KeyboardButton(text="📅 Моё расписание"),
                KeyboardButton(text="📚 Моя домашка"),
            ],
            [
                KeyboardButton(text="🔁 Перенести/отменить занятие"),
                KeyboardButton(text="🧾 История занятий"),
            ],
            [
                KeyboardButton(text="⏰ Напоминания"),
                KeyboardButton(text="🔗 Полезные ссылки"),
            ],
            [
                KeyboardButton(text=FEEDBACK_TEXT),
            ],
        ]

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
    )


@router.message(lambda message: message.text == "➕ Слот")
async def handle_add_slot_button(message: Message, state: FSMContext):
    await state.clear()  # ← ВАЖНО

    if not is_teacher(message):
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    # сохраняем список в FSM, чтобы пагинация работала
    await state.update_data(slot_students=students)

    keyboard, total_pages = create_action_keyboard(students, "slot", page=0)

    # можно поставить любое состояние, но лучше логически — ожидание выбора ученика
    # если у тебя уже есть SetSlotStates.waiting_user — ставь его
    await state.set_state(SetSlotStates.waiting_user)

    await message.answer(
        "➕ <b>Добавление слота</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.callback_query(lambda c: c.data.startswith("slot_student_"))
async def slot_select_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    # slot_student_{student_id}_{page}
    student_id = int(parts[2])

    await state.update_data(slot_student_id=student_id)
    await state.set_state(SetSlotStates.waiting_weekday)

    await callback_query.message.edit_text(
        "📅 На какой день недели поставить слот?",
        reply_markup=slot_weekday_inline_kb()
    )

    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith("slot_weekday_"), SetSlotStates.waiting_weekday)
async def slot_pick_weekday(callback_query: CallbackQuery, state: FSMContext):
    wd = int(callback_query.data.split("_")[2])  # slot_weekday_{0..6}

    await state.update_data(slot_weekday=wd)
    await state.set_state(SetSlotStates.waiting_time)

    await callback_query.message.edit_text(
        "Во сколько? Введи время в формате HH:MM, например 18:30."
    )
    await callback_query.message.answer(
        "Можно отменить кнопкой «Назад».",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()




@router.callback_query(lambda c: c.data.startswith("slot_page_"))
async def slot_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("slot_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, total_pages = create_action_keyboard(students, "slot", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")

@router.message(SetSlotStates.waiting_weekday)
async def slot_enter_weekday(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=main_menu_keyboard(True))
        return

    if not text.isdigit():
        await message.answer("Нужно число 1–7.", reply_markup=back_keyboard())
        return

    day = int(text)
    if day < 1 or day > 7:
        await message.answer("Нужно число 1–7.", reply_markup=back_keyboard())
        return

    # В БД weekday обычно 0..6 (Mon..Sun)
    await state.update_data(slot_weekday=day - 1)
    await state.set_state(SetSlotStates.waiting_time)

    await message.answer(
        "Во сколько? Введи время в формате HH:MM, например 18:30.",
        reply_markup=back_keyboard()
    )


@router.message(SetSlotStates.waiting_time)
async def slot_enter_time(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=main_menu_keyboard(True))
        return

    # парсим время
    try:
        hh, mm = map(int, text.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        t_str = f"{hh:02d}:{mm:02d}"
    except Exception:
        await message.answer("Время неверно. Формат: HH:MM (например 18:30).", reply_markup=back_keyboard())
        return

    data = await state.get_data()

    # ✅ ВАЖНО: берём slot_student_id, а не hw_student_id
    student_id = data.get("slot_student_id")
    weekday = data.get("slot_weekday")

    if student_id is None or weekday is None:
        await state.clear()
        await message.answer(
            "Сессия добавления слота сбилась. Начни заново: ➕ Добавить занятие → ➕ Слот",
            reply_markup=main_menu_keyboard(True)
        )
        return

    # ✅ добавляем слот через функцию (там же есть проверка на дубликат)
    student = add_weekly_slot(student_id, weekday, t_str)

    if not student:
        # слот уже существует (add_weekly_slot возвращает None)
        await state.clear()
        await message.answer(
            "⚠️ Такой слот уже есть у ученика. Ничего не менял.",
            reply_markup=main_menu_keyboard(True)
        )
        return

    # ✅ уведомляем ученика о новом регулярном занятии
    try:
        await notify_new_regular_lesson(student["telegram_id"], weekday, t_str)
    except Exception:
        pass

    await state.clear()
    await message.answer("✅ Слот добавлен и ученик уведомлён.", reply_markup=main_menu_keyboard(True))


@router.message(lambda m: m.text == "➕ Добавить занятие")
async def handle_add_lesson_button(message: Message, state: FSMContext):
    if not is_teacher(message):
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Слот")],
            [KeyboardButton(text="✨ Доп. занятие")],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True
    )

    await message.answer(
        "Что именно ты хочешь добавить?",
        reply_markup=keyboard
    )



@router.message(lambda message: message.text == "🧾 История ученика")
async def handle_student_history_button(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    # сохраняем для пагинации
    await state.update_data(history_students=students)

    keyboard, total_pages = create_action_keyboard(students, "history", page=0)

    # оставляем то же состояние, но теперь выбираем кнопкой
    await state.set_state(AdminStudentHistoryStates.waiting_student)

    await message.answer(
        "🧾 <b>История ученика</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@router.callback_query(lambda c: c.data.startswith("history_student_"))
async def history_select_student(callback_query: CallbackQuery, state: FSMContext):
    # history_student_{student_id}_{page}
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    rows = get_lesson_history_for_student(student["id"], limit=100)
    if not rows:
        student_name = student["full_name"] or student["username"] or str(student["telegram_id"])
        await callback_query.message.edit_text(
            f"У ученика {student_name} история занятий пустая."
        )
        await callback_query.answer()
        await state.clear()
        return

    history_kb, total_pages = create_history_keyboard(student["id"], rows, page=0)

    student_name = student["full_name"] or student["username"] or str(student["telegram_id"])
    await callback_query.message.edit_text(
        f"🧾 <b>История занятий ученика {student_name}</b>\n\n"
        f"Нажми на занятие, чтобы изменить статус оплаты:",
        parse_mode="HTML",
        reply_markup=history_kb
    )

    await callback_query.answer()
    await state.clear()

@router.callback_query(lambda c: c.data.startswith("history_page_"))
async def history_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("history_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, total_pages = create_action_keyboard(students, "history", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")


@router.message(lambda message: message.text == "💰 Отметить оплату")
async def handle_mark_payment_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки 'Отметить оплату'"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    # Запускаем мастер просмотра истории с кнопками оплаты
    await start_admin_student_history_wizard(message, state)

# 1. Исправляем состояние в кнопке отмены занятия
@router.message(lambda message: message.text == "❌ Отменить занятие")
async def handle_cancel_lesson_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки 'Отменить занятие'"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(cancel_students=students)

    keyboard, total_pages = create_action_keyboard(students, "cancel", page=0)

    # ИСПРАВЛЕНО: используем правильное состояние
    await state.set_state(CancelStates.choosing_student_smart)
    await message.answer(
        "❌ <b>Отмена занятия</b>\n\n"
        "Выберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.message(lambda message: message.text == "✏️ Задать домашку")
async def handle_set_homework_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки 'Задать домашку'"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    # Сохраняем студентов в состоянии
    await state.update_data(hw_students=students)

    # Создаем клавиатуру
    keyboard, total_pages = create_students_keyboard(students, "homework", page=0)

    # ИСПРАВЛЕНО: используем правильное состояние
    await state.set_state(HomeworkStates.choosing_student_smart)  # Было: .choosing_student
    await message.answer(
        "📝 <b>Задание домашнего задания</b>\n\n"
        "Выберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

class HomeworkStates(StatesGroup):
    waiting_user = State()
    waiting_text = State()
    choosing_student_smart = State()

class FeedbackStates(StatesGroup):
    waiting_text = State()


@router.callback_query(
    lambda c: c.data.startswith("hw_student_"),
    HomeworkStates.choosing_student_smart
)
async def hw_select_student(callback_query: CallbackQuery, state: FSMContext):

    """Обработка выбора ученика для домашнего задания"""
    parts = callback_query.data.split("_")
    student_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    # Получаем данные ученика
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    # Сохраняем ID ученика в состоянии
    await state.update_data(hw_student_id=student_id)
    await state.set_state(HomeworkStates.waiting_text)

    await callback_query.message.edit_text(
        f"📝 <b>Домашнее задание для {student['full_name'] or student['username']}</b>\n\n"
        f"Отправьте текст домашнего задания одним сообщением:",
        parse_mode="HTML"
    )
    await callback_query.answer()

    await callback_query.message.edit_text(
        f"📝 <b>Домашнее задание для {student['full_name'] or student['username']}</b>\n\n"
        f"Сейчас пришлите текст домашнего задания одним сообщением.\n"
        f"Чтобы отменить — нажмите «{BACK_TEXT}».",
        f"Чтобы отменить — нажмите «{BACK_TEXT}».",
        parse_mode="HTML"
    )

    await callback_query.message.answer(
        "✍️ Введите текст домашнего задания одним сообщением:",
        reply_markup=back_keyboard()
    )


# Для отмены занятия
def create_cancel_students_keyboard(students, page: int = 0):
    """Клавиатура для отмены занятия"""
    builder = InlineKeyboardBuilder()

    page_size = 10
    total_pages = (len(students) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(students))
    page_students = students[start_idx:end_idx]

    for student in page_students:
        student_id = student["id"]
        name = student["full_name"] or student["username"] or str(student["telegram_id"])

        if len(name) > 20:
            name = name[:17] + "..."

        builder.button(
            text=name,
            callback_data=f"cancel_student_{student_id}_{page}"
        )

    builder.adjust(2)

    # Пагинация
    if total_pages > 1:
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"cancel_page_{page - 1}"
            ))

        pagination_buttons.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="page_info"
        ))

        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"cancel_page_{page + 1}"
            ))

        builder.row(*pagination_buttons)

    return builder.as_markup(), total_pages


@router.callback_query(lambda c: c.data.startswith("back_from_"))
async def back_from_action(callback_query: CallbackQuery, state: FSMContext):
    await state.clear()

    # удаляем сообщение с inline-кнопками, чтобы нельзя было нажать "назад" второй раз
    await callback_query.message.delete()

    await callback_query.message.answer(
        "Возвращаю в главное меню.",
        reply_markup=main_menu_keyboard(True),
    )
    await callback_query.answer()




@router.message(lambda message: message.text == "💰 Отметить оплату")
async def handle_mark_payment_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки 'Отметить оплату'"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(payment_students=students)

    keyboard, total_pages = create_action_keyboard(students, "payment", page=0)

    # ИСПРАВЛЕНО: используем правильное состояние
    await state.set_state(PaymentStates.choosing_student_smart)  # Было: .choosing_student
    await message.answer(
        "💰 <b>Отметить оплату</b>\n\n"
        "Выберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

def inline_back_to_menu_kb(action_type: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в меню", callback_data=f"back_from_{action_type}")
    builder.adjust(1)
    return builder.as_markup()


def create_action_keyboard(students, action_type: str, page: int = 0):
    """Универсальная клавиатура для действий с учениками"""
    builder = InlineKeyboardBuilder()

    page_size = 10
    total_pages = (len(students) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(students))
    page_students = students[start_idx:end_idx]

    # Эмодзи для разных действий
    emojis = {
        "price": "💵",
        "psched": "📅",
        "phw": "📚",
        "phist": "🧾",
        "homework": "📝",
        "cancel": "❌",
        "payment": "💰",
        "history": "🧾",
        "delete": "🗑️",
        "slot": "➕",
        "extra": "✨",
        "addextra": "✨",
        "links": "🔗",
        "edit": "✏️",
        "reschedule": "🔄",
        "add_history": "📝",
        "parentlink": "👨‍👩‍👧",
        "pchild": "👤",
        "adminhw": "📚",

    }

    emoji = emojis.get(action_type, "👤")

    for student in page_students:
        student_id = student["id"]
        name = student["full_name"] or student["username"] or str(student["telegram_id"])

        if len(name) > 18:
            name = name[:15] + "..."

        builder.button(
            text=f"{emoji} {name}",
            callback_data=f"{action_type}_student_{student_id}_{page}"
        )

    builder.adjust(2)

    # Пагинация
    if total_pages > 1:
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"{action_type}_page_{page - 1}"
            ))

        pagination_buttons.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="page_info"
        ))

        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"{action_type}_page_{page + 1}"
            ))

        builder.row(*pagination_buttons)

    builder.row(InlineKeyboardButton(
        text="⬅️ Назад в меню",
        callback_data=f"back_from_{action_type}"
    ))

    return builder.as_markup(), total_pages

def get_homeworks_for_student(student_id: int, limit: int = 50):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, text, created_at, is_done
        FROM homeworks
        WHERE student_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (student_id, limit))
    return cur.fetchall()

def get_homework_by_id(hw_id: int):
    cur = conn.cursor()
    cur.execute("""
        SELECT h.*, s.full_name, s.username, s.telegram_id
        FROM homeworks h
        JOIN students s ON s.id = h.student_id
        WHERE h.id = ?
    """, (hw_id,))
    return cur.fetchone()

def delete_homework(hw_id: int):
    cur = conn.cursor()
    cur.execute("DELETE FROM homeworks WHERE id = ?", (hw_id,))
    conn.commit()

def update_homework_text(hw_id: int, new_text: str):
    cur = conn.cursor()
    cur.execute("UPDATE homeworks SET text = ? WHERE id = ?", (new_text, hw_id))
    conn.commit()

def toggle_homework_done(hw_id: int):
    cur = conn.cursor()
    cur.execute("SELECT is_done FROM homeworks WHERE id = ?", (hw_id,))
    row = cur.fetchone()
    if not row:
        return None
    new_val = 0 if int(row["is_done"] or 0) == 1 else 1
    cur.execute("UPDATE homeworks SET is_done = ? WHERE id = ?", (new_val, hw_id))
    conn.commit()
    return new_val


@router.message(lambda m: m.text == "📚 Домашки учеников")
async def admin_homeworks_menu(message: Message, state: FSMContext):
    if not is_teacher(message):
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(adminhw_students=students)
    kb, _ = create_action_keyboard(students, "adminhw", page=0)
    await state.set_state(AdminHomeworkStates.choosing_student)

    await message.answer(
        "📚 <b>Домашки учеников</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(lambda c: c.data.startswith("adminhw_page_"), AdminHomeworkStates.choosing_student)
async def adminhw_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("adminhw_students", [])
    kb, _ = create_action_keyboard(students, "adminhw", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()


def build_admin_homeworks_list_kb(student_id: int, homeworks):
    kb = InlineKeyboardBuilder()
    for hw in homeworks[:30]:
        done = "✅" if int(hw["is_done"] or 0) == 1 else "⬜️"
        created = (hw["created_at"] or "")[:16]
        kb.button(
            text=f"{done} {created} (id:{hw['id']})",
            callback_data=f"{ADMIN_HW_PICK_PREFIX}{hw['id']}"
        )
    kb.button(text="⬅️ Назад к ученикам", callback_data="back_from_adminhw")
    kb.adjust(1)
    return kb.as_markup()

@router.callback_query(lambda c: c.data.startswith("adminhw_student_"), AdminHomeworkStates.choosing_student)
async def adminhw_pick_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    hws = get_homeworks_for_student(student_id, only_open=False)[:50]

    if not hws:
        await callback_query.message.edit_text("У ученика пока нет домашних заданий.")
        await callback_query.answer()
        return

    await state.update_data(adminhw_student_id=student_id)
    await state.set_state(AdminHomeworkStates.choosing_homework)

    await callback_query.message.edit_text(
        "📚 Выберите домашку:",
        reply_markup=build_admin_homeworks_list_kb(student_id, hws)
    )
    await callback_query.answer()


def build_admin_homework_actions_kb(hw_id: int, student_id: int, is_done: int):
    kb = InlineKeyboardBuilder()
    kb.button(text=("✅ Выполнена" if is_done else "⬜️ Отметить выполненной"),
              callback_data=f"{ADMIN_HW_TOGGLE_PREFIX}{hw_id}")
    kb.button(text="✏️ Изменить", callback_data=f"{ADMIN_HW_EDIT_PREFIX}{hw_id}")
    kb.button(text="🗑️ Удалить", callback_data=f"{ADMIN_HW_DELETE_PREFIX}{hw_id}")
    kb.button(text="⬅️ Назад к списку", callback_data=f"{ADMIN_HW_BACK_TO_LIST}{student_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(lambda c: c.data.startswith(ADMIN_HW_PICK_PREFIX), AdminHomeworkStates.choosing_homework)
async def adminhw_open_homework(callback_query: CallbackQuery, state: FSMContext):
    hw_id = int(callback_query.data.split("_")[-1])  # если префикс без "_" — подгони split
    hw = get_homework_by_id(hw_id)
    if not hw:
        await callback_query.answer("Домашка не найдена", show_alert=True)
        return

    await state.update_data(adminhw_hw_id=hw_id)

    student_name = hw["full_name"] or hw["username"] or str(hw["telegram_id"])
    done = int(hw["is_done"] or 0)

    text = (
        f"📚 <b>Домашка ученика {student_name}</b>\n"
        f"🆔 {hw['id']}\n"
        f"🗓 {hw['created_at']}\n"
        f"Статус: {'✅ выполнена' if done else '⬜️ не выполнена'}\n\n"
        f"{hw['text']}"
    )

    await callback_query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=build_admin_homework_actions_kb(hw_id, hw["student_id"], done)
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith(ADMIN_HW_TOGGLE_PREFIX))
async def adminhw_toggle_done(callback_query: CallbackQuery, state: FSMContext):
    hw_id = int(callback_query.data.split("_")[-1])
    new_val = toggle_homework_done(hw_id)
    hw = get_homework_by_id(hw_id)
    if not hw:
        await callback_query.answer("Домашка не найдена", show_alert=True)
        return

    student_name = hw["full_name"] or hw["username"] or str(hw["telegram_id"])
    text = (
        f"📚 <b>Домашка ученика {student_name}</b>\n"
        f"🆔 {hw['id']}\n"
        f"🗓 {hw['created_at']}\n"
        f"Статус: {'✅ выполнена' if int(hw['is_done'] or 0) else '⬜️ не выполнена'}\n\n"
        f"{hw['text']}"
    )
    await callback_query.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=build_admin_homework_actions_kb(hw_id, hw["student_id"], int(hw["is_done"] or 0))
    )
    await callback_query.answer("Готово")


@router.callback_query(lambda c: c.data.startswith(ADMIN_HW_DELETE_PREFIX))
async def adminhw_delete(callback_query: CallbackQuery, state: FSMContext):
    hw_id = int(callback_query.data.split("_")[-1])
    hw = get_homework_by_id(hw_id)
    if not hw:
        await callback_query.answer("Домашка не найдена", show_alert=True)
        return

    delete_homework(hw_id)
    await callback_query.answer("Удалено")

    # вернёмся к списку ученика
    student_id = hw["student_id"]
    hws = get_homeworks_for_student(student_id, only_open=False)[:50]

    if not hws:
        await callback_query.message.edit_text("Домашек больше нет.")
        return

    await callback_query.message.edit_text(
        "📚 Выберите домашку:",
        reply_markup=build_admin_homeworks_list_kb(student_id, hws)
    )


@router.callback_query(lambda c: c.data.startswith(ADMIN_HW_EDIT_PREFIX))
async def adminhw_edit_start(callback_query: CallbackQuery, state: FSMContext):
    hw_id = int(callback_query.data.split("_")[-1])
    hw = get_homework_by_id(hw_id)
    if not hw:
        await callback_query.answer("Домашка не найдена", show_alert=True)
        return

    await state.update_data(adminhw_hw_id=hw_id)
    await state.set_state(AdminHomeworkStates.editing_text)

    await callback_query.message.answer(
        "✏️ Отправьте новый текст домашки одним сообщением:",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()

@router.message(AdminHomeworkStates.editing_text)
async def adminhw_edit_finish(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=main_menu_keyboard(True))
        return

    data = await state.get_data()
    hw_id = data.get("adminhw_hw_id")
    if not hw_id:
        await state.clear()
        await message.answer("Сессия сбилась. Откройте домашку ещё раз.", reply_markup=main_menu_keyboard(True))
        return

    update_homework_text(hw_id, text)
    await state.clear()
    await message.answer("✅ Домашка обновлена.", reply_markup=main_menu_keyboard(True))


@router.callback_query(lambda c: c.data.startswith(ADMIN_HW_BACK_TO_LIST))
async def adminhw_back_to_list(callback_query: CallbackQuery, state: FSMContext):
    student_id = int(callback_query.data.split("_")[-1])
    hws = get_homeworks_for_student(student_id, only_open=False)[:50]
    if not hws:
        await callback_query.message.edit_text("У ученика пока нет домашних заданий.")
        await callback_query.answer()
        return

    await state.set_state(AdminHomeworkStates.choosing_homework)
    await callback_query.message.edit_text(
        "📚 Выберите домашку:",
        reply_markup=build_admin_homeworks_list_kb(student_id, hws)
    )
    await callback_query.answer()



@router.message(lambda m: m.text == "📅 Расписание ученика")
async def parent_schedule_menu(message: Message, state: FSMContext):
    students = get_parent_students(message.from_user.id)
    if not students:
        await message.answer("У вас пока нет привязанных учеников.")
        return

    # ✅ если один ребёнок — сразу показываем
    if len(students) == 1:
        st = students[0]
        text = build_student_schedule_text(st["id"])
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu(message))
        return

    await state.update_data(psched_students=students)
    kb, _ = create_action_keyboard(students, "psched", page=0)
    await message.answer("Выберите ученика:", reply_markup=kb)



@router.message(lambda m: m.text == "📚 Домашка ученика")
async def parent_hw_menu(message: Message, state: FSMContext):
    students = get_parent_students(message.from_user.id)
    if not students:
        await message.answer("У вас пока нет привязанных учеников.")
        return

    if len(students) == 1:
        st = students[0]
        text = build_student_homework_text(st["id"])
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu(message))
        return

    await state.update_data(phw_students=students)
    kb, _ = create_action_keyboard(students, "phw", page=0)
    await message.answer("Выберите ученика:", reply_markup=kb)



@router.message(lambda m: m.text == "🧾 История занятий ученика")
async def parent_history_menu(message: Message, state: FSMContext):
    students = get_parent_students(message.from_user.id)
    if not students:
        await message.answer("У вас пока нет привязанных учеников.")
        return

    if len(students) == 1:
        st = students[0]
        text = build_student_history_text(st["id"])
        await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu(message))
        return

    await state.update_data(phist_students=students)
    kb, _ = create_action_keyboard(students, "phist", page=0)
    await message.answer("Выберите ученика:", reply_markup=kb)



@router.callback_query(lambda c: c.data and c.data.startswith("parentreq_pick_"))
async def parentreq_pick(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in TEACHER_IDS:
        await callback_query.answer("Только для админа")
        return

    req_id = int(callback_query.data.split("_")[2])
    req = get_parent_request(req_id)
    if not req or req["status"] != "pending":
        await callback_query.answer("Запрос не найден или уже обработан")
        return

    students = get_all_students()
    if not students:
        await callback_query.message.edit_text("Нет учеников для привязки.")
        await callback_query.answer()
        return

    await state.update_data(parentlink_req_id=req_id, parentlink_students=students)

    keyboard, _ = create_action_keyboard(students, "parentlink", page=0)  # :contentReference[oaicite:4]{index=4}

    await callback_query.message.edit_text(
        f"👨‍👩‍👧 Привязка родителя (запрос #{req_id})\n\n"
        f"Родитель написал: {req['child_info']}\n\n"
        f"Выберите ученика:",
        reply_markup=keyboard
    )
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith("psched_page_"))
async def psched_page(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("psched_students", [])
    kb, _ = create_action_keyboard(students, "psched", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("phw_page_"))
async def phw_page(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("phw_students", [])
    kb, _ = create_action_keyboard(students, "phw", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("phist_page_"))
async def phist_page(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("phist_students", [])
    kb, _ = create_action_keyboard(students, "phist", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()


def parent_can_access_student(parent_tg_id: int, student_id: int) -> bool:
    return any(s["id"] == student_id for s in get_parent_students(parent_tg_id))


@router.callback_query(lambda c: c.data.startswith("psched_student_"))
async def psched_pick_student(callback_query: CallbackQuery):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    if not parent_can_access_student(callback_query.from_user.id, student_id):
        await callback_query.answer("Нет доступа к этому ученику", show_alert=True)
        return

    text = build_student_schedule_text(student_id)  # сделаем ниже
    await callback_query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=inline_back_to_menu_kb("psched")
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("phw_student_"))
async def phw_pick_student(callback_query: CallbackQuery):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    if not parent_can_access_student(callback_query.from_user.id, student_id):
        await callback_query.answer("Нет доступа к этому ученику", show_alert=True)
        return

    text = build_student_homework_text(student_id)  # сделаем ниже
    await callback_query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=inline_back_to_menu_kb("phw")
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("phist_student_"))
async def phist_pick_student(callback_query: CallbackQuery):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    if not parent_can_access_student(callback_query.from_user.id, student_id):
        await callback_query.answer("Нет доступа к этому ученику", show_alert=True)
        return

    text = build_student_history_text(student_id)  # сделаем ниже
    await callback_query.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=inline_back_to_menu_kb("phist")
    )
    await callback_query.answer()


def build_student_schedule_text(student_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT full_name, username, telegram_id FROM students WHERE id=?", (student_id,))
    st = cur.fetchone()

    if st:
        base = st["full_name"] or st["username"] or str(st["telegram_id"])
        uname = st["username"]
        if uname:
            uname = uname if uname.startswith("@") else f"@{uname}"
            # если full_name уже пустой и base == username, то второй раз не дублируем
            if (st["full_name"] or "").strip():
                name = f"{base} ({uname})"
            else:
                name = uname
        else:
            name = base
    else:
        name = f"#{student_id}"

    # weekly slots
    cur.execute("""
        SELECT weekday, time
        FROM weekly_lessons
        WHERE student_id=? AND is_active=1
        ORDER BY
            weekday,
            CAST(substr(time, 1, instr(time, ':') - 1) AS INTEGER),
            CAST(substr(time, instr(time, ':') + 1) AS INTEGER)
    """, (student_id,))

    weekly = cur.fetchall()

    if not weekly:
        return f"📅 <b>Расписание ученика {name}</b>\n\nПока нет регулярных слотов."

    lines = [f"📅 <b>Расписание ученика {name}</b>\n"]

    for i, row in enumerate(weekly, start=1):
        day = DAY_NAMES[row["weekday"]]
        lines.append(f"{i}) {day} — {row['time']}")
    return "\n".join(lines)


def build_student_homework_text(student_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT full_name, username, telegram_id FROM students WHERE id=?", (student_id,))
    st = cur.fetchone()
    name = (st["full_name"] or st["username"] or str(st["telegram_id"])) if st else f"#{student_id}"

    # пример под таблицу homeworks: (id, student_id, text, created_at)
    cur.execute("""
        SELECT text, created_at
        FROM homeworks
        WHERE student_id=?
        ORDER BY id DESC
        LIMIT 1
    """, (student_id,))
    hw = cur.fetchone()

    if not hw:
        return f"📚 <b>Домашка ученика {name}</b>\n\nДомашних заданий пока нет."

    created = hw["created_at"] or ""
    return f"📚 <b>Домашка ученика {name}</b>\n\n🗓 {created}\n\n{hw['text']}"


def build_student_history_text(student_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT full_name, username, telegram_id FROM students WHERE id=?", (student_id,))
    st = cur.fetchone()
    name = (st["full_name"] or st["username"] or str(st["telegram_id"])) if st else f"#{student_id}"

    debt_sum, unpaid_cnt, price = get_student_debt(student_id)

    header = f"🧾 <b>История занятий ученика {name}</b>\n"
    if price > 0:
        header += f"💳 <b>Долг:</b> {debt_sum} ₽  (❌ {unpaid_cnt} × {price} ₽)\n"
    header += "\n"
    lines = [header]

    cur.execute("""
        SELECT date, time, topic, paid, status
        FROM lesson_history
        WHERE student_id=?
        ORDER BY date DESC, time(time) DESC

        LIMIT 30
    """, (student_id,))
    rows = cur.fetchall()

    if not rows:
        return f"🧾 <b>История занятий ученика {name}</b>\n\nИстория пока пустая."

    # НЕ перезатираем lines — в нём уже header с долгом
    # lines = [f"🧾 <b>История занятий ученика {name}</b>\n"]

    for r in rows:
        dt = r["date"]
        tm = r["time"]
        topic = r["topic"] or "—"
        status = r["status"] or "done"

        if status != "done":
            pay_text = "—"
        else:
            pay_text = "✅ оплачено" if (r["paid"] or 0) == 1 else "❌ не оплачено"

        lines.append(f"• {dt} {tm} — {topic} — <b>{pay_text}</b>")

    return "\n".join(lines)

from datetime import time as dtime

def normalize_time_str(t: str) -> str:
    tt = parse_time_str(t)  # твоя функция
    return f"{tt.hour:02d}:{tt.minute:02d}"

def get_active_parent_ids_for_student(student_id: int) -> list[int]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT parent_telegram_id
        FROM parent_links
        WHERE student_id = ? AND is_active = 1
        """,
        (student_id,),
    )
    return [row["parent_telegram_id"] for row in cur.fetchall()]


@router.callback_query(lambda c: c.data and c.data.startswith("parentlink_student_"))
async def parentlink_choose_student(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in TEACHER_IDS:
        await callback_query.answer("Только для админа")
        return

    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    data = await state.get_data()
    req_id = data.get("parentlink_req_id")
    if not req_id:
        await callback_query.answer("Не вижу ID запроса")
        return

    req = get_parent_request(req_id)
    if not req or req["status"] != "pending":
        await callback_query.answer("Запрос не найден или уже обработан")
        return

    parent_tg_id = req["parent_telegram_id"]

    # создаём привязку (если уже есть — просто активируем)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO parent_links(parent_telegram_id, student_id, is_active, created_at)
        VALUES (?, ?, 1, ?)
        """,
        (parent_tg_id, student_id, datetime.now().isoformat(timespec="seconds"))
    )
    cur.execute(
        "UPDATE parent_links SET is_active = 1 WHERE parent_telegram_id = ? AND student_id = ?",
        (parent_tg_id, student_id)
    )
    conn.commit()

    set_parent_request_status(req_id, "approved")

    # уведомляем родителя
    try:
        await bot.send_message(
            parent_tg_id,
            "✅ Администратор одобрил привязку. Теперь вам доступно расписание/домашка/история ученика.",
            reply_markup=parent_menu_keyboard()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить родителя {parent_tg_id}: {e}")

    await callback_query.message.edit_text(f"✅ Привязка выполнена. Запрос #{req_id} закрыт.")
    await callback_query.answer("Готово")
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("parentreq_reject_"))
async def parentreq_reject(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in TEACHER_IDS:
        await callback_query.answer("Только для админа")
        return

    req_id = int(callback_query.data.split("_")[2])
    req = get_parent_request(req_id)
    if not req or req["status"] != "pending":
        await callback_query.answer("Запрос не найден или уже обработан")
        return

    set_parent_request_status(req_id, "rejected")

    parent_tg_id = req["parent_telegram_id"]
    try:
        await bot.send_message(parent_tg_id, "❌ Запрос привязки отклонён. Если это ошибка — отправьте запрос ещё раз.")
    except Exception:
        pass

    await callback_query.message.edit_text(f"❌ Запрос #{req_id} отклонён.")
    await callback_query.answer("Отклонено")
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("parentlink_page_"))
async def parentlink_page(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in TEACHER_IDS:
        await callback_query.answer("Только для админа")
        return

    page = int(callback_query.data.split("_")[2])
    data = await state.get_data()
    students = data.get("parentlink_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, _ = create_action_keyboard(students, "parentlink", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer()


@router.message(lambda message: message.text == "🔄 Перенести занятие")
async def handle_reschedule_button(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    await state.clear()  # <-- ВАЖНО: чтобы не остаться в чужом состоянии

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(reschedule_students=students)
    keyboard, total_pages = create_action_keyboard(students, "reschedule", page=0)

    await state.set_state(RescheduleStates.choosing_student)

    await message.answer(
        "🔄 <b>Перенос занятия</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


def get_upcoming_lessons_for_student(student_id: int, days_ahead: int = 30):
    today = date.today()
    end = today + timedelta(days=days_ahead)

    items = []

    # 1) регулярные/оверрайды (по дням)
    d = today
    while d <= end:
        day_lessons = get_lessons_for_date(d)
        for l in day_lessons:
            if l["student_id"] != student_id:
                continue
            # отменённые переносить не надо
            if l.get("change_kind") == "one_time":
                items.append({
                    "kind": "override",
                    "override_id": l["override_id"],
                    "date": d,
                    "time": l["time"]
                })

            # сохраняем "экземпляр занятия" = weekly_lesson_id + дата
            items.append({
                "kind": "weekly",
                "weekly_lesson_id": l["weekly_lesson_id"],
                "date": d,
                "time": l["time"],
                "change_kind": l.get("change_kind"),
            })
        d += timedelta(days=1)

    # 2) дополнительные занятия
    extras = get_future_extra_lessons_for_student(student_id, days_ahead=days_ahead)
    for e in extras:
        items.append({
            "kind": "extra",
            "extra_id": e["id"],
            "date": date.fromisoformat(e["date"]),
            "time": e["time"],
            "topic": e.get("topic"),
        })

    # сортировка
    def key(x):
        return (x["date"], parse_time_str(x["time"]))

    items.sort(key=key)

    return items

def build_reschedule_lessons_kb(lessons):
    builder = InlineKeyboardBuilder()

    for item in lessons[:40]:  # можно потом сделать пагинацию, но для старта ок
        d_str = item["date"].strftime("%d.%m.%Y")
        time_str = item["time"]

        if item["kind"] == "weekly":
            # resch_pick_weekly_{weekly_id}_{YYYY-MM-DD}
            cb = f"resch_pick_weekly_{item['weekly_lesson_id']}_{item['date'].isoformat()}"
            text = f"📅 {d_str} {time_str}"

        elif item["kind"] == "override":
            cb = f"resch_pick_override_{item['override_id']}"
            text = f"🔁 {d_str} {time_str} (перенос)"

        else:
            # resch_pick_extra_{extra_id}
            cb = f"resch_pick_extra_{item['extra_id']}"
            topic = item.get("topic") or "доп. занятие"
            text = f"⭐ {d_str} {time_str} — {topic}"

        builder.button(text=text, callback_data=cb)

    builder.button(text="⬅️ Назад к ученикам", callback_data="back_to_students_reschedule")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(lambda c: c.data.startswith("resch_pick_override_"))
async def resch_pick_override(callback_query: CallbackQuery, state: FSMContext):
    override_id = int(callback_query.data.split("_")[3])

    ov = get_override_by_id(override_id)
    if not ov:
        await callback_query.answer("Перенос не найден")
        return

    await state.update_data(
        resch_kind="override",
        resch_override_id=override_id,
        resch_old_date=date.fromisoformat(ov["date"]),
        resch_old_time=ov["new_time"]
    )

    await state.set_state(RescheduleStates.entering_date)

    await callback_query.message.edit_text(
        "Введите новую дату для переноса перенесенного занятия:"
    )

@router.callback_query(lambda c: c.data.startswith("reschedule_page_"), RescheduleStates.choosing_student)
async def reschedule_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("reschedule_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, total_pages = create_action_keyboard(students, "reschedule", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")

# --- НАЗАД к списку учеников (перенос) ---
@router.callback_query(lambda c: c.data == "back_to_students_reschedule")
async def back_to_students_reschedule(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    students = data.get("reschedule_students", [])
    keyboard, _ = create_action_keyboard(students, "reschedule", page=0)

    await state.set_state(RescheduleStates.choosing_student)
    await callback_query.message.edit_text(
        "🔄 <b>Перенос занятия</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_query.answer()



# --- ВЫБОР КОНКРЕТНОГО ЗАНЯТИЯ (регулярное) ---
@router.callback_query(lambda c: c.data and c.data.startswith("resch_pick_weekly_"))
async def resch_pick_weekly(callback_query: CallbackQuery, state: FSMContext):
    # resch_pick_weekly_{weekly_id}_{YYYY-MM-DD}
    parts = callback_query.data.split("_")
    weekly_id = int(parts[3])
    old_date = date.fromisoformat(parts[4])

    wl = get_weekly_lesson_by_id(weekly_id)
    if not wl:
        await callback_query.answer("Слот не найден")
        return

    old_time_str = wl["time"]

    await state.update_data(
        resch_kind="weekly",
        resch_weekly_id=weekly_id,
        resch_old_date=old_date,
        resch_old_time=old_time_str,
        resch_student_tg=wl["telegram_id"],
        resch_old_weekday=wl["weekday"],
    )

    await state.set_state(RescheduleStates.entering_date)
    await callback_query.message.edit_text(
        f"🔄 Перенос занятия\n\n"
        f"Было: {old_date.strftime('%d.%m.%Y')} {old_time_str}\n\n"
        f"Введите новую дату (ДД.ММ или ДД.ММ.ГГГГ):",
        reply_markup=inline_back_to_menu_kb("reschedule")
    )
    await callback_query.answer()


# --- ВЫБОР КОНКРЕТНОГО ЗАНЯТИЯ (доп. занятие) ---
@router.callback_query(lambda c: c.data and c.data.startswith("resch_pick_extra_"))
async def resch_pick_extra(callback_query: CallbackQuery, state: FSMContext):
    extra_id = int(callback_query.data.split("_")[3])

    cur = conn.cursor()
    cur.execute("SELECT * FROM extra_lessons WHERE id = ?", (extra_id,))
    e = cur.fetchone()
    if not e:
        await callback_query.answer("Доп. занятие не найдено")
        return

    old_date = date.fromisoformat(e["date"])
    old_time_str = e["time"]

    await state.update_data(
        resch_kind="extra",
        resch_extra_id=extra_id,
        resch_old_date=old_date,
        resch_old_time=old_time_str,
    )

    await state.set_state(RescheduleStates.entering_date)
    await callback_query.message.edit_text(
        f"🔄 Перенос доп. занятия\n\n"
        f"Было: {old_date.strftime('%d.%m.%Y')} {old_time_str}\n\n"
        f"Введите новую дату (ДД.ММ или ДД.ММ.ГГГГ):",
        reply_markup=inline_back_to_menu_kb("reschedule")
    )
    await callback_query.answer()


# --- ВВОД НОВОЙ ДАТЫ ---
@router.message(RescheduleStates.entering_date)
async def resch_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(is_teacher(message)))
        return

    new_date = parse_date_str(text)
    if not new_date:
        await message.answer("Дата неверна. Формат: ДД.ММ или ДД.ММ.ГГГГ", reply_markup=back_keyboard())
        return

    await state.update_data(resch_new_date=new_date)
    await state.set_state(RescheduleStates.entering_time)
    await message.answer("Введите новое время (HH:MM):", reply_markup=back_keyboard())


# --- ВВОД НОВОГО ВРЕМЕНИ ---
@router.message(RescheduleStates.entering_time)
async def resch_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(is_teacher(message)))
        return

    try:
        hh, mm = map(int, text.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer("Время неверно. Формат: HH:MM", reply_markup=back_keyboard())
        return

    data = await state.get_data()
    new_date = data["resch_new_date"]
    old_date = data["resch_old_date"]
    old_time = data["resch_old_time"]

    await state.update_data(resch_new_time=new_time)
    await state.set_state(RescheduleStates.confirming)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Да, перенести")], [KeyboardButton(text="❌ Нет, отменить")]],
        resize_keyboard=True
    )

    await message.answer(
        f"Подтвердите перенос:\n"
        f"Было: {old_date.strftime('%d.%m.%Y')} {old_time}\n"
        f"Стало: {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}",
        reply_markup=kb
    )


# --- ПОДТВЕРЖДЕНИЕ ---
@router.message(RescheduleStates.confirming)
async def resch_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(is_teacher(message)))
        return

    if text != "✅ Да, перенести":
        await message.answer("Нажмите ✅ Да, перенести или ❌ Нет, отменить.")
        return

    data = await state.get_data()
    kind = data["resch_kind"]
    old_date = data["resch_old_date"]
    old_time = data["resch_old_time"]
    new_date = data["resch_new_date"]
    new_time: dtime = data["resch_new_time"]

    if kind == "weekly":
        weekly_id = data["resch_weekly_id"]
        wl = get_weekly_lesson_by_id(weekly_id)
        if not wl:
            await state.clear()
            await message.answer("Слот не найден.", reply_markup=main_menu_keyboard(is_teacher(message)))
            return

        # 1) на старую дату ставим cancel
        hh2, mm2 = map(int, wl["time"].split(":"))
        normal_time = dtime(hh2, mm2)
        create_lesson_override(
            weekly_lesson_id=weekly_id,
            override_date=old_date,
            new_time=normal_time,
            change_kind="cancel",
            original_date=None,
            original_time=None
        )

        # 2) на новую дату ставим one_time (и сохраняем откуда переносили)
        create_lesson_override(
            weekly_lesson_id=weekly_id,
            override_date=new_date,
            new_time=new_time,
            change_kind="one_time",
            original_date=old_date,
            original_time=old_time
        )

        # уведомление ученику (если хочешь)
        try:
            await notify_one_time_change(
                student_telegram_id=wl["telegram_id"],
                change_date=new_date,
                new_time=new_time.strftime("%H:%M"),
                old_weekday=wl["weekday"],
                old_time=wl["time"],
                is_cancellation=False
            )
        except Exception:
            pass

    elif kind == "override":
        override_id = data["resch_override_id"]

        update_lesson_override(
            override_id,
            new_date,
            new_time,
            change_kind="one_time"
        )

    else:
        extra_id = data["resch_extra_id"]
        cur = conn.cursor()
        cur.execute(
            "UPDATE extra_lessons SET date = ?, time = ? WHERE id = ?",
            (new_date.isoformat(), new_time.strftime("%H:%M"), extra_id)
        )
        conn.commit()

    await state.clear()
    await message.answer(
        "✅ Перенос выполнен.",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )




@router.callback_query(lambda c: c.data and c.data.startswith("reschedule_student_"))
async def reschedule_select_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    # подстраховка: если состояние сбилось — ставим нужное
    cur_state = await state.get_state()
    if cur_state != RescheduleStates.choosing_student.state:
        await state.set_state(RescheduleStates.choosing_student)

    await state.update_data(reschedule_student_id=student_id)

    lessons = get_upcoming_lessons_for_student(student_id, days_ahead=30)
    if not lessons:
        await callback_query.message.edit_text("У этого ученика нет занятий на ближайшие 30 дней.")
        await callback_query.answer()
        return

    await state.update_data(reschedule_lessons=lessons)
    await state.set_state(RescheduleStates.choosing_lesson)

    kb = build_reschedule_lessons_kb(lessons)
    await callback_query.message.edit_text(
        "Выберите занятие, которое переносим:",
        reply_markup=kb
    )
    await callback_query.answer()




@router.message(lambda message: message.text == "❌ Отменить занятие")
async def handle_cancel_lesson_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки 'Отменить занятие'"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(cancel_students=students)

    keyboard, total_pages = create_cancel_students_keyboard(students, page=0)

    # ИСПРАВЛЕНО: используем правильное состояние
    await state.set_state(CancelStates.choosing_student_smart)  # Было: .choosing_student
    await message.answer(
        "❌ <b>Отмена занятия</b>\n\n"
        "Выберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@router.callback_query(lambda c: c.data.startswith("price_page_"), SetPriceStates.choosing_student)
async def price_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("price_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    kb, _ = create_action_keyboard(students, "price", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith("price_student_"), SetPriceStates.choosing_student)
async def price_select_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    cur = conn.cursor()
    cur.execute("SELECT full_name, username, telegram_id, lesson_price FROM students WHERE id=?", (student_id,))
    st = cur.fetchone()
    if not st:
        await callback_query.answer("Ученик не найден")
        return

    name = st["full_name"] or st["username"] or str(st["telegram_id"])
    current_price = int(st["lesson_price"] or 0)

    await state.update_data(price_student_id=student_id)
    await state.set_state(SetPriceStates.waiting_price)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BACK_TEXT)]],  # BACK_TEXT у тебя есть :contentReference[oaicite:8]{index=8}
        resize_keyboard=True
    )

    await callback_query.message.answer(
        f"💵 <b>Ставка ученика {name}</b>\n\n"
        f"Текущая ставка: <b>{current_price}</b>\n"
        f"Отправьте новую ставку числом (например 1500):",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback_query.answer()

@router.message(SetPriceStates.waiting_price)
async def price_enter_value(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if text == BACK_TEXT:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=main_menu_keyboard(True))
        return

    if not text.isdigit():
        await message.answer("Нужно число. Пример: 1500", reply_markup=back_keyboard())
        return

    new_price = int(text)

    data = await state.get_data()
    student_id = data.get("price_student_id")
    if not student_id:
        await state.clear()
        await message.answer("Сессия сбилась. Откройте кнопку ещё раз.", reply_markup=main_menu_keyboard(True))
        return

    cur = conn.cursor()
    cur.execute("UPDATE students SET lesson_price=? WHERE id=?", (new_price, student_id))
    conn.commit()

    await state.clear()
    await message.answer(f"✅ Ставка сохранена: {new_price}", reply_markup=main_menu_keyboard(True))


@router.callback_query(lambda c: c.data.startswith("hw_page_"))
async def hw_page_callback(callback_query: CallbackQuery, state: FSMContext):
    """Обработка пагинации для списка учеников"""
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("hw_students", [])

    if not students:
        await callback_query.answer("Нет учеников")
        return

    # Создаем обновленную клавиатуру
    keyboard, total_pages = create_students_keyboard(students, "homework", page)

    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")


# ---------- ИНИЦИАЛИЗАЦИЯ БД ----------


def init_db():
    cur = conn.cursor()



    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_roles (
        telegram_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL,              -- 'student' | 'parent'
        created_at TEXT
    )
    """)
    conn.commit()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS parents (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        created_at TEXT
    )
    """)
    conn.commit()

    # --- запросы от родителей ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS parent_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_telegram_id INTEGER NOT NULL,
        parent_username TEXT,
        parent_name TEXT,
        child_info TEXT,          -- что родитель написал про ребенка
        status TEXT DEFAULT 'pending',  -- pending/approved/rejected
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_parent_requests_status
    ON parent_requests(status)
    """)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            full_name TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            weekday INTEGER,
            time TEXT,
            remind_before_minutes INTEGER DEFAULT 60,
            is_active INTEGER DEFAULT 1
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS system_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            value TEXT,
            updated_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            weekly_lesson_id INTEGER,
            old_weekday INTEGER,
            old_time TEXT,
            new_date TEXT,
            new_time TEXT,
            change_kind TEXT,
            comment TEXT,
            status TEXT,
            created_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weekly_lesson_id INTEGER,
            date TEXT,
            new_time TEXT,
            change_kind TEXT,
            remind_before_minutes INTEGER DEFAULT 60,
            original_date TEXT,
            original_time TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS homeworks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            text TEXT,
            created_at TEXT,
            is_done INTEGER DEFAULT 0
        )
        """
    )

    # История занятий (для учёта + оплаты)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            weekly_lesson_id INTEGER,
            date TEXT,
            time TEXT,
            status TEXT,          -- 'done', 'cancelled'
            paid INTEGER,         -- 0/1
            note TEXT,
            topic TEXT,           -- ТЕМА ЗАНЯТИЯ (НОВОЕ ПОЛЕ)
            created_at TEXT
        )
        """
    )

    # Индивидуальные полезные ссылки ученика
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS student_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            title TEXT,
            url TEXT
        )
        """
    )

    # Таблица для споров (оспаривание занятий)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER,
            student_id INTEGER,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            resolved_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS parent_links (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          parent_telegram_id INTEGER NOT NULL,
          student_id INTEGER NOT NULL,
          is_active INTEGER DEFAULT 1,
          created_at TEXT
        )
        """
    )

    # --- обращения/предложения от пользователей ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        role TEXT NOT NULL,           -- 'student' | 'parent'
        username TEXT,
        full_name TEXT,
        text TEXT NOT NULL,
        created_at TEXT,
        status TEXT DEFAULT 'new'     -- new/read/closed (на будущее)
    )
    """)
    conn.commit()


    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_parent_student
        ON parent_links(parent_telegram_id, student_id)
        """
    )

    cur.execute("""
       CREATE TABLE IF NOT EXISTS parents (
           telegram_id INTEGER PRIMARY KEY,
           username TEXT,
           full_name TEXT
           -- created_at может отсутствовать в старой БД, добавим миграцией ниже
       )
       """)
    conn.commit()

    # Backfill: все, кто уже есть в students, но без роли — считаем учениками
    cur.execute("""
         INSERT OR IGNORE INTO user_roles(telegram_id, role, created_at)
         SELECT telegram_id, 'student', ?
         FROM students
         WHERE telegram_id IS NOT NULL
     """, (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()

    # ✅ МИГРАЦИЯ: если база старая и created_at нет — добавим
    try:
        cur.execute("ALTER TABLE parents ADD COLUMN created_at TEXT")
    except sqlite3.OperationalError:
        pass

    # ✅ МИГРАЦИЯ: фикс-ставка занятия для ученика
    try:
        cur.execute("ALTER TABLE students ADD COLUMN lesson_price INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()

    conn.commit()

    # В init_db() после CREATE TABLE parent_requests ...
    try:
        cur.execute("ALTER TABLE parent_requests ADD COLUMN requested_student_id INTEGER")
    except sqlite3.OperationalError:
        pass

    # На случай, если старая change_requests была без comment
    try:
        cur.execute("ALTER TABLE change_requests ADD COLUMN comment TEXT")
    except sqlite3.OperationalError:
        pass

    # Добавляем поле is_active в weekly_lessons, если его нет
    try:
        cur.execute("ALTER TABLE weekly_lessons ADD COLUMN is_active INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    # Добавляем DEFAULT 60 к remind_before_minutes, если его нет
    try:
        cur.execute("ALTER TABLE weekly_lessons ADD COLUMN remind_before_minutes INTEGER DEFAULT 60")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE lesson_overrides ADD COLUMN remind_before_minutes INTEGER DEFAULT 60")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE lesson_overrides ADD COLUMN original_date TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE lesson_overrides ADD COLUMN original_time TEXT")
    except sqlite3.OperationalError:
        pass

    # Добавляем поле topic в lesson_history, если его нет
    try:
        cur.execute("ALTER TABLE lesson_history ADD COLUMN topic TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()

def ensure_students_has_price():
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(students)")
    cols = {r["name"] for r in cur.fetchall()}
    if "lesson_price" not in cols:
        cur.execute("ALTER TABLE students ADD COLUMN lesson_price INTEGER DEFAULT 0")
        conn.commit()

def get_student_debt(student_id: int) -> tuple[int, int, int]:
    """
    returns: (debt_sum, unpaid_count, price)
    """
    cur = conn.cursor()

    cur.execute("SELECT lesson_price FROM students WHERE id=?", (student_id,))
    st = cur.fetchone()
    price = int(st["lesson_price"] or 0) if st else 0

    cur.execute("""
        SELECT COUNT(*) AS cnt
        FROM lesson_history
        WHERE student_id=?
          AND status='done'
          AND (paid=0 OR paid IS NULL)
    """, (student_id,))
    cnt = int(cur.fetchone()["cnt"] or 0)

    return cnt * price, cnt, price



def upsert_parent(telegram_id: int, username: str | None, full_name: str | None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO parents(telegram_id, username, full_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
          username=excluded.username,
          full_name=excluded.full_name
        """,
        (telegram_id, username, full_name, datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()


# ---------- УКАЗАНИЕ ТЕМ ----------

@router.message(lambda message: message.text == "📚 Указать темы")
async def handle_set_topics_button(message: Message):
    """Обработка нажатия кнопки "Указать темы" """
    ensure_history_for_past_lessons(lookback_days=14, min_after_start_minutes=30)
    await cmd_set_topics(message)


# ---------- УТИЛИТЫ ----------


def is_teacher(message: Message) -> bool:
    return message.from_user.id in TEACHER_IDS


def weekday_to_name(weekday):
    if weekday is None:
        return "неизвестный день"

    if 0 <= weekday <= 6:
        return DAY_NAMES[weekday]  # DAY_NAMES у тебя уже есть
    return f"день {weekday}"



def parse_date_str(date_str: str) -> date | None:
    """
    Улучшенный парсер дат с поддержкой различных форматов.
    Принимает: ДД.ММ.ГГ, ДД.ММ.ГГГГ, ДД-ММ-ГГ, ДД/ММ/ГГГГ, ДД.ММ, ДД-ММ, ДД/ММ
    Также понимает: ДД ММ ГГГГ, ДД ММ ГГ, ДД ММ
    """
    try:
        # Убираем лишние пробелы
        date_str = date_str.strip()

        # Заменяем различные разделители на точки
        for sep in ['-', '/', ',', '\\', ' ']:
            date_str = date_str.replace(sep, '.')

        # Убираем возможные множественные точки
        parts = []
        current_part = ""
        for char in date_str:
            if char == '.':
                if current_part:
                    parts.append(current_part)
                    current_part = ""
            else:
                current_part += char
        if current_part:
            parts.append(current_part)

        # Если частей меньше 2 или больше 3 - ошибка
        if len(parts) < 2 or len(parts) > 3:
            return None

        # Преобразуем части в числа
        day = int(parts[0])
        month = int(parts[1])

        # Если год не указан, берем текущий
        if len(parts) == 2:
            year = datetime.now().year
        else:
            year_part = parts[2]
            # Если год указан двумя цифрами
            if len(year_part) == 2:
                year = 2000 + int(year_part)
            elif len(year_part) == 4:
                year = int(year_part)
            else:
                return None

        # Проверяем корректность даты
        return date(year, month, day)

    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка парсинга даты '{date_str}': {e}")
        return None


def get_student_by_username(username: str):
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE username = ?", (username,))
    return cur.fetchone()


def get_student_by_telegram_id(telegram_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE telegram_id = ?", (telegram_id,))
    return cur.fetchone()


def get_student_by_user_key(user_key: str):
    """@username или числовой telegram_id"""
    if user_key.startswith("@"):
        username = user_key[1:]
        return get_student_by_username(username)
    else:
        try:
            telegram_id = int(user_key)
        except ValueError:
            return None
        return get_student_by_telegram_id(telegram_id)


def get_all_students():
    cur = conn.cursor()
    cur.execute("""
        SELECT s.*
        FROM students s
        LEFT JOIN user_roles ur ON ur.telegram_id = s.telegram_id
        WHERE ur.role = 'student' OR ur.role IS NULL
        ORDER BY s.full_name, s.username, s.telegram_id
    """)
    return cur.fetchall()




def add_weekly_slot(student_id: int, weekday: int, time_str: str):
    """Добавляет новый слот с напоминанием по умолчанию 60 минут"""
    cur = conn.cursor()

    # Проверяем, не существует ли уже такой слот у ученика
    cur.execute(
        """
        SELECT id FROM weekly_lessons 
        WHERE student_id = ? AND weekday = ? AND time = ? AND is_active = 1
        """,
        (student_id, weekday, time_str)
    )
    existing = cur.fetchone()

    if existing:
        return None  # Слот уже существует

    cur.execute(
        """
        INSERT INTO weekly_lessons (student_id, weekday, time, remind_before_minutes)
        VALUES (?, ?, ?, 60)
        """,
        (student_id, weekday, time_str),
    )
    conn.commit()

    # Получаем данные ученика для уведомления
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    return student


def update_weekly_lesson_remind(lesson_id: int, remind_before: int):
    cur = conn.cursor()
    cur.execute(
        "UPDATE weekly_lessons SET remind_before_minutes = ? WHERE id = ?",
        (remind_before, lesson_id),
    )
    conn.commit()


def get_weekly_lessons_for_student(student_id: int, active_only: bool = True):
    cur = conn.cursor()
    if active_only:
        cur.execute(
            """
            SELECT w.*, s.telegram_id, s.username, s.full_name
            FROM weekly_lessons w
            JOIN students s ON s.id = w.student_id
            WHERE student_id = ? AND w.is_active = 1
            ORDER BY w.weekday, w.time
            """,
            (student_id,),
        )
    else:
        cur.execute(
            """
            SELECT w.*, s.telegram_id, s.username, s.full_name
            FROM weekly_lessons w
            JOIN students s ON s.id = w.student_id
            WHERE student_id = ?
            ORDER BY w.weekday, w.time
            """,
            (student_id,),
        )
    return cur.fetchall()


def get_weekly_lesson_by_id(lesson_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT w.*, s.telegram_id, s.username, s.full_name
        FROM weekly_lessons w
        JOIN students s ON s.id = w.student_id
        WHERE w.id = ?
        """,
        (lesson_id,),
    )
    return cur.fetchone()


def get_all_weekly_lessons(active_only: bool = True):
    cur = conn.cursor()
    if active_only:
        cur.execute(
            """
            SELECT w.*, s.telegram_id, s.username, s.full_name
            FROM weekly_lessons w
            JOIN students s ON s.id = w.student_id
            WHERE w.is_active = 1
            ORDER BY w.weekday, time(w.time), s.full_name
            """
        )
    else:
        cur.execute(
            """
            SELECT w.*, s.telegram_id, s.username, s.full_name
            FROM weekly_lessons w
            JOIN students s ON s.id = w.student_id
            ORDER BY w.weekday, time(w.time), s.full_name
            """
        )
    return cur.fetchall()



def deactivate_weekly_lesson(lesson_id: int):
    """Помечает слот как неактивный (удаляет)"""
    cur = conn.cursor()
    cur.execute(
        "UPDATE weekly_lessons SET is_active = 0 WHERE id = ?",
        (lesson_id,),
    )
    conn.commit()

    # Получаем данные для уведомления
    cur.execute(
        """
        SELECT w.*, s.telegram_id, s.username, s.full_name
        FROM weekly_lessons w
        JOIN students s ON s.id = w.student_id
        WHERE w.id = ?
        """,
        (lesson_id,)
    )
    return cur.fetchone()


# ---------- ОВЕРРАЙДЫ ----------


def get_overrides_for_date(target_date: date):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.*, w.student_id, w.weekday, w.time AS weekly_time,
               w.remind_before_minutes AS weekly_remind_before,
               s.telegram_id, s.username, s.full_name
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        JOIN students s ON s.id = w.student_id
        WHERE o.date = ? AND w.is_active = 1
        """,
        (target_date.isoformat(),),
    )
    return cur.fetchall()


def get_future_overrides_for_student(student_id: int, days_ahead: int = 30):
    today = date.today()
    end = today + timedelta(days=days_ahead)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.*, w.weekday, w.time AS weekly_time
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        WHERE w.student_id = ? AND w.is_active = 1
          AND o.date >= ?
          AND o.date <= ?
        ORDER BY o.date, o.new_time
        """,
        (student_id, today.isoformat(), end.isoformat()),
    )
    return cur.fetchall()


def get_future_overrides_for_all(days_ahead: int = 30):
    today = date.today()
    end = today + timedelta(days=days_ahead)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.*, w.student_id, w.weekday, w.time AS weekly_time,
               s.telegram_id, s.username, s.full_name
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        JOIN students s ON s.id = w.student_id
        WHERE o.date >= ?
          AND o.date <= ?
          AND w.is_active = 1
        ORDER BY o.date, o.new_time
        """,
        (today.isoformat(), end.isoformat()),
    )
    return cur.fetchall()


def get_override_by_id(override_id: int):
    """Получает оверрайд по ID"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT o.*, w.student_id, w.weekday, w.time AS weekly_time,
               s.telegram_id, s.username, s.full_name
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        JOIN students s ON s.id = w.student_id
        WHERE o.id = ?
        """,
        (override_id,),
    )
    return cur.fetchone()


def get_parent_ids_for_student(student_id: int) -> list[int]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT parent_telegram_id
        FROM parent_links
        WHERE student_id = ?
        """,
        (student_id,),
    )
    return [row["parent_telegram_id"] for row in cur.fetchall()]


def create_lesson_override(
        weekly_lesson_id: int,
        override_date: date,
        new_time: dtime,
        change_kind: str,
        original_date: date = None,
        original_time: str = None,
):
    """Создает оверрайд с напоминанием по умолчанию 60 минут"""
    cur = conn.cursor()

    # Удаляем старый оверрайд на эту же дату, если есть
    cur.execute(
        """
        DELETE FROM lesson_overrides 
        WHERE weekly_lesson_id = ? AND date = ?
        """,
        (weekly_lesson_id, override_date.isoformat()),
    )

    cur.execute(
        """
        INSERT INTO lesson_overrides
        (weekly_lesson_id, date, new_time, change_kind, remind_before_minutes, original_date, original_time)
        VALUES (?, ?, ?, ?, 60, ?, ?)
        """,
        (
            weekly_lesson_id,
            override_date.isoformat(),
            new_time.strftime("%H:%M"),
            change_kind,
            original_date.isoformat() if original_date else None,
            original_time,
        ),
    )
    conn.commit()

    # Получаем данные ученика для уведомления
    cur.execute(
        """
        SELECT w.*, s.telegram_id, s.username, s.full_name
        FROM weekly_lessons w
        JOIN students s ON s.id = w.student_id
        WHERE w.id = ?
        """,
        (weekly_lesson_id,)
    )
    return cur.fetchone()


def update_lesson_override(
        override_id: int,
        new_date: date,
        new_time: dtime,
        change_kind: str = None,
):
    """Обновляет существующий оверрайд"""
    cur = conn.cursor()

    if change_kind:
        cur.execute(
            """
            UPDATE lesson_overrides
            SET date = ?, new_time = ?, change_kind = ?
            WHERE id = ?
            """,
            (
                new_date.isoformat(),
                new_time.strftime("%H:%M"),
                change_kind,
                override_id,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE lesson_overrides
            SET date = ?, new_time = ?
            WHERE id = ?
            """,
            (
                new_date.isoformat(),
                new_time.strftime("%H:%M"),
                override_id,
            ),
        )
    conn.commit()

    # Получаем обновленные данные
    return get_override_by_id(override_id)


def delete_lesson_override(override_id: int):
    """Удаляет оверрайд"""
    cur = conn.cursor()

    # Получаем данные оверрайда перед удалением
    override = get_override_by_id(override_id)

    cur.execute(
        "DELETE FROM lesson_overrides WHERE id = ?",
        (override_id,),
    )
    conn.commit()

    return override


# ---------- УВЕДОМЛЕНИЯ ДЛЯ УЧЕНИКОВ ----------

async def notify_student_about_schedule_change(student_telegram_id: int, message: str):
    """Отправляет уведомление ученику об изменении расписания"""
    try:
        await bot.send_message(
            student_telegram_id,
            message,
            parse_mode="HTML"
        )
        logging.info(f"Уведомление отправлено ученику {student_telegram_id}")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление ученику {student_telegram_id}: {e}")


async def notify_new_regular_lesson(student_telegram_id: int, weekday: int, time_str: str):
    """Уведомление о новом регулярном занятии"""
    weekday_name = weekday_to_name(weekday)
    message = (
        f"📅 <b>Добавлено новое регулярное занятие!</b>\n\n"
        f"• День: <b>{weekday_name}</b>\n"
        f"• Время: <b>{time_str}</b>\n"
        f"• Напоминание: за <b>60</b> минут до начала\n\n"
        f"Занятие будет проходить каждую неделю в это время. "
        f"Используйте команду /set_remind чтобы изменить время напоминания."
    )
    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_one_time_change(student_telegram_id: int, change_date: date, new_time: str,
                                 old_weekday: int, old_time: str, is_cancellation: bool = False):
    """Уведомление о разовом переносе или отмене"""
    weekday_old = weekday_to_name(old_weekday)
    date_str = change_date.strftime("%d.%m.%Y")

    if is_cancellation:
        message = (
            f"❌ <b>Занятие отменено!</b>\n\n"
            f"• Дата: <b>{date_str}</b>\n"
            f"• Обычное время: {weekday_old} {old_time}\n\n"
            f"Это разовая отмена. Регулярное занятие остаётся без изменений."
        )
    else:
        message = (
            f"🔄 <b>Занятие перенесено!</b>\n\n"
            f"• Новая дата: <b>{date_str}</b>\n"
            f"• Новое время: <b>{new_time}</b>\n"
            f"• Обычно: {weekday_old} {old_time}\n\n"
            f"Это разовый перенос. Регулярное занятие остаётся без изменений."
        )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_override_rescheduled(student_telegram_id: int, old_date: date, old_time: str,
                                      new_date: date, new_time: str):
    """Уведомление о переносе уже перенесенного занятия"""
    old_date_str = old_date.strftime("%d.%m.%Y")
    new_date_str = new_date.strftime("%d.%m.%Y")

    message = (
        f"🔄 <b>Перенесенное занятие изменено!</b>\n\n"
        f"• Было: <b>{old_date_str} в {old_time}</b>\n"
        f"• Стало: <b>{new_date_str} в {new_time}</b>\n\n"
        f"Это разовый перенос. Регулярное занятие остаётся без изменений."
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_permanent_change(student_telegram_id: int, old_weekday: int, old_time: str,
                                  new_weekday: int, new_time: str):
    """Уведомление о постоянном изменении расписания"""
    old_weekday_name = weekday_to_name(old_weekday)
    new_weekday_name = weekday_to_name(new_weekday)

    message = (
        f"🔄 <b>Расписание изменено на постоянной основе!</b>\n\n"
        f"<s>• Было: {old_weekday_name} {old_time}</s>\n"
        f"• Стало: <b>{new_weekday_name} {new_time}</b>\n\n"
        f"Теперь занятие будет проходить каждую неделю в новое время."
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_reminder_changed(student_telegram_id: int, weekday: int, time_str: str, new_remind: int):
    """Уведомление об изменении времени напоминания"""
    weekday_name = weekday_to_name(weekday)

    message = (
        f"⏰ <b>Изменено время напоминания</b>\n\n"
        f"• Занятие: {weekday_name} {time_str}\n"
        f"• Новое напоминание: за <b>{new_remind}</b> минут до начала\n\n"
        f"Теперь вы будете получать уведомление за указанное время до занятия."
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_homework_assigned(student_telegram_id: int, homework_text: str):
    """Уведомление о новом домашнем задании"""
    message = (
        f"📚 <b>Новое домашнее задание!</b>\n\n"
        f"{homework_text}\n\n"
        f"Когда выполните задание, используйте команду /done_hw"
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_homework_done(student_telegram_id: int, homework_id: int):
    """Уведомление о выполненном домашнем задании"""
    message = (
        f"✅ <b>Домашнее задание отмечено как выполненное!</b>\n\n"
        f"Задание #{homework_id} проверено преподавателем.\n"
        f"Молодец! Продолжайте в том же духе! 💪"
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_payment_status(student_telegram_id: int, lesson_date: date, lesson_time: str, is_paid: bool):
    """Уведомление об изменении статуса оплаты"""
    date_str = lesson_date.strftime("%d.%m.%Y")

    if is_paid:
        message = (
            f"💰 <b>Занятие оплачено!</b>\n\n"
            f"• Дата: {date_str}\n"
            f"• Время: {lesson_time}\n\n"
            f"Статус оплаты изменён на <b>оплачено</b>."
        )
    else:
        message = (
            f"⚠️ <b>Статус оплаты изменён</b>\n\n"
            f"• Дата: {date_str}\n"
            f"• Время: {lesson_time}\n\n"
            f"Статус оплаты изменён на <b>не оплачено</b>."
        )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_slot_deleted(student_telegram_id: int, weekday: int, time_str: str):
    """Уведомление об удалении регулярного занятия"""
    weekday_name = weekday_to_name(weekday)

    message = (
        f"🗑️ <b>Регулярное занятие отменено!</b>\n\n"
        f"• День: {weekday_name}\n"
        f"• Время: {time_str}\n\n"
        f"Это занятие больше не будет проходить. Если нужно возобновить занятия, "
        f"свяжитесь с преподавателем."
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


async def notify_student_deleted(student_telegram_id: int):
    """Уведомление об удалении ученика из системы"""
    message = (
        f"🚫 <b>Вы были удалены из системы бота!</b>\n\n"
        f"Если это произошло по ошибке или вы хотите продолжить занятия, "
        f"пожалуйста, зарегистрируйтесь снова, отправив команду /start."
    )

    try:
        await bot.send_message(
            student_telegram_id,
            message,
            parse_mode="HTML"
        )
        logging.info(f"Уведомление об удалении отправлено ученику {student_telegram_id}")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление об удалении ученику {student_telegram_id}: {e}")


async def notify_dispute_created(student_telegram_id: int, history_id: int, reason: str):
    """Уведомление ученику о создании спора"""
    message = (
        f"⚖️ <b>Спор создан!</b>\n\n"
        f"Вы оспорили запись #{history_id} в истории занятий.\n"
        f"Причина: {reason}\n\n"
        f"Преподаватель рассмотрит ваш спор в ближайшее время."
    )

    try:
        await bot.send_message(
            student_telegram_id,
            message,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление о споре ученику {student_telegram_id}: {e}")


async def notify_teachers_about_dispute(history_id: int, student_name: str, reason: str):
    """Уведомление преподавателей о новом споре"""
    message = (
        f"⚖️ <b>Новый спор!</b>\n\n"
        f"Ученик: {student_name}\n"
        f"Запись в истории: #{history_id}\n"
        f"Причина: {reason}\n\n"
        f"Проверьте историю занятий и разрешите спор."
    )

    for admin_id in TEACHER_IDS:
        try:
            await bot.send_message(
                admin_id,
                message,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление о споре преподавателю {admin_id}: {e}")


# ---------- ЗАПРОСЫ НА ПЕРЕНОС ----------


def create_change_request(
        student_id: int,
        weekly_lesson_id: int,
        old_weekday: int,
        old_time: str,
        new_date: date,
        new_time: dtime,
        change_kind: str,
        comment: str | None,
):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO change_requests
        (student_id, weekly_lesson_id, old_weekday, old_time, new_date, new_time,
         change_kind, comment, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            student_id,
            weekly_lesson_id,
            old_weekday,
            old_time,
            new_date.isoformat(),
            new_time.strftime("%H:%M"),
            change_kind,
            comment,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_pending_requests():
    cleanup_old_requests()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT cr.*, s.username, s.full_name, s.telegram_id
        FROM change_requests cr
        JOIN students s ON s.id = cr.student_id
        WHERE status = 'pending'
        ORDER BY created_at
        """
    )
    return cur.fetchall()


def get_change_request_by_id(req_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT cr.*, s.username, s.full_name, s.telegram_id
        FROM change_requests cr
        JOIN students s ON s.id = cr.student_id
        WHERE cr.id = ?
        """,
        (req_id,),
    )
    return cur.fetchone()


def update_change_request_status(req_id: int, status: str):
    cur = conn.cursor()
    cur.execute("UPDATE change_requests SET status=? WHERE id=?", (status, req_id))
    conn.commit()


def approve_transfer_request(req_id: int):
    """
    Одобрение запроса:
    - one_time  -> lesson_overrides(change_kind='one_time')
    - cancel    -> lesson_overrides(change_kind='cancel')
    - permanent -> обновляем weekly_lessons (weekday берём из new_date.weekday())
    Возвращает dict запроса (для уведомлений) либо None.
    """
    r = get_change_request_by_id(req_id)
    if not r or r["status"] != "pending":
        return None

    wl = get_weekly_lesson_by_id(r["weekly_lesson_id"])
    if not wl:
        return None

    # даты/время
    d = date.fromisoformat(r["new_date"]) if r["new_date"] else None

    new_time = parse_time_str(r["new_time"]) if r["new_time"] else parse_time_str(r["old_time"])

    if r["change_kind"] in ("one_time", "cancel"):
        # original_date/original_time можно хранить, но в текущей логике не критично
        create_lesson_override(
            weekly_lesson_id=r["weekly_lesson_id"],
            new_date=d,
            new_time=new_time,
            change_kind=r["change_kind"],  # 'one_time' или 'cancel'
            original_date=None,
            original_time=None,
        )

    elif r["change_kind"] == "permanent":
        # ВАЖНО: у вас new_weekday в БД отдельно не хранится, поэтому берём weekday из new_date
        new_weekday = d.weekday() if d else int(r["old_weekday"])
        cur = conn.cursor()
        cur.execute(
            "UPDATE weekly_lessons SET weekday=?, time=? WHERE id=?",
            (new_weekday, new_time.strftime("%H:%M"), r["weekly_lesson_id"])
        )
        conn.commit()

    else:
        return None

    update_change_request_status(req_id, "approved")
    return dict(r)


def reject_transfer_request(req_id: int):
    """Отклонение запроса. Возвращает dict запроса либо None."""
    r = get_change_request_by_id(req_id)
    if not r or r["status"] != "pending":
        return None

    update_change_request_status(req_id, "rejected")
    return dict(r)



# ---------- СПОРЫ ----------

def create_dispute(history_id: int, student_id: int, reason: str):
    """Создает запись о споре"""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO disputes (history_id, student_id, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (history_id, student_id, reason, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return cur.lastrowid


def get_dispute_by_id(dispute_id: int):
    """Получает спор по ID"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.*, s.username, s.full_name, s.telegram_id,
               lh.date, lh.time, lh.status, lh.paid, lh.note, lh.topic
        FROM disputes d
        JOIN students s ON s.id = d.student_id
        JOIN lesson_history lh ON lh.id = d.history_id
        WHERE d.id = ?
        """,
        (dispute_id,),
    )
    return cur.fetchone()


def get_pending_disputes():
    """Получает все необработанные споры"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.*, s.username, s.full_name, s.telegram_id,
               lh.date, lh.time, lh.status, lh.paid, lh.topic
        FROM disputes d
        JOIN students s ON s.id = d.student_id
        JOIN lesson_history lh ON lh.id = d.history_id
        WHERE d.status = 'pending'
        ORDER BY d.created_at
        """
    )
    return cur.fetchall()


def update_dispute_status(dispute_id: int, status: str):
    """Обновляет статус спора"""
    cur = conn.cursor()
    resolved_at = datetime.now().isoformat(timespec="seconds") if status in ['resolved', 'rejected'] else None
    cur.execute(
        "UPDATE disputes SET status = ?, resolved_at = ? WHERE id = ?",
        (status, resolved_at, dispute_id),
    )
    conn.commit()


# ---------- ДОМАШКА ----------


def add_homework(student_id: int, text: str):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO homeworks (student_id, text, created_at, is_done)
        VALUES (?, ?, ?, 0)
        """,
        (student_id, text, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def get_homeworks_for_student(student_id: int, only_open: bool = True):
    cur = conn.cursor()
    if only_open:
        cur.execute(
            """
            SELECT * FROM homeworks
            WHERE student_id = ? AND is_done = 0
            ORDER BY id DESC

            """,
            (student_id,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM homeworks
            WHERE student_id = ?
            ORDER BY id DESC

            """,
            (student_id,),
        )
    return cur.fetchall()


def get_homework_by_id(hw_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT h.*, s.telegram_id, s.username, s.full_name
        FROM homeworks h
        JOIN students s ON s.id = h.student_id
        WHERE h.id = ?
        """,
        (hw_id,),
    )
    return cur.fetchone()


def mark_homework_done(hw_id: int):
    cur = conn.cursor()
    cur.execute(
        "UPDATE homeworks SET is_done = 1 WHERE id = ?",
        (hw_id,),
    )
    conn.commit()


# ---------- ИСТОРИЯ ЗАНЯТИЙ / ОПЛАТА ----------


def add_lesson_history(
        student_id: int,
        lesson_date: date,
        lesson_time: dtime,
        status: str,  # 'done' / 'cancelled'
        paid: bool = False,
        note: str | None = None,
        topic: str | None = None,
        weekly_lesson_id: Optional[int] = None,
):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO lesson_history
        (student_id, weekly_lesson_id, date, time, status, paid, note, topic, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            student_id,
            weekly_lesson_id,
            lesson_date.isoformat(),
            lesson_time.strftime("%H:%M"),
            status,
            1 if paid else 0,
            note,
            topic,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_lesson_history(
        history_id: int,
        status: str | None = None,
        paid: bool | None = None,
        note: str | None = None,
        topic: str | None = None,
        lesson_date: str | None = None,   # <-- НОВОЕ (ISO YYYY-MM-DD)
        lesson_time: str | None = None,   # <-- НОВОЕ (HH:MM)
):
    cur = conn.cursor()

    updates = []
    params = []

    if status is not None:
        updates.append("status = ?")
        params.append(status)

    if paid is not None:
        updates.append("paid = ?")
        params.append(1 if paid else 0)

    if note is not None:
        updates.append("note = ?")
        params.append(note)

    if topic is not None:
        updates.append("topic = ?")
        params.append(topic)

    if lesson_date is not None:
        updates.append("date = ?")
        params.append(lesson_date)

    if lesson_time is not None:
        updates.append("time = ?")
        params.append(lesson_time)

    if not updates:
        return None

    params.append(history_id)
    query = f"UPDATE lesson_history SET {', '.join(updates)} WHERE id = ?"
    cur.execute(query, tuple(params))
    conn.commit()

    return get_lesson_history_by_id(history_id)



def delete_lesson_history(history_id: int):
    """Удаляет запись из истории занятий"""
    cur = conn.cursor()

    # Получаем данные перед удалением
    history_record = get_lesson_history_by_id(history_id)

    cur.execute("DELETE FROM lesson_history WHERE id = ?", (history_id,))
    conn.commit()

    return history_record


def get_lesson_history_for_student(student_id: int, limit: int = 20):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM lesson_history
        WHERE student_id = ?
        ORDER BY date DESC, time DESC
        LIMIT ?
        """,
        (student_id, limit),
    )
    return cur.fetchall()


def get_lesson_history_by_id(history_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT lh.*, s.telegram_id, s.username, s.full_name
        FROM lesson_history lh
        JOIN students s ON s.id = lh.student_id
        WHERE lh.id = ?
        """,
        (history_id,),
    )
    return cur.fetchone()


def get_lesson_history_for_date(lesson_date: date):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT lh.*, s.telegram_id, s.username, s.full_name
        FROM lesson_history lh
        JOIN students s ON s.id = lh.student_id
        WHERE lh.date = ?
        ORDER BY lh.time
        """,
        (lesson_date.isoformat(),),
    )
    return cur.fetchall()

def get_done_lessons_without_topic(min_after_start_minutes: int = 30):
    """
    Возвращает занятия из истории (status='done') без темы,
    но только те, у которых прошло минимум min_after_start_minutes
    от времени начала (date + time).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT lh.*, s.full_name
        FROM lesson_history lh
        JOIN students s ON s.id = lh.student_id
        WHERE lh.status = 'done'
          AND (
                lh.topic IS NULL
                OR TRIM(lh.topic) = ''
                OR TRIM(LOWER(lh.topic)) = 'тема не указана'
              )
        ORDER BY lh.date DESC, lh.time DESC
    """)
    rows = cur.fetchall()

    cutoff = datetime.now() - timedelta(minutes=min_after_start_minutes)

    filtered = []
    for r in rows:
        try:
            # date: YYYY-MM-DD, time: HH:MM
            dt = datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M")
        except Exception:
            # если в данных вдруг мусор — просто пропускаем
            continue

        if dt <= cutoff:
            filtered.append(r)

    return filtered



def set_lesson_paid(history_id: int, paid: int):
    cur = conn.cursor()
    cur.execute("UPDATE lesson_history SET paid = ? WHERE id = ?", (paid, history_id))
    conn.commit()

    cur.execute("""
        SELECT lh.date, lh.time, lh.student_id, s.telegram_id
        FROM lesson_history lh
        JOIN students s ON s.id = lh.student_id
        WHERE lh.id = ?
    """, (history_id,))
    row = cur.fetchone()
    if row:
        return row["date"], row["time"], row["telegram_id"], row["student_id"]
    return None, None, None, None



def set_lesson_status(history_id: int, status: str):
    cur = conn.cursor()
    cur.execute(
        "UPDATE lesson_history SET status = ? WHERE id = ?",
        (status, history_id),
    )
    conn.commit()

def role_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨‍🎓 Я ученик")],
            [KeyboardButton(text="👨‍👩‍👧 Я родитель")],
        ],
        resize_keyboard=True
    )


def history_entry_exists(
        student_id: int, weekly_lesson_id: int, lesson_date: date, lesson_time: dtime
):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM lesson_history
        WHERE student_id = ? AND weekly_lesson_id = ? AND date = ? AND time = ?
        """,
        (
            student_id,
            weekly_lesson_id,
            lesson_date.isoformat(),
            lesson_time.strftime("%H:%M"),
        ),
    )
    return cur.fetchone() is not None


def get_lessons_for_date(target_date: date):
    """
    Возвращает список занятий на дату (учитывая оверрайды).
    """
    lessons_for_day = []
    weekday = target_date.weekday()

    overrides = get_overrides_for_date(target_date)
    overridden_ids = {o["weekly_lesson_id"] for o in overrides}

    all_weekly = get_all_weekly_lessons()

    # Регулярные занятия без оверрайдов
    for wl in all_weekly:
        if wl["weekday"] != weekday:
            continue
        if wl["id"] in overridden_ids:
            continue

        lessons_for_day.append(
            {
                "weekly_lesson_id": wl["id"],
                "student_id": wl["student_id"],
                "telegram_id": wl["telegram_id"],
                "full_name": wl["full_name"],
                "username": wl["username"],
                "time": wl["time"],
                "change_kind": None,
            }
        )

    # Оверрайды
    for o in overrides:
        if o["change_kind"] == "cancel":
            time_to_use = o["weekly_time"]
        else:
            time_to_use = o["new_time"]

        lessons_for_day.append(
            {
                "weekly_lesson_id": o["weekly_lesson_id"],
                "student_id": o["student_id"],
                "telegram_id": o["telegram_id"],
                "full_name": o["full_name"],
                "username": o["username"],
                "time": time_to_use,
                "change_kind": o["change_kind"],
            }
        )

    return lessons_for_day


# ---------- УДАЛЕНИЕ УЧЕНИКА ----------

def delete_student_by_id(student_id: int):
    """Удаляет ученика и все связанные данные, возвращает telegram_id удаленного ученика"""
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM students WHERE id = ?", (student_id,))
    row = cur.fetchone()
    if row is None:
        return None

    telegram_id = row["telegram_id"]

    # связанные записи ученика
    cur.execute("DELETE FROM weekly_lessons WHERE student_id = ?", (student_id,))
    cur.execute("DELETE FROM change_requests WHERE student_id = ?", (student_id,))
    cur.execute(
        "DELETE FROM lesson_overrides WHERE weekly_lesson_id IN (SELECT id FROM weekly_lessons WHERE student_id = ?)",
        (student_id,)
    )
    cur.execute("DELETE FROM homeworks WHERE student_id = ?", (student_id,))
    cur.execute("DELETE FROM lesson_history WHERE student_id = ?", (student_id,))
    cur.execute("DELETE FROM student_links WHERE student_id = ?", (student_id,))
    cur.execute("DELETE FROM disputes WHERE student_id = ?", (student_id,))

    # ✅ ВАЖНО: удалить родительские привязки/заявки к этому ученику
    cur.execute("DELETE FROM parent_links WHERE student_id = ?", (student_id,))
    cur.execute("DELETE FROM parent_requests WHERE requested_student_id = ?", (student_id,))

    # удалить ученика
    cur.execute("DELETE FROM students WHERE id = ?", (student_id,))

    # ✅ ВАЖНО: удалить роль этого telegram_id (иначе при /start он останется “учеником/родителем”)
    cur.execute("DELETE FROM user_roles WHERE telegram_id = ?", (telegram_id,))

    conn.commit()
    return telegram_id



# ---------- ПОЛЕЗНЫЕ ССЫЛКИ ----------


def get_links_for_student(student_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM student_links
        WHERE student_id = ?
        ORDER BY id
        """,
        (student_id,),
    )
    return cur.fetchall()


def replace_links_for_student(student_id: int, links: list[tuple[str, str]]):
    cur = conn.cursor()
    cur.execute("DELETE FROM student_links WHERE student_id = ?", (student_id,))
    for title, url in links:
        cur.execute(
            """
            INSERT INTO student_links (student_id, title, url)
            VALUES (?, ?, ?)
            """,
            (student_id, title, url),
        )
    conn.commit()

def get_all_parents():
    cur = conn.cursor()
    cur.execute("""
        SELECT
            ur.telegram_id,
            p.full_name AS parent_full_name,
            p.username AS parent_username
        FROM user_roles ur
        LEFT JOIN parents p ON p.telegram_id = ur.telegram_id
        WHERE ur.role = 'parent'
        ORDER BY COALESCE(p.full_name, p.username, CAST(ur.telegram_id AS TEXT))
    """)
    return cur.fetchall()

def delete_parent_completely(parent_telegram_id: int):
    cur = conn.cursor()
    cur.execute("DELETE FROM parent_links WHERE parent_telegram_id = ?", (parent_telegram_id,))
    cur.execute("DELETE FROM parent_requests WHERE parent_telegram_id = ?", (parent_telegram_id,))
    cur.execute("DELETE FROM parents WHERE telegram_id = ?", (parent_telegram_id,))
    cur.execute("DELETE FROM user_roles WHERE telegram_id = ?", (parent_telegram_id,))
    conn.commit()


def delete_user_kind_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👨‍🎓 Ученик", callback_data="deluser_kind_student")
    kb.button(text="👨‍👩‍👧 Родитель", callback_data="deluser_kind_parent")
    kb.button(text="⬅️ Назад", callback_data="deluser_cancel")
    kb.adjust(1)
    return kb.as_markup()

FEEDBACK_LIST_PREFIX = "fb_list_"
FEEDBACK_OPEN_PREFIX = "fb_open_"
FEEDBACK_DONE_PREFIX = "fb_done_"
FEEDBACK_BACK_PREFIX = "fb_back_"

def get_feedback_items(statuses=("new", "read")):
    cur = conn.cursor()
    q_marks = ",".join("?" for _ in statuses)
    cur.execute(
        f"""
        SELECT id, telegram_id, role, username, full_name, text, created_at, status
        FROM feedback
        WHERE status IN ({q_marks})
        ORDER BY id DESC
        """,
        tuple(statuses),
    )
    return cur.fetchall()

def set_feedback_status(feedback_id: int, status: str):
    cur = conn.cursor()
    cur.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))
    conn.commit()

def build_feedback_list_kb(items, page: int = 0, page_size: int = 10):
    # пагинация через твой Paginator :contentReference[oaicite:2]{index=2}
    page_items, page, total_pages, _ = Paginator.get_page(items, page, page_size)

    kb = InlineKeyboardBuilder()

    for it in page_items:
        fid = int(it["id"])
        name = it["full_name"] or it["username"] or str(it["telegram_id"])
        txt = (it["text"] or "").replace("\n", " ").strip()
        short = txt[:30] + ("…" if len(txt) > 30 else "")
        # кнопка открытия карточки
        kb.button(
            text=f"#{fid} — {name}: {short}",
            callback_data=f"{FEEDBACK_OPEN_PREFIX}{fid}_{page}",
        )

    kb.adjust(1)

    # навигация страницами
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"{FEEDBACK_LIST_PREFIX}{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="page_info"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"{FEEDBACK_LIST_PREFIX}{page+1}"))
    if total_pages > 1:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="⬅️ Назад в меню", callback_data=f"{FEEDBACK_BACK_PREFIX}menu"))
    return kb.as_markup()



def create_parent_action_keyboard(parents, action_type: str, page: int = 0, page_size: int = 10):
    total = len(parents)
    total_pages = (total + page_size - 1) // page_size
    page = max(0, min(page, max(0, total_pages - 1)))

    start = page * page_size
    end = start + page_size
    slice_ = parents[start:end]

    kb = InlineKeyboardBuilder()

    for p in slice_:
        tg_id = p["telegram_id"]
        title = p["parent_full_name"] or (f"@{p['parent_username']}" if p["parent_username"] else str(tg_id))
        kb.button(text=f"👨‍👩‍👧 {title}", callback_data=f"{action_type}_parent_{tg_id}_{page}")

    # пагинация
    nav = []
    if total_pages > 1:
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{action_type}_page_{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{action_type}_page_{page+1}"))
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="deluser_cancel"))
    return kb.as_markup(), total_pages


@router.message(lambda m: m.text == ADMIN_FEEDBACK_TEXT)
async def admin_feedback_menu(message: Message, state: FSMContext):
    if not is_teacher(message):  # у тебя проверка админа тут :contentReference[oaicite:3]{index=3}
        return

    items = get_feedback_items(statuses=("new", "read"))
    if not items:
        await message.answer("✅ Замечаний нет (всё закрыто).", reply_markup=main_menu_keyboard(True))
        return

    await state.update_data(feedback_items=[dict(x) for x in items])  # чтобы не терять список при пагинации

    kb = build_feedback_list_kb(items, page=0, page_size=10)
    await message.answer(
        "🛠️ <b>Замечания/предложения</b>\n\n"
        "Нажми на пункт, чтобы открыть и отметить «исправлено».",
        parse_mode="HTML",
        reply_markup=kb,
    )

@router.callback_query(lambda c: c.data.startswith(FEEDBACK_LIST_PREFIX))
async def admin_feedback_list_page(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.replace(FEEDBACK_LIST_PREFIX, ""))
    data = await state.get_data()
    items = data.get("feedback_items", [])

    if not items:
        await callback_query.answer("Список пуст")
        return

    kb = build_feedback_list_kb(items, page=page, page_size=10)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer()

def build_feedback_card_kb(feedback_id: int, back_page: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Исправлено", callback_data=f"{FEEDBACK_DONE_PREFIX}{feedback_id}_{back_page}")
    kb.button(text="⬅️ Назад к списку", callback_data=f"{FEEDBACK_LIST_PREFIX}{back_page}")
    kb.adjust(1)
    return kb.as_markup()

@router.callback_query(lambda c: c.data.startswith(FEEDBACK_OPEN_PREFIX))
async def admin_feedback_open(callback_query: CallbackQuery, state: FSMContext):
    # fb_open_{id}_{page}
    rest = callback_query.data.replace(FEEDBACK_OPEN_PREFIX, "")
    fid_str, page_str = rest.split("_")
    fid = int(fid_str)
    back_page = int(page_str)

    data = await state.get_data()
    items = data.get("feedback_items", [])
    item = next((x for x in items if int(x.get("id")) == fid), None)

    if not item:
        await callback_query.answer("Не нашёл замечание (обнови список)")
        return

    name = item.get("full_name") or item.get("username") or str(item.get("telegram_id"))
    role = item.get("role")
    created = item.get("created_at") or ""
    text = item.get("text") or ""

    await callback_query.message.edit_text(
        f"🛠️ <b>Замечание #{fid}</b>\n"
        f"👤 {name} ({role})\n"
        f"🕒 {created}\n\n"
        f"{text}",
        parse_mode="HTML",
        reply_markup=build_feedback_card_kb(fid, back_page),
    )
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith(FEEDBACK_DONE_PREFIX))
async def admin_feedback_done(callback_query: CallbackQuery, state: FSMContext):
    # fb_done_{id}_{page}
    rest = callback_query.data.replace(FEEDBACK_DONE_PREFIX, "")
    fid_str, page_str = rest.split("_")
    fid = int(fid_str)
    back_page = int(page_str)

    set_feedback_status(fid, "closed")

    # обновим список в состоянии (уберем закрытое)
    data = await state.get_data()
    items = data.get("feedback_items", [])
    items = [x for x in items if int(x.get("id")) != fid]
    await state.update_data(feedback_items=items)

    if not items:
        await callback_query.message.edit_text("✅ Все замечания закрыты.")
        await callback_query.message.answer("Возвращаю в меню.", reply_markup=main_menu_keyboard(True))
        await callback_query.answer("Готово")
        return

    kb = build_feedback_list_kb(items, page=min(back_page, max(0, (len(items)-1)//10)), page_size=10)
    await callback_query.message.edit_text(
        "🛠️ <b>Замечания/предложения</b>\n\n"
        "Нажми на пункт, чтобы открыть и отметить «исправлено».",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback_query.answer("Отмечено как исправленное ✅")


@router.callback_query(lambda c: c.data.startswith(FEEDBACK_BACK_PREFIX))
async def admin_feedback_back(callback_query: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback_query.message.delete()
    await callback_query.message.answer("Возвращаю в главное меню.", reply_markup=main_menu_keyboard(True))
    await callback_query.answer()



@router.message(lambda m: m.text == "🗑️ Удалить пользователя")
async def admin_delete_user_start(message: Message, state: FSMContext):
    if not is_teacher(message):
        return

    await state.clear()
    await state.set_state(DeleteUserStates.choosing_kind)

    await message.answer(
        "🗑️ <b>Удаление пользователя</b>\n\nКого удаляем?",
        parse_mode="HTML",
        reply_markup=delete_user_kind_keyboard()
    )


@router.callback_query(lambda c: c.data == "deluser_kind_student", DeleteUserStates.choosing_kind)
async def deluser_kind_student(cb: CallbackQuery, state: FSMContext):
    students = get_all_students()
    if not students:
        await cb.message.edit_text("Пока нет ни одного ученика.")
        await cb.answer()
        await state.clear()
        return

    await state.update_data(del_students=students)
    await state.set_state(DeleteUserStates.choosing_student)

    kb, _ = create_action_keyboard(students, "delstudent", page=0)
    await cb.message.edit_text("👨‍🎓 Выберите ученика для удаления:", reply_markup=kb)
    await cb.answer()


@router.callback_query(lambda c: c.data.startswith("delstudent_page_"), DeleteUserStates.choosing_student)
async def delstudent_page(cb: CallbackQuery, state: FSMContext):
    page = int(cb.data.split("_")[2])
    data = await state.get_data()
    students = data.get("del_students", [])

    kb, _ = create_action_keyboard(students, "delstudent", page=page)
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()


@router.callback_query(lambda c: c.data.startswith("delstudent_student_"), DeleteUserStates.choosing_student)
async def delstudent_pick(cb: CallbackQuery, state: FSMContext):
    _, _, student_id, page = cb.data.split("_")
    student_id = int(student_id)

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    st = cur.fetchone()
    if not st:
        await cb.answer("Ученик не найден")
        return

    title = st["full_name"] or (f"@{st['username']}" if st["username"] else str(st["telegram_id"]))

    await state.update_data(del_kind="student", del_student_id=student_id)
    await state.set_state(DeleteUserStates.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data="deluser_confirm")
    kb.button(text="❌ Отмена", callback_data="deluser_cancel")
    kb.adjust(1)

    await cb.message.edit_text(
        f"⚠️ Удалить ученика <b>{title}</b>?\n\n"
        "Будут удалены расписание/история/домашка и роль пользователя.",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
    await cb.answer()


@router.callback_query(lambda c: c.data == "deluser_kind_parent", DeleteUserStates.choosing_kind)
async def deluser_kind_parent(cb: CallbackQuery, state: FSMContext):
    parents = get_all_parents()
    if not parents:
        await cb.message.edit_text("Пока нет ни одного родителя.")
        await cb.answer()
        await state.clear()
        return

    await state.update_data(del_parents=parents)
    await state.set_state(DeleteUserStates.choosing_parent)

    kb, _ = create_parent_action_keyboard(parents, "delparent", page=0, page_size=10)
    await cb.message.edit_text("👨‍👩‍👧 Выберите родителя для удаления:", reply_markup=kb)
    await cb.answer()

@router.callback_query(lambda c: c.data.startswith("delparent_page_"), DeleteUserStates.choosing_parent)
async def delparent_page(cb: CallbackQuery, state: FSMContext):
    page = int(cb.data.split("_")[2])
    data = await state.get_data()
    parents = data.get("del_parents", [])

    kb, _ = create_parent_action_keyboard(parents, "delparent", page=page, page_size=10)
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()

@router.callback_query(lambda c: c.data.startswith("delparent_parent_"), DeleteUserStates.choosing_parent)
async def delparent_pick(cb: CallbackQuery, state: FSMContext):
    _, _, tg_id, page = cb.data.split("_")
    tg_id = int(tg_id)

    cur = conn.cursor()
    cur.execute("SELECT full_name, username FROM parents WHERE telegram_id = ?", (tg_id,))
    p = cur.fetchone()

    title = None
    if p:
        title = p["full_name"] or (f"@{p['username']}" if p["username"] else None)
    if not title:
        title = str(tg_id)

    await state.update_data(del_kind="parent", del_parent_tg=tg_id)
    await state.set_state(DeleteUserStates.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data="deluser_confirm")
    kb.button(text="❌ Отмена", callback_data="deluser_cancel")
    kb.adjust(1)

    await cb.message.edit_text(
        f"⚠️ Удалить родителя <b>{title}</b>?\n\n"
        "Будут удалены: роль, заявки, привязки и запись в таблице родителей.",
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )
    await cb.answer()

@router.callback_query(lambda c: c.data == "deluser_confirm", DeleteUserStates.confirming)
async def deluser_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kind = data.get("del_kind")  # <-- важно: del_kind

    if kind == "student":
        student_id = data.get("del_student_id")
        if not student_id:
            await cb.answer("❌ Не выбран ученик.", show_alert=True)
            return

        # Родителей нужно получить ДО удаления ученика (если удаляются связи)
        parent_ids = get_parent_ids_for_student(student_id)

        delete_student_by_id(student_id)
        await cb.message.edit_text("✅ Ученик удалён.")
        await cb.answer()
        await state.clear()

        # Уведомляем родителей
        for p_id in parent_ids:
            try:
                await cb.bot.send_message(
                    p_id,
                    "ℹ️ Ученик был удалён из базы. Если это ошибка — напишите преподавателю."
                )
            except Exception:
                pass
        return

    elif kind == "parent":
        parent_tg = data.get("del_parent_tg")
        if not parent_tg:
            await cb.answer("❌ Не выбран родитель.", show_alert=True)
            return

        # Если ты хочешь уведомлять самого родителя о том, что его удалили:
        try:
            await cb.bot.send_message(
                parent_tg,
                "ℹ️ Ваш профиль родителя был удалён из базы. Если это ошибка — напишите преподавателю."
            )
        except Exception:
            pass

        delete_parent_completely(parent_tg)
        await cb.message.edit_text("✅ Родитель удалён.")
        await cb.answer()
        await state.clear()
        return

    else:
        await cb.message.edit_text("Что-то пошло не так (неизвестный тип удаления).")
        await cb.answer()
        await state.clear()


async def notify_parents_about_payment(student_id: int, text: str):
    parent_ids = get_active_parent_ids_for_student(student_id)
    for pid in parent_ids:
        try:
            await bot.send_message(pid, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось уведомить родителя {pid} об оплате: {e}")



@router.callback_query(lambda c: c.data == "deluser_cancel")
async def deluser_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Ок, отменено.")
    await cb.message.answer("Главное меню:", reply_markup=main_menu_keyboard(True))
    await cb.answer()

@router.callback_query(lambda c: c.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ---------- FSM СОСТОЯНИЯ ----------

class RegisterStates(StatesGroup):
    choosing_role = State()
    waiting_name = State()
    waiting_parent_name = State()




class DeleteUserStates(StatesGroup):
    choosing_kind = State()   # ученик или родитель
    choosing_student = State()
    choosing_parent = State()
    confirming = State()


class ParentRequestStates(StatesGroup):
    choosing_child = State()



class MoveStates(StatesGroup):
    choosing_lesson = State()
    choosing_kind = State()
    entering_datetime = State()
    entering_weekday = State()
    entering_time = State()
    entering_comment = State()


class SetSlotStates(StatesGroup):
    waiting_user = State()
    waiting_weekday = State()
    waiting_time = State()




class CancelStates(StatesGroup):
    choosing_student_smart = State()  # Умный выбор ученика
    choosing_lesson = State()
    entering_date = State()

class PaymentStates(StatesGroup):
    choosing_student_smart = State()  # Умный выбор ученика
    choosing_history = State()

class HomeworkDoneStates(StatesGroup):
    choosing_hw = State()
    confirming_hw = State()




class DeleteSlotStates(StatesGroup):
    choosing_student = State()
    choosing_slot = State()
    confirming = State()


class AdminStudentHistoryStates(StatesGroup):
    waiting_student = State()


class StudentRemindStates(StatesGroup):
    choosing_lesson = State()
    entering_minutes = State()


class AdminEditLinksStates(StatesGroup):
    waiting_student = State()
    waiting_links = State()


class BroadcastStates(StatesGroup):
    choosing_scope = State()
    entering_group = State()
    entering_text = State()



class AddManualHistoryStates(StatesGroup):
    waiting_student = State()
    waiting_date = State()
    waiting_time = State()
    waiting_status = State()
    waiting_paid = State()
    waiting_note = State()
    waiting_topic = State()


class DeleteStudentStates(StatesGroup):
    choosing_student = State()
    confirming = State()





class DisputeStates(StatesGroup):
    choosing_history = State()
    entering_reason = State()


class RescheduleOverrideStates(StatesGroup):
    choosing_override = State()
    entering_date = State()
    entering_time = State()
    confirming = State()


class EditHistoryStates(StatesGroup):
    choosing_student = State()
    choosing_history = State()
    choosing_field = State()
    editing_status = State()
    editing_paid = State()
    editing_note = State()
    editing_topic = State()
    editing_datetime = State()   # <-- НОВОЕ



# ---------- /start и регистрация ----------


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user

    # админ как раньше
    if is_teacher(message):
        await state.clear()
        await message.answer("...", reply_markup=main_menu_keyboard(True))
        return

    role = get_user_role(user.id)
    if role is None:
        await state.clear()
        await state.set_state(RegisterStates.choosing_role)
        await message.answer("Привет! 👋\n\nКто вы?", reply_markup=role_keyboard())
        return

    # если ученик — обновляем/создаем students
    if role == "student":
        cur = conn.cursor()
        cur.execute("SELECT * FROM students WHERE telegram_id = ?", (user.id,))
        row = cur.fetchone()
        is_new = row is None

        full_name_from_telegram = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
        username = user.username

        if is_new:
            cur.execute(
                "INSERT INTO students (telegram_id, username, full_name) VALUES (?, ?, ?)",
                (user.id, username, full_name_from_telegram),
            )
        else:
            cur.execute(
                "UPDATE students SET username = ?, full_name = ? WHERE telegram_id = ?",
                (username, full_name_from_telegram, user.id),
            )
        conn.commit()

        await message.answer("Рад тебя видеть!", reply_markup=main_menu_keyboard(False))
        return

    # если родитель — сохраняем в parents, но НЕ в students
    elif role == "parent":
        user = message.from_user

        # проверяем, есть ли уже ФИО в базе
        cur = conn.cursor()
        cur.execute("SELECT full_name FROM parents WHERE telegram_id = ?", (user.id,))
        row = cur.fetchone()

        full_name = (row["full_name"] if row else "") or ""
        full_name = full_name.strip()

        if not full_name:
            await message.answer(
                "👋 Чтобы продолжить, напишите, пожалуйста, ваше имя и фамилию (ФИО).\n\n"
                "Например: Иванова Мария",
                reply_markup=ReplyKeyboardRemove()
            )
            await state.set_state(RegisterStates.waiting_parent_name)
            return

        # если ФИО уже есть — просто обновим username (ФИО не трогаем)
        upsert_parent(user.id, user.username, full_name)

        await message.answer("Привет! Вы родитель ✅", reply_markup=parent_menu_keyboard())
        return


def parent_waiting_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨‍👩‍👧 Запросить привязку")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True
    )

@router.message(lambda m: m.text == "💵 Ставка ученика")
async def handle_set_price_button(message: Message, state: FSMContext):
    if not is_teacher(message):  # is_teacher у тебя уже есть :contentReference[oaicite:5]{index=5}
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    await state.update_data(price_students=students)
    kb, _ = create_action_keyboard(students, "price", page=0)  # :contentReference[oaicite:6]{index=6}
    await state.set_state(SetPriceStates.choosing_student)

    await message.answer(
        "💵 <b>Фикс-ставка ученика</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.message(RegisterStates.choosing_role)
async def choose_role_handler(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    if txt == "👨‍🎓 Я ученик":
        set_user_role(message.from_user.id, "student")
        await state.set_state(RegisterStates.waiting_name)
        await message.answer(
            "Отлично! Напиши, пожалуйста, фамилию и имя).",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if txt == "👨‍👩‍👧 Я родитель":
        set_user_role(message.from_user.id, "parent")

        # если раньше случайно попал в students — убрать
        cur = conn.cursor()
        cur.execute("DELETE FROM students WHERE telegram_id = ?", (message.from_user.id,))
        conn.commit()

        # ❗️ФИО ОБЯЗАТЕЛЬНО вводит родитель вручную
        await state.set_state(RegisterStates.waiting_parent_name)
        await message.answer(
            "👋 Чтобы продолжить, напишите, пожалуйста, ваше имя и фамилию (ФИО).\n\n"
            "Например: Иванова Мария",
            reply_markup=ReplyKeyboardRemove()
        )
        return

        upsert_parent(message.from_user.id, message.from_user.username, tg_full_name)

        await state.clear()
        await message.answer(
            f"Вы в режиме родителя 👨‍👩‍👧\n\n"
            f"ФИО: <b>{tg_full_name}</b>\n\n"
            "Чтобы получить доступ — запросите привязку к ребёнку.",
            parse_mode="HTML",
            reply_markup=parent_waiting_keyboard()
        )
        return

    await message.answer("Пожалуйста, выберите роль кнопкой ниже 👇", reply_markup=role_keyboard())

def get_parent_display_name(parent_tg_id: int) -> str:
    cur = conn.cursor()
    cur.execute("SELECT full_name, username FROM parents WHERE telegram_id = ?", (parent_tg_id,))
    p = cur.fetchone()
    if not p:
        return str(parent_tg_id)
    return p["full_name"] or (f"@{p['username']}" if p["username"] else str(parent_tg_id))



@router.message(lambda m: m.text == "👨‍👩‍👧 Запросить привязку")
async def parent_request_start(message: Message, state: FSMContext):
    if is_teacher(message):
        return

    # если уже привязан — не надо
    if is_parent(message):
        await message.answer("Вы уже привязаны к ученику ✅", reply_markup=parent_menu_keyboard())
        return

    # защита от дублей: если уже есть pending-заявка — не создавать новую
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM parent_requests
        WHERE parent_telegram_id = ? AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
    """, (message.from_user.id,))
    pending = cur.fetchone()
    if pending:
        await message.answer(
            "⏳ Ваша заявка уже отправлена администратору и ожидает решения.",
            reply_markup=parent_waiting_keyboard()
        )
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет учеников для привязки. Попробуйте позже.")
        return

    await state.clear()
    await state.set_state(ParentRequestStates.choosing_child)
    await state.update_data(parentreq_students=students)

    kb, _ = create_action_keyboard(students, "pchild", page=0)  # используем твою универсальную клавиатуру :contentReference[oaicite:2]{index=2}

    await message.answer(
        "👨‍👩‍👧 Выберите ученика, к которому хотите получить доступ:",
        reply_markup=kb
    )

@router.callback_query(lambda c: c.data.startswith("pchild_page_"), ParentRequestStates.choosing_child)
async def pchild_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("parentreq_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    kb, _ = create_action_keyboard(students, "pchild", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=kb)
    await callback_query.answer(f"Страница {page + 1}")

@router.callback_query(lambda c: c.data and c.data.startswith("pchild_student_"), ParentRequestStates.choosing_child)
async def pchild_choose_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()
    if not student:
        await callback_query.answer("Ученик не найден")
        return

    student_name = student["full_name"] or (f"@{student['username']}" if student["username"] else str(student["telegram_id"]))

    parent_username = callback_query.from_user.username
    parent_name = callback_query.from_user.full_name

    # child_info чисто для человека-админа (чтобы было видно кого выбрали)
    child_info = f"Выбран ученик: {student_name} (student_id={student_id})"

    req_id = create_parent_request(
        parent_tg_id=callback_query.from_user.id,
        parent_username=parent_username,
        parent_name=parent_name,
        child_info=child_info,
        requested_student_id=student_id
    )

    # сообщение админу
    uname_text = f"@{parent_username}" if parent_username else "(без username)"
    text = (
        "👨‍👩‍👧 <b>Запрос привязки родителя</b>\n\n"
        f"ID запроса: <b>{req_id}</b>\n"
        f"Родитель: {parent_name}\n"
        f"Username: {uname_text}\n"
        f"Telegram ID: <code>{callback_query.from_user.id}</code>\n\n"
        f"<b>Родитель выбрал:</b> {student_name}\n\n"
        "Действие: привязать или отклонить."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Привязать", callback_data=f"parentreq_approve_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"parentreq_reject_{req_id}")  # у тебя уже есть reject handler :contentReference[oaicite:3]{index=3}
    kb.adjust(1)

    for admin_id in TEACHER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            logging.error(f"Не удалось отправить запрос админу {admin_id}: {e}")

    await callback_query.message.edit_text(
        "✅ Заявка отправлена администратору.\nЯ сообщу, когда её обработают."
    )
    await callback_query.answer("Отправлено")
    await state.clear()

@router.callback_query(lambda c: c.data and c.data.startswith("parentreq_approve_"))
async def parentreq_approve(callback_query: CallbackQuery, state: FSMContext):
    if callback_query.from_user.id not in TEACHER_IDS:
        await callback_query.answer("Только для админа")
        return

    req_id = int(callback_query.data.split("_")[2])
    req = get_parent_request(req_id)
    if not req or req["status"] != "pending":
        await callback_query.answer("Запрос не найден или уже обработан")
        return

    student_id = req["requested_student_id"]
    if not student_id:
        # если это старая заявка (через /parent_request), там student_id нет —
        # можно перекинуть на старый сценарий "выбрать ученика"
        await callback_query.answer("В заявке не выбран ученик. Откройте выбор ученика.")
        # по желанию: можно просто вызвать старую логику (parentreq_pick_)
        await callback_query.message.edit_text(
            "В этой заявке ученик не выбран (старый формат). Нажмите «🔗 Привязать к ученику» и выберите ученика."
        )
        return

    parent_tg_id = req["parent_telegram_id"]

    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO parent_links(parent_telegram_id, student_id, is_active, created_at)
        VALUES (?, ?, 1, ?)
        """,
        (parent_tg_id, student_id, datetime.now().isoformat(timespec="seconds"))
    )
    cur.execute(
        "UPDATE parent_links SET is_active = 1 WHERE parent_telegram_id = ? AND student_id = ?",
        (parent_tg_id, student_id)
    )
    conn.commit()

    set_parent_request_status(req_id, "approved")

    # уведомляем родителя
    try:
        await bot.send_message(
            parent_tg_id,
            "✅ Администратор одобрил привязку.\nТеперь вам доступно расписание/домашка/история ученика.",
            reply_markup=parent_menu_keyboard()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить родителя {parent_tg_id}: {e}")

    await callback_query.message.edit_text(f"✅ Привязка выполнена. Запрос #{req_id} закрыт.")
    await callback_query.answer("Готово")
    await state.clear()


@router.message(RegisterStates.waiting_name)
async def register_name(message: Message, state: FSMContext):
    tg = message.from_user
    name = (message.text or "").strip()

    if not name:
        await message.answer("Похоже, пришло пустое имя. Напиши, пожалуйста, как тебя зовут.")
        return

    cur = conn.cursor()

    # 1) гарантируем, что ученик существует в students
    full_name_from_telegram = (
        (tg.first_name or "") + (" " + tg.last_name if tg.last_name else "")
    ).strip()

    cur.execute(
        "INSERT OR IGNORE INTO students (telegram_id, username, full_name) VALUES (?, ?, ?)",
        (tg.id, tg.username, full_name_from_telegram),
    )

    # 2) записываем имя, введённое учеником, и обновляем username
    cur.execute(
        "UPDATE students SET full_name = ?, username = ? WHERE telegram_id = ?",
        (name, tg.username, tg.id),
    )
    conn.commit()

    # уведомление админу
    for admin_id in TEACHER_IDS:
        try:
            username = tg.username
            uname_text = f"@{username}" if username else "(без username)"
            await bot.send_message(
                admin_id,
                "Новый ученик зарегистрировался:\n"
                f"Имя: {name}\n"
                f"Telegram ID: {tg.id}\n"
                f"Username: {uname_text}",
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить админа {admin_id} о новом ученике: {e}")

    await message.answer(
        "Спасибо, я запомнил! ✍️",
        reply_markup=main_menu_keyboard(False),
    )
    await state.clear()

@router.message(RegisterStates.waiting_parent_name)
async def register_parent_name(message: Message, state: FSMContext):
    user = message.from_user
    full_name = (message.text or "").strip()

    if not full_name:
        await message.answer(
            "Похоже, пришло пустое ФИО. Напишите, пожалуйста, имя и фамилию.\n\n"
            "Например: Иванова Мария"
        )
        return

    # сохраняем родителя (теперь ФИО всегда ручное)
    upsert_parent(user.id, user.username, full_name)

    await state.clear()
    await message.answer(
        "✅ ФИО сохранено. Вы в режиме родителя 👨‍👩‍👧\n\n"
        "Чтобы получить доступ — запросите привязку к ребёнку.",
        reply_markup=parent_waiting_keyboard()
    )


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    await message.answer(
        f"Твой Telegram ID: {message.from_user.id}\n\n"
        "Если хочешь быть преподавателем, добавь этот ID в список TEACHER_IDS в коде бота."
    )


# ---------- РАСПИСАНИЕ ДЛЯ УЧЕНИКА ----------


@router.message(Command("myschedule"))
async def cmd_myschedule(message: Message):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    lessons = get_weekly_lessons_for_student(student["id"])
    overrides = get_future_overrides_for_student(student["id"], days_ahead=30)

    if not lessons and not overrides:
        await message.answer(
            "Для тебя пока не задано ни одного занятия и нет переносов.\n"
            "Попроси преподавателя настроить расписание."
        )
        return

    lines = []

    if lessons:
        lines.append("📅 <b>Регулярные занятия (по неделям):</b>")
        for wl in lessons:
            weekday_name = weekday_to_name(wl["weekday"])
            lines.append(
                f"• <b>{weekday_name} в {wl['time']}</b> (напоминание за {wl['remind_before_minutes']} мин)"
            )

    if overrides:
        lines.append("\n🔄 <b>Ближайшие разовые изменения:</b>")
        for o in overrides:
            d = date.fromisoformat(o["date"])
            weekday_old = weekday_to_name(o["weekday"])
            if o["change_kind"] == "cancel":
                lines.append(
                    f"• <b>{d.strftime('%d.%m.%Y')}</b> — занятие <b>ОТМЕНЕНО</b> "
                    f"(обычно: {weekday_old} {o['weekly_time']})"
                )
            else:
                lines.append(
                    f"• <b>{d.strftime('%d.%m.%Y')} в {o['new_time']}</b> "
                    f"(обычно: {weekday_old} {o['weekly_time']})"
                )

    lines.append(
        "\nЕсли хочешь изменить время напоминания о занятиях — используй команду /set_remind."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- МАСТЕР СЛОТА ----------


async def start_set_slot_wizard(message: Message, state: FSMContext):
    """Пошаговый мастер назначения слота ученику."""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    ids = []
    lines = ["Выбери ученика для назначения слота (номер в списке):"]
    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])
        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(student_ids=ids)
    await state.set_state(SetSlotStates.waiting_user)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


# ---------- /set_slot (одной строкой или пошагово со списком учеников) ----------


@router.message(Command("set_slot"))
async def cmd_set_slot(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    parts = message.text.split()
    # Пошаговый режим
    if len(parts) == 1:
        await start_set_slot_wizard(message, state)
        return

    # Однострочный режим
    if len(parts) != 4:
        await message.answer(
            "Форматы:\n"
            "1) /set_slot @username день_недели время(HH:MM)\n"
            "   Пример: /set_slot @masha 2 18:00\n"
            "   День недели — число от 1 до 7, где:\n"
            "   1 - Понедельник\n2 - Вторник\n3 - Среда\n4 - Четверг\n"
            "   5 - Пятница\n6 - Суббота\n7 - Воскресенье\n"
            "2) Просто /set_slot — и я спрошу всё по шагам, с выбором ученика из списка."
        )
        return

    _, user_key, weekday_str, time_str = parts

    student = get_student_by_user_key(user_key)
    if not student:
        await message.answer(
            "Не нашёл такого ученика в базе.\n"
            "Убедись, что ученика уже писал боту /start."
        )
        return

    try:
        weekday_human = int(weekday_str)
        if not 1 <= weekday_human <= 7:
            raise ValueError
    except ValueError:
        await message.answer(
            "День недели должен быть числом от 1 до 7, где:\n"
            "1 - Понедельник\n2 - Вторник\n3 - Среда\n4 - Четверг\n"
            "5 - Пятница\n6 - Суббота\n7 - Воскресенье"
        )
        return

    weekday = weekday_human - 1

    try:
        hh, mm = map(int, time_str.split(":"))
        _t = dtime(hh, mm)
    except Exception:
        await message.answer("Время должно быть в формате HH:MM, например 18:00.")
        return

    # Добавляем слот с напоминанием по умолчанию 60 минут
    student_data = add_weekly_slot(
        student_id=student["id"],
        weekday=weekday,
        time_str=time_str,
    )

    if student_data is None:
        await message.answer(
            f"У ученика {student['full_name'] or student['username'] or student['telegram_id']} "
            f"уже есть занятие в {weekday_to_name(weekday)} в {time_str}."
        )
        return

    # Отправляем уведомление ученику
    if student_data and student_data["telegram_id"]:
        await notify_new_regular_lesson(
            student_telegram_id=student_data["telegram_id"],
            weekday=weekday,
            time_str=time_str
        )

    await message.answer(
        f"Добавлен слот для ученика {student['full_name'] or student['username'] or student['telegram_id']}: "
        f"{weekday_to_name(weekday)} в {time_str}, напоминание по умолчанию за 60 мин."
    )


@router.message(SetSlotStates.waiting_user)
async def slot_wait_user(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю назначение слота. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("student_ids", [])

    student = None

    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(student_id=student["id"])
    await state.set_state(SetSlotStates.waiting_weekday)
    await message.answer(
        "На какой день недели записываем?\n"
        "Введи число от 1 до 7, где:\n"
        "1 - Понедельник\n2 - Вторник\n3 - Среда\n4 - Четверг\n"
        "5 - Пятница\n6 - Суббота\n7 - Воскресенье",
        reply_markup=back_keyboard(),
    )


@router.message(SetSlotStates.waiting_weekday)
async def slot_wait_weekday(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю назначение слота. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        weekday_human = int(text)
        if not 1 <= weekday_human <= 7:
            raise ValueError
    except ValueError:
        await message.answer(
            "Нужно число от 1 до 7, где:\n"
            "1 - Понедельник\n2 - Вторник\n3 - Среда\n4 - Четверг\n"
            "5 - Пятница\n6 - Суббота\n7 - Воскресенье\n"
            "Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    weekday = weekday_human - 1
    await state.update_data(weekday=weekday)
    await state.set_state(SetSlotStates.waiting_time)
    await message.answer(
        "Во сколько? Введи время в формате HH:MM, например 18:30.",
        reply_markup=back_keyboard(),
    )


@router.message(SetSlotStates.waiting_time)
async def slot_wait_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю назначение слота. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    time_str = text
    try:
        hh, mm = map(int, time_str.split(":"))
        _ = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате HH:MM, например 18:30. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    weekday = data["weekday"]

    student_id = data.get("hw_student_id")
    if not student_id:
        await message.answer("Не выбран ученик. Начни заново: ✏️ Задать домашку")
        await state.clear()
        return

    # Добавляем слот с напоминанием по умолчанию 60 минут
    student_data = add_weekly_slot(
        student_id=student_id,
        weekday=weekday,
        time_str=time_str,
    )

    if student_data is None:
        cur = conn.cursor()
        cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        student = cur.fetchone()

        await message.answer(
            f"У ученика {student['full_name'] or student['username'] or student['telegram_id']} "
            f"уже есть занятие в {weekday_to_name(weekday)} в {time_str}.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    # Отправляем уведомление ученику
    if student_data and student_data["telegram_id"]:
        await notify_new_regular_lesson(
            student_telegram_id=student_data["telegram_id"],
            weekday=weekday,
            time_str=time_str
        )

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    await message.answer(
        f"Добавлен слот для ученика {student['full_name'] or student['username'] or student['telegram_id']}: "
        f"{weekday_to_name(weekday)} в {time_str}, напоминание по умолчанию за 60 мин.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )
    await state.clear()


# ---------- СПИСКИ ДЛЯ ПРЕПОДА ----------


@router.message(Command("list_students"))
async def cmd_list_students(message: Message):
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.*, w.weekday, w.time, w.remind_before_minutes
        FROM students s
        LEFT JOIN weekly_lessons w ON w.student_id = s.id AND w.is_active = 1
        ORDER BY s.full_name, w.weekday, w.time
        """
    )
    rows = cur.fetchall()

    if not rows:
        await message.answer(
            "В базе пока нет учеников. Пусть они напишут боту /start."
        )
        return

    # Разбиваем на страницы
    page = 0  # Можно добавить параметр для указания страницы
    page_size = 15  # Учеников на странице
    total_pages = (len(rows) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(rows))
    page_rows = rows[start_idx:end_idx]

    # Формируем сообщение
    lines = [f"Ученики и слоты (страница {page + 1}/{total_pages}):"]

    for r in page_rows:
        line = f"ID={r['telegram_id']} | @{r['username'] or '-'} | {r['full_name'] or '-'}"
        if r["time"] is not None:
            line += (
                f" | {weekday_to_name(r['weekday'])} {r['time']} "
                f"(за {r['remind_before_minutes']} мин)"
            )
        else:
            line += " | слот не задан"
        lines.append(line)

    # Добавляем пагинацию в виде кнопок
    if total_pages > 1:
        builder = InlineKeyboardBuilder()

        if page > 0:
            builder.button(
                text="◀️ Предыдущая",
                callback_data=f"students_page_{page - 1}"
            )

        builder.button(
            text=f"{page + 1}/{total_pages}",
            callback_data="page_info"
        )

        if page < total_pages - 1:
            builder.button(
                text="Следующая ▶️",
                callback_data=f"students_page_{page + 1}"
            )

        builder.adjust(3)

        await message.answer(
            "\n".join(lines),
            reply_markup=builder.as_markup()
        )
    else:
        await message.answer("\n".join(lines))



def get_user_role(telegram_id: int) -> str | None:
    cur = conn.cursor()
    cur.execute("SELECT role FROM user_roles WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    return row["role"] if row else None

def set_user_role(telegram_id: int, role: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO user_roles(telegram_id, role, created_at) VALUES (?, ?, ?)",
        (telegram_id, role, datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()


def create_parent_request(
    parent_tg_id: int,
    parent_username: str | None,
    parent_name: str | None,
    child_info: str,
    requested_student_id: int | None = None
):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO parent_requests(
            parent_telegram_id, parent_username, parent_name, child_info,
            requested_student_id, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            parent_tg_id,
            parent_username,
            parent_name,
            child_info,
            requested_student_id,
            datetime.now().isoformat(timespec="seconds")
        )
    )
    conn.commit()
    return cur.lastrowid


def add_feedback(telegram_id: int, role: str, username: str | None, full_name: str | None, text_: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO feedback(telegram_id, role, username, full_name, text, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'new')
        """,
        (
            telegram_id,
            role,
            username,
            full_name,
            text_,
            datetime.now().isoformat(timespec="seconds"),
        )
    )
    conn.commit()
    return cur.lastrowid


def get_parent_request(req_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM parent_requests WHERE id = ?", (req_id,))
    return cur.fetchone()

def set_parent_request_status(req_id: int, status: str):
    cur = conn.cursor()
    cur.execute("UPDATE parent_requests SET status = ? WHERE id = ?", (status, req_id))
    conn.commit()

@router.message(Command("parent_request"))
async def cmd_parent_request(message: Message):
    # запретить учителю спамить себе же
    if is_teacher(message):
        await message.answer("Эта команда для родителей 🙂")
        return

    # если уже привязан — не надо
    if is_parent(message):
        await message.answer("Вы уже привязаны к ученику. Откройте меню родителя.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Напишите запрос так:\n"
            "/parent_request <ФИО ребёнка или @username ребёнка + комментарий>\n\n"
            "Пример:\n"
            "/parent_request Иван Петров, занимаемся по вт/чт 17:00"
        )
        return

    child_info = parts[1].strip()
    parent_username = message.from_user.username
    parent_name = message.from_user.full_name

    req_id = create_parent_request(
        parent_tg_id=message.from_user.id,
        parent_username=parent_username,
        parent_name=parent_name,
        child_info=child_info
    )

    # уведомляем админов (TEACHER_IDS у тебя уже используются для уведомлений) :contentReference[oaicite:1]{index=1}
    uname_text = f"@{parent_username}" if parent_username else "(без username)"
    text = (
        "👨‍👩‍👧 <b>Запрос привязки родителя</b>\n\n"
        f"ID запроса: <b>{req_id}</b>\n"
        f"Родитель: {parent_name}\n"
        f"Username: {uname_text}\n"
        f"Telegram ID: <code>{message.from_user.id}</code>\n\n"
        f"Что написал родитель про ребёнка:\n<i>{child_info}</i>\n\n"
        "Действие: выберите ученика для привязки или отклоните."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Привязать к ученику", callback_data=f"parentreq_pick_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"parentreq_reject_{req_id}")
    kb.adjust(1)

    for admin_id in TEACHER_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception as e:
            logging.error(f"Не удалось отправить запрос админу {admin_id}: {e}")

    await message.answer("✅ Запрос отправлен администратору. Я сообщу, когда его обработают.")


def create_smart_student_keyboard(action_type: str = None, page: int = 0):
    """
    Создает умную клавиатуру выбора учеников с пагинацией

    action_type может быть:
    - 'homework': задать домашку (показывает тех, у кого нет невыполненной домашки)
    - 'cancel': отмена занятия (показывает тех, у кого есть занятия сегодня)
    - 'payment': оплата (показывает тех, у кого есть неоплаченные занятия)
    - 'topic': указать тему (показывает тех, у кого сегодня занятия без темы)
    - None: все ученики
    """
    # Получаем размер страницы для пользователя или используем значение по умолчанию
    user_id = None  # Будем получать из контекста
    page_size = USER_PAGE_SIZES.get(user_id, PAGE_SIZE)

    builder = InlineKeyboardBuilder()

    if action_type == 'homework':
        students = get_students_without_homework()
        if not students:
            students = get_all_students()
        title = "👤 Ученики без невыполненной домашки:"
    elif action_type == 'cancel':
        students = get_students_with_lessons_today()
        if not students:
            students = get_all_students()
        title = "👤 Ученики с занятиями сегодня:"
    elif action_type == 'payment':
        students = get_students_with_unpaid_lessons()
        if not students:
            students = get_all_students()
        title = "👤 Ученики с неоплаченными занятиями:"
    elif action_type == 'topic':
        students = get_students_without_topic_for_today()
        if not students:
            students = get_all_students()
        title = "👤 Ученики с занятиями сегодня без темы:"
    else:
        students = get_all_students()
        title = "👤 Все ученики:"

    if not students:
        return None, "Нет учеников"

    # Пагинация
    total_pages = (len(students) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(students))
    page_students = students[start_idx:end_idx]

    builder = InlineKeyboardBuilder()

    for student in page_students:
        student_id = student["id"]
        name = student["full_name"] or student["username"] or str(student["telegram_id"])

        # Добавляем эмодзи в зависимости от типа действия
        if action_type == 'homework':
            emoji = "📚"
        elif action_type == 'cancel':
            emoji = "❌"
        elif action_type == 'payment':
            emoji = "💰"
        elif action_type == 'topic':
            emoji = "📝"
        else:
            emoji = "👤"

        # Обрезаем длинные имена
        if len(name) > 20:
            name = name[:17] + "..."

        builder.button(
            text=f"{emoji} {name}",
            callback_data=f"select_student_{action_type}_{student_id}_{page}"
        )

    builder.adjust(1)

    # Добавляем пагинацию если нужно
    pagination_row = []
    if total_pages > 1:
        if page > 0:
            pagination_row.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"students_page_{action_type}_{page - 1}"
            ))

        pagination_row.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="page_info"
        ))

        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"students_page_{action_type}_{page + 1}"
            ))

        builder.row(*pagination_row)

    # Добавляем кнопку "Показать всех" если мы показываем отфильтрованный список
    if action_type is not None and len(students) < len(get_all_students()):
        builder.row(InlineKeyboardButton(
            text="📋 Показать всех учеников",
            callback_data=f"show_all_students_{action_type}_{page}"
        ))

    back_callback = f"back_from_{action_type}" if action_type else "back_to_main_menu"
    builder.row(InlineKeyboardButton(
        text="⬅️ Назад в меню",
        callback_data=back_callback
    ))

    return builder.as_markup(), title, total_pages




@router.callback_query(lambda c: c.data.startswith("students_page_"))
async def students_page_callback(callback_query: CallbackQuery, state: FSMContext):
    """Обработчик пагинации для списка учеников"""
    parts = callback_query.data.split("_")
    action_type = parts[2]
    page = int(parts[3])

    # Получаем размер страницы для пользователя
    user_id = callback_query.from_user.id
    USER_PAGE_SIZES[user_id] = USER_PAGE_SIZES.get(user_id, PAGE_SIZE)

    # Создаем обновленную клавиатуру
    keyboard, title, total_pages = create_smart_student_keyboard(action_type, page)

    if keyboard:
        await callback_query.message.edit_text(
            f"{title}\n\n"
            f"Выберите ученика:",
            reply_markup=keyboard
        )

    await callback_query.answer(f"Страница {page + 1}")


def create_overrides_keyboard(overrides):
    """Создает инлайн-клавиатуру с кнопками для работы с оверрайдами"""
    builder = InlineKeyboardBuilder()

    for ov in overrides:
        ov_id = ov["id"]
        student_name = ov["full_name"] or ov["username"] or str(ov["telegram_id"])
        d = date.fromisoformat(ov["date"])
        date_str = d.strftime("%d.%m.%Y")

        if ov["change_kind"] == "cancel":
            kind_text = "отмена"
            time_text = f"отменено ({ov['weekly_time']})"
        else:
            kind_text = "перенос"
            time_text = ov["new_time"]

        # Кнопка для просмотра деталей и действий
        builder.button(
            text=f"#{ov_id} {student_name} - {date_str} {time_text}",
            callback_data=f"view_override_{ov_id}"
        )

    builder.adjust(1)  # одна кнопка в ряд
    return builder.as_markup()


@router.message(Command("list_overrides"))
async def cmd_list_overrides(message: Message):
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    overrides = get_future_overrides_for_all(days_ahead=30)
    if not overrides:
        await message.answer(
            "Нет ближайших разовых переносов/отмен (на ближайшие 30 дней)."
        )
        return

    # Отправляем сообщение с инлайн-клавиатурой
    await message.answer(
        "📌 <b>Ближайшие разовые изменения:</b>\n\n"
        "Нажми на изменение для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=create_overrides_keyboard(overrides)
    )


@router.callback_query(lambda c: c.data.startswith("view_override_"))
async def view_override_details(callback_query: CallbackQuery):
    """Просмотр деталей оверрайда и действий"""
    ov_id = int(callback_query.data.split("_")[2])
    o = get_override_by_id(ov_id)

    if not o:
        await callback_query.answer("Изменение не найдено")
        return

    d = date.fromisoformat(o["date"])
    date_str = d.strftime("%d.%m.%Y")
    weekday_old = weekday_to_name(o["weekday"])

    if o["change_kind"] == "cancel":
        kind_text = "разовая отмена"
        time_text = f"<b>Отменено</b> (обычно: {weekday_old} {o['weekly_time']})"
    else:
        kind_text = "разовый перенос"
        time_text = f"<b>{o['new_time']}</b> (обычно: {weekday_old} {o['weekly_time']})"

    message_text = (
        f"📋 <b>Детали изменения #{o['id']}</b>\n\n"
        f"👤 <b>Ученик:</b> {o['full_name'] or o['username']}\n"
        f"📅 <b>Тип:</b> {kind_text}\n"
        f"📆 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {time_text}\n"
    )

    if o["original_date"] and o["original_time"]:
        original_date = date.fromisoformat(o["original_date"])
        original_date_str = original_date.strftime("%d.%m.%Y")
        message_text += f"\n🔄 <b>Изначально было:</b> {original_date_str} {o['original_time']}"

    # Создаем клавиатуру с действиями
    builder = InlineKeyboardBuilder()

    if o["change_kind"] != "cancel":
        # Для переносов можно перенести снова
        builder.button(text="🔄 Перенести снова", callback_data=f"{RESCHEDULE_OVERRIDE_PREFIX}{ov_id}")

    builder.button(text="🗑️ Удалить", callback_data=f"{DELETE_OVERRIDE_PREFIX}{ov_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_overrides_list")
    builder.adjust(2)

    await callback_query.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data == "back_to_overrides_list")
async def back_to_overrides_list(callback_query: CallbackQuery):
    """Возврат к списку оверрайдов"""
    overrides = get_future_overrides_for_all(days_ahead=30)
    if not overrides:
        await callback_query.message.edit_text("Нет ближайших разовых переносов/отмен (на ближайшие 30 дней).")
        await callback_query.answer()
        return

    await callback_query.message.edit_text(
        "📌 <b>Ближайшие разовые изменения:</b>\n\n"
        "Нажми на изменение для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=create_overrides_keyboard(overrides)
    )
    await callback_query.answer()

async def notify_payment_status(student_tg_id: int, lesson_date: date, lesson_time: str, paid: bool):
    date_str = lesson_date.strftime("%d.%m.%Y")
    status_text = "✅ <b>оплачено</b>" if paid else "❌ <b>не оплачено</b>"

    message = (
        f"💰 <b>Статус оплаты изменён</b>\n\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {lesson_time}\n"
        f"💳 <b>Статус:</b> {status_text}\n"
    )

    await notify_student_about_schedule_change(student_tg_id, message)

    # ДОБАВЬ ВОТ ЭТУ СТРОКУ:
    return message


@router.callback_query(lambda c: c.data.startswith(DELETE_OVERRIDE_PREFIX))
async def delete_override_callback(callback_query: CallbackQuery):
    """Удаление оверрайда через кнопку"""
    ov_id = int(callback_query.data[len(DELETE_OVERRIDE_PREFIX):])

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Удаляем оверрайд
    deleted_override = delete_lesson_override(ov_id)

    if not deleted_override:
        await callback_query.answer("Изменение не найдено")
        return

    # Уведомляем ученика
    student_name = deleted_override["full_name"] or deleted_override["username"] or str(deleted_override["telegram_id"])
    d = date.fromisoformat(deleted_override["date"])
    date_str = d.strftime("%d.%m.%Y")

    if deleted_override["change_kind"] == "cancel":
        message_text = f"❌ <b>Отмена занятия отменена!</b>\n\nЗанятие {date_str} восстановлено по обычному расписанию."
    else:
        message_text = f"❌ <b>Перенос занятия отменен!</b>\n\nЗанятие {date_str} восстановлено по обычному расписанию."

    try:
        await bot.send_message(
            deleted_override["telegram_id"],
            message_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение ученику: {e}")

    await callback_query.answer(f"Изменение #{ov_id} удалено")

    # Возвращаемся к списку оверрайдов
    overrides = get_future_overrides_for_all(days_ahead=30)
    if not overrides:
        await callback_query.message.edit_text("Нет ближайших разовых переносов/отмен (на ближайшие 30 дней).")
        return

    await callback_query.message.edit_text(
        "📌 <b>Ближайшие разовые изменения:</b>\n\n"
        "Нажми на изменение для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=create_overrides_keyboard(overrides)
    )


@router.callback_query(lambda c: c.data.startswith(RESCHEDULE_OVERRIDE_PREFIX))
async def reschedule_override_callback(callback_query: CallbackQuery, state: FSMContext):
    """Начало процесса переноса оверрайда"""
    ov_id = int(callback_query.data[len(RESCHEDULE_OVERRIDE_PREFIX):])

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Получаем данные оверрайда
    override = get_override_by_id(ov_id)
    if not override:
        await callback_query.answer("Изменение не найдено")
        return

    # Сохраняем ID оверрайда в состояние
    await state.update_data(reschedule_override_id=ov_id)
    await state.update_data(reschedule_original_date=date.fromisoformat(override["date"]))
    await state.update_data(
        reschedule_original_time=override["new_time"] if override["change_kind"] != "cancel" else override[
            "weekly_time"])

    # Устанавливаем состояние для ввода новой даты
    await state.set_state(RescheduleOverrideStates.entering_date)

    await callback_query.message.answer(
        f"🔄 <b>Перенос уже перенесенного занятия</b>\n\n"
        f"Ученик: {override['full_name'] or override['username']}\n"
        f"Текущая дата: {date.fromisoformat(override['date']).strftime('%d.%m.%Y')}\n"
        f"Текущее время: {override['new_time'] if override['change_kind'] != 'cancel' else override['weekly_time']}\n\n"
        f"На какую дату переносим занятие?\n"
        f"Формат: ДД.ММ или ДД.ММ.ГГГГ",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()


@router.message(RescheduleOverrideStates.entering_date)
async def reschedule_override_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    new_date = parse_date_str(text)
    if not new_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(reschedule_new_date=new_date)
    await state.set_state(RescheduleOverrideStates.entering_time)

    await message.answer(
        "На какое время переносим занятие? (формат HH:MM, например 19:00)",
        reply_markup=back_keyboard(),
    )


@router.message(RescheduleOverrideStates.entering_time)
async def reschedule_override_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ, например 19:00. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(reschedule_new_time=new_time)
    await state.set_state(RescheduleOverrideStates.confirming)

    data = await state.get_data()
    override_id = data.get("reschedule_override_id")
    original_date = data.get("reschedule_original_date")
    original_time = data.get("reschedule_original_time")
    new_date = data.get("reschedule_new_date")

    override = get_override_by_id(override_id)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, перенести")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        f"Вы действительно хотите перенести занятие?\n"
        f"Ученик: {override['full_name'] or override['username']}\n"
        f"Было: {original_date.strftime('%d.%m.%Y')} в {original_time}\n"
        f"Стало: {new_date.strftime('%d.%m.%Y')} в {new_time.strftime('%H:%M')}\n\n"
        f"Это разовый перенос. Регулярное расписание останется без изменений.",
        reply_markup=kb,
    )


@router.message(RescheduleOverrideStates.confirming)
async def reschedule_override_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю перенос занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, перенести":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, перенести» или «❌ Нет, отменить»."
        )
        return

    data = await state.get_data()
    override_id = data.get("reschedule_override_id")
    new_date = data.get("reschedule_new_date")
    new_time = data.get("reschedule_new_time")
    original_date = data.get("reschedule_original_date")
    original_time = data.get("reschedule_original_time")

    if not override_id or not new_date or not new_time:
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Получаем данные оверрайда
    override = get_override_by_id(override_id)
    if not override:
        await state.clear()
        await message.answer(
            "Ошибка: изменение не найдено.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Обновляем оверрайд
    updated_override = update_lesson_override(
        override_id=override_id,
        new_date=new_date,
        new_time=new_time,
        change_kind="one_time" if override["change_kind"] != "cancel" else "cancel"
    )

    # Отправляем уведомление ученику
    if updated_override and updated_override["telegram_id"]:
        await notify_override_rescheduled(
            student_telegram_id=updated_override["telegram_id"],
            old_date=original_date,
            old_time=original_time,
            new_date=new_date,
            new_time=new_time.strftime("%H:%M")
        )

    student_name = override["full_name"] or override["username"] or str(override["telegram_id"])

    await message.answer(
        f"Занятие для {student_name} перенесено с {original_date.strftime('%d.%m.%Y')} {original_time} "
        f"на {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}.\n"
        f"Регулярный слот остаётся без изменений.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


def create_requests_keyboard(requests, page: int = 0, student_id: str = ""):
    """Создает инлайн-клавиатуру с кнопками для запросов с пагинацией"""
    builder = InlineKeyboardBuilder()

    # Получаем элементы для текущей страницы
    # ИЗМЕНЕНИЕ: метод get_page возвращает 4 значения
    page_requests, current_page, total_pages, page_size = Paginator.get_page(requests, page)

    for req in page_requests:
        req_id = req["id"]
        student_name = req["full_name"] or req["username"] or str(req["telegram_id"])
        change_kind = req["change_kind"]

        if change_kind == "one_time":
            kind_text = "разовый перенос"
        elif change_kind == "permanent":
            kind_text = "постоянный перенос"
        elif change_kind == "cancel":
            kind_text = "отмена"
        else:
            kind_text = change_kind

        date_str = req["new_date"]
        time_str = req["new_time"]

        # Обрезаем длинные имена для компактности
        if len(student_name) > 15:
            student_name = student_name[:12] + "..."

        builder.button(
            text=f"#{req_id} {student_name} - {kind_text}",
            callback_data=f"view_req_{req_id}_{page}_{student_id}"
        )

    builder.adjust(1)  # одна кнопка в ряд

    # Добавляем пагинацию если нужно
    pagination_keyboard = Paginator.create_pagination_keyboard(
        current_page=current_page,
        total_pages=total_pages,
        prefix="req",
        data=student_id,
        show_info=True
    )

    return builder.as_markup(), pagination_keyboard, total_pages


@router.message(Command("list_requests"))
async def cmd_list_requests(message: Message):
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    rows = get_pending_requests()
    if not rows:
        await message.answer("Нет ожидающих запросов на перенос/отмену.")
        return

    # Получаем клавиатуры
    requests_kb, pagination_kb, total_pages = create_requests_keyboard(rows, page=0)

    # Создаем сообщение
    message_text = (
        f"📜 <b>Ожидающие запросы (страница 1/{total_pages}):</b>\n\n"
        "Нажми на запрос для просмотра деталей и действий:"
    )

    if pagination_kb:
        # Если есть пагинация, отправляем два сообщения
        await message.answer(message_text, parse_mode="HTML")
        await message.answer(
            "Выберите запрос:",
            reply_markup=requests_kb
        )
        await message.answer(
            "Навигация по страницам:",
            reply_markup=pagination_kb
        )
    else:
        # Если нет пагинации, отправляем одним сообщением
        await message.answer(
            message_text,
            parse_mode="HTML",
            reply_markup=requests_kb
        )


@router.callback_query(lambda c: c.data.startswith("req_page_"))
async def req_page_callback(callback_query: CallbackQuery):
    """Обработчик пагинации для списка запросов"""
    page, student_id = Paginator.parse_callback_data(callback_query.data)

    rows = get_pending_requests()
    if not rows:
        await callback_query.message.edit_text("Нет ожидающих запросов на перенос/отмену.")
        await callback_query.answer()
        return

    # Получаем клавиатуры для новой страницы
    requests_kb, pagination_kb, total_pages = create_requests_keyboard(rows, page, student_id)

    # Обновляем сообщение с запросами
    await callback_query.message.edit_text(
        "Выберите запрос:",
        reply_markup=requests_kb
    )

    # Обновляем сообщение с пагинацией (если оно есть)
    try:
        # Ищем сообщение с пагинацией (обычно следующее после текущего)
        async for msg in callback_query.message.bot.get_chat_history(
                callback_query.message.chat.id,
                limit=3
        ):
            if "Навигация по страницам" in msg.text:
                if pagination_kb:
                    await msg.edit_text(
                        f"Навигация по страницам (страница {page + 1}/{total_pages}):",
                        reply_markup=pagination_kb
                    )
                else:
                    await msg.delete()
                break
    except Exception as e:
        logging.error(f"Ошибка при обновлении пагинации: {e}")

    await callback_query.answer(f"Страница {page + 1}")


@router.callback_query(lambda c: c.data.startswith("view_req_"))
async def view_request_details(callback_query: CallbackQuery):
    """Просмотр деталей запроса и действий с учетом пагинации"""
    parts = callback_query.data.split("_")
    req_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    student_id = parts[4] if len(parts) > 4 else ""

    r = get_change_request_by_id(req_id)
    if not r:
        await callback_query.answer("Запрос не найден")
        return

    d = date.fromisoformat(r["new_date"])
    date_str = d.strftime("%d.%m.%Y")
    weekday_old = weekday_to_name(r["old_weekday"])

    if r["change_kind"] == "one_time":
        kind_text = "разовый перенос"
        result_text = f"Желаемый вариант: {date_str} {r['new_time']}"
    elif r["change_kind"] == "permanent":
        kind_text = "перенос на постоянной основе"
        result_text = f"Желаемый вариант: {weekday_to_name(d.weekday())} {r['new_time']}"
    else:
        kind_text = "разовая отмена"
        result_text = f"Дата отмены: {date_str} {r['new_time']}"

    message_text = (
        f"📋 <b>Запрос #{req_id}</b>\n\n"
        f"👤 <b>Ученик:</b> {r['full_name'] or r['username']}\n"
        f"📝 <b>Тип:</b> {kind_text}\n"
        f"📅 <b>Было:</b> {weekday_old} {r['old_time']}\n"
        f"🔄 <b>Хочет:</b> {result_text}\n"
    )

    if r["comment"]:
        message_text += f"\n💬 <b>Комментарий ученика:</b>\n{r['comment']}"

    # В кнопке "Назад" передаем страницу и student_id
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"{APPROVE_REQUEST_PREFIX}{req_id}_{page}_{student_id}")
    builder.button(text="❌ Отклонить", callback_data=f"{REJECT_REQUEST_PREFIX}{req_id}_{page}_{student_id}")
    builder.button(text="⬅️ Назад к списку", callback_data=f"back_to_requests_list_{page}_{student_id}")
    builder.adjust(2)

    await callback_query.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("back_to_requests_list"))
async def back_to_requests_list(callback_query: CallbackQuery):
    # back_to_requests_list_{page}_{student_id}
    parts = callback_query.data.split("_")

    # parts = ["back", "to", "requests", "list", "{page}", "{student_id}"]
    try:
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        student_id = parts[5] if len(parts) > 5 else ""
    except (ValueError, IndexError):
        page = 0
        student_id = ""

    rows = get_pending_requests()
    if not rows:
        await callback_query.message.edit_text("Нет ожидающих запросов на перенос/отмену.")
        await callback_query.answer()
        return

    requests_kb, pagination_kb, total_pages = create_requests_keyboard(rows, page=page, student_id=student_id)

    await callback_query.message.edit_text(
        f"📜 <b>Ожидающие запросы (страница {page + 1}/{total_pages}):</b>\n\n"
        "Нажми на запрос для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=requests_kb
    )
    await callback_query.answer()



# ====== APPROVE / REJECT transfer requests (FIXED) ======

@router.callback_query(lambda c: c.data and c.data.startswith(APPROVE_REQUEST_PREFIX))
async def approve_request_callback(callback_query: CallbackQuery):
    # approve_req_{req_id}_{page}_{student_id}
    tail = callback_query.data[len(APPROVE_REQUEST_PREFIX):]
    parts = tail.split("_")

    try:
        req_id = int(parts[0])
    except Exception:
        # даже если что-то сломано — просто закроем "крутилку"
        try:
            await callback_query.answer("Ошибка: некорректный запрос.", show_alert=True)
        except Exception:
            pass
        return

    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    student_id = parts[2] if len(parts) > 2 else ""

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⬅️ Назад к списку",
            callback_data=f"back_to_requests_list_{page}_{student_id}"
        )
    ]])

    # 1) СРАЗУ показываем “обрабатываю” прямо в сообщении + отключаем кнопки,
    # чтобы пользователь видел реакцию даже если callback.answer уже “одноразовый”.
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        await callback_query.message.edit_text(
            (callback_query.message.text or "") + "\n\n⏳ Обрабатываю...",
            reply_markup=None
        )
    except Exception:
        # если текст редактировать нельзя — не критично, продолжим обработку
        pass

    try:
        r = approve_transfer_request(req_id)
        if not r:
            try:
                await callback_query.message.edit_text(
                    "❌ Ошибка: запрос не найден или уже обработан.",
                    reply_markup=back_kb
                )
            finally:
                try:
                    await callback_query.answer()
                except Exception:
                    pass
            return

        # уведомляем ученика (если можем)
        try:
            await bot.send_message(
                int(r["telegram_id"]),
                "✅ Преподаватель одобрил ваш запрос на перенос/отмену занятия."
            )
        except Exception:
            logging.exception("Failed to notify student about approved request")

        # финальное сообщение учителю
        await callback_query.message.edit_text(
            "✅ Запрос успешно одобрен.",
            reply_markup=back_kb
        )

        await callback_query.answer()

    except Exception:
        logging.exception("approve_request_callback failed")
        # ВАЖНО: не пытаемся второй раз answer(show_alert=True), если уже ответили ранее.
        try:
            await callback_query.message.edit_text(
                "❌ Ошибка при обработке запроса. Проверь логи (approve_request_callback).",
                reply_markup=back_kb
            )
        except Exception:
            pass
        try:
            await callback_query.answer()
        except Exception:
            pass



@router.callback_query(lambda c: c.data and c.data.startswith(REJECT_REQUEST_PREFIX))
async def reject_request_callback(callback_query: CallbackQuery):
    # reject_req_{req_id}_{page}_{student_id}
    tail = callback_query.data[len(REJECT_REQUEST_PREFIX):]
    parts = tail.split("_")

    try:
        req_id = int(parts[0])
    except Exception:
        try:
            await callback_query.answer("Ошибка: некорректный запрос.", show_alert=True)
        except Exception:
            pass
        return

    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    student_id = parts[2] if len(parts) > 2 else ""

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⬅️ Назад к списку",
            callback_data=f"back_to_requests_list_{page}_{student_id}"
        )
    ]])

    # показываем прогресс и отключаем кнопки
    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        await callback_query.message.edit_text(
            (callback_query.message.text or "") + "\n\n⏳ Обрабатываю...",
            reply_markup=None
        )
    except Exception:
        pass

    try:
        r = reject_transfer_request(req_id)
        if not r:
            try:
                await callback_query.message.edit_text(
                    "❌ Ошибка: запрос не найден или уже обработан.",
                    reply_markup=back_kb
                )
            finally:
                try:
                    await callback_query.answer()
                except Exception:
                    pass
            return

        # уведомляем ученика
        try:
            await bot.send_message(
                int(r["telegram_id"]),
                "🚫 Преподаватель отклонил ваш запрос на перенос/отмену занятия."
            )
        except Exception:
            logging.exception("Failed to notify student about rejected request")

        await callback_query.message.edit_text(
            "🚫 Запрос отклонён.",
            reply_markup=back_kb
        )

        await callback_query.answer()

    except Exception:
        logging.exception("reject_request_callback failed")
        try:
            await callback_query.message.edit_text(
                "❌ Ошибка при обработке запроса. Проверь логи (reject_request_callback).",
                reply_markup=back_kb
            )
        except Exception:
            pass
        try:
            await callback_query.answer()
        except Exception:
            pass




@router.callback_query(lambda c: c.data == "page_info")
async def page_info_callback(callback_query: CallbackQuery):
    await callback_query.answer()  # можно show_alert=False по умолчанию


@router.callback_query(lambda c: c.data == "page_info")
async def page_info_callback(callback_query: CallbackQuery):
    await callback_query.answer("Это индикатор страницы 🙂", show_alert=True)



@router.callback_query(lambda c: c.data.startswith(REJECT_REQUEST_PREFIX))
async def reject_request_callback(callback_query: CallbackQuery):
    # reject_req_{req_id}_{page}_{student_id}
    try:
        await callback_query.answer("⏳ Обрабатываю...")

        tail = callback_query.data[len(REJECT_REQUEST_PREFIX):]
        parts = tail.split("_")

        req_id = int(parts[0])
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        student_id = parts[2] if len(parts) > 2 else ""

        rejected = reject_transfer_request(req_id)

        if rejected:
            await callback_query.message.edit_text(
                "🚫 Запрос отклонён.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅️ Назад к списку",
                        callback_data=f"back_to_requests_list_{page}_{student_id}"
                    )
                ]])
            )
        else:
            await callback_query.message.edit_text(
                "❌ Ошибка: запрос не найден или уже обработан.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅️ Назад к списку",
                        callback_data=f"back_to_requests_list_{page}_{student_id}"
                    )
                ]])
            )

    except Exception:
        logging.exception("reject_request_callback failed")
        try:
            await callback_query.answer("Ошибка при обработке (см. логи).", show_alert=True)
        except Exception:
            pass


from datetime import date
import sqlite3


def cleanup_old_requests():
    today = date.today().isoformat()

    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM transfer_requests
                WHERE request_date < ?
            """, (today,))

            deleted = cursor.rowcount
            conn.commit()

            if deleted:
                logging.info(f"🧹 Удалено старых заявок: {deleted}")

    except Exception:
        logging.exception("cleanup_old_requests failed")

cleanup_old_requests()

# ---------- МНОГОШАГОВЫЙ /move ----------


@router.message(Command("move"))
async def cmd_move(message: Message, state: FSMContext):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    lessons = get_weekly_lessons_for_student(student["id"])
    if not lessons:
        await message.answer("Для тебя пока не задано ни одного слота.")
        return

    lines = ["Какое занятие хочешь изменить? Напиши номер:"]
    ids = []
    for i, wl in enumerate(lessons, start=1):
        ids.append(wl["id"])
        lines.append(f"{i}) {weekday_to_name(wl['weekday'])} {wl['time']}")

    await state.update_data(lesson_ids=ids)
    await state.set_state(MoveStates.choosing_lesson)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(MoveStates.choosing_lesson)
async def move_choose_lesson(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("lesson_ids", [])
    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно прислать номер занятия (1, 2, 3 ...).", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(ids)):
        await message.answer(
            "Нет занятия с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    chosen_id = ids[idx - 1]

    # Проверяем, что занятие активно
    wl = get_weekly_lesson_by_id(chosen_id)
    if not wl or wl["is_active"] != 1:
        await message.answer(
            "Это занятие больше не активно. Возможно, преподаватель удалил его. Выберите другое занятие или вернитесь в меню.",
            reply_markup=back_keyboard()
        )
        return

    await state.update_data(chosen_lesson_id=chosen_id)
    await state.set_state(MoveStates.choosing_kind)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="1"),
                KeyboardButton(text="2"),
                KeyboardButton(text="3"),
            ],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        "Что именно делаем с занятием?\n"
        "1 — разовый перенос (только одно занятие)\n"
        "2 — перенос на постоянной основе (каждую неделю)\n"
        "3 — ОТМЕНИТЬ это занятие разово в один из дней\n\n"
        "Выбери 1, 2 или 3 (можно нажать кнопку).",
        reply_markup=kb,
    )


@router.message(MoveStates.choosing_kind)
async def move_choose_kind(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text not in ("1", "2", "3"):
        await message.answer(
            "Ответь, пожалуйста, 1, 2 или 3.", reply_markup=back_keyboard()
        )
        return

    if text == "1":
        change_kind = "one_time"
    elif text == "2":
        change_kind = "permanent"
    else:
        change_kind = "cancel"

    await state.update_data(change_kind=change_kind)

    if change_kind == "permanent":
        await state.set_state(MoveStates.entering_weekday)
        await message.answer(
            "На какой день недели переносим занятие?\n"
            "Введи число от 1 до 7, где:\n"
            "1 - Понедельник\n2 - Вторник\n3 - Среда\n4 - Четверг\n"
            "5 - Пятница\n6 - Суббота\n7 - Воскресенье",
            reply_markup=back_keyboard(),
        )
    else:
        await state.set_state(MoveStates.entering_datetime)
        if change_kind == "cancel":
            hint = (
                "Укажи дату (и время, можно стандартное для этого занятия), "
                "когда НУЖНО ОТМЕНИТЬ урок.\n"
            )
        else:
            hint = "Укажи новую дату и время занятия.\n"

        await message.answer(
            hint
            + "Формат: ДД.ММ ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ\n"
              "Например: 05.12 19:00",
            reply_markup=back_keyboard(),
        )


@router.message(MoveStates.entering_weekday)
async def move_enter_weekday(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        weekday_human = int(text)
        if not 1 <= weekday_human <= 7:
            raise ValueError
        new_weekday = weekday_human - 1
    except ValueError:
        await message.answer(
            "День недели должен быть числом от 1 до 7. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(new_weekday=new_weekday)
    await state.set_state(MoveStates.entering_time)
    await message.answer(
        "На какое время переносим занятие? (формат HH:MM, например 19:00)",
        reply_markup=back_keyboard(),
    )


@router.message(MoveStates.entering_time)
async def move_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ, например 19:00. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(new_time=new_time)
    await state.set_state(MoveStates.entering_comment)

    await message.answer(
        "Напиши, пожалуйста, короткий комментарий, почему нужна смена расписания.\n"
        f"Если не хочешь писать комментарий — отправь просто «-».\n"
        f"Для отмены запроса — нажми «{BACK_TEXT}».",
        reply_markup=back_keyboard(),
    )


@router.message(MoveStates.entering_datetime)
async def move_enter_datetime(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    parts = text.split()
    if len(parts) != 2:
        await message.answer(
            "Нужно ввести: ДД.ММ[.ГГГГ] ЧЧ:ММ\nНапример: 05.12 19:00",
            reply_markup=back_keyboard(),
        )
        return

    date_str, time_str = parts

    new_date = parse_date_str(date_str)
    if not new_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ.",
            reply_markup=back_keyboard(),
        )
        return

    try:
        hh, mm = map(int, time_str.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ, например 19:00.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(new_date=new_date, new_time=new_time)
    await state.set_state(MoveStates.entering_comment)

    await message.answer(
        "Напиши, пожалуйста, короткий комментарий, почему нужна смена/отмена занятия.\n"
        "Например: «болею», «буду в дороге» и т.п.\n"
        f"Если не хочешь писать комментарий — отправь просто «-».\n"
        f"Для отмена запроса — нажми «{BACK_TEXT}».",
        reply_markup=back_keyboard(),
    )


@router.message(MoveStates.entering_comment)
async def move_enter_comment(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю запрос на перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    comment_text = text
    if comment_text in ("-", "—"):
        comment_text = None

    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        await state.clear()
        return

    data = await state.get_data()
    chosen_lesson_id = data["chosen_lesson_id"]
    change_kind = data["change_kind"]

    wl = get_weekly_lesson_by_id(chosen_lesson_id)
    if not wl:
        await message.answer(
            "Не удалось найти выбранное занятие. Попробуй ещё раз с /move."
        )
        await state.clear()
        return

    # Дополнительная проверка активности занятия
    if wl["is_active"] != 1:
        await message.answer(
            "Это занятие больше не активно. Возможно, преподаватель удалил его. "
            "Запрос на перенос/отмену не может быть создан.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    if change_kind == "permanent":
        new_weekday = data["new_weekday"]
        new_time: dtime = data["new_time"]
        # Для постоянного переноса используем сегодняшнюю дату, т.к. важны только день недели
        new_date = date.today()
    else:
        new_date: date = data["new_date"]
        new_time: dtime = data["new_time"]

    req_id = create_change_request(
        student_id=student["id"],
        weekly_lesson_id=chosen_lesson_id,
        old_weekday=wl["weekday"],
        old_time=wl["time"],
        new_date=new_date,
        new_time=new_time,
        change_kind=change_kind,
        comment=comment_text,
    )

    weekday_old = weekday_to_name(wl["weekday"])
    if change_kind == "one_time":
        kind_text = "разовый перенос"
        result_text = f"Желаемый вариант: {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}"
    elif change_kind == "permanent":
        kind_text = "перенос на постоянной основе"
        weekday_new = weekday_to_name(new_weekday)
        result_text = f"Желаемый вариант: {weekday_new} {new_time.strftime('%H:%M')}"
    else:
        kind_text = "разовая отмена"
        result_text = f"Дата отмены: {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}"

    await message.answer(
        f"Я отправил {kind_text} преподавателям.\n"
        f"Текущее занятие: {weekday_old} {wl['time']}\n"
        f"{result_text}\n"
        f"Номер запроса: #{req_id}.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    for admin_id in TEACHER_IDS:
        try:
            text_msg = (
                f"Новый запрос #{req_id} от {wl['full_name'] or wl['username']}.\n"
                f"Тип: {kind_text}\n"
                f"Было: {weekday_old} {wl['time']}\n"
                f"Хочет: {result_text}\n"
            )
            if comment_text:
                text_msg += f"Комментарий ученика: {comment_text}"
            await bot.send_message(admin_id, text_msg)
        except Exception as e:
            logging.error(
                f"Не удалось отправить сообщение преподавателю {admin_id}: {e}"
            )

    await state.clear()


# ---------- МАСТЕР ДОМАШКИ ДЛЯ ПРЕПОДА ----------


async def start_set_hw_wizard(message: Message, state: FSMContext):
    """Пошаговый мастер задания домашнего задания."""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    ids = []
    lines = ["Кому задаём домашку? Выбери номер ученика:"]

    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])

        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(student_ids=ids)
    await state.set_state(HomeworkStates.waiting_user)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


from aiogram.utils.keyboard import InlineKeyboardBuilder

DAY_BUTTONS = [
    ("Пн", 0),
    ("Вт", 1),
    ("Ср", 2),
    ("Чт", 3),
    ("Пт", 4),
    ("Сб", 5),
    ("Вс", 6),
]

def slot_weekday_inline_kb():
    b = InlineKeyboardBuilder()
    for title, wd in DAY_BUTTONS:
        b.button(text=title, callback_data=f"slot_weekday_{wd}")
    b.adjust(4, 3)  # 4 кнопки в ряд, затем 3
    return b.as_markup()


# ---------- ДОМАШКА: /set_hw ----------


@router.message(Command("set_hw"))
async def cmd_set_hw(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    parts = message.text.split(maxsplit=2)
    # Пошаговый режим
    if len(parts) == 1:
        await start_set_hw_wizard(message, state)
        return

    if len(parts) < 3:
        await message.answer(
            "Форматы:\n"
            "1) /set_hw @username текст домашки\n"
            "2) Просто /set_hw — и я спрошу всё по шагам, с выбором ученика из списка."
        )
        return

    _, user_key, hw_text = parts
    student = get_student_by_user_key(user_key)
    if not student:
        await message.answer(
            "Не нашёл такого ученика в базе.\n"
            "Убедись, что ученика уже писал боту /start."
        )
        return

    add_homework(student["id"], hw_text)

    # Отправляем уведомление ученику
    if student["telegram_id"]:
        await notify_homework_assigned(
            student_telegram_id=student["telegram_id"],
            homework_text=hw_text
        )

    await message.answer(
        f"Домашка для {student['full_name'] or student['username'] or student['telegram_id']} добавлена."
    )


@router.message(HomeworkStates.waiting_user)
async def hw_wait_user(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю задание домашки. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("student_ids", [])

    student = None

    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(student_id=student["id"])
    await state.set_state(HomeworkStates.waiting_text)
    await message.answer(
        "Отправь текст домашнего задания одним сообщением.",
        reply_markup=back_keyboard(),
    )


@router.message(HomeworkStates.waiting_text)
async def hw_wait_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю задание домашки. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    hw_text = text
    if not hw_text:
        await message.answer(
            "Похоже, домашка пустая. Напиши, пожалуйста, текст задания.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    student_id = data.get("hw_student_id") or data.get("student_id")

    if not student_id:
        await message.answer(
            "Не выбран ученик для домашки. Запусти процесс заново.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    add_homework(student_id, hw_text)

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    # Отправляем уведомление ученику
    if student and student["telegram_id"]:
        await notify_homework_assigned(
            student_telegram_id=student["telegram_id"],
            homework_text=hw_text
        )

    await message.answer(
        f"Домашка для {student['full_name'] or student['username'] or student['telegram_id']} добавлена.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


# ---------- ДОМАШКА: просмотр и завершение ----------


@router.message(Command("list_hw"))
async def cmd_list_hw(message: Message):
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Формат:\n"
            "/list_hw @username\n"
            "или\n"
            "/list_hw telegram_id"
        )
        return

    _, user_key = parts[0], parts[1]
    student = get_student_by_user_key(user_key)
    if not student:
        await message.answer("Не нашёл такого ученика.")
        return

    hws = get_homeworks_for_student(student["id"], only_open=False)
    if not hws:
        await message.answer("Для этого ученика ещё нет домашних заданий.")
        return

    lines = []
    for h in hws:
        status = "✅" if h["is_done"] else "❗"
        created = datetime.fromisoformat(h["created_at"]).strftime("%d.%m.%Y")
        lines.append(f"{status} #{h['id']} от {created}: {h['text']}")

    await message.answer(
        f"Домашние задания для {student['full_name'] or student['username'] or student['telegram_id']}:\n"
        + "\n".join(lines)
    )





@router.message(Command("myhw"))
async def cmd_myhw(message: Message):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    hws = get_homeworks_for_student(student["id"], only_open=True)
    if not hws:
        await message.answer("У тебя сейчас нет невыполненных домашних заданий 🎉")
        return

    lines = []
    for h in hws:
        created = datetime.fromisoformat(h["created_at"]).strftime("%d.%m.%Y")
        lines.append(f"#{h['id']} от {created}: {h['text']}")

    lines.append("\nКогда сделаешь задание, можешь написать команду /done_hw.")
    await message.answer("Твои активные домашние задания:\n" + "\n".join(lines))


# ---------- НОВЫЙ /done_hw ----------


@router.message(Command("done_hw"))
async def cmd_done_hw(message: Message, state: FSMContext):
    # Для преподавателя остаётся старый формат /done_hw ID
    if is_teacher(message):
        parts = message.text.split()
        if len(parts) != 2:
            await message.answer("Формат для преподавателя: /done_hw ID_домашки")
            return

        try:
            hw_id = int(parts[1])
        except ValueError:
            await message.answer("ID домашки должен быть числом.")
            return

        hw = get_homework_by_id(hw_id)
        if not hw:
            await message.answer("Домашка с таким ID не найдена.")
            return

        if hw["is_done"]:
            await message.answer("Эта домашка уже отмечена как выполненная.")
            return

        mark_homework_done(hw_id)

        # Отправляем уведомление ученику
        if hw["telegram_id"]:
            await notify_homework_done(
                student_telegram_id=hw["telegram_id"],
                homework_id=hw_id
            )

        await message.answer("Домашка отмечена как выполненная ✅")

        return

    # Для ученика — мастер
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    hws = get_homeworks_for_student(student["id"], only_open=True)
    if not hws:
        await message.answer("У тебя сейчас нет невыполненных домашних заданий 🎉")
        return

    if len(hws) == 1:
        hw = hws[0]
        await state.update_data(done_hw_id=hw["id"])
        await state.set_state(HomeworkDoneStates.confirming_hw)

        created = datetime.fromisoformat(hw["created_at"]).strftime("%d.%m.%Y")
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=YES_TEXT)],
                [KeyboardButton(text=BACK_TEXT)],
            ],
            resize_keyboard=True,
        )

        await message.answer(
            f"У тебя одно активное задание:\n"
            f"#{hw['id']} от {created}: {hw['text']}\n\n"
            f"Отметить его выполненным?",
            reply_markup=kb,
        )
        return

    # Несколько домашек — просим выбрать ID
    lines = ["Выбери, какую домашку отметить выполненной. Пришли её номер (ID):"]
    for h in hws:
        created = datetime.fromisoformat(h["created_at"]).strftime("%d.%m.%Y")
        lines.append(f"#{h['id']} от {created}: {h['text']}")

    await state.set_state(HomeworkDoneStates.choosing_hw)
    await message.answer(
        "\n".join(lines) + f"\n\nЕсли передумал — нажми «{BACK_TEXT}».",
        reply_markup=back_keyboard(),
    )


@router.message(HomeworkDoneStates.choosing_hw)
async def done_hw_choose(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Окей, ничего не отмечаю. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hw_id = int(text)
    except ValueError:
        await message.answer(
            "Нужно прислать числовой ID домашки. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    hw = get_homework_by_id(hw_id)
    student = get_student_by_telegram_id(message.from_user.id)
    if not hw or not student or hw["student_id"] != student["id"]:
        await message.answer(
            "Не нашёл такую домашку среди твоих активных. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    if hw["is_done"]:
        await message.answer(
            "Эта домашка уже отмечена как выполненная.", reply_markup=back_keyboard()
        )
        return

    await state.update_data(done_hw_id=hw_id)
    await state.set_state(HomeworkDoneStates.confirming_hw)

    created = datetime.fromisoformat(hw["created_at"]).strftime("%d.%m.%Y")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=YES_TEXT)],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        f"Отметить выполненной домашку #{hw['id']} от {created}?\n\n{hw['text']}",
        reply_markup=kb,
    )


@router.message(HomeworkDoneStates.confirming_hw)
async def done_hw_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Окей, ничего не отмечаю. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text not in (YES_TEXT, "Да", "да"):
        await message.answer(
            f"Если хочешь отметить домашку выполненной — нажми «{YES_TEXT}».\n"
            f"Если передумал — нажми «{BACK_TEXT}».",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text=YES_TEXT)],
                    [KeyboardButton(text=BACK_TEXT)],
                ],
                resize_keyboard=True,
            ),
        )
        return

    data = await state.get_data()
    hw_id = data.get("done_hw_id")
    if hw_id is None:
        await state.clear()
        await message.answer(
            "Что-то пошло не так, попробуй ещё раз с /done_hw.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    hw = get_homework_by_id(hw_id)
    student = get_student_by_telegram_id(message.from_user.id)
    if not hw or not student or hw["student_id"] != student["id"]:
        await state.clear()
        await message.answer(
            "Не удалось найти домашку. Попробуй ещё раз с /done_hw.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if hw["is_done"]:
        await state.clear()
        await message.answer(
            "Эта домашка уже отмечена как выполненная.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    mark_homework_done(hw_id)

    await message.answer(
        "Домашка отмечена как выполненная ✅",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    # Уведомляем преподавателей
    for admin_id in TEACHER_IDS:
        try:
            student_name = hw["full_name"] or hw["username"] or str(hw["telegram_id"])
            await bot.send_message(
                admin_id,
                f"{student_name} отметил домашку #{hw_id} как выполненную.",
            )
        except Exception as e:
            logging.error(
                f"Не удалось уведомить преподавателя {admin_id} о выполненной домашке: {e}"
            )

    await state.clear()


# ---------- РАЗОВАЯ ОТМЕНА ОТ ПРЕПОДАВАТЕЛЯ ----------


@router.message(Command("cancel_lesson"))
async def cmd_cancel_lesson(message: Message, state: FSMContext):
    """Команда отмены занятия (старый формат)"""
    # Просто запускаем тот же мастер, что и по кнопке
    await handle_cancel_lesson_button(message, state)


@router.message(CancelStates.choosing_student_smart)
async def cancel_choose_student_smart(message: Message, state: FSMContext):
    """Обработка текстового ввода при выборе ученика для отмены (умный режим)"""
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю отмену занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Если пользователь отправил текст, а не выбрал из списка,
    # предлагаем выбрать из инлайн-клавиатуры
    await message.answer(
        "Пожалуйста, выберите ученика из списка выше, используя кнопки.",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )


@router.message(CancelStates.choosing_lesson)
async def cancel_choose_lesson(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    lesson_ids = data.get("cancel_lesson_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер занятия в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(lesson_ids)):
        await message.answer(
            "Нет занятия с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    lesson_id = lesson_ids[idx - 1]
    await state.update_data(cancel_lesson_id=lesson_id)
    await state.set_state(CancelStates.entering_date)
    await message.answer(
        "На какую дату нужно ОТМЕНИТЬ это занятие разово?\n"
        "Формат: ДД.ММ или ДД.ММ.ГГГГ",
        reply_markup=back_keyboard(),
    )


@router.message(CancelStates.entering_date)
async def cancel_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    cancel_date = parse_date_str(text)
    if not cancel_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    lesson_id = data["cancel_lesson_id"]

    wl = get_weekly_lesson_by_id(lesson_id)
    if not wl:
        await message.answer(
            "Не удалось найти занятие. Попробуй ещё раз с /cancel_lesson."
        )
        await state.clear()
        return

    hh, mm = map(int, wl["time"].split(":"))
    lesson_time = dtime(hh, mm)

    # Создаем оверрайд с напоминанием по умолчанию 60 минут
    student_data = create_lesson_override(
        weekly_lesson_id=lesson_id,
        override_date=cancel_date,
        new_time=lesson_time,
        change_kind="cancel",
    )

    # Отправляем уведомление ученику
    if student_data and student_data["telegram_id"]:
        await notify_one_time_change(
            student_telegram_id=student_data["telegram_id"],
            change_date=cancel_date,
            new_time=wl["time"],
            old_weekday=wl["weekday"],
            old_time=wl["time"],
            is_cancellation=True
        )

    student_name = wl["full_name"] or wl["username"] or str(wl["telegram_id"])

    await message.answer(
        f"Занятие для {student_name} {cancel_date.strftime('%d.%m.%Y')} в {wl['time']} "
        f"отменено разово. Регулярный слот остаётся без изменений.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


# ---------- РАЗОВЫЙ ПЕРЕНОС ОТ ПРЕПОДАВАТЕЛЯ ----------


@router.message(Command("reschedule"))
async def cmd_reschedule(message: Message, state: FSMContext):
    """Разовый перенос занятия преподавателем"""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    ids = []
    lines = ["У кого переносим занятие разово? Выбери номер ученика:"]

    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])

        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(reschedule_student_ids=ids)
    await state.set_state(RescheduleStates.choosing_student)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())

from datetime import time as dtime

def parse_time_str(t: str) -> dtime:
    # поддерживает "9:00" и "09:00"
    hh, mm = map(int, t.strip().split(":"))
    return dtime(hh, mm)


@router.message(RescheduleStates.choosing_student)
async def reschedule_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("reschedule_student_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер ученика в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(ids)):
        await message.answer(
            "Нет ученика с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    student_id = ids[idx - 1]
    lessons = get_weekly_lessons_for_student(student_id)
    if not lessons:
        await message.answer("У этого ученика нет слотов. Переносить нечего.")
        await state.clear()
        return

    lesson_ids = []
    lines = ["Какое занятие переносим разово? Выбери номер:"]
    for i, wl in enumerate(lessons, start=1):
        lesson_ids.append(wl["id"])
        lines.append(f"{i}) {weekday_to_name(wl['weekday'])} {wl['time']}")

    await state.update_data(
        reschedule_student_id=student_id,
        reschedule_lesson_ids=lesson_ids
    )
    await state.set_state(RescheduleStates.choosing_lesson)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(RescheduleStates.choosing_lesson)
async def reschedule_choose_lesson(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    lesson_ids = data.get("reschedule_lesson_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер занятия в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(lesson_ids)):
        await message.answer(
            "Нет занятия с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    lesson_id = lesson_ids[idx - 1]
    await state.update_data(reschedule_lesson_id=lesson_id)
    await state.set_state(RescheduleStates.entering_date)
    await message.answer(
        "На какую дату нужно ПЕРЕНЕСТИ это занятие разово?\n"
        "Формат: ДД.ММ или ДД.ММ.ГГГГ",
        reply_markup=back_keyboard(),
    )


@router.message(RescheduleStates.entering_date)
async def reschedule_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    new_date = parse_date_str(text)
    if not new_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(reschedule_new_date=new_date)
    await state.set_state(RescheduleStates.entering_time)
    await message.answer(
        "На какое время переносим занятие? (формат HH:MM, например 19:00)",
        reply_markup=back_keyboard(),
    )


@router.message(RescheduleStates.entering_time)
async def reschedule_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю операцию. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ, например 19:00. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    lesson_id = data["reschedule_lesson_id"]
    new_date = data["reschedule_new_date"]

    wl = get_weekly_lesson_by_id(lesson_id)
    if not wl:
        await message.answer(
            "Не удалось найти занятие. Попробуй ещё раз с /reschedule."
        )
        await state.clear()
        return

    await state.update_data(
        reschedule_new_time=new_time,
        reschedule_weekday=wl["weekday"],
        reschedule_old_time=wl["time"]
    )
    await state.set_state(RescheduleStates.confirming)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, перенести")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        f"Вы действительно хотите перенести занятие?\n"
        f"Ученик: {wl['full_name'] or wl['username']}\n"
        f"Текущее: {weekday_to_name(wl['weekday'])} {wl['time']}\n"
        f"Новое: {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}\n\n"
        f"Это разовый перенос. Регулярное расписание останется без изменений.",
        reply_markup=kb,
    )


@router.message(RescheduleStates.confirming)
async def reschedule_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю перенос занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, перенести":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, перенести» или «❌ Нет, отменить»."
        )
        return

    data = await state.get_data()
    lesson_id = data.get("reschedule_lesson_id")
    new_date = data.get("reschedule_new_date")
    new_time = data.get("reschedule_new_time")
    old_weekday = data.get("reschedule_weekday")
    old_time = data.get("reschedule_old_time")

    if not lesson_id or not new_date or not new_time:
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Создаем оверрайд с напоминанием по умолчанию 60 минут
    student_data = create_lesson_override(
        weekly_lesson_id=lesson_id,
        override_date=new_date,
        new_time=new_time,
        change_kind="one_time",
    )

    # Отправляем уведомление ученику
    if student_data and student_data["telegram_id"]:
        await notify_one_time_change(
            student_telegram_id=student_data["telegram_id"],
            change_date=new_date,
            new_time=new_time.strftime("%H:%M"),
            old_weekday=old_weekday,
            old_time=old_time,
            is_cancellation=False
        )

    student_name = student_data["full_name"] or student_data["username"] or str(student_data["telegram_id"])

    await message.answer(
        f"Занятие для {student_name} перенесено на {new_date.strftime('%d.%m.%Y')} в {new_time.strftime('%H:%M')}.\n"
        f"Регулярный слот остаётся без изменений.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


# ---------- ИСТОРИЯ ДЛЯ УЧЕНИКА /myhistory ----------

def create_student_history_keyboard(history_rows, student_id: int):
    """Создает инлайн-клавиатуру с кнопками оспаривания для истории занятий ученика - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    builder = InlineKeyboardBuilder()

    for row in history_rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        time_str = row["time"]
        status = row["status"]
        paid = bool(row["paid"])
        topic = row["topic"] or "без темы"

        status_text = "✅" if status == "done" else "❌"
        paid_text = "💰" if paid else "🆓"

        # Обрезаем длинные темы
        if len(topic) > 20:
            topic_display = topic[:17] + "..."
        else:
            topic_display = topic

        button_text = f"{status_text}{paid_text} {date_str} {time_str} - {topic_display}"

        # Кнопка для оспаривания - используем простой формат без лишних параметров
        builder.button(
            text=button_text,
            callback_data=f"{DISPUTE_PREFIX}{row['id']}"  # Просто ID записи
        )

    builder.button(text="⬅️ Назад в меню", callback_data="back_to_student_menu")
    builder.adjust(1)  # одна кнопка в ряд
    return builder.as_markup()


@router.message(Command("myhistory"))
async def cmd_myhistory(message: Message):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    rows = get_lesson_history_for_student(student["id"], limit=20)
    if not rows:
        await message.answer("История занятий пока пустая.")
        return

    # Отправляем историю с кнопками оспаривания
    await message.answer(
        "🧾 <b>Последние занятия:</b>\n\n"
        "Нажми на занятие, чтобы оспорить запись:",
        parse_mode="HTML",
        reply_markup=create_student_history_keyboard(rows, student["id"])
    )


@router.callback_query(lambda c: c.data.startswith(DISPUTE_PREFIX))
async def dispute_lesson_callback(callback_query: CallbackQuery, state: FSMContext):
    """Обработка нажатия на кнопку оспаривания - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    # Извлекаем ID записи истории
    try:
        history_id = int(callback_query.data[len(DISPUTE_PREFIX):])
    except ValueError:
        await callback_query.answer("Ошибка: неверный формат данных")
        return

    student = get_student_by_telegram_id(callback_query.from_user.id)
    if not student:
        await callback_query.answer("Ошибка: ученик не найден")
        return

    history_record = get_lesson_history_by_id(history_id)
    if not history_record:
        await callback_query.answer("Запись в истории не найдена")
        return

    # Проверяем, принадлежит ли запись ученику
    if history_record["student_id"] != student["id"]:
        await callback_query.answer("Эта запись не принадлежит вам")
        return

    # Сохраняем ID записи в состояние и переходим к вводу причины
    await state.update_data(dispute_history_id=history_id)
    await state.set_state(DisputeStates.entering_reason)

    # Форматируем дату и время
    d = date.fromisoformat(history_record["date"])
    date_str = d.strftime("%d.%m.%Y")

    await callback_query.message.answer(
        f"⚖️ <b>Оспаривание записи #{history_id}</b>\n\n"
        f"Дата: {date_str}\n"
        f"Время: {history_record['time']}\n"
        f"Статус: {'состоялось' if history_record['status'] == 'done' else 'отменено'}\n"
        f"Оплата: {'оплачено' if history_record['paid'] else 'не оплачено'}\n"
        f"Тема: {history_record['topic'] or 'не указана'}\n\n"
        f"Пожалуйста, укажите причину оспаривания этой записи:",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()


@router.message(DisputeStates.entering_reason)
async def dispute_enter_reason(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю оспаривание. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if not text:
        await message.answer(
            "Пожалуйста, укажите причину оспаривания.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    history_id = data.get("dispute_history_id")

    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Ошибка: ученик не найден")
        await state.clear()
        return

    # Создаем запись о споре
    dispute_id = create_dispute(history_id, student["id"], text)

    # Отправляем уведомление ученику
    await notify_dispute_created(
        student_telegram_id=student["telegram_id"],
        history_id=history_id,
        reason=text
    )

    # Отправляем уведомление преподавателям
    student_name = student["full_name"] or student["username"] or str(student["telegram_id"])
    await notify_teachers_about_dispute(
        history_id=history_id,
        student_name=student_name,
        reason=text
    )

    await message.answer(
        "✅ <b>Спор создан!</b>\n\n"
        f"Ваше оспаривание записи #{history_id} отправлено преподавателям.\n"
        f"Причина: {text}\n\n"
        f"Преподаватель рассмотрит ваш спор в ближайшее время.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )

    await state.clear()


# ---------- ИСТОРИЯ ДЛЯ ПРЕПОДАВАТЕЛЯ С КНОПКАМИ ОПЛАТЫ ----------

def create_history_keyboard(student_id: int, history_rows, page: int = 0):
    """Создает инлайн-клавиатуру с кнопками оплаты для истории занятий с пагинацией"""
    if not history_rows:
        return None, 0

    # Разбиваем на страницы
    page_size = 10
    total_pages = (len(history_rows) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(history_rows))
    page_rows = history_rows[start_idx:end_idx]

    builder = InlineKeyboardBuilder()

    for row in page_rows:
        paid = bool(row["paid"])
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        topic = row["topic"] or "без темы"

        if len(topic) > 20:
            topic = topic[:17] + "..."

        if paid:
            button_text = f"✅ {date_str} {row['time']} - {topic}"
            callback_data = f"{PAY_PREFIX}{row['id']}_0_{page}_{student_id}"
        else:
            button_text = f"❌ {date_str} {row['time']} - {topic}"
            callback_data = f"{PAY_PREFIX}{row['id']}_1_{page}_{student_id}"

        builder.button(text=button_text, callback_data=callback_data)

    builder.adjust(1)

    # Добавляем пагинацию, если нужно
    if total_pages > 1:
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton(
                text="◀️",
                callback_data=f"history_page_{page-1}_{student_id}"
            ))
        pagination_buttons.append(InlineKeyboardButton(
            text=f"{page+1}/{total_pages}",
            callback_data="page_info"
        ))
        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton(
                text="▶️",
                callback_data=f"history_page_{page+1}_{student_id}"
            ))
        builder.row(*pagination_buttons)

    builder.row(InlineKeyboardButton(text="⬅️ Назад в меню", callback_data=BACK_CALLBACK))

    return builder.as_markup(), total_pages

@router.callback_query(lambda c: c.data.startswith("history_page_"))
async def history_page_callback(callback_query: CallbackQuery):
    """Обработчик пагинации для истории занятий"""
    page, student_id_str = Paginator.parse_callback_data(callback_query.data)

    if not student_id_str:
        await callback_query.answer("Ошибка: не указан ID ученика")
        return

    try:
        student_id = int(student_id_str)
    except ValueError:
        await callback_query.answer("Ошибка: некорректный ID ученика")
        return

    rows = get_lesson_history_for_student(student_id, limit=100)  # Увеличиваем лимит
    if not rows:
        await callback_query.message.edit_text("История занятий пустая.")
        await callback_query.answer()
        return

    # Получаем данные ученика
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])

    # Получаем клавиатуры для новой страницы
    history_kb, pagination_kb, total_pages = create_history_keyboard(student_id, rows, page)

    # Обновляем основное сообщение
    await callback_query.message.edit_text(
        f"История занятий ученика {student_name} (страница {page + 1}/{total_pages}):\n"
        f"Нажми на занятие, чтобы изменить статус оплаты:",
        reply_markup=history_kb
    )

    # Если есть пагинация, обновляем отдельное сообщение
    if pagination_kb:
        try:
            async for msg in callback_query.message.bot.get_chat_history(
                    callback_query.message.chat.id,
                    limit=3
            ):
                if "Навигация по страницам" in msg.text:
                    await msg.edit_text(
                        f"Навигация по страницам (страница {page + 1}/{total_pages}):",
                        reply_markup=pagination_kb
                    )
                    break
        except Exception as e:
            logging.error(f"Ошибка при обновлении пагинации истории: {e}")

    await callback_query.answer(f"Страница {page + 1}")

@router.message(Command("history"))
async def cmd_history(message: Message):
    if not is_teacher(message):
        await message.answer(
            "Эта команда только для преподавателя.\nУченикам доступна /myhistory."
        )
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Формат: /history @username\n"
            "или: /history telegram_id\n\n"
            "Показываю последние 20 занятий ученика с возможностью отметить оплату."
        )
        return

    user_key = parts[1].strip()
    student = get_student_by_user_key(user_key)
    if not student:
        await message.answer("Не нашёл такого ученика.")
        return

    rows = get_lesson_history_for_student(student["id"], limit=20)
    if not rows:
        await message.answer("У этого ученика история занятий пока пустая.")
        return

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])

    # Создаем сообщение с историей и кнопками оплаты
    await message.answer(
        f"История занятий ученика {student_name}:\n"
        f"Нажми на занятие, чтобы изменить статус оплаты:",
        reply_markup=create_history_keyboard(student["id"], rows)
    )

def _get_col(row, key: str, idx: int):
    # sqlite3.Row / dict-like
    try:
        return row[key]
    except Exception:
        return row[idx]  # tuple fallback

@router.callback_query(lambda c: c.data.startswith(PAY_PREFIX))
async def process_payment_callback(callback_query: CallbackQuery):
    payload = callback_query.data[len(PAY_PREFIX):]   # всё после префикса
    parts = payload.split("_")

    # callback_data: {history_id}_{flag}_{page}_{student_id}
    history_id = int(parts[0])
    page = int(parts[2]) if len(parts) > 2 else 0
    student_id = int(parts[3]) if len(parts) > 3 else None

    # 1) узнаём текущий статус
    cur_row = get_lesson_history_by_id(history_id)
    if not cur_row:
        await callback_query.answer("Запись не найдена")
        return

    new_paid = 0 if cur_row["paid"] else 1

    # 2) обновляем
    payment_data = set_lesson_paid(history_id, paid=bool(new_paid))
    if payment_data:
        student_tg_id = _get_col(payment_data, "telegram_id", 2)
        lesson_date = date.fromisoformat(_get_col(payment_data, "date", 0))
        lesson_time = _get_col(payment_data, "time", 1)
        msg_text = await notify_payment_status(student_tg_id, lesson_date, lesson_time, bool(new_paid))

        # ДОБАВЬ: отправляем тот же текст родителям
        if student_id is not None:
            await notify_parents_about_payment(student_id, msg_text)

    # 3) перерисовываем кнопки (❌/✅) в текущем сообщении
    if student_id is not None:
        rows = get_lesson_history_for_student(student_id, limit=100)
        history_kb, total_pages = create_history_keyboard(student_id, rows, page=page)
        await callback_query.message.edit_reply_markup(reply_markup=history_kb)

    await callback_query.answer("Сохранено ✅")




@router.callback_query(lambda c: c.data == BACK_CALLBACK)
async def process_back_callback(callback_query: CallbackQuery):
    """Обработка нажатия на кнопку 'Назад в меню'"""
    await callback_query.message.delete()
    await callback_query.message.answer(
        "Возвращаю в главное меню.",
        reply_markup=main_menu_keyboard(True)
    )


@router.callback_query(lambda c: c.data == "back_to_student_menu")
async def process_back_to_menu_callback(callback_query: CallbackQuery):
    """Обработка нажатия на кнопку 'Назад в меню' для ученика"""
    student = get_student_by_telegram_id(callback_query.from_user.id)
    if not student:
        await callback_query.answer("Ошибка: ученик не найден")
        return

    await callback_query.message.delete()
    await callback_query.message.answer(
        "Возвращаю в главное меню.",
        reply_markup=main_menu_keyboard(False)
    )

# ---------- МАСТЕР ИСТОРИИ С КНОПКАМИ ОПЛАТЫ ----------

async def start_admin_student_history_wizard(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer(
            "Пока нет ни одного ученика. Пусть они напишут боту /start."
        )
        return

    ids = []
    lines = ["Выбери ученика, чью историю показать (номер в списке):"]
    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])
        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(history_student_ids=ids)
    await state.set_state(AdminStudentHistoryStates.waiting_student)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(AdminStudentHistoryStates.waiting_student)
async def admin_history_choose_student(message: Message, state: FSMContext):
    if message.text.strip() == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю показ истории. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    await message.answer("Выберите ученика кнопкой из списка выше 🙂")



# ---------- ИСТОРИЯ ПО ДНЯМ ДЛЯ ПРЕПОДАВАТЕЛЯ ----------


async def show_day_history(message: Message, lesson_date: date):
    ensure_history_for_past_lessons(lookback_days=14, min_after_start_minutes=30)
    rows = get_lesson_history_for_date(lesson_date)

    if not rows:
        await message.answer(
            f"На {lesson_date.strftime('%d.%m.%Y')} занятий в истории нет."
        )
        return

    lines = [f"История занятий за {lesson_date.strftime('%d.%m.%Y')}:"]

    for r in rows:
        t = r["time"]
        status = r["status"]
        paid = bool(r["paid"])
        topic = r["topic"] or "тема не указана"
        status_text = "состоялось" if status == "done" else "не состоялось / отменено"
        paid_text = "оплачено" if paid else "не оплачено"
        student_name = format_student_title(r["full_name"], r["username"], r["telegram_id"])
        line = f"#{r['id']} — {t} — {student_name} — {status_text}, {paid_text}, тема: {topic}"
        if r["note"]:
            line += f" (комментарий: {r['note']})"
        lines.append(line)

    await message.answer("\n".join(lines))


# ---------- НАСТРОЙКА НАПОМИНАНИЙ УЧЕНИКОМ ----------


@router.message(Command("set_remind"))
async def cmd_set_remind(message: Message, state: FSMContext):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    lessons = get_weekly_lessons_for_student(student["id"])
    if not lessons:
        await message.answer(
            "У тебя пока нет регулярных занятий. Попроси преподавателя настроить слот."
        )
        return

    ids = []
    lines = ["Выбери занятие, для которого хочешь изменить напоминание (номер в списка):"]
    for i, wl in enumerate(lessons, start=1):
        ids.append(wl["id"])
        weekday_name = weekday_to_name(wl["weekday"])
        lines.append(
            f"{i}) {weekday_name} {wl['time']} — напоминание за {wl['remind_before_minutes']} мин"
        )

    await state.update_data(student_remind_lesson_ids=ids)
    await state.set_state(StudentRemindStates.choosing_lesson)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(StudentRemindStates.choosing_lesson)
async def student_remind_choose_lesson(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Окей, ничего не меняю. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(False),
        )
        return

    data = await state.get_data()
    ids = data.get("student_remind_lesson_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер занятия в списке. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    if not (1 <= idx <= len(ids)):
        await message.answer(
            "Нет занятия с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    lesson_id = ids[idx - 1]
    await state.update_data(student_remind_lesson_id=lesson_id)
    await state.set_state(StudentRemindStates.entering_minutes)
    await message.answer(
        "За сколько минут до начала присылать напоминание?\n"
        "Например: 30, 60 или 90.",
        reply_markup=back_keyboard(),
    )


@router.message(StudentRemindStates.entering_minutes)
async def student_remind_enter_minutes(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Окей, ничего не меняю. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(False),
        )
        return

    try:
        minutes = int(text)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "Нужно положительное целое число минут, например 60. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    lesson_id = data.get("student_remind_lesson_id")
    if lesson_id is None:
        await state.clear()
        await message.answer(
            "Что-то пошло не так, попробуй ещё раз с /set_remind.",
            reply_markup=main_menu_keyboard(False),
        )
        return

    wl = get_weekly_lesson_by_id(lesson_id)
    if not wl or wl["telegram_id"] != message.from_user.id:
        await state.clear()
        await message.answer(
            "Не удалось найти занятие. Попробуй ещё раз с /set_remind.",
            reply_markup=main_menu_keyboard(False),
        )
        return

    update_weekly_lesson_remind(lesson_id, minutes)

    # Отправляем уведомление ученику об изменении напоминания
    await notify_reminder_changed(
        student_telegram_id=message.from_user.id,
        weekday=wl["weekday"],
        time_str=wl["time"],
        new_remind=minutes
    )

    weekday_name = weekday_to_name(wl["weekday"])
    await message.answer(
        f"Готово! Для занятия {weekday_name} в {wl['time']} напоминание будет приходить за {minutes} мин.",
        reply_markup=main_menu_keyboard(False),
    )
    await state.clear()


# ---------- ПОЛЕЗНЫЕ ССЫЛКИ ----------


@router.message(Command("my_links"))
async def cmd_my_links(message: Message):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    links = get_links_for_student(student["id"])
    if not links:
        await message.answer(
            "Для тебя пока не настроены полезные ссылки.\n"
            "Если они нужны — напомни, пожалуйста, преподавателю."
        )
        return

    lines = ["Твои полезные ссылки:"]
    for l in links:
        title = l["title"] or "Ссылка"
        url = l["url"]
        lines.append(f"• {title} — {url}")

    await message.answer("\n".join(lines))


async def start_edit_links_wizard(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer(
            "Пока нет ни одного ученика. Пусть они напишут боту /start."
        )
        return

    ids = []
    lines = ["Для какого ученика изменить список ссылок? Выбери номер:"]
    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])
        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(edit_links_student_ids=ids)
    await state.set_state(AdminEditLinksStates.waiting_student)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(AdminEditLinksStates.waiting_student)
async def edit_links_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю редактирование ссылок. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    data = await state.get_data()
    ids = data.get("edit_links_student_ids", [])

    student = None
    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(edit_links_student_id=student["id"])

    existing = get_links_for_student(student["id"])
    if existing:
        lines = ["Сейчас для ученика заданы ссылки:"]
        for l in existing:
            lines.append(f"- {l['title'] or 'Ссылка'} — {l['url']}")
        lines.append("")
    else:
        lines = ["Сейчас для ученика ссылки не заданы."]

    lines.append(
        "Пришли НОВЫЙ список ссылок одним сообщением.\n"
        "Формат: каждая строка вида\n"
        "Название - https://example.com\n"
        "Старый список будет полностью заменён."
    )

    await state.set_state(AdminEditLinksStates.waiting_links)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(AdminEditLinksStates.waiting_links)
async def edit_links_set_links(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю редактирование ссылок. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    data = await state.get_data()
    student_id = data.get("edit_links_student_id")
    if student_id is None:
        await state.clear()
        await message.answer(
            "Что-то пошло не так, попробуй ещё раз с /edit_links.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    links: list[tuple[str, str]] = []
    for line in lines:
        if " - " in line:
            title, url = line.split(" - ", 1)
        elif "-" in line:
            title, url = line.split("-", 1)
        else:
            continue
        title = title.strip()
        url = url.strip()
        if not title or not url:
            continue
        links.append((title, url))

    if not links:
        await message.answer(
            "Не удалось распознать ни одной ссылки.\n"
            "Нужен формат вида: Название - https://example.com\n"
            "Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    replace_links_for_student(student_id, links)

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    await message.answer(
        f"Список ссылок для ученика {student['full_name'] or student['username'] or student['telegram_id']} обновлён.",
        reply_markup=main_menu_keyboard(True),
    )
    await state.clear()


# ---------- ОБЪЯВЛЕНИЯ ДЛЯ УЧЕНИКОВ ----------

async def _run_broadcast_send(report_to_tg_id: int, recipients: list[int], text: str):
    """
    Фоновая отправка рассылки, чтобы не блокировать polling.
    report_to_tg_id — кому отправить итоговый отчёт (преподавателю).
    """
    sent = 0
    failed = 0

    for uid in recipients:
        # маленькая пауза, чтобы не упираться в лимиты Telegram
        await asyncio.sleep(0.05)

        try:
            await bot.send_message(uid, text)
            sent += 1

        except TelegramRetryAfter as e:
            # Telegram сказал подождать N секунд — ждём и пробуем 1 раз повторить
            try:
                await asyncio.sleep(float(getattr(e, "retry_after", 1)))
                await bot.send_message(uid, text)
                sent += 1
            except Exception as e2:
                failed += 1
                logging.error(f"[broadcast] retry failed for {uid}: {e2}")

        except Exception as e:
            failed += 1
            logging.error(f"[broadcast] send failed for {uid}: {e}")

    # Отчёт преподавателю
    try:
        await bot.send_message(
            report_to_tg_id,
            f"📢 Рассылка завершена.\n✅ Отправлено: {sent}\n❌ Ошибок: {failed}\n👥 Получателей: {len(recipients)}",
        )
    except Exception as e:
        logging.error(f"[broadcast] failed to send report to teacher {report_to_tg_id}: {e}")



async def start_broadcast_wizard(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer(
            "Пока нет ни одного ученика. Пусть они напишут боту /start."
        )
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Всем ученикам")],
            [KeyboardButton(text="👤 Группа учеников")],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True,
    )

    await state.set_state(BroadcastStates.choosing_scope)
    await message.answer(
        "Кому отправляем объявление?\n"
        "• «👥 Всем ученикам» — рассылка на всех.\n"
        "• «👤 Группа учеников» — только выбранным.\n",
        reply_markup=kb,
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    await start_broadcast_wizard(message, state)


@router.message(BroadcastStates.choosing_scope)
async def broadcast_choose_scope(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю рассылку. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if text == "👥 Всем ученикам":
        await state.update_data(broadcast_scope="all")
        await state.set_state(BroadcastStates.entering_text)
        await message.answer(
            "Пришли текст объявления, которое отправить всем ученикам.",
            reply_markup=back_keyboard(),
        )
        return

    if text == "👤 Группа учеников":
        await state.update_data(broadcast_scope="group")
        await state.set_state(BroadcastStates.entering_group)
        await message.answer(
            "Пришли список учеников через пробел или с новой строки:\n"
            "@username или telegram_id.\n"
            "Пример:\n"
            "@masha @petya 123456789",
            reply_markup=back_keyboard(),
        )
        return

    await message.answer(
        "Выбери один из вариантов: «👥 Всем ученикам» или «👤 Группа учеников», "
        "или нажми «Назад».",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="👥 Всем ученикам")],
                [KeyboardButton(text="👤 Группа учеников")],
                [KeyboardButton(text=BACK_TEXT)],
            ],
            resize_keyboard=True,
        ),
    )


@router.message(BroadcastStates.entering_group)
async def broadcast_enter_group(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю рассылку. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    tokens = text.split()
    found_ids: list[int] = []
    not_found: list[str] = []

    for token in tokens:
        st = get_student_by_user_key(token)
        if st:
            found_ids.append(st["id"])
        else:
            not_found.append(token)

    found_ids = list(dict.fromkeys(found_ids))  # уникальные

    if not found_ids:
        await message.answer(
            "Не удалось найти ни одного ученика по этим данным.\n"
            "Убедись в @username / telegram_id и попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(broadcast_student_ids=found_ids)
    await state.set_state(BroadcastStates.entering_text)

    lines = ["Я нашёл следующих учеников:"]
    cur = conn.cursor()
    for sid in found_ids:
        cur.execute("SELECT * FROM students WHERE id = ?", (sid,))
        s = cur.fetchone()
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])

        lines.append(f"- {name} (ID={s['telegram_id']})")

    if not_found:
        lines.append("\nЭтих не нашёл:")
        for nf in not_found:
            lines.append(f"- {nf}")

    lines.append(
        "\nТеперь пришли текст объявления — я отправлю его только найденным ученикам."
    )

    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(BroadcastStates.entering_text)
async def broadcast_enter_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю рассылку. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if not text:
        await message.answer(
            "Похоже, объявление пустое. Напиши текст, пожалуйста.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    scope = data.get("broadcast_scope")

    recipients: list[int] = []  # telegram_id

    if scope == "all":
        students = get_all_students()
        for s in students:
            if s["telegram_id"]:
                recipients.append(s["telegram_id"])
        recipients = list(dict.fromkeys(recipients))
    elif scope == "group":
        ids: list[int] = data.get("broadcast_student_ids", [])
        cur = conn.cursor()
        for sid in ids:
            cur.execute("SELECT telegram_id FROM students WHERE id = ?", (sid,))
            row = cur.fetchone()
            if row and row["telegram_id"]:
                recipients.append(row["telegram_id"])
        recipients = list(dict.fromkeys(recipients))
    else:
        await state.clear()
        await message.answer(
            "Что-то пошло не так, попробуй ещё раз с /broadcast.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if not recipients:
        await state.clear()
        await message.answer(
            "Не удалось найти получателей рассылки.", reply_markup=main_menu_keyboard(True)
        )
        return

    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            logging.error(f"Не удалось отправить объявление {uid}: {e}")

    await state.clear()
    await message.answer(
        f"Объявление отправлено {sent} ученикам.",
        reply_markup=main_menu_keyboard(True),
    )@router.message(BroadcastStates.entering_text)
async def broadcast_enter_text(message: Message, state: FSMContext):
    # Важно: message.text может быть None (например, если прислали стикер/фото)
    text = (message.text or "").strip()

    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю рассылку. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if not text:
        await message.answer(
            "Похоже, объявление пустое. Напиши текст, пожалуйста.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    scope = data.get("broadcast_scope")

    recipients: list[int] = []  # telegram_id

    if scope == "all":
        students = get_all_students()
        for s in students:
            tg_id = s["telegram_id"]
            if tg_id:
                recipients.append(tg_id)
        recipients = list(dict.fromkeys(recipients))

    elif scope == "group":
        ids: list[int] = data.get("broadcast_student_ids", [])
        cur = conn.cursor()
        for sid in ids:
            cur.execute("SELECT telegram_id FROM students WHERE id = ?", (sid,))
            row = cur.fetchone()
            tg_id = row["telegram_id"] if row else None
            if tg_id:
                recipients.append(tg_id)
        recipients = list(dict.fromkeys(recipients))

    else:
        await state.clear()
        await message.answer(
            "Что-то пошло не так, попробуй ещё раз с /broadcast.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if not recipients:
        await state.clear()
        await message.answer(
            "Не удалось найти получателей рассылки.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    # Сразу освобождаем состояние и возвращаем меню — чтобы бот НЕ зависал на рассылке
    await state.clear()
    await message.answer(
        f"📢 Начинаю рассылку на {len(recipients)} учеников.\n"
        f"Я пришлю отчёт сюда, когда закончу.",
        reply_markup=main_menu_keyboard(True),
    )

    # Фоновая отправка, чтобы polling не блокировался
    asyncio.create_task(_run_broadcast_send(message.from_user.id, recipients, text))



# ---------- УДАЛЕНИЕ СЛОТА ПРЕПОДАВАТЕЛЕМ ----------


@router.message(Command("delete_slot"))
async def cmd_delete_slot(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    # сохраняем список в FSM, чтобы работала пагинация
    await state.update_data(delete_slot_students=students)

    # ⚠️ action_type сделаем отдельный, чтобы не конфликтовать с удалением пользователя и т.п.
    keyboard, _ = create_action_keyboard(students, "delslot", page=0)

    await state.set_state(DeleteSlotStates.choosing_student)

    await message.answer(
        "🗑️ <b>Удаление слота</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

@router.callback_query(lambda c: c.data.startswith("delslot_student_"))
async def delslot_select_student(callback_query: CallbackQuery, state: FSMContext):
    # delslot_student_{student_id}_{page}
    parts = callback_query.data.split("_")
    student_id = int(parts[2])

    await state.update_data(delete_slot_student_id=student_id)

    lessons = get_weekly_lessons_for_student(student_id, active_only=True)
    if not lessons:
        await state.clear()
        await callback_query.message.edit_text("У этого ученика нет активных слотов — удалять нечего.")
        await callback_query.answer()
        return

    kb = InlineKeyboardBuilder()
    for wl in lessons:
        # wl содержит w.* + full_name/username/telegram_id
        text = f"{weekday_to_name(wl['weekday'])} {wl['time']}"
        kb.add(InlineKeyboardButton(text=text, callback_data=f"delslot_lesson_{wl['id']}"))

    # по 1 кнопке в строке (чтобы не было каши)
    kb.adjust(1)

    await callback_query.message.edit_text(
        "🗑️ Какой слот удалить? Выбери из списка:",
        reply_markup=kb.as_markup(),
    )
    await callback_query.answer()



@router.callback_query(lambda c: c.data.startswith("delslot_lesson_"))
async def delslot_delete_lesson(callback_query: CallbackQuery, state: FSMContext):
    success = False
    try:
        lesson_id = int(callback_query.data.split("_")[2])
        deleted = deactivate_weekly_lesson(lesson_id)

        await state.clear()

        if not deleted:
            await callback_query.message.edit_text("Не нашёл слот (возможно, уже удалён).")
            return

        # данные слота (sqlite3.Row -> доступ по [])
        student_tg_id = deleted["telegram_id"]
        weekday = deleted["weekday"]
        time_str = deleted["time"]

        # ✅ уведомляем ученика (и не валим обработчик, если отправка не удалась)
        if student_tg_id:
            try:
                await notify_slot_deleted(
                    student_telegram_id=student_tg_id,
                    weekday=weekday,
                    time_str=time_str,
                )
            except Exception:
                logging.exception("Не удалось отправить уведомление ученику о удалении слота")

        student_label = (
            deleted["full_name"]
            or (f"@{deleted['username']}" if deleted["username"] else None)
            or str(student_tg_id or "")
        )

        await callback_query.message.edit_text(
            "✅ Слот удалён:\n"
            f"{weekday_to_name(weekday)} {time_str}\n"
            f"Ученик: {student_label}"
        )

        # ✅ “уведомление админу” отдельным сообщением + меню
        await callback_query.message.answer("✅ Готово.", reply_markup=main_menu_keyboard(True))

        success = True

    except Exception:
        logging.exception("Ошибка при удалении слота (delslot_delete_lesson)")

    finally:
        # ✅ чтобы не было вечного “крутится…”
        try:
            await callback_query.answer("Удалено ✅" if success else "Ошибка ❌", show_alert=not success)
        except Exception:
            pass





@router.callback_query(lambda c: c.data.startswith("delslot_page_"))
async def delslot_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("delete_slot_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, _ = create_action_keyboard(students, "delslot", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")



@router.message(DeleteSlotStates.choosing_student)
async def delete_slot_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю удаление слота. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("delete_slot_student_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер ученика в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(ids)):
        await message.answer(
            "Нет ученика с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    student_id = ids[idx - 1]
    lessons = get_weekly_lessons_for_student(student_id, active_only=False)
    if not lessons:
        await message.answer("У этого ученика нет слотов. Удалять нечего.")
        await state.clear()
        return

    # Фильтруем только активные слоты
    active_lessons = [l for l in lessons if l["is_active"] == 1]
    if not active_lessons:
        await message.answer("У этого ученика нет активных слотов. Все уже удалены.")
        await state.clear()
        return

    lesson_ids = []
    lines = ["Какой слот удаляем? Выбери номер:"]
    for i, wl in enumerate(active_lessons, start=1):
        lesson_ids.append(wl["id"])
        status = "✅ АКТИВНЫЙ" if wl["is_active"] == 1 else "❌ НЕАКТИВНЫЙ"
        lines.append(f"{i}) {weekday_to_name(wl['weekday'])} {wl['time']} — {status}")

    await state.update_data(
        delete_slot_student_id=student_id,
        delete_slot_lesson_ids=lesson_ids,
        delete_slot_lessons=active_lessons
    )
    await state.set_state(DeleteSlotStates.choosing_slot)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(DeleteSlotStates.choosing_slot)
async def delete_slot_choose_slot(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю удаление слота. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    lesson_ids = data.get("delete_slot_lesson_ids", [])
    lessons = data.get("delete_slot_lessons", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер слота в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(lesson_ids)):
        await message.answer(
            "Нет слота с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    lesson_id = lesson_ids[idx - 1]
    selected_lesson = lessons[idx - 1]

    # Удаляем слот (помечаем как неактивный)
    student_data = deactivate_weekly_lesson(lesson_id)

    # Уведомляем ученика
    if student_data and student_data["telegram_id"]:
        await notify_slot_deleted(
            student_telegram_id=student_data["telegram_id"],
            weekday=selected_lesson["weekday"],
            time_str=selected_lesson["time"]
        )

    student_name = (
            selected_lesson["full_name"]
            or selected_lesson["username"]
            or str(selected_lesson["telegram_id"])
    )

    await message.answer(
        f"🗑️ Слот удалён:\n"
        f"{student_name}\n"
        f"{weekday_to_name(selected_lesson['weekday'])} {selected_lesson['time']}",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()



# ---------- РУЧНОЕ ДОБАВЛЕНИЕ ЗАНЯТИЯ В ИСТОРИЮ ----------


@router.message(Command("add_history"))
async def cmd_add_history(message: Message, state: FSMContext):
    """Добавление занятия в историю вручную"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    # сохраняем студентов для пагинации
    await state.update_data(add_history_students=students)

    keyboard, total_pages = create_action_keyboard(students, "add_history", page=0)

    # остаёмся в этом же состоянии (логично: ждём выбора ученика)
    await state.set_state(AddManualHistoryStates.waiting_student)

    await message.answer(
        "📝 <b>Добавление занятия в историю</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@router.callback_query(lambda c: c.data.startswith("add_history_student_"))
async def add_history_select_student(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    student_id = int(parts[3])   # add_history_student_{id}_{page}
    # page = int(parts[4])  # можно не использовать

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    await state.update_data(add_history_student_id=student["id"])
    await state.set_state(AddManualHistoryStates.waiting_date)

    # убираем клавиатуру (редактируем сообщение с кнопками)
    await callback_query.message.edit_text(
        f"✅ Выбран ученик: {student['full_name'] or student['username'] or student['telegram_id']}"
    )

    # дальше идём по твоему существующему сценарию ввода даты
    await callback_query.message.answer(
        "Выберите дату занятия (последние 14 дней) или введите вручную (ДД.ММ.ГГГГ / ДД.ММ):",
        reply_markup=add_history_date_keyboard_last14(),
    )

    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith("add_history_page_"))
async def add_history_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[3])  # add_history_page_{page}

    data = await state.get_data()
    students = data.get("add_history_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, total_pages = create_action_keyboard(students, "add_history", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")


@router.message(AddManualHistoryStates.waiting_student)
async def add_history_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    data = await state.get_data()
    ids = data.get("add_history_student_ids", [])

    student = None
    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(add_history_student_id=student["id"])
    await state.set_state(AddManualHistoryStates.waiting_date)

    await callback_query.message.answer(
        "Выберите дату занятия (последние 14 дней) или введите вручную (ДД.ММ.ГГГГ / ДД.ММ):",
        reply_markup=add_history_date_keyboard_last14(),
    )


@router.message(AddManualHistoryStates.waiting_date)
async def add_history_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    lesson_date = parse_date_str(text)
    if not lesson_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. "
            "Выберите дату кнопкой (последние 14 дней) или введите вручную:",
            reply_markup=add_history_date_keyboard_last14(),
        )

        return

    await state.update_data(add_history_date=lesson_date)
    await state.set_state(AddManualHistoryStates.waiting_time)

    await message.answer(
        "Выберите время занятия:",
        reply_markup=add_history_time_keyboard_17_23(),
    )


@router.message(AddManualHistoryStates.waiting_time)
async def add_history_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        lesson_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Выберите время кнопкой (12:00–23:00) или введите вручную в формате ЧЧ:ММ.",
            reply_markup=add_history_time_keyboard_17_23(),
        )
        return

    await state.update_data(add_history_time=lesson_time)
    await state.set_state(AddManualHistoryStates.waiting_status)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Состоялось")],
            [KeyboardButton(text="❌ Отменено")],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        "Выберите статус занятия:",
        reply_markup=kb,
    )


@router.message(AddManualHistoryStates.waiting_status)
async def add_history_choose_status(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if text == "✅ Состоялось":
        status = "done"
    elif text == "❌ Отменено":
        status = "cancelled"
    else:
        await message.answer(
            "Пожалуйста, выберите статус: «✅ Состоялось» или «❌ Отменено».",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="✅ Состоялось")],
                    [KeyboardButton(text="❌ Отменено")],
                    [KeyboardButton(text=BACK_TEXT)],
                ],
                resize_keyboard=True,
            ),
        )
        return

    await state.update_data(add_history_status=status)
    await state.set_state(AddManualHistoryStates.waiting_paid)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Оплачено")],
            [KeyboardButton(text="❌ Не оплачено")],
            [KeyboardButton(text=BACK_TEXT)],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        "Выберите статус оплаты:",
        reply_markup=kb,
    )


@router.message(AddManualHistoryStates.waiting_paid)
async def add_history_choose_paid(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if text == "✅ Оплачено":
        paid = True
    elif text == "❌ Не оплачено":
        paid = False
    else:
        await message.answer(
            "Пожалуйста, выберите статус оплаты: «✅ Оплачено» или «❌ Не оплачено».",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="✅ Оплачено")],
                    [KeyboardButton(text="❌ Не оплачено")],
                    [KeyboardButton(text=BACK_TEXT)],
                ],
                resize_keyboard=True,
            ),
        )
        return

    await state.update_data(add_history_paid=paid)
    await state.set_state(AddManualHistoryStates.waiting_note)

    await message.answer(
        "Введите комментарий к занятию (или '-' чтобы пропустить):",
        reply_markup=back_keyboard(),
    )


@router.message(AddManualHistoryStates.waiting_note)
async def add_history_enter_note(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    note = None if text == "-" else text
    await state.update_data(add_history_note=note)
    await state.set_state(AddManualHistoryStates.waiting_topic)

    await message.answer(
        "Введите тему занятия (или '-' чтобы пропустить):",
        reply_markup=back_keyboard(),
    )


@router.message(AddManualHistoryStates.waiting_topic)
async def add_history_enter_topic(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    topic = None if text == "-" else text

    data = await state.get_data()
    student_id = data.get("add_history_student_id")
    lesson_date = data.get("add_history_date")
    lesson_time = data.get("add_history_time")
    status = data.get("add_history_status")
    paid = data.get("add_history_paid")
    note = data.get("add_history_note")

    # Добавляем запись в историю
    history_id = add_lesson_history(
        student_id=student_id,
        lesson_date=lesson_date,
        lesson_time=lesson_time,
        status=status,
        paid=paid,
        note=note,
        topic=topic,
        weekly_lesson_id=None,  # Нет связи с регулярным занятием
    )

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    status_text = "состоялось" if status == "done" else "отменено"
    paid_text = "оплачено" if paid else "не оплачено"

    await message.answer(
        f"Занятие добавлено в историю!\n"
        f"Ученик: {student['full_name'] or student['username']}\n"
        f"Дата: {lesson_date.strftime('%d.%m.%Y')}\n"
        f"Время: {lesson_time.strftime('%H:%M')}\n"
        f"Статус: {status_text}\n"
        f"Оплата: {paid_text}\n"
        f"Комментарий: {note or 'нет'}\n"
        f"Тема: {topic or 'не указана'}\n"
        f"ID записи: #{history_id}",
        reply_markup=main_menu_keyboard(True),
    )

    # Отправляем уведомление ученику
    if student["telegram_id"]:
        try:
            notification_text = (
                f"📝 <b>Добавлено занятие в историю</b>\n\n"
                f"• Дата: {lesson_date.strftime('%d.%m.%Y')}\n"
                f"• Время: {lesson_time.strftime('%H:%M')}\n"
                f"• Статус: {status_text}\n"
                f"• Оплата: {paid_text}\n"
                f"• Тема: {topic or 'не указана'}"
            )
            if note:
                notification_text += f"\n• Комментарий: {note}"

            await bot.send_message(
                student["telegram_id"],
                notification_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление ученику: {e}")

    await state.clear()


# ---------- РЕДАКТИРОВАНИЕ ИСТОРИИ ЗАНЯТИЙ ----------


@router.message(Command("edit_history"))
async def cmd_edit_history(message: Message, state: FSMContext):
    """Редактирование истории занятий"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    # сохраняем список в FSM для пагинации
    await state.update_data(edit_students=students)

    keyboard, total_pages = create_action_keyboard(students, "edit", page=0)

    await state.set_state(EditHistoryStates.choosing_student)
    await message.answer(
        "✏️ <b>Редактирование истории</b>\n\nВыберите ученика:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BACK_TEXT)]],
        resize_keyboard=True,
    )

@router.callback_query(lambda c: c.data.startswith("edit_student_"), EditHistoryStates.choosing_student)
async def edit_pick_student_callback(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split("_")
    student_id = int(parts[2])

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()
    if not student:
        await cb.answer("Ученик не найден", show_alert=True)
        return

    rows = get_lesson_history_for_student(student_id, limit=20)
    if not rows:
        await cb.message.edit_text("У этого ученика история занятий пока пустая.", reply_markup=None)
        await cb.answer()
        return

    await state.update_data(edit_history_student_id=student_id)
    await state.update_data(edit_history_rows=rows)
    await state.set_state(EditHistoryStates.choosing_history)

    builder = InlineKeyboardBuilder()
    for row in rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        status_text = "✅" if row["status"] == "done" else "❌"
        paid_text = "💰" if row["paid"] else "🆓"
        topic = row["topic"] or "без темы"
        builder.button(text=f"{status_text}{paid_text} {date_str} {row['time']} - {topic}",
                       callback_data=f"{EDIT_HISTORY_PREFIX}{row['id']}")

    builder.button(text="⬅️ Назад к выбору ученика", callback_data="back_to_student_select")
    builder.adjust(1)

    student_name = student["full_name"] or student["username"] or str(student["telegram_id"])
    await cb.message.edit_text(
        f"Выберите запись для редактирования (ученик {student_name}):",
        reply_markup=builder.as_markup()
    )
    await cb.answer()



@router.callback_query(lambda c: c.data.startswith("edit_page_"), EditHistoryStates.choosing_student)
async def edit_page_callback(callback_query: CallbackQuery, state: FSMContext):
    page = int(callback_query.data.split("_")[2])

    data = await state.get_data()
    students = data.get("edit_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    keyboard, total_pages = create_action_keyboard(students, "edit", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer(f"Страница {page + 1}")


@router.message(EditHistoryStates.choosing_student)
async def edit_history_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю редактирование истории. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    data = await state.get_data()
    ids = data.get("edit_history_student_ids", [])

    student = None
    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        students = get_all_students()
        if not students:
            await message.answer("Пока нет ни одного ученика.")
            await state.clear()
            return

        # важно: сохраняем для пагинации edit_page_
        await state.update_data(edit_students=students)
        await state.set_state(EditHistoryStates.choosing_student)

        kb, _ = create_action_keyboard(students, action_type="edit", page=0)

        await message.answer(
            "Выберите ученика для редактирования истории:",
            reply_markup=kb
        )
        return

    # Если ученик найден по тексту, продолжаем как раньше
    rows = get_lesson_history_for_student(student["id"], limit=20)
    if not rows:
        await message.answer("У этого ученика история занятий пока пустая.")
        await state.clear()
        await message.answer(
            "Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    await state.update_data(edit_history_student_id=student["id"])
    await state.update_data(edit_history_rows=rows)
    await state.set_state(EditHistoryStates.choosing_history)

    # Создаем клавиатуру с кнопками для выбора записи
    builder = InlineKeyboardBuilder()
    for row in rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        status_text = "✅" if row["status"] == "done" else "❌"
        paid_text = "💰" if row["paid"] else "🆓"
        topic = row["topic"] or "без темы"
        button_text = f"{status_text}{paid_text} {date_str} {row['time']} - {topic}"
        builder.button(text=button_text, callback_data=f"{EDIT_HISTORY_PREFIX}{row['id']}")

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])
    await message.answer(
        f"Выберите запись для редактирования (ученик {student_name}):",
        reply_markup=builder.as_markup()
    )

from aiogram.exceptions import TelegramBadRequest

@router.callback_query(lambda c: c.data == "back_to_student_select")
async def back_to_student_select(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    students = data.get("edit_students") or get_all_students()

    if not students:
        await callback_query.answer("Нет учеников")
        return

    # важно: сохраняем список, чтобы пагинация работала
    await state.update_data(edit_students=students)
    await state.set_state(EditHistoryStates.choosing_student)

    kb, _ = create_action_keyboard(students, "edit", page=0)

    try:
        await callback_query.message.edit_text(
            "Для какого ученика редактируем историю? Выбери ученика:",
            reply_markup=kb
        )
    except TelegramBadRequest as e:
        # если Telegram ругается "message is not modified" — просто молча игнорируем
        if "message is not modified" not in str(e):
            raise

    await callback_query.answer()



@router.callback_query(lambda c: c.data == "back_to_student_select")
async def back_to_student_select(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    students = data.get("edit_students")

    if not students:
        students = get_all_students()
        await state.update_data(edit_students=students)

    await state.set_state(EditHistoryStates.choosing_student)
    kb, _ = create_action_keyboard(students, action_type="edit", page=0)

    await cb.message.edit_text("Выберите ученика для редактирования истории:", reply_markup=kb)
    await cb.answer()





@router.callback_query(lambda c: c.data.startswith(EDIT_HISTORY_PREFIX))
async def edit_history_choose_record(callback_query: CallbackQuery, state: FSMContext):
    """Выбор записи для редактирования"""
    history_id = int(callback_query.data[len(EDIT_HISTORY_PREFIX):])

    record = get_lesson_history_by_id(history_id)
    if not record:
        await callback_query.answer("Запись не найдена")
        return

    await state.update_data(edit_history_id=history_id)
    await state.set_state(EditHistoryStates.choosing_field)

    # Форматируем дату
    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    # Создаем клавиатуру с полями для редактирования
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Статус", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}status")
    builder.button(text="💰 Оплата", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}paid")
    builder.button(text="📝 Комментарий", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}note")
    builder.button(text="📚 Тема", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}topic")
    builder.button(text="🗑️ Удалить запись", callback_data=f"{DELETE_HISTORY_PREFIX}{history_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_history_list")
    builder.button(text="📅 Дата/время", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}datetime")
    builder.adjust(2)

    status_text = "✅ состоялось" if record["status"] == "done" else "❌ отменено"
    paid_text = "💰 оплачено" if record["paid"] else "🆓 не оплачено"

    await callback_query.message.edit_text(
        f"📋 <b>Запись #{history_id}</b>\n\n"
        f"👤 <b>Ученик:</b> {record['full_name'] or record['username']}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {record['time']}\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"💳 <b>Оплата:</b> {paid_text}\n"
        f"📝 <b>Комментарий:</b> {record['note'] or 'нет'}\n"
        f"📚 <b>Тема:</b> {record['topic'] or 'не указана'}\n\n"
        f"Что вы хотите отредактировать?",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()

def get_student_by_id(student_id: int):
    """Получает ученика по ID"""
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    return cur.fetchone()

@router.callback_query(lambda c: c.data == "back_to_history_list")
async def back_to_history_list(callback_query: CallbackQuery, state: FSMContext):
    """Возврат к списку записей истории"""
    data = await state.get_data()
    student_id = data.get("edit_history_student_id")

    if not student_id:
        await callback_query.answer("Ошибка: не найден ID ученика")
        return

    student = get_student_by_telegram_id(student_id)
    if not student:
        cur = conn.cursor()
        cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        student = cur.fetchone()

    rows = get_lesson_history_for_student(student_id, limit=20)
    if not rows:
        await callback_query.message.edit_text("У этого ученика история занятий пока пустая.")
        await callback_query.answer()
        return

    await state.set_state(EditHistoryStates.choosing_history)
    await state.update_data(edit_history_rows=rows)

    # Создаем клавиатуру с кнопками для выбора записи
    builder = InlineKeyboardBuilder()
    for row in rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        status_text = "✅" if row["status"] == "done" else "❌"
        paid_text = "💰" if row["paid"] else "🆓"
        topic = row["topic"] or "без темы"
        button_text = f"{status_text}{paid_text} {date_str} {row['time']} - {topic}"
        builder.button(text=button_text, callback_data=f"{EDIT_HISTORY_PREFIX}{row['id']}")

    builder.button(text="⬅️ Назад к выбору ученика", callback_data="back_to_student_select")
    builder.adjust(1)

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])
    await callback_query.message.edit_text(
        f"Выберите запись для редактирования (ученик {student_name}):",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith(DELETE_HISTORY_PREFIX))
async def delete_history_record(callback_query: CallbackQuery, state: FSMContext):
    """Удаление записи из истории"""
    history_id = int(callback_query.data[len(DELETE_HISTORY_PREFIX):])

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Удаляем запись
    deleted_record = delete_lesson_history(history_id)

    if not deleted_record:
        await callback_query.answer("Запись не найдена")
        return

    await callback_query.answer(f"Запись #{history_id} удалена")

    # Возвращаемся к списку записей
    data = await state.get_data()
    student_id = data.get("edit_history_student_id")

    if not student_id:
        await callback_query.message.edit_text("Запись удалена. Ошибка: не найден ID ученика.")
        return

    student = get_student_by_telegram_id(student_id)
    if not student:
        cur = conn.cursor()
        cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        student = cur.fetchone()

    rows = get_lesson_history_for_student(student_id, limit=20)

    if not rows:
        await callback_query.message.edit_text(
            f"Запись удалена. Ученика {student['full_name'] or student['username']} больше нет записей в истории."
        )
        await state.clear()
        return

    await state.set_state(EditHistoryStates.choosing_history)
    await state.update_data(edit_history_rows=rows)

    # Создаем клавиатуру с кнопками для выбора записи
    builder = InlineKeyboardBuilder()
    for row in rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        status_text = "✅" if row["status"] == "done" else "❌"
        paid_text = "💰" if row["paid"] else "🆓"
        topic = row["topic"] or "без темы"
        button_text = f"{status_text}{paid_text} {date_str} {row['time']} - {topic}"
        builder.button(text=button_text, callback_data=f"{EDIT_HISTORY_PREFIX}{row['id']}")

    builder.button(text="⬅️ Назад к выбору ученика", callback_data="back_to_student_select")
    builder.adjust(1)

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])
    await callback_query.message.edit_text(
        f"Запись удалена. Выберите запись для редактирования (ученик {student_name}):",
        reply_markup=builder.as_markup()
    )


@router.callback_query(lambda c: c.data.startswith(EDIT_HISTORY_FIELD_PREFIX))
async def edit_history_choose_field(callback_query: CallbackQuery, state: FSMContext):
    """Выбор поля для редактирования"""
    field = callback_query.data[len(EDIT_HISTORY_FIELD_PREFIX):]

    data = await state.get_data()
    history_id = data.get("edit_history_id")

    if not history_id:
        await callback_query.answer("Ошибка: не найден ID записи")
        return

    record = get_lesson_history_by_id(history_id)
    if not record:
        await callback_query.answer("Запись не найдена")
        return

    if field == "status":
        await state.set_state(EditHistoryStates.editing_status)
        await callback_query.message.edit_text(
            "Выберите новый статус занятия:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Состоялось", callback_data="set_status_done")],
                [InlineKeyboardButton(text="❌ Отменено", callback_data="set_status_cancelled")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{EDIT_HISTORY_PREFIX}{history_id}")]
            ])
        )
    elif field == "paid":
        await state.set_state(EditHistoryStates.editing_paid)
        await callback_query.message.edit_text(
            "Выберите новый статус оплаты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Оплачено", callback_data="set_paid_1")],
                [InlineKeyboardButton(text="❌ Не оплачено", callback_data="set_paid_0")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{EDIT_HISTORY_PREFIX}{history_id}")]
            ])
        )
    elif field == "note":
        await state.set_state(EditHistoryStates.editing_note)
        await callback_query.message.answer(
            "Введите новый комментарий (или '-' чтобы удалить):",
            reply_markup=back_keyboard()
        )
        await callback_query.answer()
    elif field == "topic":
        await state.set_state(EditHistoryStates.editing_topic)
        await callback_query.message.answer(
            "Введите новую тему занятия (или '-' чтобы удалить):",
            reply_markup=back_keyboard()
        )
        await callback_query.answer()
    elif field == "datetime":
        await state.set_state(EditHistoryStates.editing_datetime)
        await callback_query.message.answer(
            "Введите новую дату и время занятия:\n\n"
            "Формат: 31.01.2026 14:30\n"
            "или:    2026-01-31 14:30\n\n"
            "Напишите одним сообщением.",
            reply_markup=back_keyboard()
        )
        await callback_query.answer()

    await callback_query.answer()

@router.message(EditHistoryStates.editing_datetime)
async def edit_history_set_datetime(message: Message, state: FSMContext):
    text = message.text.strip()

    if text == BACK_TEXT:
        # назад к карточке записи
        data = await state.get_data()
        history_id = data.get("edit_history_id")
        await state.set_state(EditHistoryStates.choosing_field)
        if history_id:
            # покажем снова меню полей
            dummy_cb = type("Dummy", (), {})()
            # проще: просто попросим нажать запись ещё раз или вызвать edit_history_choose_record через callback
            await message.answer("Ок, возвращаюсь назад. Откройте запись ещё раз кнопкой в списке.")
        return

    data = await state.get_data()
    history_id = data.get("edit_history_id")
    if not history_id:
        await message.answer("Ошибка: не найден ID записи")
        await state.clear()
        return

    # парсим два формата
    dt_obj = None
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt_obj = datetime.strptime(text, fmt)
            break
        except ValueError:
            pass

    if not dt_obj:
        await message.answer(
            "Не понял формат 😕\n"
            "Введите так: 31.01.2026 14:30 (или 2026-01-31 14:30)"
        )
        return

    new_date = dt_obj.date().isoformat()
    new_time = dt_obj.strftime("%H:%M")

    updated_record = update_lesson_history(history_id, lesson_date=new_date, lesson_time=new_time)
    if not updated_record:
        await message.answer("Ошибка при обновлении даты/времени")
        await state.clear()
        return

    await message.answer("✅ Дата и время обновлены.")

    # возвращаемся к меню полей (как у других правок)
    await state.set_state(EditHistoryStates.choosing_field)

    record = get_lesson_history_by_id(history_id)
    if not record:
        await message.answer("Запись не найдена")
        await state.clear()
        return

    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Статус", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}status")
    builder.button(text="💰 Оплата", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}paid")
    builder.button(text="📝 Комментарий", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}note")
    builder.button(text="📚 Тема", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}topic")
    builder.button(text="📅 Дата/время", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}datetime")
    builder.button(text="🗑️ Удалить запись", callback_data=f"{DELETE_HISTORY_PREFIX}{history_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_history_list")
    builder.adjust(2)

    status_text = "✅ состоялось" if record["status"] == "done" else "❌ отменено"
    paid_text = "💰 оплачено" if record["paid"] else "🆓 не оплачено"

    await message.answer(
        f"📋 <b>Запись #{history_id}</b>\n\n"
        f"👤 <b>Ученик:</b> {record['full_name'] or record['username']}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {record['time']}\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"💳 <b>Оплата:</b> {paid_text}\n"
        f"📝 <b>Комментарий:</b> {record['note'] or 'нет'}\n"
        f"📚 <b>Тема:</b> {record['topic'] or 'не указана'}\n\n"
        f"Что вы хотите отредактировать?",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@router.callback_query(EditHistoryStates.editing_status)
async def edit_history_set_status(callback_query: CallbackQuery, state: FSMContext):
    """Установка нового статуса"""
    status = callback_query.data.split("_")[2]  # done или cancelled

    data = await state.get_data()
    history_id = data.get("edit_history_id")

    if not history_id:
        await callback_query.answer("Ошибка: не найден ID записи")
        return

    # Обновляем статус
    updated_record = update_lesson_history(history_id, status=status)

    if not updated_record:
        await callback_query.answer("Ошибка при обновлении статуса")
        return

    await callback_query.answer(f"Статус изменен на {'состоялось' if status == 'done' else 'отменено'}")

    # Возвращаемся к выбору поля
    await state.set_state(EditHistoryStates.choosing_field)
    await edit_history_choose_record(callback_query, state)


@router.callback_query(EditHistoryStates.editing_paid)
async def edit_history_set_paid(callback_query: CallbackQuery, state: FSMContext):
    """Установка нового статуса оплаты"""
    paid = int(callback_query.data.split("_")[2])  # 1 или 0

    data = await state.get_data()
    history_id = data.get("edit_history_id")

    if not history_id:
        await callback_query.answer("Ошибка: не найден ID записи")
        return

    # Обновляем статус оплаты
    updated_record = update_lesson_history(history_id, paid=bool(paid))

    if not updated_record:
        await callback_query.answer("Ошибка при обновлении оплаты")
        return

    await callback_query.answer(f"Оплата изменена на {'оплачено' if paid else 'не оплачено'}")

    # Возвращаемся к выбору поля
    await state.set_state(EditHistoryStates.choosing_field)
    await edit_history_choose_record(callback_query, state)


@router.message(EditHistoryStates.editing_note)
async def edit_history_set_note(message: Message, state: FSMContext):
    """Установка нового комментария"""
    text = message.text.strip()

    data = await state.get_data()
    history_id = data.get("edit_history_id")

    if not history_id:
        await message.answer("Ошибка: не найден ID записи")
        await state.clear()
        return

    note = None if text == "-" else text

    # Обновляем комментарий
    updated_record = update_lesson_history(history_id, note=note)

    if not updated_record:
        await message.answer("Ошибка при обновлении комментария")
        await state.clear()
        return

    await message.answer(f"Комментарий {'удален' if note is None else 'обновлен'}")

    # Возвращаемся к выбору поля
    await state.set_state(EditHistoryStates.choosing_field)

    # Получаем обновленную запись
    record = get_lesson_history_by_id(history_id)
    if not record:
        await message.answer("Запись не найдена")
        await state.clear()
        return

    # Форматируем дату
    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    # Создаем клавиатуру с полями для редактирования
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Статус", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}status")
    builder.button(text="💰 Оплата", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}paid")
    builder.button(text="📝 Комментарий", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}note")
    builder.button(text="📚 Тема", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}topic")
    builder.button(text="🗑️ Удалить запись", callback_data=f"{DELETE_HISTORY_PREFIX}{history_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_history_list")
    builder.adjust(2)

    status_text = "✅ состоялось" if record["status"] == "done" else "❌ отменено"
    paid_text = "💰 оплачено" if record["paid"] else "🆓 не оплачено"

    await message.answer(
        f"📋 <b>Запись #{history_id}</b>\n\n"
        f"👤 <b>Ученик:</b> {record['full_name'] or record['username']}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {record['time']}\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"💳 <b>Оплата:</b> {paid_text}\n"
        f"📝 <b>Комментарий:</b> {record['note'] or 'нет'}\n"
        f"📚 <b>Тема:</b> {record['topic'] or 'не указана'}\n\n"
        f"Что вы хотите отредактировать?",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@router.message(EditHistoryStates.editing_topic)
async def edit_history_set_topic(message: Message, state: FSMContext):
    """Установка новой темы"""
    text = message.text.strip()

    data = await state.get_data()
    history_id = data.get("edit_history_id")

    if not history_id:
        await message.answer("Ошибка: не найден ID записи")
        await state.clear()
        return

    topic = None if text == "-" else text

    # Обновляем тему
    updated_record = update_lesson_history(history_id, topic=topic)

    if not updated_record:
        await message.answer("Ошибка при обновлении темы")
        await state.clear()
        return

    await message.answer(f"Тема {'удалена' if topic is None else 'обновлена'}")

    # Возвращаемся к выбору поля
    await state.set_state(EditHistoryStates.choosing_field)

    # Получаем обновленную запись
    record = get_lesson_history_by_id(history_id)
    if not record:
        await message.answer("Запись не найдена")
        await state.clear()
        return

    # Форматируем дату
    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    # Создаем клавиатуру с полями для редактирования
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Статус", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}status")
    builder.button(text="💰 Оплата", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}paid")
    builder.button(text="📝 Комментарий", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}note")
    builder.button(text="📚 Тема", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}topic")
    builder.button(text="🗑️ Удалить запись", callback_data=f"{DELETE_HISTORY_PREFIX}{history_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_history_list")
    builder.button(text="📅 Дата/время", callback_data=f"{EDIT_HISTORY_FIELD_PREFIX}datetime")
    builder.adjust(2)

    status_text = "✅ состоялось" if record["status"] == "done" else "❌ отменено"
    paid_text = "💰 оплачено" if record["paid"] else "🆓 не оплачено"

    await message.answer(
        f"📋 <b>Запись #{history_id}</b>\n\n"
        f"👤 <b>Ученик:</b> {record['full_name'] or record['username']}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {record['time']}\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"💳 <b>Оплата:</b> {paid_text}\n"
        f"📝 <b>Комментарий:</b> {record['note'] or 'нет'}\n"
        f"📚 <b>Тема:</b> {record['topic'] or 'не указана'}\n\n"
        f"Что вы хотите отредактировать?",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------- УДАЛЕНИЕ УЧЕНИКА ----------


@router.message(Command("delete_student"))
async def cmd_delete_student(message: Message, state: FSMContext):
    """Удаление ученика из системы"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика.")
        return

    ids = []
    lines = ["Какого ученика удаляем? Выбери номер:"]

    for i, s in enumerate(students, start=1):
        ids.append(s["id"])
        name = format_student_title(s["full_name"], s["username"], s["telegram_id"])

        lines.append(f"{i}) {name} (ID={s['telegram_id']})")

    await state.update_data(delete_student_ids=ids)
    await state.set_state(DeleteStudentStates.choosing_student)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(DeleteStudentStates.choosing_student)
async def delete_student_choose(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю удаление ученика. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    data = await state.get_data()
    ids = data.get("delete_student_ids", [])

    student = None
    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(
        delete_student_id=student["id"],
        delete_student_name=student["full_name"] or student["username"] or str(student["telegram_id"]),
        delete_student_telegram_id=student["telegram_id"]
    )
    await state.set_state(DeleteStudentStates.confirming)

    # Получаем количество связанных записей
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as count FROM weekly_lessons WHERE student_id = ?", (student["id"],))
    weekly_count = cur.fetchone()["count"]

    cur.execute("SELECT COUNT(*) as count FROM homeworks WHERE student_id = ?", (student["id"],))
    hw_count = cur.fetchone()["count"]

    cur.execute("SELECT COUNT(*) as count FROM lesson_history WHERE student_id = ?", (student["id"],))
    history_count = cur.fetchone()["count"]

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, удалить")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        f"⚠️ <b>ВНИМАНИЕ!</b> Вы действительно хотите удалить ученика?\n\n"
        f"Ученик: {student['full_name'] or student['username']}\n"
        f"Telegram ID: {student['telegram_id']}\n\n"
        f"Будут удалены все связанные данные:\n"
        f"• Регулярные занятия: {weekly_count}\n"
        f"• Домашние задания: {hw_count}\n"
        f"• Записи в истории: {history_count}\n\n"
        f"<b>Это действие нельзя отменить!</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.message(DeleteStudentStates.confirming)
async def delete_student_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю удаление ученика. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    if text != "✅ Да, удалить":
        await message.answer(
            "Пожалуйста, выберите один из варианты: «✅ Да, удалить» или «❌ Нет, отменить»."
        )
        return

    data = await state.get_data()
    student_id = data.get("delete_student_id")
    student_name = data.get("delete_student_name")
    telegram_id = data.get("delete_student_telegram_id")

    if not student_id:
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(True),
        )
        return

    # Удаляем ученика и все связанные данные
    deleted_telegram_id = delete_student_by_id(student_id)

    if deleted_telegram_id:
        # Отправляем уведомление ученику
        await notify_student_deleted(telegram_id)

        await message.answer(
            f"✅ Ученик {student_name} и все связанные данные успешно удалены.\n"
            f"Ученик получил уведомление об удалении.",
            reply_markup=main_menu_keyboard(True),
        )
    else:
        await message.answer(
            f"❌ Не удалось удалить ученика {student_name}. Попробуйте снова.",
            reply_markup=main_menu_keyboard(True),
        )

    await state.clear()

@router.message(lambda m: (m.text or "").strip() == "🗑️ Удалить слот")
async def handle_delete_slot_button(message: Message, state: FSMContext):
    await state.clear()
    await cmd_delete_slot(message, state)  # переиспользуем /delete_slot


# ---------- ГОРЯЧИЕ КНОПКИ НИЖНЕГО МЕНЮ ----------

@router.message(lambda m: (m.text or "").strip() == FEEDBACK_TEXT)
async def start_feedback(message: Message, state: FSMContext):
    # Только для учеников/родителей (если хочешь — можно разрешить и админу тоже)
    if is_teacher(message):
        await message.answer("Эта кнопка для учеников и родителей 🙂", reply_markup=main_menu_keyboard(True))
        return

    await state.clear()
    await state.set_state(FeedbackStates.waiting_text)
    await message.answer(
        "💡 <b>Предложения и исправления</b>\n\n"
        "Напишите одним сообщением, что можно улучшить/исправить.\n"
        "Я передам это администраторам.\n\n"
        f"Чтобы отменить — нажмите «{BACK_TEXT}».",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@router.message(FeedbackStates.waiting_text)
async def feedback_wait_text(message: Message, state: FSMContext):
    text_ = (message.text or "").strip()

    if text_ == BACK_TEXT:
        await state.clear()
        await message.answer("Окей, отменил. Возвращаю в меню.", reply_markup=get_main_menu(message))
        return

    if not text_:
        await message.answer("Похоже, сообщение пустое. Напишите текст предложения 🙂", reply_markup=back_keyboard())
        return

    role = "parent" if is_parent(message) else "student"
    tg = message.from_user
    full_name = ((tg.first_name or "") + (" " + (tg.last_name or ""))).strip() or None
    username = tg.username

    feedback_id = add_feedback(
        telegram_id=tg.id,
        role=role,
        username=username,
        full_name=full_name,
        text_=text_,
    )

    # Уведомляем админов
    notify_text = (
        "💬 <b>Новое предложение / исправление</b>\n"
        f"ID: <b>#{feedback_id}</b>\n"
        f"Роль: <b>{'Родитель' if role == 'parent' else 'Ученик'}</b>\n"
        f"От: <b>{full_name or (('@' + username) if username else str(tg.id))}</b>\n"
        f"Telegram ID: <code>{tg.id}</code>\n\n"
        f"Текст:\n{text_}"
    )

    for admin_id in TEACHER_IDS:
        try:
            await bot.send_message(admin_id, notify_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить feedback админу {admin_id}: {e}")

    await message.answer(
        "✅ Спасибо! Сообщение отправлено администраторам.",
        reply_markup=get_main_menu(message),
    )
    await state.clear()


@router.message(StateFilter(None))
async def handle_main_menu_buttons(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    # Кнопки для всех учеников
    if text == "📅 Моё расписание":
        await cmd_myschedule(message)
        return

    if text == "📚 Моя домашка":
        await cmd_myhw(message)
        return

    if text == "🔁 Перенести/отменить занятие":
        await cmd_move(message, state)
        return

    if text == "🧾 История занятий":
        await cmd_myhistory(message)
        return

    if text == "⏰ Напоминания":
        await cmd_set_remind(message, state)
        return

    if text == "🔗 Полезные ссылки":
        await cmd_my_links(message)
        return

    # Ниже — только для преподавателей
    if not is_teacher(message):
        await message.answer(
            "Я тебя не очень понял.\nВот что я могу сделать:",
            reply_markup=main_menu_keyboard(False),
        )
        return

    if text == "👥 Расписание":
        await show_global_schedule(message)
        return

    if text == "➕ Слот":
        await start_set_slot_wizard(message, state)
        return

    if text == "✨ Доп. занятие":
        await cmd_add_extra(message, state)
        return

    if text == "✏️ Задать домашку":
        await handle_set_homework_button(message, state)
        return

    if text == "❌ Отменить занятие":
        await handle_cancel_lesson_button(message, state)
        return

    if text == "💰 Отметить оплату":
        await handle_mark_payment_button(message, state)
        return

    if text == "📅 Массовая отмена":
        await cmd_mass_cancel(message, state)
        return

    if text == "🔄 Перенести занятие":
        await cmd_reschedule(message, state)
        return

    if text == "🗑️ Удалить слот":
        await cmd_delete_slot(message, state)
        return

    if text == "🧾 История ученика":
        await handle_student_history_button(message, state)
        return

    if text == "📝 Добавить занятие в историю":
        await cmd_add_history(message, state)
        return

    if text == "📜 Запросы":
        await cmd_list_requests(message)
        return

    if text == "📌 Переносы/отмены":
        await cmd_list_overrides(message)
        return

    if text == "🔗 Ссылки ученика":
        await start_edit_links_wizard(message, state)
        return

    if text == "📢 Объявление":
        await start_broadcast_wizard(message, state)
        return

    if text == "📚 Указать темы":
        await cmd_set_topics(message)
        return

    if text == "🗑️ Удалить ученика":
        await cmd_delete_student(message, state)
        return

    if text == "✏️ Редактировать историю":
        await cmd_edit_history(message, state)
        return

    # Любой странный текст от преподавателя
    await message.answer(
        "Я тебя не очень понял.\nВот что я могу сделать:",
        reply_markup=main_menu_keyboard(True),
    )


# ---------- НАПОМИНАНИЯ И ВЕЧЕРНЕЕ ПОДТВЕРЖДЕНИЕ ----------

already_notified = set()  # (telegram_id, date_iso, key)
last_logged_date: date | None = None


async def auto_summary_today_lessons(today: date):
    """
    В 23:00:
    - НЕ создаём новые записи в истории (они должны были появиться по ходу дня),
    - но на всякий случай добавляем пропущенные,
    - отправляем админу сводку по всем занятиям за день и запрашиваем темы.
    """
    lessons = get_lessons_for_date(today)
    if not lessons:
        return

    # На всякий случай создаём записи для тех занятий, что вдруг ещё не в истории
    created_entries = []
    for l in lessons:
        time_str = l["time"]
        try:
            hh, mm = map(int, time_str.split(":"))
            lesson_time = dtime(hh, mm)
        except Exception:
            continue

        if history_entry_exists(
                l["student_id"], l["weekly_lesson_id"], today, lesson_time
        ):
            continue

        status = "cancelled" if l["change_kind"] == "cancel" else "done"
        hist_id = add_lesson_history(
            student_id=l["student_id"],
            weekly_lesson_id=l["weekly_lesson_id"],
            lesson_date=today,
            lesson_time=lesson_time,
            status=status,
            paid=False,
            note="Авто-добавлено при вечернем подтверждении",
            topic=None,
        )
        created_entries.append(hist_id)

    rows = get_lesson_history_for_date(today)
    if not rows:
        return

    # Группируем занятия по ученику для удобства
    lessons_by_student = {}
    for r in rows:
        student_id = r["student_id"]
        if student_id not in lessons_by_student:
            cur = conn.cursor()
            cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
            student = cur.fetchone()
            lessons_by_student[student_id] = {
                "student_name": student['full_name'] or student['username'] or str(student['telegram_id']),
                "lessons": []
            }
        lessons_by_student[student_id]["lessons"].append(r)

    # Отправляем сводку и запрашиваем темы для каждого администратора
    for admin_id in TEACHER_IDS:
        try:
            # Отправляем сводку
            summary_lines = [f"📊 <b>Итоги занятий за {today.strftime('%d.%m.%Y')}:</b>"]

            for student_id, data in lessons_by_student.items():
                summary_lines.append(f"\n👤 <b>{data['student_name']}:</b>")
                for r in data["lessons"]:
                    t = r["time"]
                    status = r["status"]
                    paid = bool(r["paid"])
                    topic = r["topic"] or "тема не указана"
                    status_text = "✅ состоялось" if status == "done" else "❌ отменено"
                    paid_text = "💰 оплачено" if paid else "🆓 не оплачено"
                    summary_lines.append(f"   #{r['id']} — {t} — {status_text}, {paid_text}, тема: {topic}")

            if created_entries:
                summary_lines.append("\n📝 <i>Автоматически добавлены в историю занятия, которых там не было.</i>")

            await bot.send_message(admin_id, "\n".join(summary_lines), parse_mode="HTML")

            # Теперь отправляем запрос на указание тем ТОЛЬКО для состоявшихся занятий без тем
            lessons_without_topic = []
            for student_id, data in lessons_by_student.items():
                for r in data["lessons"]:
                    # ИЗМЕНЕНИЕ: проверяем статус "done" и отсутствие темы
                    if r["status"] == "done" and (not r["topic"] or r["topic"].lower() == "тема не указана"):
                        lessons_without_topic.append(r)

            if lessons_without_topic:
                # Создаем инлайн-клавиатуру с кнопками для каждого занятия без темы
                builder = InlineKeyboardBuilder()
                for r in lessons_without_topic:
                    student_info = lessons_by_student[r["student_id"]]
                    button_text = f"#{r['id']} {r['time']} - {student_info['student_name']}"
                    builder.button(text=button_text, callback_data=f"set_topic_{r['id']}")

                builder.button(text="✅ Все темы указаны", callback_data="topics_done")
                builder.adjust(1)

                await bot.send_message(
                    admin_id,
                    "📚 <b>Укажите темы для состоявшихся занятий без тем:</b>\n\n"
                    "Нажмите на занятие, чтобы добавить тему:",
                    parse_mode="HTML",
                    reply_markup=builder.as_markup()
                )
            else:
                await bot.send_message(admin_id, "🎉 Все состоявшиеся занятия уже имеют указанные темы!")

        except Exception as e:
            logging.error(f"Не удалось отправить сводку администратору {admin_id}: {e}")

async def show_global_schedule(message: Message):
    if not is_teacher(message):
        return

    lessons = get_all_weekly_lessons(active_only=True)

    if not lessons:
        await message.answer("Пока не задано ни одного занятия.")
        return

    schedule_by_day = {i: [] for i in range(7)}

    # for lesson in lessons:
    #     weekday = lesson["weekday"]
    #     name = lesson["full_name"] or lesson["username"] or lesson["telegram_id"]
    #     time = lesson["time"]
    #     schedule_by_day[weekday].append((name, time))
    for lesson in lessons:
        weekday = lesson["weekday"]
        full_name = lesson["full_name"]
        username = lesson["username"]
        tg_id = lesson["telegram_id"]

        # name — как раньше, но telegram_id приводим к строке
        name = full_name or (f"@{username}" if username else str(tg_id))
        time = lesson["time"]

        # ВАЖНО: передаём username третьим элементом — тогда _fmt_name() добавит (@username) к ФИО
        schedule_by_day[weekday].append((name, time, username))


    lines = []

    def _time_key(t: str):
        try:
            h, m = t.split(":")
            return int(h), int(m)
        except Exception:
            return (99, 99)  # если вдруг мусор — уедет вниз

    def _fmt_name(item):
        # item может быть:
        # 1) (name, time)
        # 2) (name, time, username)
        # 3) {"name":..., "time":..., "username":...}  (если где-то так формируешь)
        if isinstance(item, dict):
            name = item.get("name") or ""
            username = item.get("username")
            if username:
                username = username if username.startswith("@") else f"@{username}"
                if username not in name:
                    name = f"{name} ({username})" if name else username
            return name

        # tuple/list
        name = item[0]
        username = item[2] if len(item) >= 3 else None
        if username:
            username = username if username.startswith("@") else f"@{username}"
            # не дублируем, если уже вписан
            if username not in name:
                name = f"{name} ({username})"
        return name

    def _get_time(item):
        return item.get("time") if isinstance(item, dict) else item[1]

    for weekday in range(7):
        day_lessons = schedule_by_day[weekday]
        if not day_lessons:
            continue

        lines.append(f"<b>{DAY_NAMES[weekday]}</b>")

        for item in sorted(day_lessons, key=lambda x: _time_key(_get_time(x))):
            name = _fmt_name(item)
            time = _get_time(item)
            lines.append(f"{name} — {time}")

        lines.append("")

    await message.answer("\n".join(lines), parse_mode="HTML")

from datetime import datetime, timedelta, date, time as dtime

def ensure_history_for_past_lessons(
    lookback_days: int = 14,
    min_after_start_minutes: int = 30,
):
    """
    Автоматически создаёт записи в lesson_history для занятий, которые уже
    начались минимум min_after_start_minutes назад (по расписанию).
    """
    now = datetime.now()
    start_day = now.date() - timedelta(days=lookback_days - 1)

    for i in range(lookback_days):
        day = start_day + timedelta(days=i)

        # Берём занятия по расписанию на этот день (у тебя такая функция уже есть)
        lessons = get_lessons_for_date_with_extras(day)

        for lesson in lessons:
            # пропускаем отмены (если у тебя так отмечается)
            if lesson.get("change_kind") == "cancel":
                continue

            time_str = (lesson.get("time") or "").strip()
            if not time_str:
                continue

            try:
                hh, mm = map(int, time_str.split(":"))
                lesson_t = dtime(hh, mm)
            except Exception:
                continue

            lesson_dt = datetime.combine(day, lesson_t)

            # Ждём 30 минут после начала
            if now < lesson_dt + timedelta(minutes=min_after_start_minutes):
                continue

            student_id = lesson.get("student_id")
            if not student_id:
                continue

            # Не создаём дубль
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM lesson_history WHERE student_id=? AND date=? AND time=? LIMIT 1",
                (student_id, day.isoformat(), lesson_t.strftime("%H:%M")),
            )
            if cur.fetchone():
                continue

            # Создаём запись: занятие состоялось, тема пока пустая
            add_lesson_history(
                student_id=student_id,
                lesson_date=day,
                lesson_time=lesson_t,
                status="done",
                paid=False,
                note=None,
                topic=None,
                weekly_lesson_id=lesson.get("weekly_lesson_id"),
            )



@router.message(SetTopicStates.waiting_topic)
async def set_topic_enter(message: Message, state: FSMContext):
    """Обработка ввода темы занятия"""
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяем ввод темы.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if not text:
        await message.answer(
            "Пожалуйста, введите тему занятия.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    history_id = data.get("set_topic_history_id")

    if not history_id:
        await state.clear()
        await message.answer(
            "Ошибка: не найден ID занятия.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Обновляем тему в истории
    updated_record = update_lesson_history(history_id, topic=text)

    if not updated_record:
        await message.answer(
            "Ошибка при обновлении темы.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    # Получаем обновленные данные для подтверждения
    record = get_lesson_history_by_id(history_id)
    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    await message.answer(
        f"✅ <b>Тема успешно добавлена!</b>\n\n"
        f"Ученик: {record['full_name'] or record['username']}\n"
        f"Дата: {date_str}\n"
        f"Время: {record['time']}\n"
        f"Тема: {text}",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    # Проверяем, есть ли еще занятия без тем (по всей истории)
    lessons_without_topic = get_done_lessons_without_topic(min_after_start_minutes=30)

    if lessons_without_topic:
        builder = InlineKeyboardBuilder()
        for r in lessons_without_topic:
            d = date.fromisoformat(r["date"])
            date_str = d.strftime("%d.%m.%Y")
            student = r["full_name"] or r["username"] or str(r["telegram_id"] or "")
            time_ = r["time"] or ""
            button_text = f"#{r['id']} {date_str} {time_} - {student}"
            builder.button(text=button_text, callback_data=f"set_topic_{r['id']}")

        builder.button(text="✅ Все темы указаны", callback_data="topics_done")
        builder.adjust(1)

        await message.answer(
            "📚 <b>Остались занятия без тем:</b>\n\n"
            "Нажмите на занятие, чтобы добавить тему:",
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
    else:
        await message.answer(
            "🎉 <b>Все темы указаны!</b>\nСпасибо за работу!",
            parse_mode="HTML",
        )

    await state.clear()


@router.callback_query(lambda c: c.data == "topics_done")
async def topics_done_callback(callback_query: CallbackQuery):
    """Кнопка '✅ Все темы указаны' — проверяем ВСЮ историю и уведомляем админов"""

    lessons_without_topic = get_done_lessons_without_topic()
    if lessons_without_topic:
        await callback_query.answer(
            f"Ещё остались занятия без темы: {len(lessons_without_topic)}",
            show_alert=True
        )
        return

    author_name = callback_query.from_user.full_name
    author_uname = f"@{callback_query.from_user.username}" if callback_query.from_user.username else ""
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    notify_text = "\n".join([
        "✅ <b>Темы занятий отмечены</b>",
        f"🕒 {now_str}",
        f"👤 Отметил(а): {author_name} {author_uname}".strip(),
        "",
        "В истории занятий не осталось занятий без темы."
    ])

    for admin_id in TEACHER_IDS:
        if admin_id == callback_query.from_user.id:
            continue
        try:
            await bot.send_message(admin_id, notify_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление о темах админу {admin_id}: {e}")

    await callback_query.message.edit_text(
        "✅ <b>Спасибо! Все темы указаны.</b>",
        parse_mode="HTML",
        reply_markup=None
    )
    await callback_query.answer()




@router.message(Command("set_topics"))
async def cmd_set_topics(message: Message):
    """Ручной запуск процесса указания тем (по всей истории)"""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    ensure_history_for_past_lessons(lookback_days=14, min_after_start_minutes=30)
    lessons_without_topic = get_done_lessons_without_topic(min_after_start_minutes=30)
    if not lessons_without_topic:
        await message.answer("🎉 Все темы уже указаны — занятий без темы нет.")
        return

    builder = InlineKeyboardBuilder()
    for r in lessons_without_topic:
        d = date.fromisoformat(r["date"])
        date_str = d.strftime("%d.%m.%Y")
        student = r["full_name"] or r["username"] or str(r["telegram_id"] or "")
        time_ = r["time"] or ""
        button_text = f"#{r['id']} {date_str} {time_} - {student}"
        builder.button(text=button_text, callback_data=f"set_topic_{r['id']}")

    builder.button(text="✅ Все темы указаны", callback_data="topics_done")
    builder.adjust(1)

    await message.answer(
        "📚 <b>Укажите темы для занятий без темы (вся история):</b>\n\n"
        "Нажмите на занятие, чтобы добавить тему:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )



async def reminder_loop():
    """
    - Рассылает напоминания перед занятием (за 60 минут по умолчанию).
    - По факту наступления времени занятия автоматически создаёт записи в истории.
    - В 23:00 присылает админам сводку за день.
    """
    global last_logged_date
    while True:
        try:
            now = datetime.now()
            today = now.date()
            weekday_now = now.weekday()

            # Оверрайды на сегодня
            overrides_today = get_overrides_for_date(today)
            overridden_weekly_ids = {o["weekly_lesson_id"] for o in overrides_today}

            # Регулярные занятия (без оверрайдов)
            lessons = get_all_weekly_lessons()
            for wl in lessons:
                if wl["weekday"] != weekday_now:
                    continue
                if wl["id"] in overridden_weekly_ids:
                    continue

                time_str = wl["time"]
                try:
                    hh, mm = map(int, time_str.split(":"))
                    lesson_time = dtime(hh, mm)
                except Exception:
                    continue

                remind_before = wl["remind_before_minutes"]
                lesson_dt = now.replace(
                    hour=hh, minute=mm, second=0, microsecond=0
                )
                remind_dt = lesson_dt - timedelta(minutes=remind_before)

                # Напоминание
                diff_remind = (now - remind_dt).total_seconds()
                if 0 <= diff_remind < 60:
                    key = (
                        wl["telegram_id"],
                        lesson_dt.date().isoformat(),
                        f"weekly:{time_str}",
                    )
                    if key not in already_notified:
                        student_name = (
                                wl["full_name"]
                                or wl["username"]
                                or str(wl["telegram_id"])
                        )
                        text = (
                            f"Привет, {student_name}!\n"
                            f"Напоминание: у тебя занятие сегодня в {time_str}."
                        )
                        try:
                            await bot.send_message(wl["telegram_id"], text)
                            already_notified.add(key)
                            logging.info(
                                f"Напоминание отправлено {wl['telegram_id']} на {time_str}"
                            )
                        except Exception as e:
                            logging.error(f"Ошибка отправки напоминания: {e}")

                # Авто-добавление в историю (по умолчанию считаем, что занятие состоялось)
                if now >= lesson_dt:
                    if not history_entry_exists(
                            wl["student_id"], wl["id"], today, lesson_time
                    ):
                        add_lesson_history(
                            student_id=wl["student_id"],
                            weekly_lesson_id=wl["id"],
                            lesson_date=today,
                            lesson_time=lesson_time,
                            status="done",
                            paid=False,
                            note=None,
                            topic=None,
                        )

            # Оверрайды
            for o in overrides_today:
                # Время занятия (для отмены берём обычное время)
                if o["change_kind"] == "cancel":
                    time_str = o["weekly_time"]
                    remind_before = o["weekly_remind_before"]
                else:
                    time_str = o["new_time"]
                    remind_before = o["remind_before_minutes"]

                try:
                    hh, mm = map(int, time_str.split(":"))
                    lesson_time = dtime(hh, mm)
                except Exception:
                    continue

                lesson_dt = now.replace(
                    hour=hh, minute=mm, second=0, microsecond=0
                )
                remind_dt = lesson_dt - timedelta(minutes=remind_before)

                # Напоминание только для перенесённых (не отменённых)
                diff_remind = (now - remind_dt).total_seconds()
                if o["change_kind"] != "cancel" and 0 <= diff_remind < 60:
                    key = (
                        o["telegram_id"],
                        lesson_dt.date().isoformat(),
                        f"override:{time_str}",
                    )
                    if key not in already_notified:
                        student_name = (
                                o["full_name"]
                                or o["username"]
                                or str(o["telegram_id"])
                        )
                        text = (
                            f"Привет, {student_name}!\n"
                            f"Напоминание: занятие перенесено на сегодня в {time_str}."
                        )
                        try:
                            await bot.send_message(o["telegram_id"], text)
                            already_notified.add(key)
                            logging.info(
                                f"Напоминание (override) отправлено {o['telegram_id']} на {time_str}"
                            )
                        except Exception as e:
                            logging.error(
                                f"Ошибка отправки override-напоминания: {e}"
                            )

                # Авто-добавление в историю по оверрайду
                if now >= lesson_dt:
                    status = "cancelled" if o["change_kind"] == "cancel" else "done"
                    if not history_entry_exists(
                            o["student_id"], o["weekly_lesson_id"], today, lesson_time
                    ):
                        add_lesson_history(
                            student_id=o["student_id"],
                            weekly_lesson_id=o["weekly_lesson_id"],
                            lesson_date=today,
                            lesson_time=lesson_time,
                            status=status,
                            paid=False,
                            note=None,
                            topic=None,
                        )

            # Чистка уже уведомлённых
            if len(already_notified) > 1000:
                today_iso = today.isoformat()
                kept = {k for k in already_notified if k[1] >= today_iso}
                already_notified.clear()
                already_notified.update(kept)

            # Вечерний итог в 23:00 (один раз в день)
            if now.hour == 23 and (last_logged_date != today):
                await auto_summary_today_lessons(today)
                last_logged_date = today

        except Exception as e:
            logging.error(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(60)


def get_students_without_homework():
    """Получает учеников без невыполненных домашних заданий"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.* FROM students s
        WHERE NOT EXISTS (
            SELECT 1 FROM homeworks h 
            WHERE h.student_id = s.id AND h.is_done = 0
        )
        AND EXISTS (
            SELECT 1 FROM weekly_lessons w 
            WHERE w.student_id = s.id AND w.is_active = 1
        )
        ORDER BY s.full_name
        """
    )
    return cur.fetchall()


def get_students_with_lessons_today():
    """Получает учеников с занятиями сегодня"""
    today = date.today()
    weekday = today.weekday()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT s.* 
        FROM students s
        JOIN weekly_lessons w ON w.student_id = s.id AND w.is_active = 1
        WHERE w.weekday = ?
        AND NOT EXISTS (
            SELECT 1 FROM lesson_overrides o 
            WHERE o.weekly_lesson_id = w.id 
            AND o.date = ? 
            AND o.change_kind = 'cancel'
        )
        ORDER BY w.time, s.full_name
        """,
        (weekday, today.isoformat())
    )
    return cur.fetchall()


def get_students_with_unpaid_lessons():
    """Получает учеников с неоплаченными занятиями"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT s.* 
        FROM students s
        JOIN lesson_history lh ON lh.student_id = s.id
        WHERE lh.paid = 0 
        AND lh.status = 'done'
        AND lh.date >= date('now', '-30 days')
        ORDER BY lh.date DESC, s.full_name
        """
    )
    return cur.fetchall()


def get_students_without_topic_for_today():
    """Получает учеников, у которых сегодня занятия без указанной темы"""
    today = date.today()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT s.* 
        FROM students s
        JOIN weekly_lessons w ON w.student_id = s.id AND w.is_active = 1
        LEFT JOIN lesson_history lh ON lh.student_id = s.id 
            AND lh.date = ? 
            AND lh.status = 'done'
        WHERE w.weekday = ?
        AND (lh.topic IS NULL OR lh.topic = '')
        AND NOT EXISTS (
            SELECT 1 FROM lesson_overrides o 
            WHERE o.weekly_lesson_id = w.id 
            AND o.date = ? 
            AND o.change_kind = 'cancel'
        )
        ORDER BY w.time, s.full_name
        """,
        (today.isoformat(), today.weekday(), today.isoformat())
    )
    return cur.fetchall()





@router.callback_query(lambda c: c.data.startswith("show_all_students_"))
async def show_all_students_callback(callback_query: CallbackQuery, state: FSMContext):
    """Показать всех учеников"""
    action_type = callback_query.data.split("_")[3]

    # Создаем клавиатуру со всеми учениками
    builder = InlineKeyboardBuilder()
    students = get_all_students()

    for student in students:
        student_id = student["id"]
        name = student["full_name"] or student["username"] or str(student["telegram_id"])

        if len(name) > 20:
            name = name[:17] + "..."

        builder.button(
            text=f"👤 {name}",
            callback_data=f"select_student_{action_type}_{student_id}"
        )

    builder.adjust(1)

    await callback_query.message.edit_text(
        "👤 <b>Все ученики:</b>\n\n"
        "Выберите ученика:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.message(lambda message: message.text == "❌ Отменить занятие")
async def handle_cancel_lesson_smart(message: Message, state: FSMContext):
    """Умный выбор ученика для отмены занятия"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    # Создаем умную клавиатуру
    keyboard, title, total_pages = create_smart_student_keyboard('cancel')

    if keyboard is None:
        await message.answer("Нет учеников с занятиями сегодня.")
        return

    # Сохраняем тип действия в состоянии
    await state.update_data(action_type='cancel')
    await state.set_state(CancelStates.choosing_student_smart)

    await message.answer(
        f"{title}\n\n"
        "Выберите ученика, у которого хотите отменить занятие:",
        reply_markup=keyboard
    )


@router.message(lambda message: message.text == "💰 Отметить оплату")
async def handle_mark_payment_smart(message: Message, state: FSMContext):
    """Умный выбор ученика для отметки оплаты"""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    # Создаем умную клавиатуру
    keyboard, title, total_pages = create_smart_student_keyboard('payment')

    if keyboard is None:
        await message.answer("Нет учеников с неоплаченными занятиями.")
        return

    # Сохраняем тип действия в состоянии
    await state.update_data(action_type='payment')
    await state.set_state(PaymentStates.choosing_student_smart)

    await message.answer(
        f"{title}\n\n"
        "Выберите ученика для отметки оплаты:",
        reply_markup=keyboard
    )


@router.message(Command("attention"))
async def cmd_attention(message: Message):
    """Показать учеников, требующих внимания"""
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    attention_students = get_students_needing_attention()

    lines = ["👁️ <b>Ученики, требующие внимания:</b>\n"]

    for category, students in attention_students.items():
        if students:
            lines.append(f"\n<b>{category} ({len(students)}):</b>")
            for student in students[:5]:  # Показываем только первых 5
                name = student["full_name"] or student["username"] or str(student["telegram_id"])
                lines.append(f"• {name}")

            if len(students) > 5:
                lines.append(f"  ... и еще {len(students) - 5}")

    # Проверяем, есть ли хоть кто-то
    if all(not students for students in attention_students.values()):
        lines.append("\n🎉 Все в порядке! Нет учеников, требующих срочного внимания.")

    await message.answer("\n".join(lines), parse_mode="HTML")

def get_students_needing_attention():
    """Получает учеников, требующих внимания (разные категории)"""
    attention_students = {
        "Без домашнего задания": get_students_without_homework(),
        "Занятия сегодня": get_students_with_lessons_today(),
        "Неоплаченные занятия": get_students_with_unpaid_lessons(),
        "Без темы на сегодня": get_students_without_topic_for_today()
    }
    return attention_students

@router.callback_query(lambda c: c.data.startswith("set_topic_") and c.data.count("_") == 2)
async def set_topic_callback(callback_query: CallbackQuery, state: FSMContext):
    try:
        history_id = int(callback_query.data[len("set_topic_"):])
    except Exception:
        await callback_query.answer("Некорректные данные кнопки.", show_alert=True)
        return


    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    record = get_lesson_history_by_id(history_id)
    if not record:
        await callback_query.answer("Запись не найдена")
        return

    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Указать тему", callback_data=f"{SET_TOPIC_WRITE_PREFIX}{history_id}")
    builder.button(text="🗑️ Занятие не состоялось (удалить)", callback_data=f"{SET_TOPIC_DEL_PREFIX}{history_id}")
    builder.button(text="⬅️ Назад к списку", callback_data=SET_TOPICS_BACK)
    builder.adjust(1)

    await callback_query.message.answer(
        f"📚 <b>Выберите действие для занятия:</b>\n\n"
        f"Ученик: {record['full_name'] or record['username']}\n"
        f"Дата: {date_str}\n"
        f"Время: {record['time']}",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith(SET_TOPIC_WRITE_PREFIX))
async def set_topic_write_callback(callback_query: CallbackQuery, state: FSMContext):
    try:
        history_id = int(callback_query.data[len(SET_TOPIC_WRITE_PREFIX):])
    except Exception:
        await callback_query.answer("Некорректные данные кнопки.", show_alert=True)
        return


    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Сохраняем ID записи в состоянии
    await state.update_data(set_topic_history_id=history_id)
    await state.set_state(SetTopicStates.waiting_topic)

    record = get_lesson_history_by_id(history_id)
    if not record:
        await callback_query.answer("Запись не найдена")
        return

    d = date.fromisoformat(record["date"])
    date_str = d.strftime("%d.%m.%Y")

    await callback_query.message.answer(
        f"📚 <b>Укажите тему занятия:</b>\n\n"
        f"Ученик: {record['full_name'] or record['username']}\n"
        f"Дата: {date_str}\n"
        f"Время: {record['time']}\n\n"
        f"Введите тему занятия:",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()

@router.callback_query(lambda c: c.data.startswith(SET_TOPIC_DEL_NO_PREFIX))
async def set_topic_delete_cancel(callback_query: CallbackQuery):
    # history_id можно не парсить вообще, но если хочешь — можно так же как в confirm
    await callback_query.message.answer("Ок, не удаляю.")
    await callback_query.answer()


@router.callback_query(
    lambda c: c.data.startswith(SET_TOPIC_DEL_PREFIX)
    and not c.data.startswith(SET_TOPIC_DEL_OK_PREFIX)
    and not c.data.startswith(SET_TOPIC_DEL_NO_PREFIX)
)
async def set_topic_delete_ask(callback_query: CallbackQuery):

    try:
        history_id = int(callback_query.data[len(SET_TOPIC_DEL_PREFIX):])
    except Exception:
        await callback_query.answer("Некорректные данные кнопки.", show_alert=True)
        return


    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"{SET_TOPIC_DEL_OK_PREFIX}{history_id}")
    builder.button(text="❌ Нет", callback_data=f"{SET_TOPIC_DEL_NO_PREFIX}{history_id}")
    builder.adjust(1)

    await callback_query.message.answer(
        "🗑️ <b>Пометить занятие как не состоявшееся?</b>\n"
        "Оно исчезнет из «Указать темы» и не будет создаваться снова.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith(SET_TOPIC_DEL_OK_PREFIX))
async def set_topic_delete_confirm(callback_query: CallbackQuery):
    try:
        history_id = int(callback_query.data[len(SET_TOPIC_DEL_OK_PREFIX):])
    except Exception:
        await callback_query.answer("Некорректные данные кнопки.", show_alert=True)
        return


    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # НЕ удаляем запись физически — иначе она будет снова создана автогенерацией истории.
    # Вместо этого помечаем занятие как отменённое.
    updated = update_lesson_history(history_id, status="cancelled", topic="отменено")
    if not updated:
        await callback_query.answer("Запись не найдена", show_alert=True)
        return

    await callback_query.message.answer(
        "✅ Занятие помечено как не состоявшееся (скрыто из списка «Указать темы»)."
    )
    await callback_query.answer()


    # Показать обновлённый список занятий без темы
    lessons_without_topic = get_done_lessons_without_topic(min_after_start_minutes=30)
    if not lessons_without_topic:
        await callback_query.message.answer("🎉 Все темы уже указаны — занятий без темы нет.")
        return

    builder = InlineKeyboardBuilder()
    for r in lessons_without_topic:
        d = date.fromisoformat(r["date"])
        date_str = d.strftime("%d.%m.%Y")
        student = r["full_name"] or r["username"] or str(r["telegram_id"] or "")
        time_ = r["time"] or ""
        button_text = f"#{r['id']} {date_str} {time_} - {student}"
        builder.button(text=button_text, callback_data=f"set_topic_{r['id']}")

    builder.button(text="✅ Все темы указаны", callback_data="topics_done")
    builder.adjust(1)

    await callback_query.message.answer(
        "📚 <b>Остались занятия без тем:</b>\n\n"
        "Нажмите на занятие, чтобы добавить тему или удалить (если не состоялось):",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )

@router.callback_query(lambda c: c.data.startswith(SET_TOPIC_DEL_NO_PREFIX))
async def set_topic_delete_cancel(callback_query: CallbackQuery):
    await callback_query.answer("Ок, не удаляем.")



# ---------- ДОПОЛНИТЕЛЬНЫЕ ЗАНЯТИЯ (БЕЗ ПЕРЕНОСОВ) ----------

def create_extra_lessons_table():
    """Создает таблицу для дополнительных занятий"""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS extra_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            date TEXT,
            time TEXT,
            remind_before_minutes INTEGER DEFAULT 60,
            topic TEXT,
            status TEXT DEFAULT 'scheduled',
            created_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students (id)
        )
        """
    )
    conn.commit()



# Вызываем создание таблицы при инициализации
create_extra_lessons_table()


class AddExtraLessonStates(StatesGroup):
    waiting_student = State()
    waiting_date = State()
    waiting_time = State()
    waiting_topic = State()
    waiting_reminder = State()
    confirming = State()



def addextra_dates_kb(days_back: int = 14) -> InlineKeyboardMarkup:
    """
    Даты за последние N дней, начиная с сегодня.
    callback: addextra_date_YYYY-MM-DD
    """
    today = dt_date.today()
    buttons = []
    for i in range(days_back):
        d = today - timedelta(days=i)
        buttons.append(
            InlineKeyboardButton(
                text=d.strftime("%d.%m"),
                callback_data=f"addextra_date_{d.isoformat()}",
            )
        )

    # 2 колонки (можешь поменять)
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="addextra_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def addextra_times_kb(start_h: int = 17, end_h: int = 23) -> InlineKeyboardMarkup:
    """
    Время кнопками 17:00 ... 23:00
    callback: addextra_time_HH:MM
    """
    buttons = []
    for h in range(start_h, end_h + 1):
        t = f"{h:02d}:00"
        buttons.append(
            InlineKeyboardButton(text=t, callback_data=f"addextra_time_{t}")
        )

    # 4 колонки (можешь поменять)
    rows = [buttons[i:i+4] for i in range(0, len(buttons), 4)]
    rows.append([
        InlineKeyboardButton(text="⌨️ Ввести другое время", callback_data="addextra_time_other"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="addextra_back_to_dates"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_extra_lesson(
        student_id: int,
        lesson_date: date,
        lesson_time: dtime,
        topic: str = None,
        remind_before_minutes: int = 60
) -> int:
    """Добавляет дополнительное занятие (без связи с регулярным)"""
    cur = conn.cursor()

    # Проверяем, нет ли уже занятия на эту дату и время у ученика
    cur.execute(
        """
        SELECT id FROM extra_lessons 
        WHERE student_id = ? AND date = ? AND time = ? AND status = 'scheduled'
        """,
        (student_id, lesson_date.isoformat(), lesson_time.strftime("%H:%M"))
    )
    existing = cur.fetchone()

    if existing:
        return None  # Занятие уже существует

    cur.execute(
        """
        INSERT INTO extra_lessons (student_id, date, time, remind_before_minutes, topic, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'scheduled', ?)
        """,
        (
            student_id,
            lesson_date.isoformat(),
            lesson_time.strftime("%H:%M"),
            remind_before_minutes,
            topic,
            datetime.now().isoformat(timespec="seconds")
        )
    )
    conn.commit()
    return cur.lastrowid


def get_extra_lesson_by_id(extra_lesson_id: int):
    """Получает дополнительное занятие по ID"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, s.telegram_id, s.username, s.full_name
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.id = ?
        """,
        (extra_lesson_id,)
    )
    return cur.fetchone()


def get_extra_lessons_for_date(target_date: date):
    """Получает дополнительные занятия на дату"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, s.telegram_id, s.username, s.full_name
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.date = ? AND e.status = 'scheduled'
        ORDER BY e.time
        """,
        (target_date.isoformat(),)
    )
    return cur.fetchall()


def get_future_extra_lessons_for_student(student_id: int, days_ahead: int = 30):
    """Получает будущие дополнительные занятия для ученика"""
    today = date.today()
    end = today + timedelta(days=days_ahead)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*
        FROM extra_lessons e
        WHERE e.student_id = ? 
          AND e.status = 'scheduled'
          AND e.date >= ?
          AND e.date <= ?
        ORDER BY e.date, e.time
        """,
        (student_id, today.isoformat(), end.isoformat())
    )
    return cur.fetchall()


def delete_extra_lesson(extra_lesson_id: int):
    """Удаляет дополнительное занятие"""
    cur = conn.cursor()

    # Получаем данные перед удалением
    lesson_data = get_extra_lesson_by_id(extra_lesson_id)

    cur.execute(
        "DELETE FROM extra_lessons WHERE id = ?",
        (extra_lesson_id,)
    )
    conn.commit()

    return lesson_data


def mark_extra_lesson_as_done(extra_lesson_id: int):
    """Помечает дополнительное занятие как выполненное и добавляет в историю"""
    cur = conn.cursor()

    # Получаем данные занятия
    extra_lesson = get_extra_lesson_by_id(extra_lesson_id)
    if not extra_lesson:
        return None

    # Добавляем в историю
    lesson_date = date.fromisoformat(extra_lesson["date"])
    hh, mm = map(int, extra_lesson["time"].split(":"))
    lesson_time = dtime(hh, mm)

    history_id = add_lesson_history(
        student_id=extra_lesson["student_id"],
        lesson_date=lesson_date,
        lesson_time=lesson_time,
        status="done",
        paid=False,
        note="Дополнительное занятие",
        topic=extra_lesson["topic"],
        weekly_lesson_id=None
    )

    # Удаляем из дополнительных занятий
    cur.execute(
        "DELETE FROM extra_lessons WHERE id = ?",
        (extra_lesson_id,)
    )
    conn.commit()

    return history_id


async def notify_extra_lesson_added(student_telegram_id: int, lesson_date: date, lesson_time: str, topic: str = None):
    """Уведомление о добавлении дополнительного занятия"""
    date_str = lesson_date.strftime("%d.%m.%Y")

    message = (
        f"📅 <b>Добавлено дополнительное занятие!</b>\n\n"
        f"• Дата: <b>{date_str}</b>\n"
        f"• Время: <b>{lesson_time}</b>\n"
    )

    if topic:
        message += f"• Тема: <b>{topic}</b>\n"

    message += (
        f"• Напоминание: за <b>60</b> минут до начала\n\n"
        f"Это разовое занятие, не связанное с вашим регулярным расписанием."
    )

    await notify_student_about_schedule_change(student_telegram_id, message)


# ---------- КОМАНДА ДЛЯ ДОБАВЛЕНИЯ ДОПОЛНИТЕЛЬНОГО ЗАНЯТИЯ ----------

@router.message(Command("add_extra"))
async def cmd_add_extra(message: Message, state: FSMContext):
    if not is_teacher(message):
        await message.answer("Эта команда только для преподавателя.")
        return

    students = get_all_students()
    if not students:
        await message.answer("Пока нет ни одного ученика. Пусть они напишут боту /start.")
        return

    # ⬇️ ВАЖНО: сохраним список студентов в state (для пагинации/перерисовки)
    await state.update_data(addextra_students=students)

    await state.set_state(AddExtraLessonStates.waiting_student)

    keyboard, _ = create_action_keyboard(students, "addextra", page=0)

    await message.answer(
        "Кому назначаем доп. занятие? Выбери ученика:",
        reply_markup=keyboard
    )



@router.callback_query(lambda c: c.data.startswith("addextra_page_"))
async def addextra_page_cb(callback_query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    students = data.get("addextra_students", [])
    if not students:
        await callback_query.answer("Нет учеников")
        return

    prefix = "addextra_page_"
    page = int(callback_query.data[len(prefix):])

    keyboard, _ = create_action_keyboard(students, "addextra", page=page)
    await callback_query.message.edit_reply_markup(reply_markup=keyboard)
    await callback_query.answer()



@router.callback_query(lambda c: c.data.startswith("addextra_student_"))
async def addextra_student_cb(callback_query: CallbackQuery, state: FSMContext):
    prefix = "addextra_student_"
    rest = callback_query.data[len(prefix):]  # "{student_id}_{page}"
    last_us = rest.rfind("_")
    student_id = int(rest[:last_us])

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()
    if not student:
        await callback_query.answer("Ученик не найден", show_alert=True)
        return

    await state.update_data(
        add_extra_student_id=student["id"],
        add_extra_student_telegram_id=student["telegram_id"],
        add_extra_student_name=student["full_name"] or student["username"] or str(student["telegram_id"]),
    )
    await state.set_state(AddExtraLessonStates.waiting_date)

    await callback_query.message.answer(
        "Выбери дату доп. занятия (последние 14 дней):",
        reply_markup=addextra_dates_kb(days_back=14),
    )
    await callback_query.answer()




@router.callback_query(lambda c: c.data == "addextra_back_to_dates")
async def addextra_back_to_dates_cb(callback_query: CallbackQuery, state: FSMContext):
    await state.set_state(AddExtraLessonStates.waiting_date)
    await callback_query.message.answer(
        "Выбери дату доп. занятия:",
        reply_markup=addextra_dates_kb(days_back=14),
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("addextra_date_"))
async def addextra_date_cb(callback_query: CallbackQuery, state: FSMContext):
    prefix = "addextra_date_"  # YYYY-MM-DD дальше
    date_iso = callback_query.data[len(prefix):]

    try:
        lesson_date = dt_date.fromisoformat(date_iso)
    except Exception:
        await callback_query.answer("Некорректная дата", show_alert=True)
        return

    await state.update_data(add_extra_date=lesson_date)
    await state.set_state(AddExtraLessonStates.waiting_time)

    await callback_query.message.answer(
        "Теперь выбери время:",
        reply_markup=addextra_times_kb(17, 23),
    )
    await callback_query.answer()



@router.callback_query(lambda c: c.data == "addextra_time_other")
async def addextra_time_other_cb(callback_query: CallbackQuery, state: FSMContext):
    # остаёмся в waiting_time, но просим текстом
    await state.set_state(AddExtraLessonStates.waiting_time)
    await callback_query.message.answer("Ок, введи время в формате HH:MM (например 18:30):")
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("addextra_time_"))
async def addextra_time_cb(callback_query: CallbackQuery, state: FSMContext):
    prefix = "addextra_time_"
    t_str = callback_query.data[len(prefix):]  # HH:MM

    try:
        hh, mm = map(int, t_str.split(":"))
        lesson_time = dtime(hh, mm)
    except Exception:
        await callback_query.answer("Некорректное время", show_alert=True)
        return

    await state.update_data(add_extra_time=lesson_time)
    await state.set_state(AddExtraLessonStates.waiting_topic)

    await callback_query.message.answer(
        "Введите тему дополнительного занятия (или '-' чтобы пропустить):",
        reply_markup=back_keyboard(),
    )
    await callback_query.answer()



@router.callback_query(lambda c: c.data == "addextra_cancel")
async def addextra_cancel_cb(callback_query: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback_query.message.answer("Ок, отменил назначение доп. занятия.", reply_markup=main_menu_keyboard(True))
    await callback_query.answer()



@router.message(AddExtraLessonStates.waiting_student)
async def add_extra_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("add_extra_student_ids", [])

    student = None
    if ids:
        try:
            idx = int(text)
            if 1 <= idx <= len(ids):
                student_id = ids[idx - 1]
                cur = conn.cursor()
                cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
                student = cur.fetchone()
        except ValueError:
            pass

    if student is None:
        student = get_student_by_user_key(text)

    if not student:
        await message.answer(
            "Не нашёл такого ученика.\n"
            "Попробуй ещё раз: номер из списка, @username или telegram id.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(
        add_extra_student_id=student["id"],
        add_extra_student_telegram_id=student["telegram_id"],
        add_extra_student_name=student["full_name"] or student["username"] or str(student["telegram_id"])
    )
    await state.set_state(AddExtraLessonStates.waiting_date)

    await message.answer(
        "Введите дату дополнительного занятия (формат: ДД.ММ.ГГГГ или ДД.ММ):\n"
        "Пример: 15.12.2024 или 15.12",
        reply_markup=back_keyboard(),
    )


@router.message(AddExtraLessonStates.waiting_date)
async def add_extra_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    lesson_date = parse_date_str(text)
    if not lesson_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(add_extra_date=lesson_date)
    await state.set_state(AddExtraLessonStates.waiting_time)

    await message.answer(
        "Введите время дополнительного занятия (формат: ЧЧ:ММ):\n"
        "Пример: 18:30",
        reply_markup=back_keyboard(),
    )


@router.message(AddExtraLessonStates.waiting_time)
async def add_extra_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        lesson_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(add_extra_time=lesson_time)
    await state.set_state(AddExtraLessonStates.waiting_topic)

    await message.answer(
        "Введите тему дополнительного занятия (или '-' чтобы пропустить):",
        reply_markup=back_keyboard(),
    )


@router.message(AddExtraLessonStates.waiting_topic)
async def add_extra_enter_topic(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    topic = None if text == "-" else text
    await state.update_data(add_extra_topic=topic)
    await state.set_state(AddExtraLessonStates.waiting_reminder)

    await message.answer(
        "За сколько минут до начала присылать напоминание?\n"
        "Введите число минут (по умолчанию 60):",
        reply_markup=back_keyboard(),
    )


@router.message(AddExtraLessonStates.waiting_reminder)
async def add_extra_enter_reminder(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        remind_before = int(text) if text.isdigit() else 60
        if remind_before < 1:
            remind_before = 60
    except ValueError:
        remind_before = 60

    await state.update_data(add_extra_remind_before=remind_before)

    data = await state.get_data()
    lesson_date = data.get("add_extra_date")
    lesson_time = data.get("add_extra_time")
    topic = data.get("add_extra_topic")
    student_name = data.get("add_extra_student_name")

    date_str = lesson_date.strftime("%d.%m.%Y")
    time_str = lesson_time.strftime("%H:%M")

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, добавить")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
    )

    message_text = (
        f"Подтвердите добавление дополнительного занятия:\n\n"
        f"Ученик: {student_name}\n"
        f"Дата: {date_str}\n"
        f"Время: {time_str}\n"
        f"Напоминание за {remind_before} минут\n"
    )

    if topic:
        message_text += f"Тема: {topic}\n"

    # ... вы уже собрали message_text выше

    # Заканчиваем описание (если у вас строка была оборвана — сделайте её нормальной)
    message_text += "\nЭто разовое занятие, оно не повторяется по расписанию."

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, добавить")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await state.set_state(AddExtraLessonStates.confirming)
    await message.answer(message_text, parse_mode="HTML", reply_markup=kb)


@router.message(AddExtraLessonStates.confirming)
async def add_extra_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю добавление занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, добавить":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, добавить» или «❌ Нет, отменить»."
        )
        return

    data = await state.get_data()
    student_id = data.get("add_extra_student_id")
    lesson_date = data.get("add_extra_date")
    lesson_time = data.get("add_extra_time")
    topic = data.get("add_extra_topic")
    remind_before = data.get("add_extra_remind_before")
    telegram_id = data.get("add_extra_student_telegram_id")
    student_name = data.get("add_extra_student_name")

    # Добавляем дополнительное занятие
    extra_lesson_id = add_extra_lesson(
        student_id=student_id,
        lesson_date=lesson_date,
        lesson_time=lesson_time,
        topic=topic,
        remind_before_minutes=remind_before
    )

    if extra_lesson_id is None:
        await message.answer(
            f"У ученика {student_name} уже есть дополнительное занятие на {lesson_date.strftime('%d.%m.%Y')} в {lesson_time.strftime('%H:%M')}.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    # Отправляем уведомление ученику
    if telegram_id:
        await notify_extra_lesson_added(
            student_telegram_id=telegram_id,
            lesson_date=lesson_date,
            lesson_time=lesson_time.strftime("%H:%M"),
            topic=topic
        )

    date_str = lesson_date.strftime("%d.%m.%Y")
    time_str = lesson_time.strftime("%H:%M")

    message_text = (
        f"✅ <b>Дополнительное занятие добавлено!</b>\n\n"
        f"Ученик: {student_name}\n"
        f"Дата: {date_str}\n"
        f"Время: {time_str}\n"
        f"Напоминание за {remind_before} минут\n"
    )

    if topic:
        message_text += f"Тема: {topic}\n"

    message_text += f"\nID занятия: #{extra_lesson_id}"

    await message.answer(
        message_text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )
    await state.clear()


# ---------- ОБНОВЛЕНИЕ ФУНКЦИЙ ДЛЯ УЧЕТА ДОПОЛНИТЕЛЬНЫХ ЗАНЯТИЙ ----------

def get_lessons_for_date_with_extras(target_date: date):
    """
    Возвращает список всех занятий на дату (регулярные, оверрайды и дополнительные).
    """
    lessons_for_day = []
    weekday = target_date.weekday()

    # 1. Регулярные занятия без оверрайдов
    overrides = get_overrides_for_date(target_date)
    overridden_ids = {o["weekly_lesson_id"] for o in overrides}

    all_weekly = get_all_weekly_lessons()

    for wl in all_weekly:
        if wl["weekday"] != weekday:
            continue
        if wl["id"] in overridden_ids:
            continue

        lessons_for_day.append({
            "type": "regular",
            "weekly_lesson_id": wl["id"],
            "student_id": wl["student_id"],
            "telegram_id": wl["telegram_id"],
            "full_name": wl["full_name"],
            "username": wl["username"],
            "time": wl["time"],
            "change_kind": None,
        })

    # 2. Оверрайды
    for o in overrides:
        if o["change_kind"] == "cancel":
            time_to_use = o["weekly_time"]
        else:
            time_to_use = o["new_time"]

        lessons_for_day.append({
            "type": "override",
            "weekly_lesson_id": o["weekly_lesson_id"],
            "student_id": o["student_id"],
            "telegram_id": o["telegram_id"],
            "full_name": o["full_name"],
            "username": o["username"],
            "time": time_to_use,
            "change_kind": o["change_kind"],
        })

    # 3. Дополнительные занятия
    extra_lessons = get_extra_lessons_for_date(target_date)
    for e in extra_lessons:
        lessons_for_day.append({
            "type": "extra",
            "extra_lesson_id": e["id"],
            "student_id": e["student_id"],
            "telegram_id": e["telegram_id"],
            "full_name": e["full_name"],
            "username": e["username"],
            "time": e["time"],
            "topic": e["topic"],
        })

    # Сортируем по времени
    lessons_for_day.sort(key=lambda x: x["time"])
    return lessons_for_day


# Обновляем функцию get_lessons_for_date, чтобы использовать новую
def get_lessons_for_date(target_date: date):
    return get_lessons_for_date_with_extras(target_date)


# ---------- ОБНОВЛЕНИЕ REMINDER_LOOP ДЛЯ ДОПОЛНИТЕЛЬНЫХ ЗАНЯТИЙ ----------

async def reminder_loop_with_extras():
    """
    Обновленный цикл напоминаний с поддержкой:
    1. Напоминаний о занятиях за 60/35 минут
    2. Напоминаний о домашке за 2 часа до занятия
    3. Уведомлений о пропущенных напоминаниях через час после занятия
    """
    global last_logged_date
    while True:
        try:
            now = datetime.now()
            today = now.date()
            weekday_now = now.weekday()

            # 1. Обычные напоминания о занятиях (существующий код)
            # ... существующий код напоминаний о занятиях ...

            # 2. Напоминания о домашнем задании за 2 часа до занятия
            await send_homework_reminders()  # Используем исправленную функцию

            # 3. Уведомления о пропущенных напоминаниях через час после занятия
            await send_missed_homework_notifications()  # Используем исправленную функцию

            # Чистка уже уведомлённых
            if len(already_notified) > 1000:
                today_iso = today.isoformat()
                kept = {k for k in already_notified if k[1] >= today_iso}
                already_notified.clear()
                already_notified.update(kept)

            # Вечерний итог в 23:00 (один раз в день)
            # Отправляем "после 23:00", чтобы не промахнуться по минутам/перезапускам
            if (last_logged_date != today) and (now.hour > 23 or (now.hour == 23 and now.minute >= 0)):
                try:
                    await auto_summary_today_lessons(today)
                    last_logged_date = today
                except Exception as e:
                    logging.error(f"Ошибка при отправке вечернего итога: {e}")

            # Очистка старых системных флагов (старше 7 дней)
            week_ago = (now - timedelta(days=7)).isoformat()
            cur = conn.cursor()

            cur.execute(
                "DELETE FROM system_flags WHERE updated_at < ?",
                (week_ago,)
            )
            conn.commit()

        except Exception as e:
            logging.error(f"Ошибка в reminder_loop: {e}")

        await asyncio.sleep(60)  # Проверяем каждую минуту


# Обновляем запуск reminder_loop в main()
async def main():
    init_db()
    cleanup_old_requests()

    ensure_students_has_price()
    create_extra_lessons_table()  # Создаем таблицу если не существует
    asyncio.create_task(reminder_loop_with_extras())  # Используем обновленную версию
    await dp.start_polling(bot)

@router.callback_query(lambda c: c.data.startswith("student_"), EditHistoryStates.choosing_student)
async def edit_history_select_student_callback(callback_query: CallbackQuery, state: FSMContext):
    """Обработка выбора ученика через инлайн-кнопку"""
    try:
        student_id = int(callback_query.data.split("_")[1])
    except (IndexError, ValueError):
        await callback_query.answer("Ошибка выбора ученика")
        return

    # Получаем данные ученика
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    rows = get_lesson_history_for_student(student_id, limit=20)
    if not rows:
        await callback_query.message.edit_text("У этого ученика история занятий пока пустая.")
        await state.clear()
        return

    await state.update_data(edit_history_student_id=student_id)
    await state.update_data(edit_history_rows=rows)
    await state.set_state(EditHistoryStates.choosing_history)

    # Создаем клавиатуру с кнопками для выбора записи
    builder = InlineKeyboardBuilder()
    for row in rows:
        d = date.fromisoformat(row["date"])
        date_str = d.strftime("%d.%m.%Y")
        status_text = "✅" if row["status"] == "done" else "❌"
        paid_text = "💰" if row["paid"] else "🆓"
        topic = row["topic"] or "без темы"
        button_text = f"{status_text}{paid_text} {date_str} {row['time']} - {topic}"
        builder.button(text=button_text, callback_data=f"{EDIT_HISTORY_PREFIX}{row['id']}")

    builder.button(text="⬅️ Назад к выбору ученика", callback_data="back_to_student_select")
    builder.adjust(1)

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])
    await callback_query.message.edit_text(
        f"Выберите запись для редактирования (ученик {student_name}):",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()

# ---------- ОБНОВЛЕНИЕ КОМАНДЫ /myschedule ДЛЯ ОТОБРАЖЕНИЯ ДОПОЛНИТЕЛЬНЫХ ЗАНЯТИЙ ----------

@router.message(Command("myschedule"))
async def cmd_myschedule_updated(message: Message):
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    lessons = get_weekly_lessons_for_student(student["id"])
    overrides = get_future_overrides_for_student(student["id"], days_ahead=30)
    extra_lessons = get_future_extra_lessons_for_student(student["id"], days_ahead=30)

    if not lessons and not overrides and not extra_lessons:
        await message.answer(
            "Для тебя пока не задано ни одного занятия и нет переносов.\n"
            "Попроси преподавателя настроить расписание."
        )
        return

    lines = []

    if lessons:
        lines.append("📅 <b>Регулярные занятия (по неделям):</b>")
        for wl in lessons:
            weekday_name = weekday_to_name(wl["weekday"])
            lines.append(
                f"• <b>{weekday_name} в {wl['time']}</b> (напоминание за {wl['remind_before_minutes']} мин)"
            )

    if overrides:
        lines.append("\n🔄 <b>Ближайшие разовые изменения:</b>")
        for o in overrides:
            d = date.fromisoformat(o["date"])
            weekday_old = weekday_to_name(o["weekday"])
            if o["change_kind"] == "cancel":
                lines.append(
                    f"• <b>{d.strftime('%d.%m.%Y')}</b> — занятие <b>ОТМЕНЕНО</b> "
                    f"(обычно: {weekday_old} {o['weekly_time']})"
                )
            else:
                lines.append(
                    f"• <b>{d.strftime('%d.%m.%Y')} в {o['new_time']}</b> "
                    f"(обычно: {weekday_old} {o['weekly_time']})"
                )

    if extra_lessons:
        lines.append("\n✨ <b>Дополнительные занятия:</b>")
        for e in extra_lessons:
            d = date.fromisoformat(e["date"])
            lines.append(
                f"• <b>{d.strftime('%d.%m.%Y')} в {e['time']}</b>"
            )
            if e["topic"]:
                lines.append(f"  Тема: {e['topic']}")

    lines.append(
        "\nЕсли хочешь изменить время напоминания о занятиях — используй команду /set_remind."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- ДОБАВЛЯЕМ КНОПКУ В МЕНЮ ПРЕПОДАВАТЕЛЯ ----------

async def notify_admin_before_lesson(student_name: str, lesson_date: date, lesson_time: str, topic: str = None):
    """Отправляет напоминание админу за 35 минут до урока"""
    notification_text = (
        f"⏰ <b>Напоминание о занятии</b>\n\n"
        f"• Ученик: <b>{student_name}</b>\n"
        f"• Время: через <b>35 минут</b>\n"
        f"• Дата: {lesson_date.strftime('%d.%m.%Y')}\n"
        f"• Начало: {lesson_time}"
    )

    if topic:
        notification_text += f"\n• Тема: {topic}"

    for admin_id in TEACHER_IDS:
        try:
            await bot.send_message(
                admin_id,
                notification_text,
                parse_mode="HTML"
            )
            logging.info(f"Напоминание админу {admin_id} отправлено за 35 минут до урока для {student_name}")
        except Exception as e:
            logging.error(f"Не удалось отправить напоминание админу {admin_id}: {e}")


async def send_homework_reminders():
    """
    Отправляет напоминания о домашке за 2 часа до занятия, если она не сделана.
    Учитывает ограничения по времени: не раньше 8:00 и не позже 23:00.
    """
    now = datetime.now()
    today = now.date()

    # Проверяем, что текущее время в допустимом диапазоне (8:00 - 23:00)
    if now.hour < 8 or now.hour > 23:
        return

    # Получаем все занятия на сегодня
    lessons_today = get_lessons_for_date_with_extras(today)

    for lesson in lessons_today:
        # Пропускаем отмененные занятия
        if lesson.get('change_kind') == 'cancel':
            continue

        student_id = lesson['student_id']
        time_str = lesson['time']

        try:
            hh, mm = map(int, time_str.split(':'))
            lesson_time = dtime(hh, mm)
            lesson_dt = datetime.combine(today, lesson_time)
        except Exception:
            continue

        # Проверяем, что занятие еще не началось или прошло больше часа
        # (нельзя напоминать во время занятия и в течение часа после)
        time_diff = (lesson_dt - now).total_seconds()

        # Если занятие уже прошло и прошло больше часа - можно напоминать
        if time_diff < 0:
            # Занятие уже прошло, проверяем, прошел ли час
            hours_passed = abs(time_diff) / 3600
            if hours_passed < 1:
                # Еще не прошел час с начала занятия
                continue
        else:
            # Занятие еще не началось
            # Проверяем, что до занятия осталось примерно 2 часа (±5 минут)
            if not (7000 <= time_diff <= 7300):  # 1:57 - 2:02 часа
                continue

        # Получаем данные ученика
        cur = conn.cursor()
        cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
        student = cur.fetchone()

        if not student or not student["telegram_id"]:
            continue

        # Проверяем, есть ли невыполненные домашние задания
        hws = get_homeworks_for_student(student_id, only_open=True)

        if not hws:
            # Нет невыполненных домашних заданий
            continue

        # Проверяем, не отправляли ли уже напоминание сегодня для этого занятия
        reminder_key = f"hw_reminder_{student_id}_{today.isoformat()}_{time_str}"

        # Проверяем в базе данных, отправлялось ли уже напоминание
        cur.execute(
            """
            SELECT value FROM system_flags 
            WHERE key = ?
            """,
            (reminder_key,)
        )
        existing = cur.fetchone()

        if existing:
            continue  # Уже напоминали

        # Формируем сообщение
        hw_count = len(hws)
        if hw_count == 1:
            hw_text = f"1 задание: {hws[0]['text']}"
        else:
            hw_text = f"{hw_count} заданий"

        message = (
            f"📚 <b>Напоминание о домашнем задании!</b>\n\n"
            f"У вас занятие сегодня в <b>{time_str}</b>\n"
            f"Осталось невыполненных заданий: <b>{hw_text}</b>\n\n"
        )

        # Добавляем список заданий, если их немного
        if hw_count <= 3:
            for i, hw in enumerate(hws[:3], 1):
                message += f"{i}. {hw['text']}\n"

        message += (
            f"\nПожалуйста, выполните задания до начала занятия.\n"
            f"Когда закончите, используйте команду /done_hw"
        )

        try:
            # Отправляем сообщение ученику
            await bot.send_message(
                student["telegram_id"],
                message,
                parse_mode="HTML"
            )

            # Отмечаем в базе, что напоминание отправлено
            cur.execute(
                """
                INSERT OR REPLACE INTO system_flags (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (reminder_key, "sent", datetime.now().isoformat())
            )
            conn.commit()

            logging.info(f"Напоминание о домашке отправлено ученику {student['telegram_id']} на {time_str}")

        except Exception as e:
            logging.error(f"Ошибка отправки напоминания о домашке ученику {student['telegram_id']}: {e}")

@router.message(lambda message: message.text == "👁️ Внимание")
async def handle_attention_button(message: Message):
    """Обработка нажатия кнопки 'Внимание'"""
    await cmd_attention(message)

async def send_missed_homework_notifications():
    """
    Отправляет уведомления о пропущенных домашних заданиях через час после занятия
    (если не было сделано напоминание за 2 часа)
    """
    now = datetime.now()
    today = now.date()

    # Проверяем, что текущее время в допустимом диапазоне (8:00 - 23:00)
    if now.hour < 8 or now.hour >= 23:
        return

    # Получаем все занятия, которые были ровно час назад (±5 минут)
    target_time = now - timedelta(hours=1)
    hour_ago = target_time.time()

    # Ищем занятия, которые были в это время
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT 
            s.id as student_id,
            s.telegram_id,
            s.full_name,
            s.username,
            COALESCE(lo.new_time, wl.time, el.time) as lesson_time
        FROM students s
        LEFT JOIN weekly_lessons wl ON wl.student_id = s.id AND wl.is_active = 1
        LEFT JOIN lesson_overrides lo ON lo.weekly_lesson_id = wl.id 
            AND lo.date = ? 
            AND lo.change_kind != 'cancel'
        LEFT JOIN extra_lessons el ON el.student_id = s.id 
            AND el.date = ? 
            AND el.status = 'scheduled'
        WHERE (
            (wl.id IS NOT NULL AND strftime('%H:%M', ?) = wl.time AND wl.weekday = ? 
                AND NOT EXISTS (SELECT 1 FROM lesson_overrides lo2 
                                WHERE lo2.weekly_lesson_id = wl.id AND lo2.date = ?))
            OR (lo.id IS NOT NULL AND strftime('%H:%M', ?) = lo.new_time)
            OR (el.id IS NOT NULL AND strftime('%H:%M', ?) = el.time)
        )
        """,
        (
            today.isoformat(),
            today.isoformat(),
            hour_ago.strftime("%H:%M"),
            today.weekday(),
            today.isoformat(),  # для подзапроса
            hour_ago.strftime("%H:%M"),
            hour_ago.strftime("%H:%M")
        )
    )

    lessons = cur.fetchall()

    for lesson in lessons:
        student_id = lesson["student_id"]
        time_str = lesson["lesson_time"]  # Изменено с lesson["time"]

        # Проверяем, есть ли невыполненные домашние задания
        hws = get_homeworks_for_student(student_id, only_open=True)

        if not hws:
            continue

        # Проверяем, отправляли ли уже напоминание за 2 часа
        reminder_key = f"hw_reminder_{student_id}_{today.isoformat()}_{time_str}"
        cur.execute(
            """
            SELECT value FROM system_flags 
            WHERE key = ?
            """,
            (reminder_key,)
        )
        already_reminded = cur.fetchone()

        if already_reminded:
            continue  # Уже напоминали за 2 часа

        # Проверяем, не отправляли ли уже уведомление о пропуске
        missed_key = f"hw_missed_{student_id}_{today.isoformat()}_{time_str}"
        cur.execute(
            """
            SELECT value FROM system_flags 
            WHERE key = ?
            """,
            (missed_key,)
        )
        already_notified = cur.fetchone()

        if already_notified:
            continue

        # Формируем сообщение
        hw_count = len(hws)
        message = (
            f"⏰ <b>Вы пропустили напоминание о домашнем задании!</b>\n\n"
            f"Занятие было в <b>{time_str}</b>\n"
            f"Осталось невыполненных заданий: <b>{hw_count}</b>\n\n"
        )

        if hw_count <= 3:
            for i, hw in enumerate(hws[:3], 1):
                message += f"{i}. {hw['text']}\n"

        message += (
            f"\nПожалуйста, выполните задания как можно скорее.\n"
            f"Когда закончите, используйте команду /done_hw"
        )

        try:
            # Отправляем сообщение ученику
            await bot.send_message(
                lesson["telegram_id"],
                message,
                parse_mode="HTML"
            )

            # Отмечаем в базе, что уведомление отправлено
            cur.execute(
                """
                INSERT OR REPLACE INTO system_flags (key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (missed_key, "sent", datetime.now().isoformat())
            )
            conn.commit()

            logging.info(f"Уведомление о пропущенной домашке отправлено ученику {lesson['telegram_id']}")

        except Exception as e:
            logging.error(f"Ошибка отправки уведомления о пропущенной домашке ученику {lesson['telegram_id']}: {e}")

# ---------- ОБРАБОТКА КНОПКИ "✨ ДОП. ЗАНЯТИЕ" ----------

@router.message(lambda message: message.text == "✨ Доп. занятие")
async def handle_add_extra_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки "Доп. занятие" """
    await cmd_add_extra(message, state)

# ---------- ЗАПУСК ----------


async def main():
    init_db()
    create_extra_lessons_table()
    asyncio.create_task(reminder_loop_with_extras())  # Используем обновленную версию с напоминаниями админу
    await dp.start_polling(bot)







def create_mass_cancel_overrides(
        weekly_lesson_id: int,
        start_date: date,
        end_date: date,
        weekday: int,
        time_str: str
):
    """Создает оверрайды отмены для всех дней в диапазоне, соответствующих дню недели"""
    cur = conn.cursor()

    # Получаем данные слота
    cur.execute(
        """
        SELECT w.*, s.telegram_id, s.username, s.full_name
        FROM weekly_lessons w
        JOIN students s ON s.id = w.student_id
        WHERE w.id = ?
        """,
        (weekly_lesson_id,)
    )
    slot_data = cur.fetchone()

    if not slot_data:
        return None

    created_count = 0
    skipped_count = 0

    # Проходим по всем дням в диапазоне
    current_date = start_date
    delta = timedelta(days=1)

    while current_date <= end_date:
        # Проверяем, соответствует ли день недели
        if current_date.weekday() == weekday:
            # Проверяем, есть ли уже оверрайд на эту дату
            cur.execute(
                """
                SELECT id FROM lesson_overrides 
                WHERE weekly_lesson_id = ? AND date = ?
                """,
                (weekly_lesson_id, current_date.isoformat())
            )
            existing_override = cur.fetchone()

            # Если есть оверрайд, сохраняем оригинальные данные
            original_date = None
            original_time = None
            if existing_override:
                cur.execute(
                    """
                    SELECT date, new_time FROM lesson_overrides 
                    WHERE id = ?
                    """,
                    (existing_override["id"],)
                )
                old_override = cur.fetchone()
                if old_override:
                    original_date = date.fromisoformat(old_override["date"])
                    original_time = old_override["new_time"]

            # Создаем оверрайд отмены
            hh, mm = map(int, time_str.split(":"))
            lesson_time = dtime(hh, mm)

            create_lesson_override(
                weekly_lesson_id=weekly_lesson_id,
                override_date=current_date,
                new_time=lesson_time,
                change_kind="cancel",
                original_date=original_date,
                original_time=original_time
            )

            created_count += 1
        else:
            skipped_count += 1

        current_date += delta

    return {
        "slot_data": slot_data,
        "created_count": created_count,
        "skipped_count": skipped_count,
        "weekday": weekday,
        "time_str": time_str,
        "start_date": start_date,
        "end_date": end_date
    }


@router.message(MassCancelAllStates.choosing_student)
async def mass_cancel_choose_student(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    ids = data.get("mass_cancel_student_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер ученика в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(ids)):
        await message.answer(
            "Нет ученика с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    student_id = ids[idx - 1]
    lessons = get_weekly_lessons_for_student(student_id)
    if not lessons:
        await message.answer("У этого ученика нет слотов. Отменять нечего.")
        await state.clear()
        return

    lesson_ids = []
    lines = ["Какое регулярное занятие отменяем? Выбери номер:"]
    for i, wl in enumerate(lessons, start=1):
        lesson_ids.append(wl["id"])
        lines.append(f"{i}) {weekday_to_name(wl['weekday'])} {wl['time']}")

    await state.update_data(
        mass_cancel_student_id=student_id,
        mass_cancel_lesson_ids=lesson_ids
    )
    await state.set_state(MassCancelAllStates.choosing_lesson)
    await message.answer("\n".join(lines), reply_markup=back_keyboard())


@router.message(MassCancelAllStates.choosing_lesson)
async def mass_cancel_choose_lesson(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    data = await state.get_data()
    lesson_ids = data.get("mass_cancel_lesson_ids", [])

    try:
        idx = int(text)
    except ValueError:
        await message.answer(
            "Нужно число — номер занятия в списке.", reply_markup=back_keyboard()
        )
        return

    if not (1 <= idx <= len(lesson_ids)):
        await message.answer(
            "Нет занятия с таким номером. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    lesson_id = lesson_ids[idx - 1]
    wl = get_weekly_lesson_by_id(lesson_id)

    await state.update_data(
        mass_cancel_lesson_id=lesson_id,
        mass_cancel_weekday=wl["weekday"],
        mass_cancel_time=wl["time"]
    )
    await state.set_state(MassCancelAllStates.entering_start_date)

    await message.answer(
        "📅 <b>Начало периода отмены</b>\n\n"
        "Введите дату, с которой начинаем отменять занятия:\n"
        "Формат: ДД.ММ.ГГГГ или ДД.ММ\n"
        "Пример: 15.12.2024 или 15.12",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@router.message(MassCancelAllStates.entering_start_date)
async def mass_cancel_enter_start_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    start_date = parse_date_str(text)
    if not start_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(mass_cancel_start_date=start_date)
    await state.set_state(MassCancelAllStates.entering_end_date)

    await message.answer(
        "📅 <b>Конец периода отмены</b>\n\n"
        "Введите дату, до которой отменяем занятия (включительно):\n"
        "Формат: ДД.ММ.ГГГГ или ДД.ММ\n"
        "Пример: 31.12.2024 или 31.12",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@router.message(MassCancelAllStates.entering_end_date)
async def mass_cancel_enter_end_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    end_date = parse_date_str(text)
    if not end_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    start_date = data.get("mass_cancel_start_date")

    if end_date < start_date:
        await message.answer(
            "Конечная дата не может быть раньше начальной. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(mass_cancel_end_date=end_date)

    # Рассчитываем количество занятий для отмены
    weekday = data.get("mass_cancel_weekday")
    time_str = data.get("mass_cancel_time")

    # Подсчитываем, сколько раз выпадает нужный день недели в диапазоне
    current_date = start_date
    delta = timedelta(days=1)
    matching_days = 0
    dates_list = []

    while current_date <= end_date:
        if current_date.weekday() == weekday:
            matching_days += 1
            dates_list.append(current_date)
        current_date += delta

    if matching_days == 0:
        await message.answer(
            f"❌ В указанном диапазоне нет занятий по {weekday_to_name(weekday)}.\n"
            f"Начало: {start_date.strftime('%d.%m.%Y')}\n"
            f"Конец: {end_date.strftime('%d.%m.%Y')}\n\n"
            f"Попробуйте другой диапазон.",
            reply_markup=back_keyboard(),
        )
        await state.clear()
        return

    # Проверяем, есть ли уже отмены в этом диапазоне
    cur = conn.cursor()
    existing_cancels = 0
    for d in dates_list:
        cur.execute(
            """
            SELECT id FROM lesson_overrides 
            WHERE weekly_lesson_id = ? AND date = ? AND change_kind = 'cancel'
            """,
            (data.get("mass_cancel_lesson_id"), d.isoformat())
        )
        if cur.fetchone():
            existing_cancels += 1

    await state.set_state(MassCancelAllStates.confirming)

    # Создаем список дат для отображения
    date_strings = []
    for d in dates_list[:5]:  # Показываем только первые 5 дат
        date_strings.append(d.strftime("%d.%m.%Y"))

    if len(dates_list) > 5:
        date_strings.append(f"... и еще {len(dates_list) - 5} дней")

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, отменить все")],
            [KeyboardButton(text="❌ Нет, отменить операцию")],
        ],
        resize_keyboard=True,
    )

    message_text = (
        f"⚠️ <b>Подтверждение массовой отмены</b>\n\n"
        f"Ученик: {get_weekly_lesson_by_id(data.get('mass_cancel_lesson_id'))['full_name']}\n"
        f"Занятие: {weekday_to_name(weekday)} {time_str}\n"
        f"Период: {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        f"📅 <b>Будут отменены занятия:</b>\n"
        f"• Всего занятий в периоде: {matching_days}\n"
    )

    if existing_cancels > 0:
        message_text += f"• Уже отменено: {existing_cancels}\n"
        message_text += f"• Новых отмен: {matching_days - existing_cancels}\n"

    message_text += f"\nДаты отмены:\n"
    for ds in date_strings:
        message_text += f"• {ds}\n"

    message_text += (
        f"\n<b>Внимание!</b> Это действие нельзя отменить. "
        f"Ученик получит уведомление об отменах."
    )

    await message.answer(message_text, parse_mode="HTML", reply_markup=kb)


@router.message(MassCancelAllStates.confirming)

async def mass_cancel_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить операцию", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену занятий. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, отменить все":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, отменить все» или «❌ Нет, отменить операцию»."
        )
        return

    data = await state.get_data()
    lesson_id = data.get("mass_cancel_lesson_id")
    start_date = data.get("mass_cancel_start_date")
    end_date = data.get("mass_cancel_end_date")
    weekday = data.get("mass_cancel_weekday")
    time_str = data.get("mass_cancel_time")

    if not all([lesson_id, start_date, end_date, weekday is not None, time_str]):
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Выполняем массовую отмену
    result = create_mass_cancel_overrides(
        weekly_lesson_id=lesson_id,
        start_date=start_date,
        end_date=end_date,
        weekday=weekday,
        time_str=time_str
    )

    if not result:
        await message.answer(
            "❌ Ошибка при выполнении массовой отмены.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        await state.clear()
        return

    # Отправляем уведомление ученику
    slot_data = result["slot_data"]
    if slot_data and slot_data["telegram_id"]:
        # Создаем список дат отмены для уведомления
        current_date = start_date
        delta = timedelta(days=1)
        canceled_dates = []

        while current_date <= end_date:
            if current_date.weekday() == weekday:
                canceled_dates.append(current_date.strftime("%d.%m.%Y"))
            current_date += delta

        # Отправляем уведомление
        notification_text = (
            f"❌ <b>Массовая отмена занятий!</b>\n\n"
            f"Отменены занятия с {start_date.strftime('%d.%m.%Y')} "
            f"по {end_date.strftime('%d.%m.%Y')}:\n\n"
        )

        # Показываем первые 5 дат, если их много
        if len(canceled_dates) <= 5:
            for d in canceled_dates:
                notification_text += f"• {d}\n"
        else:
            for d in canceled_dates[:5]:
                notification_text += f"• {d}\n"
            notification_text += f"• ... и еще {len(canceled_dates) - 5} дней\n"

        notification_text += (
            f"\nВсего отменено: {len(canceled_dates)} занятий\n"
            f"Регулярное занятие остаётся без изменений."
        )

        try:
            await bot.send_message(
                slot_data["telegram_id"],
                notification_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление ученику: {e}")

    student_name = slot_data["full_name"] or slot_data["username"] or str(slot_data["telegram_id"])

    # Формируем отчет для преподавателя
    report_text = (
        f"✅ <b>Массовая отмена выполнена успешно!</b>\n\n"
        f"Ученик: {student_name}\n"
        f"Занятие: {weekday_to_name(weekday)} {time_str}\n"
        f"Период: {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        f"📊 <b>Результат:</b>\n"
        f"• Создано отмен: {result['created_count']}\n"
        f"• Пропущено дней (не тот день недели): {result['skipped_count']}\n\n"
        f"Ученик получил уведомление об отменах."
    )

    await message.answer(
        report_text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


class MassCancelAllStates(StatesGroup):
    entering_start_date = State()
    entering_end_date = State()
    confirming = State()


@router.message(Command("mass_cancel"))
async def cmd_mass_cancel(message: Message, state: FSMContext):
    """Массовая отмена всех занятий в диапазоне дат"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    await state.set_state(MassCancelAllStates.entering_start_date)
    await message.answer(
        "📅 <b>Начало периода массовой отмены</b>\n\n"
        "Введите дату, с которой начинаем массовую отмену:\n"
        "Формат: ДД.ММ.ГГГГ или ДД.ММ\n"
        "Пример: 15.12.2024 или 15.12",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@router.message(MassCancelAllStates.entering_start_date)
async def mass_cancel_enter_start_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    start_date = parse_date_str(text)
    if not start_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(mass_cancel_start_date=start_date)
    await state.set_state(MassCancelAllStates.entering_end_date)

    await message.answer(
        "📅 <b>Конец периода массовой отмены</b>\n\n"
        "Введите дату, до которой отменяем занятия (включительно):\n"
        "Формат: ДД.ММ.ГГГГ или ДД.ММ\n"
        "Пример: 31.12.2024 или 31.12",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


@router.message(MassCancelAllStates.entering_end_date)
async def mass_cancel_enter_end_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    end_date = parse_date_str(text)
    if not end_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ.ГГГГ или ДД.ММ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    data = await state.get_data()
    start_date = data.get("mass_cancel_start_date")

    if end_date < start_date:
        await message.answer(
            "Конечная дата не может быть раньше начальной. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(mass_cancel_end_date=end_date)

    # Рассчитываем количество занятий для отмены
    # 1. Регулярные занятия для всех учеников
    all_weekly_lessons = get_all_weekly_lessons()

    # Подсчитываем, сколько раз каждый день недели встречается в диапазоне
    weekday_counts = {i: 0 for i in range(7)}
    current_date = start_date
    delta = timedelta(days=1)

    while current_date <= end_date:
        weekday_counts[current_date.weekday()] += 1
        current_date += delta

    # Считаем общее количество регулярных занятий для отмены
    regular_cancel_count = 0
    regular_lessons_by_student = {}

    for wl in all_weekly_lessons:
        weekday = wl["weekday"]
        student_id = wl["student_id"]

        if student_id not in regular_lessons_by_student:
            regular_lessons_by_student[student_id] = {
                "count": 0,
                "lessons": []
            }

        count_for_weekday = weekday_counts.get(weekday, 0)
        if count_for_weekday > 0:
            regular_cancel_count += count_for_weekday
            regular_lessons_by_student[student_id]["count"] += count_for_weekday
            regular_lessons_by_student[student_id]["lessons"].append({
                "weekday": weekday,
                "time": wl["time"],
                "count": count_for_weekday
            })

    # 2. Разовые занятия (оверрайды) в диапазоне
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) as count FROM lesson_overrides 
        WHERE date >= ? AND date <= ?
        """,
        (start_date.isoformat(), end_date.isoformat())
    )
    override_count_result = cur.fetchone()
    override_count = override_count_result["count"] if override_count_result else 0

    # 3. Дополнительные занятия в диапазоне
    cur.execute(
        """
        SELECT COUNT(*) as count FROM extra_lessons 
        WHERE date >= ? AND date <= ? AND status = 'scheduled'
        """,
        (start_date.isoformat(), end_date.isoformat())
    )
    extra_count_result = cur.fetchone()
    extra_count = extra_count_result["count"] if extra_count_result else 0

    total_count = regular_cancel_count + override_count + extra_count

    await state.set_state(MassCancelAllStates.confirming)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, отменить всё")],
            [KeyboardButton(text="❌ Нет, отменить операцию")],
        ],
        resize_keyboard=True,
    )

    message_text = (
        f"⚠️ <b>ПОДТВЕРЖДЕНИЕ МАССОВОЙ ОТМЕНЫ</b>\n\n"
        f"📅 <b>Период:</b> {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        f"📊 <b>Будут отменены:</b>\n"
        f"• Регулярные занятия: {regular_cancel_count}\n"
        f"• Разовые занятия (переносы/отмены): {override_count}\n"
        f"• Дополнительные занятия: {extra_count}\n"
        f"• <b>ВСЕГО: {total_count} занятий</b>\n\n"
        f"👥 <b>Затронуто учеников:</b> {len(regular_lessons_by_student)}\n\n"
        f"<b>Внимание!</b> Это действие нельзя отменить. "
        f"Все ученики получат уведомления об отменах."
    )

    await message.answer(message_text, parse_mode="HTML", reply_markup=kb)


def perform_mass_cancel_for_all(start_date: date, end_date: date):
    """Выполняет массовую отмену всех занятий в диапазоне для всех учеников"""
    results = {
        "regular_cancelled": 0,
        "overrides_removed": 0,
        "extras_cancelled": 0,
        "notified_students": set(),
        "student_details": {}
    }

    cur = conn.cursor()

    # 1. ОТМЕНА РЕГУЛЯРНЫХ ЗАНЯТИЙ
    all_weekly_lessons = get_all_weekly_lessons()

    # Создаем список всех дат в диапазоне
    dates_in_range = []
    current_date = start_date
    delta = timedelta(days=1)

    while current_date <= end_date:
        dates_in_range.append(current_date)
        current_date += delta

    # Для каждого регулярного занятия создаем оверрайды отмены на каждую дату в диапазоне, соответствующую дню недели
    for wl in all_weekly_lessons:
        student_id = wl["student_id"]

        if student_id not in results["student_details"]:
            cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
            student = cur.fetchone()
            results["student_details"][student_id] = {
                "telegram_id": student["telegram_id"],
                "name": student["full_name"] or student["username"] or str(student["telegram_id"]),
                "regular_cancels": [],
                "extra_cancels": [],
                "override_cancels": []
            }

        for d in dates_in_range:
            if d.weekday() == wl["weekday"]:
                # Проверяем, есть ли уже оверрайд на эту дату
                cur.execute(
                    """
                    SELECT id, change_kind FROM lesson_overrides 
                    WHERE weekly_lesson_id = ? AND date = ?
                    """,
                    (wl["id"], d.isoformat())
                )
                existing_override = cur.fetchone()

                if existing_override:
                    # Если уже есть оверрайд, обновляем его на отмену
                    if existing_override["change_kind"] != "cancel":
                        cur.execute(
                            """
                            UPDATE lesson_overrides 
                            SET change_kind = 'cancel', new_time = ?
                            WHERE id = ?
                            """,
                            (wl["time"], existing_override["id"])
                        )
                        results["regular_cancelled"] += 1
                        results["student_details"][student_id]["override_cancels"].append({
                            "date": d,
                            "time": wl["time"],
                            "type": "updated_override"
                        })
                else:
                    # Создаем новый оверрайд отмены
                    hh, mm = map(int, wl["time"].split(":"))
                    lesson_time = dtime(hh, mm)

                    create_lesson_override(
                        weekly_lesson_id=wl["id"],
                        override_date=d,
                        new_time=lesson_time,
                        change_kind="cancel"
                    )
                    results["regular_cancelled"] += 1
                    results["student_details"][student_id]["regular_cancels"].append({
                        "date": d,
                        "time": wl["time"]
                    })

    # 2. УДАЛЕНИЕ РАЗОВЫХ ЗАНЯТИЙ (ОВЕРРАЙДОВ)
    cur.execute(
        """
        SELECT o.*, w.student_id, s.telegram_id, s.full_name, s.username
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        JOIN students s ON s.id = w.student_id
        WHERE o.date >= ? AND o.date <= ?
        """,
        (start_date.isoformat(), end_date.isoformat())
    )
    overrides = cur.fetchall()

    for ov in overrides:
        student_id = ov["student_id"]

        if student_id not in results["student_details"]:
            results["student_details"][student_id] = {
                "telegram_id": ov["telegram_id"],
                "name": ov["full_name"] or ov["username"] or str(ov["telegram_id"]),
                "regular_cancels": [],
                "extra_cancels": [],
                "override_cancels": []
            }

        # Удаляем оверрайд
        cur.execute(
            "DELETE FROM lesson_overrides WHERE id = ?",
            (ov["id"],)
        )
        results["overrides_removed"] += 1

        # Запоминаем для уведомления
        ov_date = date.fromisoformat(ov["date"])
        results["student_details"][student_id]["override_cancels"].append({
            "date": ov_date,
            "time": ov["new_time"] if ov["change_kind"] != "cancel" else ov["weekly_time"],
            "type": "removed_override"
        })

    # 3. ОТМЕНА ДОПОЛНИТЕЛЬНЫХ ЗАНЯТИЙ
    cur.execute(
        """
        SELECT e.*, s.telegram_id, s.full_name, s.username
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.date >= ? AND e.date <= ? AND e.status = 'scheduled'
        """,
        (start_date.isoformat(), end_date.isoformat())
    )
    extras = cur.fetchall()

    for extra in extras:
        student_id = extra["student_id"]

        if student_id not in results["student_details"]:
            results["student_details"][student_id] = {
                "telegram_id": extra["telegram_id"],
                "name": extra["full_name"] or extra["username"] or str(extra["telegram_id"]),
                "regular_cancels": [],
                "extra_cancels": [],
                "override_cancels": []
            }

        # Удаляем дополнительное занятие
        cur.execute(
            "DELETE FROM extra_lessons WHERE id = ?",
            (extra["id"],)
        )
        results["extras_cancelled"] += 1

        # Добавляем в историю как отмененное
        extra_date = date.fromisoformat(extra["date"])
        hh, mm = map(int, extra["time"].split(":"))
        lesson_time = dtime(hh, mm)

        add_lesson_history(
            student_id=student_id,
            lesson_date=extra_date,
            lesson_time=lesson_time,
            status="cancelled",
            paid=False,
            note="Массовая отмена",
            topic=extra["topic"],
            weekly_lesson_id=None
        )

        results["student_details"][student_id]["extra_cancels"].append({
            "date": extra_date,
            "time": extra["time"],
            "topic": extra["topic"]
        })

    conn.commit()
    return results


async def notify_student_mass_cancel(telegram_id: int, student_name: str, details: dict,
                                     start_date: date, end_date: date):
    """Уведомление ученика о массовой отмене"""
    try:
        message_lines = [
            f"❌ <b>МАССОВАЯ ОТМЕНА ЗАНЯТИЙ</b>\n\n",
            f"<b>Период:</b> {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n"
        ]

        total_cancelled = 0

        # Регулярные занятия
        if details["regular_cancels"]:
            message_lines.append(f"\n<b>Отмененные регулярные занятия:</b>")
            for cancel in details["regular_cancels"][:5]:  # Показываем первые 5
                message_lines.append(f"• {cancel['date'].strftime('%d.%m.%Y')} в {cancel['time']}")
            total_cancelled += len(details["regular_cancels"])

            if len(details["regular_cancels"]) > 5:
                message_lines.append(f"• ... и ещё {len(details['regular_cancels']) - 5} занятий")

        # Разовые занятия (оверрайды)
        if details["override_cancels"]:
            message_lines.append(f"\n<b>Отмененные разовые занятия:</b>")
            for cancel in details["override_cancels"][:3]:
                cancel_type = "отменено" if cancel.get("type") == "updated_override" else "удален перенос"
                message_lines.append(f"• {cancel['date'].strftime('%d.%m.%Y')} в {cancel['time']} ({cancel_type})")
            total_cancelled += len(details["override_cancels"])

        # Дополнительные занятия
        if details["extra_cancels"]:
            message_lines.append(f"\n<b>Отмененные дополнительные занятия:</b>")
            for cancel in details["extra_cancels"][:3]:
                topic_text = f" - {cancel['topic']}" if cancel.get("topic") else ""
                message_lines.append(f"• {cancel['date'].strftime('%d.%m.%Y')} в {cancel['time']}{topic_text}")
            total_cancelled += len(details["extra_cancels"])

        if total_cancelled == 0:
            return  # Не отправляем уведомление, если ничего не отменено

        message_lines.append(f"\n<b>Всего отменено занятий:</b> {total_cancelled}")
        message_lines.append(f"\nРегулярное расписание будет восстановлено после окончания периода отмены.")

        await bot.send_message(
            telegram_id,
            "\n".join(message_lines),
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление ученику {telegram_id}: {e}")
        return False


@router.message(MassCancelAllStates.confirming)
async def mass_cancel_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить операцию", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю массовую отмену занятий. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, отменить всё":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, отменить всё» или «❌ Нет, отменить операцию»."
        )
        return

    data = await state.get_data()
    start_date = data.get("mass_cancel_start_date")
    end_date = data.get("mass_cancel_end_date")

    if not all([start_date, end_date]):
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Выполняем массовую отмену
    await message.answer(
        "⏳ <b>Выполняю массовую отмену...</b>\n"
        "Это может занять несколько секунд.",
        parse_mode="HTML"
    )

    results = perform_mass_cancel_for_all(start_date, end_date)

    # Уведомляем учеников
    notified_count = 0
    for student_id, details in results["student_details"].items():
        if details["telegram_id"]:
            success = await notify_student_mass_cancel(
                telegram_id=details["telegram_id"],
                student_name=details["name"],
                details=details,
                start_date=start_date,
                end_date=end_date
            )
            if success:
                notified_count += 1

    # Формируем отчет для преподавателя
    report_text = (
        f"✅ <b>МАССОВАЯ ОТМЕНА ВЫПОЛНЕНА</b>\n\n"
        f"📅 <b>Период:</b> {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n"
        f"📊 <b>Результаты:</b>\n"
        f"• Отменено регулярных занятий: {results['regular_cancelled']}\n"
        f"• Удалено разовых занятий: {results['overrides_removed']}\n"
        f"• Отменено дополнительных занятий: {results['extras_cancelled']}\n"
        f"• <b>ВСЕГО: {results['regular_cancelled'] + results['overrides_removed'] + results['extras_cancelled']}</b>\n\n"
        f"👥 <b>Уведомления:</b>\n"
        f"• Затронуто учеников: {len(results['student_details'])}\n"
        f"• Получили уведомления: {notified_count}\n\n"
        f"<i>Все ученики были уведомлены об отменах.</i>"
    )

    await message.answer(
        report_text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


def get_future_changes_for_all(days_ahead: int = 30):
    """Получает все будущие изменения: оверрайды и дополнительные занятия"""
    today = date.today()
    end = today + timedelta(days=days_ahead)
    cur = conn.cursor()

    # Получаем оверрайды
    cur.execute(
        """
        SELECT 
            o.*, 
            w.student_id, 
            w.weekday, 
            w.time AS weekly_time,
            s.telegram_id, 
            s.username, 
            s.full_name,
            'override' as change_type
        FROM lesson_overrides o
        JOIN weekly_lessons w ON w.id = o.weekly_lesson_id
        JOIN students s ON s.id = w.student_id
        WHERE o.date >= ?
          AND o.date <= ?
          AND w.is_active = 1
        """,
        (today.isoformat(), end.isoformat()),
    )
    overrides = cur.fetchall()

    # Получаем дополнительные занятия
    cur.execute(
        """
        SELECT 
            e.*,
            s.telegram_id, 
            s.username, 
            s.full_name,
            'extra' as change_type
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.date >= ?
          AND e.date <= ?
          AND e.status = 'scheduled'
        """,
        (today.isoformat(), end.isoformat()),
    )
    extras = cur.fetchall()

    # Объединяем и сортируем по дате и времени
    all_changes = []

    for ov in overrides:
        all_changes.append({
            "type": "override",
            "id": ov["id"],
            "date": ov["date"],
            "time": ov["new_time"] if ov["change_kind"] != "cancel" else ov["weekly_time"],
            "change_kind": ov["change_kind"],
            "weekly_time": ov["weekly_time"],
            "student_id": ov["student_id"],
            "telegram_id": ov["telegram_id"],
            "full_name": ov["full_name"],
            "username": ov["username"],
            "weekday": ov["weekday"],
            "original_date": ov["original_date"],
            "original_time": ov["original_time"],
            "weekly_lesson_id": ov["weekly_lesson_id"],
            "extra_data": None
        })

    for ex in extras:
        all_changes.append({
            "type": "extra",
            "id": ex["id"],
            "date": ex["date"],
            "time": ex["time"],
            "change_kind": "extra_lesson",  # Специальный тип для дополнительных занятий
            "student_id": ex["student_id"],
            "telegram_id": ex["telegram_id"],
            "full_name": ex["full_name"],
            "username": ex["username"],
            "topic": ex["topic"],
            "remind_before_minutes": ex["remind_before_minutes"],
            "status": ex["status"],
            "extra_data": ex
        })

    # Сортируем по дате и времени
    all_changes.sort(key=lambda x: (x["date"], x["time"]))

    return all_changes


def create_changes_keyboard(changes, page: int = 0):
    """Создает инлайн-клавиатуру с кнопками для всех изменений с пагинацией"""
    builder = InlineKeyboardBuilder()

    # Получаем элементы для текущей страницы
    # ИЗМЕНЕНИЕ: метод get_page возвращает 4 значения, а не 3
    page_changes, current_page, total_pages, page_size = Paginator.get_page(changes, page)

    for change in page_changes:
        change_id = change["id"]
        student_name = change["full_name"] or change["username"] or str(change["telegram_id"])
        d = date.fromisoformat(change["date"])
        date_str = d.strftime("%d.%m.%Y")

        if change["type"] == "override":
            if change["change_kind"] == "cancel":
                kind_text = "отмена"
                time_text = f"отменено ({change['weekly_time']})"
                emoji = "❌"
            else:
                kind_text = "перенос"
                time_text = change["time"]
                emoji = "🔄"

            # Обрезаем длинные имена
            if len(student_name) > 12:
                student_name = student_name[:10] + "..."

            builder.button(
                text=f"{emoji} #{change_id} {student_name} - {date_str} {time_text}",
                callback_data=f"view_override_{change_id}_{page}"
            )
        else:  # type == "extra"
            kind_text = "доп. занятие"
            time_text = change["time"]
            topic = change.get("topic", "")
            emoji = "✨"

            if topic:
                if len(topic) > 15:
                    topic = topic[:12] + "..."
                topic_text = f" - {topic}"
            else:
                topic_text = ""

            # Обрезаем длинные имена
            if len(student_name) > 10:
                student_name = student_name[:8] + "..."

            builder.button(
                text=f"{emoji} #{change_id} {student_name} - {date_str} {time_text}{topic_text}",
                callback_data=f"view_extra_{change_id}_{page}"
            )

    builder.adjust(1)

    # Добавляем пагинацию если нужно
    pagination_keyboard = Paginator.create_pagination_keyboard(
        current_page=current_page,
        total_pages=total_pages,
        prefix="changes",
        show_info=True
    )

    return builder.as_markup(), pagination_keyboard, total_pages


@router.message(Command("list_overrides"))
async def cmd_list_overrides(message: Message):
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await message.answer(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней)."
        )
        return

    # Получаем клавиатуры
    changes_kb, pagination_kb, total_pages = create_changes_keyboard(changes, page=0)

    # Создаем сообщение
    message_text = (
        f"📌 <b>Ближайшие разовые изменения и дополнительные занятия (страница 1/{total_pages}):</b>\n\n"
        "🔄 - переносы/отмены регулярных занятий\n"
        "✨ - дополнительные занятия\n\n"
        "Нажми на изменение для просмотра деталей и действий:"
    )

    if pagination_kb:
        # Если есть пагинация, отправляем два сообщения
        await message.answer(message_text, parse_mode="HTML")
        await message.answer(
            "Выберите изменение:",
            reply_markup=changes_kb
        )
        await message.answer(
            "Навигация по страницам:",
            reply_markup=pagination_kb
        )
    else:
        # Если нет пагинации, отправляем одним сообщением
        await message.answer(
            message_text,
            parse_mode="HTML",
            reply_markup=changes_kb
        )


# ---------- ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С ДОПОЛНИТЕЛЬНЫМИ ЗАНЯТИЯМИ ----------

def get_extra_lesson_by_id(extra_id: int):
    """Получает дополнительное занятие по ID с данными ученика"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, s.telegram_id, s.username, s.full_name
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.id = ?
        """,
        (extra_id,)
    )
    return cur.fetchone()


@router.callback_query(lambda c: c.data.startswith("changes_page_"))
async def changes_page_callback(callback_query: CallbackQuery):
    """Обработчик пагинации для списка изменений"""
    page, _ = Paginator.parse_callback_data(callback_query.data)

    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await callback_query.message.edit_text(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней)."
        )
        await callback_query.answer()
        return

    # Получаем клавиатуры для новой страницы
    changes_kb, pagination_kb, total_pages = create_changes_keyboard(changes, page)

    # Обновляем сообщение с изменениями
    await callback_query.message.edit_text(
        "Выберите изменение:",
        reply_markup=changes_kb
    )

    # Обновляем сообщение с пагинацией
    try:
        async for msg in callback_query.message.bot.get_chat_history(
                callback_query.message.chat.id,
                limit=3
        ):
            if "Навигация по страницам" in msg.text:
                if pagination_kb:
                    await msg.edit_text(
                        f"Навигация по страницам (страница {page + 1}/{total_pages}):",
                        reply_markup=pagination_kb
                    )
                else:
                    await msg.delete()
                break
    except Exception as e:
        logging.error(f"Ошибка при обновлении пагинации: {e}")

    await callback_query.answer(f"Страница {page + 1}")

def update_extra_lesson(extra_id: int, new_date: date = None, new_time: dtime = None,
                        new_topic: str = None, new_remind_before: int = None):
    """Обновляет дополнительное занятие"""
    cur = conn.cursor()

    updates = []
    params = []

    if new_date is not None:
        updates.append("date = ?")
        params.append(new_date.isoformat())

    if new_time is not None:
        updates.append("time = ?")
        params.append(new_time.strftime("%H:%M"))

    if new_topic is not None:
        updates.append("topic = ?")
        params.append(new_topic)

    if new_remind_before is not None:
        updates.append("remind_before_minutes = ?")
        params.append(new_remind_before)

    if not updates:
        return None

    params.append(extra_id)

    query = f"UPDATE extra_lessons SET {', '.join(updates)} WHERE id = ?"
    cur.execute(query, tuple(params))
    conn.commit()

    return get_extra_lesson_by_id(extra_id)


@router.callback_query(lambda c: c.data.startswith("view_extra_"))
async def view_extra_details(callback_query: CallbackQuery):
    """Просмотр деталей дополнительного занятия и действий"""
    parts = callback_query.data.split("_")
    extra_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    e = get_extra_lesson_by_id(extra_id)

    if not e:
        await callback_query.answer("Дополнительное занятие не найдено")
        return

    d = date.fromisoformat(e["date"])
    date_str = d.strftime("%d.%m.%Y")

    message_text = (
        f"✨ <b>Дополнительное занятие #{e['id']}</b>\n\n"
        f"👤 <b>Ученик:</b> {e['full_name'] or e['username']}\n"
        f"📅 <b>Дата:</b> {date_str}\n"
        f"⏰ <b>Время:</b> {e['time']}\n"
        f"⏱️ <b>Напоминание:</b> за {e['remind_before_minutes'] or 60} минут\n"
    )

    if e["topic"]:
        message_text += f"📚 <b>Тема:</b> {e['topic']}\n"

    message_text += f"\n📊 <b>Статус:</b> запланировано"

    # Создаем клавиатуру с действиями
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Перенести", callback_data=f"reschedule_extra_{extra_id}_{page}")
    builder.button(text="🗑️ Удалить", callback_data=f"delete_extra_{extra_id}_{page}")
    builder.button(text="✅ Отметить выполненным", callback_data=f"mark_extra_done_{extra_id}_{page}")
    builder.button(text="⬅️ Назад к списку", callback_data=f"back_to_changes_list_{page}")
    builder.adjust(2)

    await callback_query.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("back_to_changes_list_"))
async def back_to_changes_list_with_page(callback_query: CallbackQuery):
    """Возврат к списку изменений с указанием страницы"""
    try:
        page = int(callback_query.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0

    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await callback_query.message.edit_text(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней).")
        await callback_query.answer()
        return

    # Получаем клавиатуры для указанной страницы
    changes_kb, pagination_kb, total_pages = create_changes_keyboard(changes, page=page)

    # Создаем сообщение
    message_text = (
        f"📌 <b>Ближайшие разовые изменения и дополнительные занятия (страница {page + 1}/{total_pages}):</b>\n\n"
        "🔄 - переносы/отмены регулярных занятий\n"
        "✨ - дополнительные занятия\n\n"
        "Нажми на изменение для просмотра деталей и действий:"
    )

    if pagination_kb:
        # Если есть пагинация, отправляем два сообщения
        await callback_query.message.edit_text(message_text, parse_mode="HTML")
        await callback_query.message.answer(
            "Выберите изменение:",
            reply_markup=changes_kb
        )
        # Отправляем пагинацию отдельным сообщением
        await callback_query.message.answer(
            f"Навигация по страницам (страница {page + 1}/{total_pages}):",
            reply_markup=pagination_kb
        )
    else:
        # Если нет пагинации, отправляем одним сообщением
        await callback_query.message.edit_text(
            message_text,
            parse_mode="HTML",
            reply_markup=changes_kb
        )
    await callback_query.answer()

@router.callback_query(lambda c: c.data == "back_to_changes_list")
async def back_to_changes_list(callback_query: CallbackQuery):
    """Возврат к списку изменений"""
    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await callback_query.message.edit_text(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней).")
        await callback_query.answer()
        return

    # Получаем клавиатуры
    changes_kb, pagination_kb, total_pages = create_changes_keyboard(changes, page=0)

    # Создаем сообщение
    message_text = (
        f"📌 <b>Ближайшие разовые изменения и дополнительные занятия (страница 1/{total_pages}):</b>\n\n"
        "🔄 - переносы/отмены регулярных занятий\n"
        "✨ - дополнительные занятия\n\n"
        "Нажми на изменение для просмотра деталей и действий:"
    )

    if pagination_kb:
        # Если есть пагинация, отправляем два сообщения
        await callback_query.message.edit_text(message_text, parse_mode="HTML")
        await callback_query.message.answer(
            "Выберите изменение:",
            reply_markup=changes_kb
        )
        # Отправляем пагинацию отдельным сообщением
        await callback_query.message.answer(
            "Навигация по страницам:",
            reply_markup=pagination_kb
        )
    else:
        # Если нет пагинации, отправляем одним сообщением
        await callback_query.message.edit_text(
            message_text,
            parse_mode="HTML",
            reply_markup=changes_kb
        )
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("delete_extra_"))
async def delete_extra_callback(callback_query: CallbackQuery):
    """Удаление дополнительного занятия через кнопку"""
    parts = callback_query.data.split("_")
    extra_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Удаляем дополнительное занятие
    deleted_extra = delete_extra_lesson(extra_id)

    if not deleted_extra:
        await callback_query.answer("Дополнительное занятие не найдено")
        return

    # Уведомляем ученика
    student_name = deleted_extra["full_name"] or deleted_extra["username"] or str(deleted_extra["telegram_id"])
    d = date.fromisoformat(deleted_extra["date"])
    date_str = d.strftime("%d.%m.%Y")

    message_text = (
        f"❌ <b>Дополнительное занятие отменено!</b>\n\n"
        f"Занятие на {date_str} в {deleted_extra['time']} было отменено преподавателем."
    )

    try:
        await bot.send_message(
            deleted_extra["telegram_id"],
            message_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение ученику: {e}")

    await callback_query.answer(f"Дополнительное занятие #{extra_id} удалено")

    # Возвращаемся к списку изменений
    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await callback_query.message.edit_text(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней).")
        return

    await callback_query.message.edit_text(
        f"📌 <b>Ближайшие разовые изменения и дополнительные занятия (страница {page + 1}):</b>\n\n"
        "🔄 - переносы/отмены регулярных занятий\n"
        "✨ - дополнительные занятия\n\n"
        "Нажми на изменение для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=create_changes_keyboard(changes, page=page)
    )


@router.callback_query(lambda c: c.data.startswith("reschedule_extra_"))
async def reschedule_extra_callback(callback_query: CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    extra_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    extra = get_extra_lesson_by_id(extra_id)
    if not extra:
        await callback_query.answer("Дополнительное занятие не найдено")
        return

    # (опционально) сохрани page, чтобы потом вернуться на нужную страницу
    await state.update_data(reschedule_extra_page=page)

    await state.update_data(reschedule_extra_id=extra_id)
    await state.update_data(reschedule_original_date=date.fromisoformat(extra["date"]))
    await state.update_data(reschedule_original_time=extra["time"])

    await state.set_state(RescheduleExtraStates.entering_date)

    await callback_query.message.answer(
        f"🔄 <b>Перенос дополнительного занятия</b>\n\n"
        f"Ученик: {extra['full_name'] or extra['username']}\n"
        f"Текущая дата: {date.fromisoformat(extra['date']).strftime('%d.%m.%Y')}\n"
        f"Текущее время: {extra['time']}\n\n"
        f"На какую дату переносим занятие?\n"
        f"Формат: ДД.ММ или ДД.ММ.ГГГГ",
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )
    await callback_query.answer()


# Создаем состояния для переноса дополнительных занятий
class RescheduleExtraStates(StatesGroup):
    entering_date = State()
    entering_time = State()
    confirming = State()


@router.message(RescheduleExtraStates.entering_date)
async def reschedule_extra_enter_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    new_date = parse_date_str(text)
    if not new_date:
        await message.answer(
            "Дата должна быть в формате ДД.ММ или ДД.ММ.ГГГГ. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(reschedule_new_date=new_date)
    await state.set_state(RescheduleExtraStates.entering_time)

    await message.answer(
        "На какое время переносим занятие? (формат HH:MM, например 19:00)",
        reply_markup=back_keyboard(),
    )


@router.message(RescheduleExtraStates.entering_time)
async def reschedule_extra_enter_time(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю перенос. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    try:
        hh, mm = map(int, text.split(":"))
        new_time = dtime(hh, mm)
    except Exception:
        await message.answer(
            "Время должно быть в формате ЧЧ:ММ, например 19:00. Попробуй ещё раз.",
            reply_markup=back_keyboard(),
        )
        return

    await state.update_data(reschedule_new_time=new_time)
    await state.set_state(RescheduleExtraStates.confirming)

    data = await state.get_data()
    extra_id = data.get("reschedule_extra_id")
    original_date = data.get("reschedule_original_date")
    original_time = data.get("reschedule_original_time")
    new_date = data.get("reschedule_new_date")

    extra = get_extra_lesson_by_id(extra_id)

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да, перенести")],
            [KeyboardButton(text="❌ Нет, отменить")],
        ],
        resize_keyboard=True,
    )

    await message.answer(
        f"Вы действительно хотите перенести дополнительное занятие?\n"
        f"Ученик: {extra['full_name'] or extra['username']}\n"
        f"Было: {original_date.strftime('%d.%m.%Y')} в {original_time}\n"
        f"Стало: {new_date.strftime('%d.%m.%Y')} в {new_time.strftime('%H:%M')}\n\n"
        f"Ученик получит уведомление о переносе.",
        reply_markup=kb,
    )


@router.message(RescheduleExtraStates.confirming)
async def reschedule_extra_confirm(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in ("❌ Нет, отменить", BACK_TEXT):
        await state.clear()
        await message.answer(
            "Отменяю перенос занятия. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    if text != "✅ Да, перенести":
        await message.answer(
            "Пожалуйста, выберите один из вариантов: «✅ Да, перенести» или «❌ Нет, отменить»."
        )
        return

    data = await state.get_data()
    extra_id = data.get("reschedule_extra_id")
    new_date = data.get("reschedule_new_date")
    new_time = data.get("reschedule_new_time")
    original_date = data.get("reschedule_original_date")
    original_time = data.get("reschedule_original_time")

    if not extra_id or not new_date or not new_time:
        await state.clear()
        await message.answer(
            "Ошибка: данные не найдены. Попробуйте снова.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Получаем данные дополнительного занятия
    extra = get_extra_lesson_by_id(extra_id)
    if not extra:
        await state.clear()
        await message.answer(
            "Ошибка: занятие не найдено.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    # Обновляем дополнительное занятие
    updated_extra = update_extra_lesson(
        extra_id=extra_id,
        new_date=new_date,
        new_time=new_time
    )

    # Отправляем уведомление ученику
    if updated_extra and updated_extra["telegram_id"]:
        await notify_extra_lesson_rescheduled(
            student_telegram_id=updated_extra["telegram_id"],
            old_date=original_date,
            old_time=original_time,
            new_date=new_date,
            new_time=new_time.strftime("%H:%M"),
            topic=extra["topic"]
        )

    student_name = extra["full_name"] or extra["username"] or str(extra["telegram_id"])

    await message.answer(
        f"Дополнительное занятие для {student_name} перенесено с {original_date.strftime('%d.%m.%Y')} {original_time} "
        f"на {new_date.strftime('%d.%m.%Y')} {new_time.strftime('%H:%M')}.",
        reply_markup=main_menu_keyboard(is_teacher(message)),
    )

    await state.clear()


@router.callback_query(lambda c: c.data.startswith("mark_extra_done_"))
async def mark_extra_done_callback(callback_query: CallbackQuery):
    parts = callback_query.data.split("_")
    extra_id = int(parts[3])  # mark_extra_done_{id}_{page} -> ["mark","extra","done","{id}","{page}"] если так сделаешь

    if not is_teacher(callback_query):
        await callback_query.answer("Эта функция только для преподавателя.")
        return

    # Помечаем дополнительное занятие как выполненное и добавляем в историю
    history_id = mark_extra_lesson_as_done(extra_id)

    if not history_id:
        await callback_query.answer("Ошибка: не удалось отметить занятие как выполненное")
        return

    # Получаем данные о занятии
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.*, s.telegram_id, s.full_name, s.username
        FROM extra_lessons e
        JOIN students s ON s.id = e.student_id
        WHERE e.id = ?
        """,
        (extra_id,)
    )
    extra = cur.fetchone()

    if extra:
        # Отправляем уведомление ученику
        try:
            d = date.fromisoformat(extra["date"])
            message_text = (
                f"✅ <b>Дополнительное занятие отмечено как выполненное!</b>\n\n"
                f"• Дата: {d.strftime('%d.%m.%Y')}\n"
                f"• Время: {extra['time']}\n"
            )
            if extra["topic"]:
                message_text += f"• Тема: {extra['topic']}\n"

            message_text += f"\nЗанятие добавлено в историю занятий."

            await bot.send_message(
                extra["telegram_id"],
                message_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение ученику: {e}")

    await callback_query.answer("Занятие отмечено как выполненное")

    # Возвращаемся к списку изменений
    changes = get_future_changes_for_all(days_ahead=30)
    if not changes:
        await callback_query.message.edit_text(
            "Нет ближайших разовых изменений и дополнительных занятий (на ближайшие 30 дней).")
        return

    await callback_query.message.edit_text(
        "📌 <b>Ближайшие разовые изменения и дополнительные занятия:</b>\n\n"
        "🔄 - переносы/отмены регулярных занятий\n"
        "✨ - дополнительные занятия\n\n"
        "Нажми на изменение для просмотра деталей и действий:",
        parse_mode="HTML",
        reply_markup=create_changes_keyboard(changes)
    )


async def notify_extra_lesson_rescheduled(student_telegram_id: int, old_date: date, old_time: str,
                                          new_date: date, new_time: str, topic: str = None):
    """Уведомление о переносе дополнительного занятия"""
    old_date_str = old_date.strftime("%d.%m.%Y")
    new_date_str = new_date.strftime("%d.%m.%Y")

    message = (
        f"🔄 <b>Дополнительное занятие перенесено!</b>\n\n"
        f"• Было: <b>{old_date_str} в {old_time}</b>\n"
        f"• Стало: <b>{new_date_str} в {new_time}</b>\n"
    )

    if topic:
        message += f"• Тема: <b>{topic}</b>\n"

    message += f"\nНапоминание придет за 60 минут до начала занятия."

    await notify_student_about_schedule_change(student_telegram_id, message)


@router.callback_query(lambda c: c.data == "page_info")
async def page_info_callback(callback_query: CallbackQuery):
    """Информация о текущей странице"""
    await callback_query.answer(
        "ℹ️ Это индикатор текущей страницы",
        show_alert=False
    )


async def send_paginated_message(
        chat_id: int,
        title: str,
        items: list,
        item_formatter: callable,
        page: int = 0,
        page_size: int = PAGE_SIZE,
        prefix: str = "page"
):
    """Универсальная функция для отправки пагинированных сообщений"""
    if not items:
        await bot.send_message(chat_id, f"{title}\n\nСписок пуст.")
        return

    total_pages = (len(items) + page_size - 1) // page_size
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(items))
    page_items = items[start_idx:end_idx]

    # Форматируем элементы
    lines = [f"{title} (страница {page + 1}/{total_pages}):"]
    for item in page_items:
        lines.append(item_formatter(item))

    # Создаем клавиатуру пагинации
    builder = InlineKeyboardBuilder()

    if page > 0:
        builder.button(
            text="◀️",
            callback_data=f"{prefix}_page_{page - 1}"
        )

    builder.button(
        text=f"{page + 1}/{total_pages}",
        callback_data="page_info"
    )

    if page < total_pages - 1:
        builder.button(
            text="▶️",
            callback_data=f"{prefix}_page_{page + 1}"
        )

    builder.adjust(3)

    await bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=builder.as_markup()
    )


@router.message(Command("hw_reminders"))
async def cmd_hw_reminders(message: Message):
    """Управление напоминаниями о домашке"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    await message.answer(
        "🔔 <b>Управление напоминаниями о домашке</b>\n\n"
        "Система автоматически отправляет напоминания:\n"
        "1. <b>За 2 часа до занятия</b> - если есть невыполненные задания\n"
        "2. <b>Через час после занятия</b> - если пропустили напоминание\n\n"
        "📋 <b>Статистика:</b>\n"
        f"• Текущее время: {datetime.now().strftime('%H:%M')}\n"
        f"• Время работы: с 8:00 до 23:00\n\n"
        "Для тестирования используйте команды:\n"
        "• /test_hw_remind @username - тест напоминания\n"
        "• /clear_hw_remind @username - очистить флаги",
        parse_mode="HTML"
    )


@router.message(Command("test_hw_remind"))
async def cmd_test_hw_remind(message: Message):
    """Тест отправки напоминания о домашке"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Формат: /test_hw_remind @username")
        return

    user_key = parts[1]
    student = get_student_by_user_key(user_key)

    if not student:
        await message.answer("Ученик не найден.")
        return

    # Проверяем, есть ли невыполненные задания
    hws = get_homeworks_for_student(student["id"], only_open=True)

    if not hws:
        await message.answer(f"У {student['full_name']} нет невыполненных заданий.")
        return

    # Формируем тестовое сообщение
    hw_count = len(hws)
    if hw_count == 1:
        hw_text = f"1 задание: {hws[0]['text']}"
    else:
        hw_text = f"{hw_count} заданий"

    test_message = (
        f"🔔 <b>ТЕСТОВОЕ НАПОМИНАНИЕ</b>\n\n"
        f"Это тестовое напоминание о домашнем задании.\n\n"
        f"У вас занятие сегодня в <b>19:00</b> (тестовое время)\n"
        f"Осталось невыполненных заданий: <b>{hw_text}</b>\n\n"
    )

    if hw_count <= 3:
        for i, hw in enumerate(hws[:3], 1):
            test_message += f"{i}. {hw['text']}\n"

    test_message += (
        f"\nВ реальной системе это напоминание пришло бы за 2 часа до занятия."
    )

    try:
        await bot.send_message(
            student["telegram_id"],
            test_message,
            parse_mode="HTML"
        )
        await message.answer(f"Тестовое напоминание отправлено {student['full_name']}")
    except Exception as e:
        await message.answer(f"Ошибка отправки: {e}")


@router.message(Command("clear_hw_remind"))
async def cmd_clear_hw_remind(message: Message):
    """Очистка флагов напоминаний для ученика"""
    if not is_teacher(message):
        await message.answer("Эта команда доступна только преподавателю.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Формат: /clear_hw_remind @username")
        return

    user_key = parts[1]
    student = get_student_by_user_key(user_key)

    if not student:
        await message.answer("Ученик не найден.")
        return

    # Очищаем флаги напоминаний
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM system_flags WHERE key LIKE ?",
        (f"hw_%_{student['id']}_%",)
    )
    conn.commit()

    await message.answer(f"Флаги напоминаний очищены для {student['full_name']}")


@router.message(lambda message: message.text == "⏰ Напоминания")
async def handle_reminders_button(message: Message):
    """Обработка нажатия кнопки 'Напоминания'"""
    student = get_student_by_telegram_id(message.from_user.id)
    if not student:
        await message.answer("Я тебя ещё не знаю. Напиши /start.")
        return

    # Проверяем, есть ли невыполненные задания
    hws = get_homeworks_for_student(student["id"], only_open=True)

    message_text = (
        "🔔 <b>Настройка напоминаний</b>\n\n"
        "Система автоматически напоминает:\n"
        "• За 60 минут до занятия (настраивается в /set_remind)\n"
        "• За 35 минут - преподавателю\n"
        "• <b>За 2 часа о домашнем задании</b> (если есть невыполненные)\n\n"
    )

    if hws:
        hw_count = len(hws)
        message_text += (
            f"📚 <b>Текущий статус:</b>\n"
            f"У вас <b>{hw_count}</b> невыполненных заданий\n\n"
            "Следующее напоминание придёт за 2 часа до занятия.\n"
            "Время работы напоминаний: с 8:00 до 23:00\n\n"
            "Используйте /set_remind для настройки обычных напоминаний."
        )
    else:
        message_text += (
            "🎉 <b>Поздравляем!</b>\n"
            "У вас нет невыполненных домашних заданий.\n"
            "Напоминания о домашке не нужны.\n\n"
            "Используйте /set_remind для настройки обычных напоминаний."
        )

    await message.answer(message_text, parse_mode="HTML")


@router.message(lambda message: message.text == "✏️ Задать домашку")
async def handle_set_homework_smart(message: Message, state: FSMContext):
    """Умный выбор ученика для задания домашнего задания"""
    if not is_teacher(message):
        await message.answer("Эта функция только для преподавателя.")
        return

    # Создаем умную клавиатуру
    keyboard, title, total_pages = create_smart_student_keyboard('homework')

    if keyboard is None:
        await message.answer("Нет учеников для задания домашнего задания.")
        return

    # Сохраняем тип действия в состоянии
    await state.update_data(action_type='homework')
    await state.set_state(HomeworkStates.choosing_student_smart)

    await message.answer(
        f"{title}\n\n"
        "Выберите ученика, которому хотите задать домашнее задание:",
        reply_markup=keyboard
    )



@router.callback_query(lambda c: c.data.startswith("select_student_"))
async def select_student_callback(callback_query: CallbackQuery, state: FSMContext):
    """Обработка выбора ученика из умной клавиатуры"""
    parts = callback_query.data.split("_")
    action_type = parts[2]
    student_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 0

    # Получаем данные ученика
    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    student_name = student['full_name'] or student['username'] or str(student['telegram_id'])

    # Сохраняем данные в состоянии
    await state.update_data(student_id=student_id)
    await state.update_data(student_name=student_name)

    # В зависимости от типа действия переходим к следующему шагу
    if action_type == 'homework':
        await state.set_state(HomeworkStates.waiting_text)

        # 1) обновляем текущее инлайн-сообщение без reply-клавиатуры
        await callback_query.message.edit_text(
            f"📝 <b>Домашнее задание для {student_name}</b>\n\n"
            "Сейчас пришлите текст домашнего задания одним сообщением.\n"
            f"Чтобы отменить — нажмите «{BACK_TEXT}».",
            parse_mode="HTML",
            reply_markup=None
        )

        # 2) отдельным сообщением показываем кнопку «Назад» (ReplyKeyboardMarkup)
        await callback_query.message.answer(
            "✍️ Введите текст домашнего задания одним сообщением:",
            reply_markup=back_keyboard()
        )


    elif action_type == 'cancel':
        # Получаем занятия ученика
        lessons = get_weekly_lessons_for_student(student_id)
        if not lessons:
            await callback_query.message.edit_text(
                f"У ученика {student_name} нет активных занятий."
            )
            await state.clear()
            return

        # Создаем клавиатуру с занятиями
        builder = InlineKeyboardBuilder()
        for i, wl in enumerate(lessons, start=1):
            builder.button(
                text=f"{weekday_to_name(wl['weekday'])} {wl['time']}",
                callback_data=f"cancel_lesson_{wl['id']}_{student_id}"
            )

        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_students_{action_type}_{page}"))

        await state.update_data(cancel_lesson_ids=[wl["id"] for wl in lessons])
        # ИСПРАВЛЕНО: устанавливаем правильное состояние
        await state.set_state(CancelStates.choosing_lesson)  # Было: .choosing_student

        await callback_query.message.edit_text(
            f"Выберите занятие для отмены (ученик: {student_name}):",
            reply_markup=builder.as_markup()
        )

    # ... остальной код

def is_teacher_callback(callback_query: CallbackQuery) -> bool:
    """Проверка прав преподавателя для callback-запросов"""
    return callback_query.from_user.id in TEACHER_IDS


@router.callback_query(lambda c: c.data.startswith("back_to_students_"))
async def back_to_students_callback(callback_query: CallbackQuery, state: FSMContext):
    """Возврат к списку учеников"""
    parts = callback_query.data.split("_")
    action_type = parts[3]
    page = int(parts[4]) if len(parts) > 4 else 0

    # Создаем обновленную клавиатуру
    keyboard, title, total_pages = create_smart_student_keyboard(action_type, page)

    if keyboard:
        await callback_query.message.edit_text(
            f"{title}\n\n"
            f"Выберите ученика:",
            reply_markup=keyboard
        )

    await state.set_state(CancelStates.choosing_student_smart)
    await callback_query.answer()


@router.callback_query(lambda c: c.data.startswith("view_student_"))
async def view_student_callback(callback_query: CallbackQuery):
    """Просмотр информации об ученике"""
    student_id = int(callback_query.data.split("_")[2])

    cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    student = cur.fetchone()

    if not student:
        await callback_query.answer("Ученик не найден")
        return

    # Получаем занятия ученика
    lessons = get_weekly_lessons_for_student(student_id)
    homeworks = get_homeworks_for_student(student_id, only_open=True)
    history = get_lesson_history_for_student(student_id, limit=5)

    message_text = (
        f"👤 <b>Информация об ученике</b>\n\n"
        f"Имя: {student['full_name'] or 'Не указано'}\n"
        f"Username: @{student['username'] or 'Нет'}\n"
        f"Telegram ID: {student['telegram_id']}\n\n"
        f"📅 <b>Занятия:</b> {len(lessons)}\n"
        f"📚 <b>Домашние задания:</b> {len(homeworks)}\n"
        f"📝 <b>История занятий:</b> {len(history)}\n\n"
    )

    if lessons:
        message_text += "Расписание:\n"
        for wl in lessons:
            message_text += f"• {weekday_to_name(wl['weekday'])} {wl['time']}\n"

    # Создаем клавиатуру с действиями
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Добавить слот", callback_data=f"add_slot_{student_id}")
    builder.button(text="📚 Задать домашку", callback_data=f"add_homework_{student_id}")
    builder.button(text="💰 Отметить оплату", callback_data=f"mark_payment_{student_id}")
    builder.button(text="❌ Отменить занятие", callback_data=f"cancel_lesson_student_{student_id}")
    builder.button(text="⬅️ Назад к списку", callback_data="back_to_students_list")
    builder.adjust(2)

    await callback_query.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )
    await callback_query.answer()


@router.message(lambda m: m.text == "👥 Расписание")
async def handle_students_schedule(message: Message):
    if not is_teacher(message):
        return

    lessons = get_all_weekly_lessons(active_only=True)

    if not lessons:
        await message.answer("Пока не задано ни одного занятия.")
        return

    schedule_by_day = {i: [] for i in range(7)}

    for lesson in lessons:
        weekday = lesson["weekday"]
        name = format_student_title(lesson["full_name"], lesson["username"], lesson["telegram_id"])
        time = lesson["time"]
        schedule_by_day[weekday].append((name, time))

    lines = []

    for weekday in range(7):
        day_lessons = schedule_by_day[weekday]
        if not day_lessons:
            continue

        lines.append(f"<b>{DAY_NAMES[weekday]}</b>")

        for name, time in sorted(day_lessons, key=lambda x: x[1]):
            lines.append(f"{name} — {time}")

        lines.append("")

    await message.answer("\n".join(lines), parse_mode="HTML")



def format_student_title(full_name: str | None, username: str | None, telegram_id: int | str):
    base = (full_name or "").strip() or (username or "").strip() or str(telegram_id)

    # Если есть username — показываем как ты и хотела
    if username:
        uname = username if username.startswith("@") else f"@{username}"
        if (full_name or "").strip():
            return f"{base} ({uname})"
        return uname

    # Если username нет — делаем кликабельное упоминание по id
    return f'<a href="tg://user?id={telegram_id}">{base}</a>'




def create_students_keyboard(students, action_type: str, page: int = 0):
    """Создает инлайн-клавиатуру со списком учеников с пагинацией и кнопкой Назад"""
    builder = InlineKeyboardBuilder()

    # Получаем размер страницы для пользователя
    user_id = None  # Можно получить из контекста
    page_size = USER_PAGE_SIZES.get(user_id, PAGE_SIZE)

    # Разбиваем на страницы
    total_pages = (len(students) + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(students))
    page_students = students[start_idx:end_idx]

    for student in page_students:
        student_id = student["id"]
        name = student["full_name"] or student["username"] or str(student["telegram_id"])

        # Обрезаем длинные имена для кнопок
        if len(name) > 20:
            name = name[:17] + "..."

        # В callback_data передаем действие и ID ученика
        builder.button(
            text=name,
            callback_data=f"hw_student_{student_id}_{page}"
        )

    builder.adjust(2)  # 2 кнопки в ряд

    # Добавляем пагинацию если нужно
    if total_pages > 1:
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"hw_page_{page - 1}"
            ))

        pagination_buttons.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="page_info"
        ))

        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"hw_page_{page + 1}"
            ))

        builder.row(*pagination_buttons)

    # Добавляем кнопку "Назад в меню" - ИСПРАВЛЕНИЕ ЗДЕСЬ
    builder.row(InlineKeyboardButton(
        text="⬅️ Назад в меню",
        callback_data="back_from_homework"
    ))

    return builder.as_markup(), total_pages

@router.callback_query(lambda c: c.data == "back_from_homework")
async def back_from_homework(callback_query: CallbackQuery, state: FSMContext):
    """Возврат из выбора ученика для домашнего задания"""
    await state.clear()
    await callback_query.message.delete()
    await callback_query.message.answer(
        "Возвращаю в главное меню.",
        reply_markup=main_menu_keyboard(True)
    )
    await callback_query.answer()

# 4. Исправляем аналогичные проблемы для HomeworkStates и PaymentStates:
@router.message(HomeworkStates.choosing_student_smart)
async def hw_choose_student_smart_text(message: Message, state: FSMContext):
    """Обработка текстового ввода при выборе ученика для домашнего задания"""
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю задание домашнего задания. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    await message.answer(
        "Пожалуйста, выберите ученика из списка выше, используя кнопки.",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )


@router.message(PaymentStates.choosing_student_smart)
async def payment_choose_student_smart_text(message: Message, state: FSMContext):
    """Обработка текстового ввода при выборе ученика для оплаты"""
    text = message.text.strip()
    if text == BACK_TEXT:
        await state.clear()
        await message.answer(
            "Отменяю отметку оплаты. Возвращаю в главное меню.",
            reply_markup=main_menu_keyboard(is_teacher(message)),
        )
        return

    await message.answer(
        "Пожалуйста, выберите ученика из списка выше, используя кнопки.",
        reply_markup=main_menu_keyboard(is_teacher(message))
    )

# ---------- ОБНОВЛЯЕМ ОБРАБОТКУ КНОПКИ "📌 Переносы/отмены" ----------

@router.message(lambda message: message.text == "📌 Переносы/отмены")
async def handle_list_overrides_button(message: Message):
    """Обработка нажатия кнопки "Переносы/отмены" """
    await cmd_list_overrides(message)

if __name__ == "__main__":
    asyncio.run(main())