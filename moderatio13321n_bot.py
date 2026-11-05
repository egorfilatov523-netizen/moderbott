import asyncio
import itertools
import os
import random
import re
import time
from dataclasses import dataclass, field
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

# Токен только в .env, не в коде!
BOT_TOKEN = os.getenv("BOT_TOKEN", "8937288340:AAFOQDffMAesEjonf59nFh89F3n8uTIolgQ").strip()
WARNING_DELETE_SECONDS = 7
CAPTCHA_TTL = 120          # сколько секунд действует капча
MAX_CHANNELS = 5           # максимум обязательных каналов
BOT_USERNAME = ""

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в .env")


LOOKALIKES = str.maketrans({
    "ё": "е",
    "a": "а", "c": "с", "e": "е", "o": "о",
    "p": "р", "x": "х", "y": "у", "k": "к",
    "m": "м", "t": "т", "h": "н", "b": "в",
    "0": "о", "3": "з",
})


# ============================================================
# ХРАНИЛИЩЕ (только в памяти, без базы данных)
# При перезапуске бота все данные сбрасываются.
# ============================================================

@dataclass
class Giveaway:
    id: int
    chat_id: int
    creator_id: int
    prize: str
    winners_count: int
    ends_at: float
    captcha: bool
    channels: list[dict]
    message_id: int | None = None
    active: bool = True
    participants: dict[int, str] = field(default_factory=dict)   # user_id -> username
    winners: list[dict] = field(default_factory=list)            # user_id, username, chance, removed


chats: dict[int, dict] = {}                    # chat_id -> {"title", "chat_type"}
connections: dict[int, set[int]] = {}          # user_id -> {chat_id}
banned_words: dict[int, set[str]] = {}         # user_id -> {слова}
giveaways: dict[int, Giveaway] = {}
giveaway_ids = itertools.count(1)
captcha_pending: dict[tuple[int, int], tuple[int, float]] = {}
admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


# ============================================================
# ЗАПРЕЩЁННЫЕ СЛОВА / ПОДКЛЮЧЕНИЯ
# ============================================================

def normalize(text: str) -> str:
    text = (text or "").lower().translate(LOOKALIKES)
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def get_user_words(user_id: int) -> list[str]:
    return sorted(banned_words.get(user_id, set()))


def get_chat_words(chat_id: int) -> list[str]:
    words: set[str] = set()
    for user_id, chat_ids in connections.items():
        if chat_id in chat_ids:
            words |= banned_words.get(user_id, set())
    return sorted(words)


def find_bad_word(text: str, words: list[str]) -> str | None:
    normalized = normalize(text)
    if not normalized:
        return None

    compact = normalized.replace(" ", "")

    for word in words:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
            return word

        compact_word = word.replace(" ", "")
        if len(compact_word) >= 5 and compact_word in compact:
            return word

    return None


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    now = time.monotonic()

    cached = admin_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in ("administrator", "creator")
        admin_cache[key] = (result, now + 30)
        return result
    except TelegramAPIError:
        return False


async def can_manage(message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    if message.chat.type in ("group", "supergroup"):
        return await is_group_admin(message.bot, message.chat.id, user.id)
    return True


async def can_manage_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    return await is_group_admin(bot, chat_id, user_id)


def is_connected(user_id: int, chat_id: int) -> bool:
    return chat_id in connections.get(user_id, set())


def connect_user_to_chat(user_id: int, chat_id: int, title: str, chat_type: str):
    chats[chat_id] = {"title": title, "chat_type": chat_type}
    connections.setdefault(user_id, set()).add(chat_id)


def disconnect_user_from_chat(user_id: int, chat_id: int):
    connections.get(user_id, set()).discard(chat_id)


def get_user_chats(user_id: int) -> list[dict]:
    result = []
    for chat_id in connections.get(user_id, set()):
        info = chats.get(chat_id, {"title": str(chat_id), "chat_type": "group"})
        result.append({"chat_id": chat_id, "title": info["title"], "chat_type": info["chat_type"]})
    return sorted(result, key=lambda c: (c["title"] or "").lower())


# ============================================================
# KEYBOARDS
# ============================================================

BTN_ADD = "➕ Добавить слово"
BTN_DEL = "🗑 Удалить слово"
BTN_WORDS = "📋 Мои слова"
BTN_CONNECT = "🔗 Подключить к чату"
BTN_DISCONNECT = "🔌 Отключить от чата"
BTN_GIVEAWAY = "🎉 Розыгрыш"
BTN_CHATS = "💬 Мои чаты"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP = "⏭ Пропустить"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_DEL)],
            [KeyboardButton(text=BTN_WORDS), KeyboardButton(text=BTN_CHATS)],
            [KeyboardButton(text=BTN_CONNECT), KeyboardButton(text=BTN_DISCONNECT)],
            [KeyboardButton(text=BTN_GIVEAWAY)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SKIP)], [KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def chats_inline(chat_list: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for chat in chat_list:
        title = chat["title"] or str(chat["chat_id"])
        rows.append([
            InlineKeyboardButton(
                text=f"💬 {title[:35]}",
                callback_data=f"{prefix}:{chat['chat_id']}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============================================================
# FSM
# ============================================================

class AddWordState(StatesGroup):
    waiting_word = State()


class DeleteWordState(StatesGroup):
    waiting_word = State()


class GiveawayState(StatesGroup):
    choosing_chat = State()
    waiting_minutes = State()
    waiting_winners = State()
    waiting_prize = State()
    waiting_channels = State()
    waiting_captcha = State()


# ============================================================
# BOT
# ============================================================

router = Router()


async def send_home(message: Message):
    await message.answer(
        "🛡 <b>Бот модерации</b>\n\n"
        "Настройки хранятся отдельно для каждого пользователя.\n"
        "Подключите бота к чату, добавьте свои запрещённые слова — "
        "они будут работать в подключённых вами чатах.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if message.chat.type == "private" and payload.startswith("join_"):
        try:
            giveaway_id = int(payload.removeprefix("join_"))
        except ValueError:
            await message.answer("Не удалось определить розыгрыш.")
            return
        await start_join_flow(message.bot, message.from_user, message.chat.id, giveaway_id)
        return

    if message.chat.type == "private" and payload.startswith("results_"):
        try:
            giveaway_id = int(payload.removeprefix("results_"))
        except ValueError:
            await message.answer("Не удалось определить розыгрыш.")
            return
        await message.answer("🎰 Подготавливаю быструю рулетку…")
        await finish_giveaway(message.bot, giveaway_id, animate=True, result_chat_id=message.chat.id)
        return

    if message.chat.type in ("group", "supergroup"):
        await message.answer(
            "ℹ️ Этот чат подключается автоматически после добавления "
            "бота администратором. Команда /start не нужна."
        )
        return

    await send_home(message)


@router.message(Command("menu"))
async def menu_command(message: Message, state: FSMContext):
    await state.clear()
    await send_home(message)


# ============================================================
# WORDS
# ============================================================

@router.message(F.text == BTN_ADD)
async def add_word_start(message: Message, state: FSMContext):
    await state.set_state(AddWordState.waiting_word)
    await message.answer(
        "✍️ Отправьте запрещённое слово одним сообщением.\n\n"
        "Оно сохранится только в вашем списке.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddWordState.waiting_word)
async def add_word_finish(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await state.clear()
        await send_home(message)
        return

    word = normalize(message.text or "")
    if not word:
        await message.answer("Введите непустое слово.")
        return

    banned_words.setdefault(message.from_user.id, set()).add(word)

    await state.clear()
    await message.answer(
        f"✅ Слово <b>{escape(word)}</b> добавлено в ваш список.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@router.message(F.text == BTN_DEL)
async def delete_word_start(message: Message, state: FSMContext):
    await state.set_state(DeleteWordState.waiting_word)
    await message.answer(
        "🗑 Отправьте слово, которое нужно удалить из вашего списка.",
        reply_markup=cancel_keyboard(),
    )


@router.message(DeleteWordState.waiting_word)
async def delete_word_finish(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await state.clear()
        await send_home(message)
        return

    word = normalize(message.text or "")
    if not word:
        await message.answer("Введите слово.")
        return

    words = banned_words.get(message.from_user.id, set())
    existed = word in words
    words.discard(word)

    await state.clear()

    if existed:
        text = f"✅ Слово <b>{escape(word)}</b> удалено."
    else:
        text = f"ℹ️ Слова <b>{escape(word)}</b> нет в вашем списке."

    await message.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


@router.message(F.text == BTN_WORDS)
async def show_words(message: Message):
    words = get_user_words(message.from_user.id)

    if not words:
        await message.answer(
            "📋 Ваш список пока пуст.\n"
            "Нажмите «➕ Добавить слово».",
            reply_markup=main_keyboard(),
        )
        return

    await message.answer(
        "📋 <b>Ваши запрещённые слова:</b>\n\n"
        + "\n".join(f"• {escape(w)}" for w in words),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CONNECT / DISCONNECT
# ============================================================

@router.message(F.text == BTN_CONNECT)
async def connect_help(message: Message):
    me = await message.bot.get_me()
    group_url = f"https://t.me/{me.username}?startgroup=true"
    channel_url = f"https://t.me/{me.username}?startchannel&admin=post_messages"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в группу", url=group_url)],
        [InlineKeyboardButton(text="📢 Добавить в канал", url=channel_url)],
    ])
    await message.answer(
        "🔗 <b>Подключение группы или канала</b>\n\n"
        "Для группы выдайте боту право удалять сообщения.\n"
        "Для канала выдайте право публиковать сообщения.\n\n"
        "После назначения администратором чат подключится автоматически.\n"
        "Розыгрыши можно публиковать и в каналах; фильтрация сообщений работает в группах "
        "и обсуждениях каналов.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(F.text == BTN_CHATS)
async def my_chats(message: Message):
    chat_list = get_user_chats(message.from_user.id)

    if not chat_list:
        await message.answer(
            "💬 У вас пока нет подключённых чатов.\n"
            "Нажмите «🔗 Подключить к чату».",
            reply_markup=main_keyboard(),
        )
        return

    lines = ["💬 <b>Ваши подключённые чаты:</b>", ""]
    for chat in chat_list:
        lines.append(f"• {escape(chat['title'] or str(chat['chat_id']))}")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@router.message(F.text == BTN_DISCONNECT)
async def disconnect_start(message: Message):
    chat_list = get_user_chats(message.from_user.id)

    if not chat_list:
        await message.answer(
            "Нет подключённых чатов.",
            reply_markup=main_keyboard(),
        )
        return

    await message.answer(
        "🔌 Выберите чат, который хотите отключить:",
        reply_markup=chats_inline(chat_list, "disconnect"),
    )


@router.callback_query(F.data.startswith("disconnect:"))
async def disconnect_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    chat_id = int(callback.data.split(":", 1)[1])

    if not is_connected(user_id, chat_id):
        await callback.answer("Этот чат уже отключён.", show_alert=True)
        return

    disconnect_user_from_chat(user_id, chat_id)
    await callback.answer("Чат отключён.")
    await callback.message.edit_text("✅ Чат отключён от вашего профиля.")


# ============================================================
# GIVEAWAYS — HELPERS
# ============================================================

def participant_label(person: dict) -> str:
    """person — dict с ключами user_id / username."""
    username = person["username"]
    if username:
        return "@" + username if " " not in username else username
    return str(person["user_id"])


def get_open_giveaway(giveaway_id: int) -> Giveaway | None:
    """Розыгрыш, в котором ещё можно участвовать."""
    g = giveaways.get(giveaway_id)
    if not g or not g.active or g.ends_at <= time.time():
        return None
    return g


def register_participant(g: Giveaway, user) -> tuple[bool, int]:
    added = user.id not in g.participants
    if added:
        g.participants[user.id] = user.username or user.full_name
    return added, len(g.participants)


def conditions_text(channels: list[dict], captcha: bool) -> str:
    if not channels and not captcha:
        return ""
    lines = ["\n📋 <b>Условия участия:</b>"]
    for ch in channels:
        lines.append(f"• Подписаться на {escape(ch['title'] or str(ch['chat_id']))}")
    if captcha:
        lines.append("• Пройти проверку «я не робот»")
    return "\n".join(lines) + "\n"


def giveaway_keyboard(giveaway_id: int, channels: list[dict] | None = None):
    rows = []
    for ch in channels or []:
        if ch.get("link"):
            rows.append([InlineKeyboardButton(
                text=f"📢 {(ch['title'] or 'Канал')[:30]}", url=ch["link"]
            )])
    rows.append([InlineKeyboardButton(text="🎉 Участвовать", callback_data=f"join:{giveaway_id}")])
    rows.append([InlineKeyboardButton(
        text="🎰 Проверить результаты",
        url=f"https://t.me/{BOT_USERNAME}?start=results_{giveaway_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------
# Каналы-условия (подписки)
# ------------------------------------------------------------

async def resolve_channels(bot: Bot, raw: str) -> tuple[list[dict], str | None]:
    tokens = [t for t in re.split(r"[\s,;]+", raw.strip()) if t]
    if not tokens:
        return [], "Отправьте хотя бы один канал или нажмите «Пропустить»."
    if len(tokens) > MAX_CHANNELS:
        return [], f"Максимум {MAX_CHANNELS} каналов."

    me = await bot.get_me()
    result: list[dict] = []
    seen: set[int] = set()

    for token in tokens:
        name = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", token).lstrip("@")
        ref = int(name) if re.fullmatch(r"-?\d+", name) else "@" + name

        try:
            chat = await bot.get_chat(ref)
        except TelegramAPIError:
            return [], f"❌ Не нашёл канал «{token}». Проверьте @username."

        if chat.type not in ("channel", "supergroup", "group"):
            return [], f"❌ «{token}» — это не канал и не группа."

        try:
            me_member = await bot.get_chat_member(chat.id, me.id)
        except TelegramAPIError:
            me_member = None
        if not me_member or me_member.status not in ("administrator", "creator"):
            return [], (
                f"❌ Сделайте бота администратором в «{chat.title or token}» — "
                "иначе он не сможет проверять подписку."
            )

        if chat.id in seen:
            continue
        seen.add(chat.id)

        link = f"https://t.me/{chat.username}" if chat.username else None
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat.id)
            except TelegramAPIError:
                link = None

        result.append({"chat_id": chat.id, "title": chat.title or str(chat.id), "link": link})

    return result, None


async def missing_subscriptions(bot: Bot, g: Giveaway, user_id: int) -> list[dict]:
    missing = []
    for ch in g.channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            ok = member.status in ("member", "administrator", "creator") or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
        except TelegramAPIError:
            ok = False
        if not ok:
            missing.append(ch)
    return missing


# ------------------------------------------------------------
# Капча «2 + 7»
# ------------------------------------------------------------

def make_captcha() -> tuple[str, int, list[int]]:
    a, b = random.randint(1, 9), random.randint(1, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(max(1, answer + random.randint(-5, 5)))
    options = list(options)
    random.shuffle(options)
    return f"{a} + {b}", answer, options


def captcha_markup(giveaway_id: int, options: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(o), callback_data=f"cap:{giveaway_id}:{o}")
        for o in options
    ]])


def new_captcha(user_id: int, giveaway_id: int) -> tuple[str, InlineKeyboardMarkup]:
    question, answer, options = make_captcha()
    captcha_pending[(user_id, giveaway_id)] = (answer, time.time() + CAPTCHA_TTL)
    text = (
        "🤖 <b>Проверка «я не робот»</b>\n\n"
        f"Сколько будет <b>{question}</b>?\n"
        f"<i>У вас {CAPTCHA_TTL} сек.</i>"
    )
    return text, captcha_markup(giveaway_id, options)


async def start_join_flow(bot: Bot, user, dm_chat_id: int, giveaway_id: int):
    """Проверяет условия и регистрирует участника (в личке с ботом)."""
    g = get_open_giveaway(giveaway_id)
    if not g:
        await bot.send_message(dm_chat_id, "Розыгрыш уже завершён.")
        return

    if user.id in g.participants:
        await bot.send_message(dm_chat_id, "✅ Вы уже участвуете в этом розыгрыше.")
        return

    missing = await missing_subscriptions(bot, g, user.id)
    if missing:
        rows = [
            [InlineKeyboardButton(text=f"📢 {(ch['title'] or 'Канал')[:30]}", url=ch["link"])]
            for ch in missing if ch.get("link")
        ]
        rows.append([InlineKeyboardButton(text="🔄 Я подписался", callback_data=f"recheck:{giveaway_id}")])
        await bot.send_message(
            dm_chat_id,
            "❗ Чтобы участвовать, подпишитесь на каналы ниже, затем нажмите «Я подписался».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return

    if g.captcha:
        text, markup = new_captcha(user.id, giveaway_id)
        await bot.send_message(dm_chat_id, text, parse_mode="HTML", reply_markup=markup)
        return

    _, count = register_participant(g, user)
    await bot.send_message(dm_chat_id, f"✅ Вы участвуете! Участников: {count}")


@router.callback_query(F.data.startswith("recheck:"))
async def recheck_callback(callback: CallbackQuery):
    giveaway_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    await start_join_flow(callback.bot, callback.from_user, callback.message.chat.id, giveaway_id)


@router.callback_query(F.data.startswith("cap:"))
async def captcha_callback(callback: CallbackQuery):
    try:
        _, gid, value = callback.data.split(":")
        giveaway_id, value = int(gid), int(value)
    except ValueError:
        await callback.answer()
        return

    user = callback.from_user
    key = (user.id, giveaway_id)
    pending = captcha_pending.get(key)

    if not pending or pending[1] < time.time():
        captcha_pending.pop(key, None)
        await callback.answer("Время вышло. Нажмите «Участвовать» заново.", show_alert=True)
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        return

    if value != pending[0]:
        await callback.answer("❌ Неверно, попробуйте ещё раз.", show_alert=True)
        text, markup = new_captcha(user.id, giveaway_id)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramAPIError:
            pass
        return

    captcha_pending.pop(key, None)

    g = get_open_giveaway(giveaway_id)
    if not g:
        await callback.answer()
        await callback.message.edit_text("Розыгрыш уже завершён.")
        return

    if await missing_subscriptions(callback.bot, g, user.id):
        await callback.answer()
        await callback.message.edit_text("❗ Подписка на каналы не найдена. Нажмите «Участвовать» заново.")
        return

    _, count = register_participant(g, user)
    await callback.answer("✅ Готово!")
    await callback.message.edit_text(f"✅ Проверка пройдена, вы участвуете! Участников: {count}")


# ============================================================
# GIVEAWAYS — CREATION
# ============================================================

async def cancelled(message: Message, state: FSMContext) -> bool:
    if message.text == BTN_CANCEL:
        await state.clear()
        await send_home(message)
        return True
    return False


@router.message(F.text == BTN_GIVEAWAY)
async def giveaway_start(message: Message, state: FSMContext):
    chat_list = get_user_chats(message.from_user.id)

    if not chat_list:
        await message.answer(
            "Сначала подключите хотя бы один чат.",
            reply_markup=main_keyboard(),
        )
        return

    await state.set_state(GiveawayState.choosing_chat)
    await message.answer(
        "🎉 Выберите чат для розыгрыша:",
        reply_markup=chats_inline(chat_list, "givechat"),
    )


@router.callback_query(F.data.startswith("givechat:"))
async def giveaway_choose_chat(callback: CallbackQuery, state: FSMContext):
    chat_id = int(callback.data.split(":", 1)[1])

    if not is_connected(callback.from_user.id, chat_id):
        await callback.answer("Чат не подключён.", show_alert=True)
        return

    await state.update_data(chat_id=chat_id)
    await state.set_state(GiveawayState.waiting_minutes)
    await callback.answer()
    await callback.message.edit_text(
        "⏳ На сколько минут запустить розыгрыш?\n"
        "Например: <code>60</code>",
        parse_mode="HTML",
    )
    await callback.message.answer("Для отмены нажмите «❌ Отмена».", reply_markup=cancel_keyboard())


@router.message(GiveawayState.waiting_minutes)
async def giveaway_minutes(message: Message, state: FSMContext):
    if await cancelled(message, state):
        return

    if not (message.text or "").strip().isdigit():
        await message.answer("Введите количество минут числом.")
        return

    minutes = int(message.text)
    if not 1 <= minutes <= 10080:
        await message.answer("Допустимо от 1 до 10080 минут.")
        return

    await state.update_data(minutes=minutes)
    await state.set_state(GiveawayState.waiting_winners)
    await message.answer("🏆 Сколько будет победителей?")


@router.message(GiveawayState.waiting_winners)
async def giveaway_winners(message: Message, state: FSMContext):
    if await cancelled(message, state):
        return

    if not (message.text or "").strip().isdigit():
        await message.answer("Введите количество победителей числом.")
        return

    count = int(message.text)
    if not 1 <= count <= 50:
        await message.answer("Допустимо от 1 до 50 победителей.")
        return

    await state.update_data(winners=count)
    await state.set_state(GiveawayState.waiting_prize)
    await message.answer("🎁 Напишите приз.")


@router.message(GiveawayState.waiting_prize)
async def giveaway_prize(message: Message, state: FSMContext):
    if await cancelled(message, state):
        return

    prize = (message.text or "").strip()
    if not prize:
        await message.answer("Напишите название приза.")
        return

    await state.update_data(prize=prize)
    await state.set_state(GiveawayState.waiting_channels)
    await message.answer(
        "📢 <b>Обязательная подписка</b>\n\n"
        "Отправьте @username каналов (до "
        f"{MAX_CHANNELS}) через пробел — участники должны быть на них подписаны.\n"
        "⚠️ Бот должен быть <b>администратором</b> в этих каналах.\n\n"
        "Если подписка не нужна — нажмите «⏭ Пропустить».",
        parse_mode="HTML",
        reply_markup=skip_keyboard(),
    )


@router.message(GiveawayState.waiting_channels)
async def giveaway_channels(message: Message, state: FSMContext):
    if await cancelled(message, state):
        return

    channels: list[dict] = []
    if message.text != BTN_SKIP:
        channels, error = await resolve_channels(message.bot, message.text or "")
        if error:
            await message.answer(error)
            return

    await state.update_data(channels=channels)
    await state.set_state(GiveawayState.waiting_captcha)
    await message.answer(
        "🤖 Включить проверку «я не робот» (простой пример, например 2 + 7)?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data="gcap:1"),
            InlineKeyboardButton(text="❌ Нет", callback_data="gcap:0"),
        ]]),
    )


@router.callback_query(GiveawayState.waiting_captcha, F.data.startswith("gcap:"))
async def giveaway_captcha_choice(callback: CallbackQuery, state: FSMContext):
    captcha = callback.data.split(":", 1)[1] == "1"
    data = await state.get_data()
    await callback.answer()

    chat_id = data["chat_id"]
    minutes = data["minutes"]
    channels = data.get("channels", [])
    user_id = callback.from_user.id

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass

    # Проверяем права непосредственно перед созданием.
    if not await can_manage_chat(callback.bot, chat_id, user_id):
        await state.clear()
        await callback.message.answer(
            "⛔ Вы больше не являетесь администратором этого чата.",
            reply_markup=main_keyboard(),
        )
        return

    g = Giveaway(
        id=next(giveaway_ids),
        chat_id=chat_id,
        creator_id=user_id,
        prize=data["prize"],
        winners_count=data["winners"],
        ends_at=time.time() + minutes * 60,
        captcha=captcha,
        channels=channels,
    )
    giveaways[g.id] = g

    try:
        sent = await callback.bot.send_message(
            chat_id,
            f"🎉 <b>РОЗЫГРЫШ №{g.id}</b>\n\n"
            f"🎁 Приз: {escape(g.prize)}\n"
            f"🏆 Победителей: {g.winners_count}\n"
            f"⏳ Завершение через: {minutes} мин.\n"
            f"{conditions_text(channels, captcha)}\n"
            "Нажмите кнопку ниже, чтобы участвовать!",
            parse_mode="HTML",
            reply_markup=giveaway_keyboard(g.id, channels),
        )
        g.message_id = sent.message_id

        asyncio.create_task(finish_giveaway_later(callback.bot, g.id, minutes * 60))

        await state.clear()
        await callback.message.answer(
            f"✅ Розыгрыш №{g.id} опубликован в чате.\n"
            f"После завершения можно переизбрать победителей: <code>/rerol {g.id}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    except TelegramAPIError as e:
        g.active = False
        await state.clear()
        await callback.message.answer(
            f"❌ Не удалось опубликовать розыгрыш.\n{e}",
            reply_markup=main_keyboard(),
        )


# ============================================================
# GIVEAWAYS — JOIN
# ============================================================

@router.callback_query(F.data.startswith("join:"))
async def join_giveaway(callback: CallbackQuery):
    try:
        giveaway_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Не удалось определить розыгрыш.", show_alert=True)
        return

    g = get_open_giveaway(giveaway_id)
    if not g:
        await callback.answer("Розыгрыш уже завершён.", show_alert=True)
        return

    user = callback.from_user

    if user.id in g.participants:
        await callback.answer(
            f"Вы уже участвуете! Всего участников: {len(g.participants)}", show_alert=True
        )
        return

    missing = await missing_subscriptions(callback.bot, g, user.id)
    if missing:
        names = ", ".join(ch["title"] or str(ch["chat_id"]) for ch in missing)
        await callback.answer(f"❗ Сначала подпишитесь: {names}"[:200], show_alert=True)
        return

    if g.captcha:
        # Капчу решаем в личке с ботом — открываем её по deep-link.
        await callback.answer(url=f"https://t.me/{BOT_USERNAME}?start=join_{giveaway_id}")
        return

    _, count = register_participant(g, user)
    await callback.answer(f"✅ Вы участвуете! Участников: {count}", show_alert=True)


# ============================================================
# GIVEAWAYS — FINISH
# ============================================================

async def finish_giveaway_later(bot: Bot, giveaway_id: int, delay: float):
    """Автоматически завершает розыгрыш по таймеру."""
    await asyncio.sleep(delay)
    g = giveaways.get(giveaway_id)
    if not g or not g.active:
        return
    await finish_giveaway(bot, giveaway_id, animate=False)


async def finish_giveaway(
    bot: Bot, giveaway_id: int, animate: bool = False,
    result_chat_id: int | None = None
):
    """
    Победители выбираются один раз и сохраняются. Пользователь, нажавший
    «Проверить результаты», получает свою анимацию в личке.
    """
    g = giveaways.get(giveaway_id)
    if not g:
        if result_chat_id:
            await bot.send_message(result_chat_id, "❌ Розыгрыш не найден (возможно, бот перезапускался).")
        return

    if g.active and g.ends_at > time.time():
        if result_chat_id:
            await bot.send_message(
                result_chat_id,
                "⏳ Розыгрыш ещё идёт. Проверьте результаты после его окончания."
            )
        return

    users_count = len(g.participants)
    first_finalize = False

    if g.active:
        # Первое завершение (между проверкой и установкой нет await — гонки нет).
        first_finalize = True
        g.active = False
        if g.participants:
            n = min(g.winners_count, users_count)
            chance = n / users_count * 100.0
            for user_id, username in random.sample(list(g.participants.items()), n):
                g.winners.append({
                    "user_id": user_id,
                    "username": username,
                    "chance": chance,
                    "removed": False,
                })

    winners = [w for w in g.winners if not w["removed"]]
    target_chat = result_chat_id or g.chat_id

    if users_count == 0:
        message = (
            f"🎁 <b>Розыгрыш №{giveaway_id} завершён.</b>\n\n"
            "В этот раз никто не участвовал."
        )
        try:
            await bot.send_message(target_chat, message, parse_mode="HTML")
        except TelegramAPIError:
            pass
        if result_chat_id and result_chat_id != g.chat_id and first_finalize:
            try:
                await bot.send_message(g.chat_id, message, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return

    if not winners:
        try:
            await bot.send_message(
                target_chat,
                f"🏁 <b>Розыгрыш №{giveaway_id}</b>: победители сейчас не определены.",
                parse_mode="HTML",
            )
        except TelegramAPIError:
            pass
        return

    chance = float(winners[0]["chance"])
    winner_lines = [
        f"{i}. <b>{escape(participant_label(w))}</b> — вероятность выигрыша: "
        f"<b>{float(w['chance']):.1f}%</b>"
        for i, w in enumerate(winners, 1)
    ]
    result = (
        f"🏁 <b>РОЗЫГРЫШ №{giveaway_id} ЗАВЕРШЁН!</b>\n\n"
        f"🎁 Приз: {escape(g.prize)}\n"
        f"👥 Участников: <b>{users_count}</b>\n"
        f"🎯 Вероятность попасть в число победителей: <b>{chance:.1f}%</b>\n\n"
        "🏆 <b>Победитель(и):</b>\n" + "\n".join(winner_lines)
    )

    # Организатору — подсказка про переизбрание.
    if first_finalize:
        try:
            await bot.send_message(
                g.creator_id,
                f"🏁 Ваш розыгрыш №{giveaway_id} завершён.\n\n"
                + "\n".join(winner_lines)
                + f"\n\nПереизбрать всех: <code>/rerol {giveaway_id}</code>\n"
                  f"Заменить только 2-го победителя: <code>/rerol {giveaway_id} 2</code>",
                parse_mode="HTML",
            )
        except TelegramAPIError:
            pass

    if animate and result_chat_id:
        try:
            animation_message = await bot.send_message(
                result_chat_id,
                f"🎰 <b>РУЛЕТКА РОЗЫГРЫША №{giveaway_id}</b>\n\n"
                f"🎁 Приз: {escape(g.prize)}\n"
                f"👥 Участников: <b>{users_count}</b>\n\n"
                "🔴  ⚫  🟢  ⚫  🔴\n\n"
                "🎯 Рулетка запускается…",
                parse_mode="HTML",
            )
            pool = [{"user_id": uid, "username": name} for uid, name in g.participants.items()]
            random.shuffle(pool)
            frame_count = min(7, max(3, len(pool)))
            for frame in range(frame_count):
                label = participant_label(pool[frame % len(pool)])
                frame_text = (
                    f"🎰 <b>РУЛЕТКА РОЗЫГРЫША №{giveaway_id}</b>\n\n"
                    f"🎁 Приз: {escape(g.prize)}\n\n"
                    "🔴  ⚫  🟢  ⚫  🔴\n\n"
                    f"🎯 Выпадает: <b>{escape(label)}</b>\n"
                    "<i>Рулетка крутится…</i>"
                )
                try:
                    await animation_message.edit_text(frame_text, parse_mode="HTML")
                except TelegramAPIError:
                    pass
                await asyncio.sleep(0.65)
            try:
                await animation_message.edit_text(result, parse_mode="HTML")
            except TelegramAPIError:
                await bot.send_message(result_chat_id, result, parse_mode="HTML")
        except TelegramAPIError:
            try:
                await bot.send_message(result_chat_id, result, parse_mode="HTML")
            except TelegramAPIError:
                pass
        return

    # Автозавершение: публикуем результат в группе/канале.
    try:
        await bot.send_message(g.chat_id, result, parse_mode="HTML")
    except TelegramAPIError:
        pass


# ============================================================
# /rerol — ПЕРЕИЗБРАНИЕ ПОБЕДИТЕЛЕЙ
# ============================================================

def do_reroll(giveaway_id: int, user_id: int, index: int | None):
    """
    Возвращает (error, replaced, new_winners).
    index=None — заменить всех, иначе — только победителя №index.
    Прежние победители повторно не выбираются.
    """
    g = giveaways.get(giveaway_id)
    if not g or g.creator_id != user_id:
        return "Розыгрыш не найден или вы не его организатор.", None, None
    if g.active:
        return "Розыгрыш ещё не завершён.", None, None

    current = [w for w in g.winners if not w["removed"]]
    if not current:
        return "Нет победителей для замены.", None, None
    if index is not None and not 1 <= index <= len(current):
        return f"Номер победителя должен быть от 1 до {len(current)}.", None, None

    used = {w["user_id"] for w in g.winners}
    pool = [(uid, name) for uid, name in g.participants.items() if uid not in used]
    if not pool:
        return "Не осталось участников, которых можно выбрать.", None, None

    targets = [current[index - 1]] if index else current
    k = min(len(targets), len(pool))
    picked = random.sample(pool, k)
    chance = k / len(pool) * 100.0
    replaced = targets[:k]

    for old in replaced:
        old["removed"] = True

    new_winners = []
    for uid, name in picked:
        winner = {"user_id": uid, "username": name, "chance": chance, "removed": False}
        g.winners.append(winner)
        new_winners.append(winner)

    return None, replaced, new_winners


@router.message(Command("rerol", "reroll"))
async def rerol_command(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if not parts or not parts[0].isdigit():
        mine = sorted(
            (g for g in giveaways.values()
             if g.creator_id == message.from_user.id and not g.active),
            key=lambda g: g.id, reverse=True,
        )[:10]
        lst = "\n".join(f"• №{g.id} — {escape(g.prize)}" for g in mine) or "У вас нет завершённых розыгрышей."
        await message.answer(
            "🔄 <b>Переизбрание победителей</b>\n\n"
            "<code>/rerol ID</code> — заменить всех победителей\n"
            "<code>/rerol ID N</code> — заменить только победителя №N\n\n"
            "Прежние победители повторно не выбираются.\n\n"
            f"<b>Ваши завершённые розыгрыши:</b>\n{lst}",
            parse_mode="HTML",
        )
        return

    giveaway_id = int(parts[0])
    index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    error, replaced, new_winners = do_reroll(giveaway_id, message.from_user.id, index)
    if error:
        await message.answer(f"⛔ {error}")
        return

    g = giveaways[giveaway_id]
    announce = (
        f"🔄 <b>Переизбрание в розыгрыше №{giveaway_id}</b>\n\n"
        f"🎁 Приз: {escape(g.prize)}\n\n"
        "❌ <b>Выбыли:</b> " + ", ".join(escape(participant_label(w)) for w in replaced) + "\n"
        "🏆 <b>Новые победители:</b>\n"
        + "\n".join(
            f"{i}. <b>{escape(participant_label(w))}</b> — вероятность: <b>{w['chance']:.1f}%</b>"
            for i, w in enumerate(new_winners, 1)
        )
    )
    try:
        await message.bot.send_message(g.chat_id, announce, parse_mode="HTML")
        await message.answer("✅ Победители переизбраны, результат опубликован в чате.")
    except TelegramAPIError:
        await message.answer(
            "✅ Победители переизбраны, но опубликовать в чат не удалось:\n\n" + announce,
            parse_mode="HTML",
        )


# ============================================================
# COMMAND COMPATIBILITY
# ============================================================

@router.message(Command("addword"))
async def legacy_addword(message: Message):
    if not await can_manage(message):
        await message.answer("⛔ Нет доступа.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Используйте кнопку «➕ Добавить слово».")
        return

    word = normalize(args[1])
    if not word:
        await message.answer("Введите непустое слово.")
        return

    banned_words.setdefault(message.from_user.id, set()).add(word)
    await message.answer(f"✅ Добавлено: {word}")


@router.message(Command("delword"))
async def legacy_delword(message: Message):
    if not await can_manage(message):
        await message.answer("⛔ Нет доступа.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Используйте кнопку «🗑 Удалить слово».")
        return

    word = normalize(args[1])
    words = banned_words.get(message.from_user.id, set())
    existed = word in words
    words.discard(word)

    await message.answer("✅ Удалено." if existed else "Такого слова нет в вашем списке.")


# ============================================================
# AUTO MODERATION
# ============================================================

async def delete_later(message: Message, delay: int = WARNING_DELETE_SECONDS):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def notify_connected_admins(
    bot: Bot,
    chat_id: int,
    chat_title: str,
    username: str,
    user_id: int,
    bad_word: str,
    original_text: str,
):
    admins = [uid for uid, chat_ids in connections.items() if chat_id in chat_ids]

    report = (
        "🚨 <b>Нарушение правил</b>\n\n"
        f"Группа: {escape(chat_title)}\n"
        f"Пользователь: {escape(username)}\n"
        f"ID: {user_id}\n"
        f"Слово: {escape(bad_word)}\n"
        f"Сообщение: {escape(original_text[:1000])}"
    )

    for admin_id in admins:
        try:
            await bot.send_message(admin_id, report, parse_mode="HTML")
        except TelegramAPIError:
            pass


# ============================================================
# АВТОПОДКЛЮЧЕНИЕ ЧАТА — БЕЗ /start
# ============================================================

@router.my_chat_member()
async def bot_chat_member_updated(event):
    chat = event.chat
    actor = event.from_user
    new_status = event.new_chat_member.status

    if chat.type not in ("group", "supergroup", "channel") or not actor:
        return

    if new_status in ("left", "kicked"):
        disconnect_user_from_chat(actor.id, chat.id)
        return

    if new_status == "member" and chat.type != "channel":
        try:
            await event.bot.send_message(
                actor.id,
                f"⚠️ Бот добавлен в «{chat.title or chat.id}», "
                "но для модерации ему нужны права администратора "
                "с правом удаления сообщений."
            )
        except TelegramAPIError:
            pass
        return

    if new_status == "administrator":
        if not await is_group_admin(event.bot, chat.id, actor.id):
            return

        required_permission = (
            getattr(event.new_chat_member, "can_post_messages", False)
            if chat.type == "channel"
            else getattr(event.new_chat_member, "can_delete_messages", False)
        )

        if not required_permission:
            try:
                explanation = (
                    "У бота нет права публиковать сообщения. Включите право «Публикация сообщений»."
                    if chat.type == "channel"
                    else "У бота нет права удалять сообщения. Включите это право."
                )
                await event.bot.send_message(
                    actor.id,
                    f"⚠️ Бот назначен администратором «{chat.title or chat.id}».\n{explanation}"
                )
            except TelegramAPIError:
                pass
            return

        connect_user_to_chat(
            actor.id,
            chat.id,
            chat.title or str(chat.id),
            chat.type,
        )

        try:
            await event.bot.send_message(
                actor.id,
                f"✅ <b>Чат подключён!</b>\n\n"
                f"💬 {escape(chat.title or str(chat.id))}\n"
                + ("📢 Бот подключён к каналу и может публиковать посты/розыгрыши.\n\n"
                   if chat.type == "channel" else "🛡 Модерация включена.\n\n")
                + "Теперь настройте слова через кнопку "
                "«➕ Добавить слово».",
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
        except TelegramAPIError:
            pass


@router.message()
async def moderate(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    user = message.from_user
    if not user or user.is_bot:
        return

    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
        return

    if await is_group_admin(message.bot, message.chat.id, user.id):
        return

    words = get_chat_words(message.chat.id)
    if not words:
        return

    bad_word = find_bad_word(text, words)
    if not bad_word:
        return

    try:
        await message.delete()
    except TelegramAPIError:
        return

    username = f"@{user.username}" if user.username else user.full_name

    try:
        warning = await message.answer(
            f"⚠️ {escape(username)}, ваше сообщение было удалено.\n"
            "Причина: нарушение правил модерации.",
            parse_mode="HTML",
        )
        asyncio.create_task(delete_later(warning))
    except TelegramAPIError:
        pass

    asyncio.create_task(
        notify_connected_admins(
            bot=message.bot,
            chat_id=message.chat.id,
            chat_title=message.chat.title or str(message.chat.id),
            username=username,
            user_id=user.id,
            bad_word=bad_word,
            original_text=text,
        )
    )


# ============================================================
# START
# ============================================================

async def main():
    bot = Bot(BOT_TOKEN)
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username or ""
    dp = Dispatcher()
    dp.include_router(router)

    print("Бот модерации запущен (без базы данных, данные хранятся в памяти).")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
