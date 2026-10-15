#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
  Анонимный Telegram-бот для заказа дипломных работ (ЮУТУ)
================================================================
  Функционал:
    • Анонимные ID для каждого клиента (#AB1234)
    • Меню услуг с inline-кнопками
    • Многоэтапная форма заказа (тема → услуги → срок → комментарии)
    • Отправка заказа администратору @san3101cha
    • Двусторонняя анонимная переписка клиент ⇄ админ
    • Обмен файлами в обе стороны (анонимно)
    • Условия оплаты 50% / 50%
    • Рассылки, список заказов, статусы
================================================================
"""

import logging
import sqlite3
import random
import string
import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ReplyParameters,
    ForceReply,
    Message,
    User,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ================================================================
#  КОНФИГУРАЦИЯ
# ================================================================

BOT_TOKEN = "8322621739:AAHjRnauWSUDpLNe7NSh5DZxO2gLTZjfSCs"
ADMIN_USERNAME = "san3101cha"        # без @ — админ определяется по этому юзернейму
ADMIN_IDS: set[int] = set()         # chat_id админов (заполняется при /start)

PAYMENT_CARD = "2202208856880381"
PAYMENT_BANK = "Сбербанк"

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_database.db")

# Логирование
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("diploma-bot")

# ================================================================
#  КАТАЛОГ УСЛУГ
# ================================================================

SERVICES: Dict[str, str] = {
    "diploma":         "🎓 Дипломная работа",
    "coursework":      "📝 Курсовая работа",
    "referat":         "📄 Реферат",
    "report_practice": "📈 Отчёт о практике",
    "presentation":    "📊 Презентация",
    "defense_speech":  "🎤 Защитное слово",
    "formatting":      "🎨 Форматирование текста",
    "antiplagiat":     "🔍 Проверка на антиплагиат",
    "review":          "📋 Рецензия",
    "consultation":    "💬 Консультация",
}

SERVICE_PRICES: Dict[str, str] = {
    "diploma":         "от 8 000 ₽",
    "coursework":      "от 3 000 ₽",
    "referat":         "от 1 200 ₽",
    "report_practice": "от 2 000 ₽",
    "presentation":    "от 1 500 ₽",
    "defense_speech":  "от 800 ₽",
    "formatting":      "от 500 ₽",
    "antiplagiat":     "от 700 ₽",
    "review":          "от 1 000 ₽",
    "consultation":    "от 500 ₽",
}

# ================================================================
#  СОСТОЯНИЯ (Conversation)
# ================================================================

STATE_TOPIC      = 1
STATE_DEADLINE   = 2
STATE_COMMENTS   = 3
STATE_CONTACT    = 4
STATE_CONFIRM    = 5

# Пользовательский "чат с менеджером" — отдельная Conversation
CHAT_STATE_ACTIVE = 10

# Режим ожидания файла от админа
ADMIN_WAIT_FILE  = 20

# ================================================================
#  БАЗА ДАННЫХ
# ================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                anonymous_id  TEXT UNIQUE NOT NULL,
                username      TEXT,
                first_name    TEXT,
                registered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                anonymous_id  TEXT NOT NULL,
                topic         TEXT NOT NULL,
                services      TEXT NOT NULL,
                deadline      TEXT,
                comments      TEXT,
                contact       TEXT,
                status        TEXT DEFAULT 'new',
                created_at    TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS admin_chats (
                chat_id   INTEGER PRIMARY KEY,
                added_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_link (
                -- связь: сообщение админа → оригинальный chat_id клиента
                admin_msg_id   INTEGER PRIMARY KEY,
                user_chat_id   INTEGER NOT NULL,
                anonymous_id   TEXT NOT NULL,
                direction      TEXT NOT NULL  -- 'user_to_admin' or 'admin_to_user'
            );

            CREATE TABLE IF NOT EXISTS admin_state (
                admin_chat_id INTEGER PRIMARY KEY,
                state         TEXT NOT NULL,
                target_anon   TEXT,
                updated_at    TEXT NOT NULL
            );
        """)
        c.commit()


def generate_anonymous_id() -> str:
    """Уникальный анонимный ID вида #AB1234."""
    with get_db() as c:
        while True:
            letters = "".join(random.choices(string.ascii_uppercase, k=2))
            digits = "".join(random.choices(string.digits, k=4))
            anon_id = f"#{letters}{digits}"
            if not c.execute(
                "SELECT 1 FROM users WHERE anonymous_id=?", (anon_id,)
            ).fetchone():
                return anon_id


def register_user(user: User) -> str:
    """Регистрация нового клиента. Возвращает его anonymous_id."""
    with get_db() as c:
        row = c.execute(
            "SELECT anonymous_id FROM users WHERE user_id=?", (user.id,)
        ).fetchone()
        if row:
            return row["anonymous_id"]
        anon_id = generate_anonymous_id()
        c.execute(
            """INSERT INTO users
               (user_id, anonymous_id, username, first_name, registered_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user.id, anon_id, user.username or "", user.first_name or "",
             datetime.utcnow().isoformat())
        )
        c.commit()
        return anon_id


def get_user_id_by_anon(anon_id: str) -> Optional[int]:
    with get_db() as c:
        row = c.execute(
            "SELECT user_id FROM users WHERE anonymous_id=?", (anon_id,)
        ).fetchone()
        return row["user_id"] if row else None


def get_anon_by_user_id(user_id: int) -> Optional[str]:
    with get_db() as c:
        row = c.execute(
            "SELECT anonymous_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["anonymous_id"] if row else None


def add_admin_chat(chat_id: int) -> None:
    with get_db() as c:
        c.execute(
            "INSERT OR IGNORE INTO admin_chats (chat_id, added_at) VALUES (?, ?)",
            (chat_id, datetime.utcnow().isoformat())
        )
        c.commit()


def get_admin_chats() -> List[int]:
    with get_db() as c:
        rows = c.execute("SELECT chat_id FROM admin_chats").fetchall()
        return [r["chat_id"] for r in rows]


def is_admin(chat_id: int) -> bool:
    return chat_id in ADMIN_IDS


def save_order(user_id: int, anon_id: str, topic: str, services: list,
               deadline: str, comments: str, contact: str) -> int:
    with get_db() as c:
        cur = c.execute(
            """INSERT INTO orders
               (user_id, anonymous_id, topic, services, deadline,
                comments, contact, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
            (user_id, anon_id, topic, json.dumps(services, ensure_ascii=False),
             deadline, comments, contact, datetime.utcnow().isoformat())
        )
        c.commit()
        return cur.lastrowid


def update_order_status(order_id: int, status: str) -> None:
    with get_db() as c:
        c.execute(
            "UPDATE orders SET status=? WHERE order_id=?",
            (status, order_id)
        )
        c.commit()


def get_recent_orders(limit: int = 10) -> list:
    with get_db() as c:
        return c.execute(
            "SELECT * FROM orders ORDER BY order_id DESC LIMIT ?",
            (limit,)
        ).fetchall()


def get_user_orders(user_id: int) -> list:
    with get_db() as c:
        return c.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY order_id DESC",
            (user_id,)
        ).fetchall()


def save_chat_link(admin_msg_id: int, user_chat_id: int,
                   anon_id: str, direction: str) -> None:
    with get_db() as c:
        c.execute(
            """INSERT OR REPLACE INTO chat_link
               (admin_msg_id, user_chat_id, anonymous_id, direction)
               VALUES (?, ?, ?, ?)""",
            (admin_msg_id, user_chat_id, anon_id, direction)
        )
        c.commit()


def get_chat_link(admin_msg_id: int) -> Optional[dict]:
    with get_db() as c:
        row = c.execute(
            "SELECT * FROM chat_link WHERE admin_msg_id=?",
            (admin_msg_id,)
        ).fetchone()
        return dict(row) if row else None


def set_admin_state(admin_chat_id: int, state: str,
                    target_anon: Optional[str] = None) -> None:
    with get_db() as c:
        c.execute(
            """INSERT OR REPLACE INTO admin_state
               (admin_chat_id, state, target_anon, updated_at)
               VALUES (?, ?, ?, ?)""",
            (admin_chat_id, state, target_anon, datetime.utcnow().isoformat())
        )
        c.commit()


def get_admin_state(admin_chat_id: int) -> Optional[dict]:
    with get_db() as c:
        row = c.execute(
            "SELECT * FROM admin_state WHERE admin_chat_id=?",
            (admin_chat_id,)
        ).fetchone()
        return dict(row) if row else None


def clear_admin_state(admin_chat_id: int) -> None:
    with get_db() as c:
        c.execute("DELETE FROM admin_state WHERE admin_chat_id=?",
                  (admin_chat_id,))
        c.commit()


# ================================================================
#  КЛАВИТУРЫ
# ================================================================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Заказать работу", callback_data="new_order")],
        [InlineKeyboardButton("📦 Мои заказы",       callback_data="my_orders"),
         InlineKeyboardButton("💳 Оплата",           callback_data="payment")],
        [InlineKeyboardButton("📋 Правила",          callback_data="rules"),
         InlineKeyboardButton("💬 Чат с менеджером",  callback_data="chat_start")],
        [InlineKeyboardButton("ℹ️ Помощь",           callback_data="help")],
    ])


def services_keyboard(selected: set) -> InlineKeyboardMarkup:
    rows = []
    for key, label in SERVICES.items():
        mark = "✅ " if key in selected else "▫️ "
        price = SERVICE_PRICES.get(key, "")
        rows.append([InlineKeyboardButton(
            f"{mark}{label}  ·  {price}",
            callback_data=f"svc_{key}"
        )])
    rows.append([
        InlineKeyboardButton("✅ Подтвердить выбор",
                             callback_data="svc_done"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    ])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить заказ", callback_data="confirm_order"),
         InlineKeyboardButton("❌ Отмена",            callback_data="cancel")],
        [InlineKeyboardButton("🔙 В меню",           callback_data="back_to_menu")],
    ])


def chat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔚 Завершить чат", callback_data="chat_end")],
        [InlineKeyboardButton("🔙 В меню",         callback_data="back_to_menu")],
    ])


def order_actions_keyboard(order_id: int, anon_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Ответить клиенту",
                              callback_data=f"admin_reply_{anon_id}"),
         InlineKeyboardButton("📎 Отправить файл",
                              callback_data=f"admin_file_{anon_id}")],
        [InlineKeyboardButton("💰 Запросить предоплату",
                              callback_data=f"admin_prepay_{order_id}"),
         InlineKeyboardButton("✅ Взять в работу",
                              callback_data=f"admin_take_{order_id}")],
        [InlineKeyboardButton("🏁 Завершить заказ",
                              callback_data=f"admin_finish_{order_id}"),
         InlineKeyboardButton("❌ Отклонить",
                              callback_data=f"admin_reject_{order_id}")],
    ])


# ================================================================
#  ТЕКСТОВЫЕ ШАБЛОНЫ
# ================================================================

WELCOME_TEXT = (
    "👋 *Добро пожаловать в сервис заказа дипломных работ для студентов ЮУТУ!*\n\n"
    "🤖 Я — анонимный бот-помощник. Я не запрашиваю ваши личные данные, "
    "общение ведётся через уникальный *анонимный ID*.\n\n"
    "🆔 Ваш анонимный ID: `{anon_id}`\n"
    "🔒 Все переписки и файлы видны только мне (менеджеру) и вам.\n\n"
    "Выберите действие в меню ниже 👇"
)

RULES_TEXT = (
    "📋 *Правила сервиса и гарантии*\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "1️⃣ *Оплата 50% / 50%*\n"
    "   • Половина суммы — предоплата после согласования темы и стоимости.\n"
    "   • Вторая половина — после готовности работы и предварительного просмотра.\n"
    "   • До старта работ заказ не берётся в разработку.\n\n"
    "2️⃣ *Анонимность*\n"
    "   • Ваш Telegram-аккаунт скрыт, общение через ID вида #AB1234.\n"
    "   • Файлы передаются через бота без раскрытия контактов.\n\n"
    "3️⃣ *Сроки*\n"
    "   • Сроки фиксируются в заказе и подтверждаются обеими сторонами.\n"
    "   • Срочные заказы (менее 3 дней) — доплата +30% к прайсу.\n\n"
    "4️⃣ *Доработки*\n"
    "   • Бесплатные правки включены до защиты работы.\n"
    "   • Доработки по новым требованиям — по согласованию.\n\n"
    "5️⃣ *Возвраты*\n"
    "   • Если работа не начата — возврат предоплаты 100%.\n"
    "   • После старта — возврат обсуждается индивидуально.\n\n"
    "6️⃣ *Авторство*\n"
    "   • Работа носит вспомогательный характер.\n"
    "   • Использование — ответственность заказчика.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "Согласие с правилами — автоматически при оформлении заказа."
)

PAYMENT_TEXT = (
    "💳 *Условия оплаты*\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🏦 *Банк:* {bank}\n"
    "💳 *Номер карты:* `{card}`\n\n"
    "📊 *Схема:*\n"
    "   • Предоплата *50%* после согласования темы и стоимости.\n"
    "   • Остаток *50%* после готовности работы.\n\n"
    "📲 После оплаты пришлите чек в чат с менеджером "
    "(кнопка «💬 Чат с менеджером») — я зачту платёж и начну работу."
)

HELP_TEXT = (
    "ℹ️ *Помощь по боту*\n\n"
    "• *📝 Заказать работу* — заполните анкету заказа.\n"
    "• *📦 Мои заказы* — список и статусы ваших заказов.\n"
    "• *💳 Оплата* — реквизиты и правила оплаты.\n"
    "• *📋 Правила* — гарантии, сроки, возвраты.\n"
    "• *💬 Чат с менеджером* — анонимная переписка с автором.\n\n"
    "По техническим вопросам — напишите в чат с менеджером."
)

ORDER_SUMMARY = (
    "🧾 *Проверьте ваш заказ*\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🆔 ID: `{anon_id}`\n"
    "📚 *Тема:* {topic}\n"
    "🛠 *Услуги:*\n{services}\n"
    "📅 *Срок сдачи:* {deadline}\n"
    "📝 *Комментарии:* {comments}\n"
    "📞 *Контакт:* {contact}\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "После подтверждения заказ уйдёт менеджеру. "
    "Вам придёт расчёт стоимости и инструкция по оплате."
)

# ================================================================
#  ОБРАБОТЧИКИ — КЛИЕНТЫ
# ================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — главное меню."""
    user = update.effective_user
    anon_id = register_user(user)

    # Если это админ (по username) — добавляем в список
    if user.username == ADMIN_USERNAME:
        add_admin_chat(user.id)
        ADMIN_IDS.add(user.id)
        await update.message.reply_text(
            "👑 *Вы авторизованы как администратор!*\n\n"
            "Вам будут приходить все новые заказы и сообщения от клиентов.\n\n"
            "*Команды администратора:*\n"
            "• `/reply ID текст` — ответить клиенту\n"
            "• `/sendfile ID` — отправить файл (следующим сообщением)\n"
            "• `/broadcast текст` — рассылка всем клиентам\n"
            "• `/orders` — последние 10 заказов\n"
            "• `/find ID` — детали конкретного заказа\n"
            "• Ответьте на любое пересланное сообщение — ответ уйдёт клиенту.\n"
            "• Перешлите файл (как ответ) — он уйдёт клиенту.",
            parse_mode=ParseMode.MARKDOWN
        )

    await update.message.reply_text(
        WELCOME_TEXT.format(anon_id=anon_id),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена любого действия — возврат в меню."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Действие отменено.", reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text(
        "🏠 *Главное меню*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Центральный обработчик inline-кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    anon_id = get_anon_by_user_id(user.id) or register_user(user)

    # ----- Отмена -----
    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text(
            "❌ Заказ отменён.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 В меню", callback_data="back_to_menu")
            ]])
        )
        return ConversationHandler.END

    if data == "back_to_menu":
        await query.edit_message_text(
            WELCOME_TEXT.format(anon_id=anon_id),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    # ----- Меню: Новый заказ -----
    if data == "new_order":
        context.user_data.clear()
        context.user_data["selected_services"] = set()
        await query.edit_message_text(
            "📝 *Оформление нового заказа*\n\n"
            "Шаг 1/5: Укажите *тему работы* (можно не одну, "
            "если хотите — приложите методичку позже в чате).\n\n"
            "Пример: *«Разработка мобильного приложения для учёта продаж»*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]])
        )
        return STATE_TOPIC

    # ----- Меню: Мои заказы -----
    if data == "my_orders":
        orders = get_user_orders(user.id)
        if not orders:
            await query.edit_message_text(
                "📦 У вас пока нет заказов.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")
                ]])
            )
            return ConversationHandler.END
        text = "📦 *Ваши заказы:*\n\n"
        status_emoji = {
            "new": "🆕", "in_progress": "⚙️", "waiting_payment": "💳",
            "done": "✅", "rejected": "❌"
        }
        for o in orders:
            text += (
                f"{status_emoji.get(o['status'], '•')} *#{o['order_id']}* "
                f"({o['anonymous_id']}) — {o['status']}\n"
                f"   📚 {o['topic'][:60]}\n"
                f"   📅 Создан: {o['created_at'][:10]}\n\n"
            )
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")
            ]])
        )
        return ConversationHandler.END

    # ----- Меню: Оплата -----
    if data == "payment":
        await query.edit_message_text(
            PAYMENT_TEXT.format(bank=PAYMENT_BANK, card=PAYMENT_CARD),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")
            ]])
        )
        return ConversationHandler.END

    # ----- Меню: Правила -----
    if data == "rules":
        await query.edit_message_text(
            RULES_TEXT, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")
            ]])
        )
        return ConversationHandler.END

    # ----- Меню: Чат с менеджером -----
    if data == "chat_start":
        context.user_data["chat_mode"] = True
        await query.edit_message_text(
            "💬 *Чат с менеджером*\n\n"
            "Все ваши сообщения (текст, фото, документы) будут пересланы "
            "менеджеру анонимно.\n\n"
            "🆔 Ваш ID в этом чате: `" + anon_id + "`\n"
            "Нажмите «🔚 Завершить чат», чтобы вернуться в меню.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=chat_keyboard()
        )
        return CHAT_STATE_ACTIVE

    if data == "chat_end":
        context.user_data.pop("chat_mode", None)
        await query.edit_message_text(
            "🔚 Чат завершён. Менеджер сможет ответить вам позже.",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    # ----- Меню: Помощь -----
    if data == "help":
        await query.edit_message_text(
            HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")
            ]])
        )
        return ConversationHandler.END

    # ----- Выбор услуги (мультиселект) -----
    if data.startswith("svc_") and data != "svc_done":
        svc_key = data[4:]
        selected: set = context.user_data.get("selected_services", set())
        if svc_key in selected:
            selected.discard(svc_key)
        else:
            selected.add(svc_key)
        context.user_data["selected_services"] = selected
        await query.edit_message_reply_markup(
            reply_markup=services_keyboard(selected)
        )
        return STATE_TOPIC  # остаёмся в выборе услуг (state должен быть STATE_TOPIC или собственное)

    if data == "svc_done":
        selected = context.user_data.get("selected_services", set())
        if not selected:
            await query.answer(
                "⚠️ Выберите хотя бы одну услугу!", show_alert=True
            )
            return STATE_TOPIC
        context.user_data["services"] = list(selected)
        await query.edit_message_text(
            "📅 Шаг 3/5: *Укажите срок сдачи.*\n\n"
            "Пример: *15 июня 2026* или *через 2 недели*.\n"
            "Чем точнее срок, тем точнее расчёт стоимости.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]])
        )
        return STATE_DEADLINE

    # ----- Подтверждение заказа -----
    if data == "confirm_order":
        ud = context.user_data
        topic = ud.get("topic", "—")
        services_list = ud.get("services", [])
        services_str = "\n".join(
            f"   • {SERVICES[k]}" for k in services_list
        ) or "—"
        deadline = ud.get("deadline", "—")
        comments = ud.get("comments", "—")
        contact = ud.get("contact", "—")

        order_id = save_order(
            user_id=user.id, anon_id=anon_id, topic=topic,
            services=services_list, deadline=deadline,
            comments=comments, contact=contact
        )
        ud["last_order_id"] = order_id

        # Уведомление админу
        admin_text = (
            f"🔔 *НОВЫЙ ЗАКАЗ #{order_id}*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Анонимный ID: `{anon_id}`\n"
            f"📚 Тема: {topic}\n"
            f"🛠 Услуги:\n{services_str}\n"
            f"📅 Срок: {deadline}\n"
            f"📝 Комментарии: {comments}\n"
            f"📞 Контакт: {contact}\n"
            f"🕒 Создан: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        for admin_chat in get_admin_chats():
            try:
                sent = await context.bot.send_message(
                    chat_id=admin_chat,
                    text=admin_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=order_actions_keyboard(order_id, anon_id)
                )
                # Сохраняем link для ответов
                save_chat_link(sent.message_id, user.id, anon_id, "user_to_admin")
            except Exception as e:
                logger.error(f"Не удалось отправить заказ админу {admin_chat}: {e}")

        # Подтверждение клиенту
        await query.edit_message_text(
            "✅ *Заказ успешно отправлен менеджеру!*\n\n"
            f"🆔 Номер заказа: *#{order_id}*\n"
            f"🆔 Ваш анонимный ID: `{anon_id}`\n\n"
            "Менеджер свяжется с вами в ближайшее время через бота.\n"
            "Вам придёт расчёт стоимости и инструкции по оплате (50% предоплата).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    return ConversationHandler.END


# ----- Этапы формы заказа -----

async def on_topic_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: Тема работы → переход к выбору услуг."""
    # Если услуги уже выбраны (это повторный ввод)
    topic = update.message.text.strip()
    if len(topic) < 5:
        await update.message.reply_text(
            "⚠️ Тема слишком короткая. Опишите подробнее (минимум 5 символов)."
        )
        return STATE_TOPIC
    context.user_data["topic"] = topic
    await update.message.reply_text(
        "🛠 Шаг 2/5: *Выберите нужные услуги*.\n"
        "Можно выбрать несколько. Нажмите на услугу, чтобы отметить/снять выделение.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=services_keyboard(context.user_data.get(
            "selected_services", set()))
    )
    # Остаёмся в STATE_TOPIC, но фактически выбираем услуги через кнопки
    # Обработка выбора идёт в on_callback
    return STATE_TOPIC  # не выходим, ждём svc_done


async def on_deadline_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: Срок сдачи."""
    deadline = update.message.text.strip()
    if len(deadline) < 3:
        await update.message.reply_text("⚠️ Укажите срок подробнее.")
        return STATE_DEADLINE
    context.user_data["deadline"] = deadline
    await update.message.reply_text(
        "📝 Шаг 4/5: *Дополнительные комментарии.*\n\n"
        "Например: методические требования, язык работы, "
        "особенности оформления, антиплагиат, объём страниц и т.д.\n"
        "Или /skip, чтобы пропустить.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comments"),
            InlineKeyboardButton("❌ Отмена",     callback_data="cancel"),
        ]])
    )
    return STATE_COMMENTS


async def on_comments_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4: Комментарии → Контакт."""
    context.user_data["comments"] = update.message.text.strip()
    await _ask_contact(update, context)
    return STATE_CONTACT


async def on_skip_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропуск комментариев."""
    query = update.callback_query
    await query.answer()
    context.user_data["comments"] = "—"
    await _ask_contact_update(query, context)
    return STATE_CONTACT


async def on_contact_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 5: Контакт → Подтверждение."""
    context.user_data["contact"] = update.message.text.strip()
    await _show_confirm(update, context)
    return STATE_CONFIRM


async def on_skip_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["contact"] = "—"
    await _show_confirm_query(query, context)
    return STATE_CONFIRM


async def on_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """В состоянии STATE_CONFIRM любой текст = показать подтверждение ещё раз."""
    await _show_confirm(update, context)
    return STATE_CONFIRM


# --- Вспомогательные функции для показа формы ---

async def _ask_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 Шаг 5/5: *Контакт для связи (необязательно).*\n\n"
        "Можно указать @username, email или номер телефона. "
        "Это поможет при технических проблемах с ботом.\n"
        "Или /skip — оставаться полностью анонимным.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Анонимно",   callback_data="skip_contact"),
            InlineKeyboardButton("❌ Отмена",      callback_data="cancel"),
        ]])
    )


async def _ask_contact_update(query, context):
    await query.edit_message_text(
        "📞 Шаг 5/5: *Контакт для связи (необязательно).*\n\n"
        "Можно указать @username, email или номер телефона.\n"
        "Или нажмите «⏭ Анонимно».",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Анонимно", callback_data="skip_contact"),
            InlineKeyboardButton("❌ Отмена",    callback_data="cancel"),
        ]])
    )


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    services_str = "\n".join(
        f"   • {SERVICES[k]}" for k in ud.get("services", [])
    ) or "—"
    text = ORDER_SUMMARY.format(
        anon_id=get_anon_by_user_id(update.effective_user.id) or "—",
        topic=ud.get("topic", "—"),
        services=services_str,
        deadline=ud.get("deadline", "—"),
        comments=ud.get("comments", "—"),
        contact=ud.get("contact", "—"),
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard()
    )


async def _show_confirm_query(query, context):
    ud = context.user_data
    services_str = "\n".join(
        f"   • {SERVICES[k]}" for k in ud.get("services", [])
    ) or "—"
    anon_id = get_anon_by_user_id(query.from_user.id) or "—"
    text = ORDER_SUMMARY.format(
        anon_id=anon_id,
        topic=ud.get("topic", "—"),
        services=services_str,
        deadline=ud.get("deadline", "—"),
        comments=ud.get("comments", "—"),
        contact=ud.get("contact", "—"),
    )
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard()
    )


# ================================================================
#  ЧАТ С МЕНЕДЖЕРОМ (клиентская сторона)
# ================================================================

async def on_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересылка сообщений клиента к админу в режиме чата."""
    user = update.effective_user
    anon_id = get_anon_by_user_id(user.id) or register_user(user)

    # Если есть активный заказ в работе, просто пересылаем
    if not context.user_data.get("chat_mode"):
        # не в режиме чата — игнорируем или предлагаем
        await update.message.reply_text(
            "ℹ️ Чтобы написать менеджеру — нажмите «💬 Чат с менеджером» в меню.",
            reply_markup=main_menu_keyboard()
        )
        return CHAT_STATE_ACTIVE

    await _forward_to_admin(update, context, anon_id)
    return CHAT_STATE_ACTIVE


async def _forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             anon_id: str):
    """Пересылает любое сообщение клиента всем админам."""
    msg = update.message
    admins = get_admin_chats()
    if not admins:
        await msg.reply_text(
            "⚠️ Менеджер сейчас не на связи. Оставьте сообщение — "
            "ответим при первой возможности."
        )
        return

    caption_header = f"📩 *Сообщение от клиента* `{anon_id}`\n"

    for admin_chat in admins:
        try:
            if msg.text:
                sent = await context.bot.send_message(
                    chat_id=admin_chat,
                    text=f"{caption_header}\n{msg.text}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.photo:
                sent = await context.bot.send_photo(
                    chat_id=admin_chat,
                    photo=msg.photo[-1].file_id,
                    caption=f"{caption_header}\n{msg.caption or ''}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.document:
                sent = await context.bot.send_document(
                    chat_id=admin_chat,
                    document=msg.document.file_id,
                    caption=f"{caption_header}\n{msg.caption or ''}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.voice:
                sent = await context.bot.send_voice(
                    chat_id=admin_chat,
                    voice=msg.voice.file_id,
                    caption=f"{caption_header}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.audio:
                sent = await context.bot.send_audio(
                    chat_id=admin_chat,
                    audio=msg.audio.file_id,
                    caption=f"{caption_header}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.video:
                sent = await context.bot.send_video(
                    chat_id=admin_chat,
                    video=msg.video.file_id,
                    caption=f"{caption_header}\n{msg.caption or ''}",
                    parse_mode=ParseMode.MARKDOWN
                )
            elif msg.sticker:
                sent = await context.bot.send_message(
                    chat_id=admin_chat,
                    text=f"{caption_header}(стикер)",
                    parse_mode=ParseMode.MARKDOWN
                )
                # сам стикер
                await context.bot.send_sticker(chat_id=admin_chat,
                                                sticker=msg.sticker.file_id)
            else:
                sent = await context.bot.send_message(
                    chat_id=admin_chat,
                    text=f"{caption_header}(неподдерживаемый тип сообщения)",
                    parse_mode=ParseMode.MARKDOWN
                )
            # Сохраняем связь
            save_chat_link(sent.message_id, msg.chat_id, anon_id,
                           "user_to_admin")
        except Exception as e:
            logger.error(f"Пересылка админу {admin_chat}: {e}")

    await msg.reply_text("✅ Сообщение переслано менеджеру.")


# ================================================================
#  АДМИН: /reply, /sendfile, /broadcast, /orders, /find
# ================================================================

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /reply <ID> <текст>"""
    if not is_admin(update.effective_chat.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /reply <ID> <текст>\n"
            "Пример: /reply #AB1234 Здравствуйте! Стоимость 8000₽."
        )
        return
    anon_id = args[0]
    text = " ".join(args[1:])
    user_id = get_user_id_by_anon(anon_id)
    if not user_id:
        await update.message.reply_text(f"❌ Клиент {anon_id} не найден.")
        return
    try:
        sent = await context.bot.send_message(
            chat_id=user_id,
            text=f"💬 *Менеджер:*\n{text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=chat_keyboard()
        )
        save_chat_link(update.message.message_id, user_id, anon_id,
                       "admin_to_user")
        # Клиенту предлагаем чат-режим
        await context.bot.send_message(
            chat_id=user_id,
            text="ℹ️ Чтобы ответить — отправьте сообщение боту, оно будет переслано менеджеру.",
        )
        await update.message.reply_text(f"✅ Ответ отправлен клиенту {anon_id}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_sendfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /sendfile <ID> — следующим сообщением ждём файл."""
    if not is_admin(update.effective_chat.id):
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Использование: /sendfile <ID>\n"
            "После команды отправьте файл (фото/документ/видео)."
        )
        return
    anon_id = args[0]
    user_id = get_user_id_by_anon(anon_id)
    if not user_id:
        await update.message.reply_text(f"❌ Клиент {anon_id} не найден.")
        return
    set_admin_state(update.effective_chat.id, "await_file", anon_id)
    await update.message.reply_text(
        f"📎 Жду файл для клиента {anon_id}. Отправьте его следующим сообщением "
        "(или /cancel для отмены)."
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /broadcast <текст> — рассылка всем клиентам."""
    if not is_admin(update.effective_chat.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Использование: /broadcast <текст>")
        return
    with get_db() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
        sent = 0
        failed = 0
        for r in rows:
            try:
                await context.bot.send_message(
                    chat_id=r["user_id"],
                    text=f"📣 *Рассылка:*\n{text}",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=main_menu_keyboard()
                )
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"✅ Отправлено: {sent}\n❌ Не доставлено: {failed}"
        )


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /orders — последние 10 заказов."""
    if not is_admin(update.effective_chat.id):
        return
    orders = get_recent_orders(limit=10)
    if not orders:
        await update.message.reply_text("📦 Заказов пока нет.")
        return
    text = "📦 *Последние 10 заказов:*\n\n"
    for o in orders:
        text += (
            f"#{o['order_id']} `{o['anonymous_id']}` — {o['status']}\n"
            f"📚 {o['topic'][:50]}\n"
            f"📅 {o['created_at'][:10]}\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ: /find <ID> — детали заказа по анонимному ID."""
    if not is_admin(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /find <ID>")
        return
    anon_id = context.args[0]
    with get_db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE anonymous_id=? "
            "ORDER BY order_id DESC", (anon_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text(f"❌ Заказов от {anon_id} не найдено.")
        return
    text = f"📋 *Заказы клиента* `{anon_id}`:\n\n"
    for o in rows:
        services = json.loads(o["services"]) if o["services"] else []
        svc_str = ", ".join(SERVICES.get(s, s) for s in services)
        text += (
            f"📦 *#{o['order_id']}* — {o['status']}\n"
            f"📚 {o['topic']}\n"
            f"🛠 {svc_str}\n"
            f"📅 Срок: {o['deadline']}\n"
            f"📝 Комментарии: {o['comments']}\n"
            f"📞 Контакт: {o['contact']}\n"
            f"🕒 Создан: {o['created_at'][:19]}\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def on_admin_file_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ присылает файл в ответ на /sendfile."""
    if not is_admin(update.effective_chat.id):
        return
    state = get_admin_state(update.effective_chat.id)
    if not state or state.get("state") != "await_file":
        return
    anon_id = state["target_anon"]
    user_id = get_user_id_by_anon(anon_id)
    if not user_id:
        await update.message.reply_text(f"❌ Клиент {anon_id} не найден.")
        clear_admin_state(update.effective_chat.id)
        return

    msg = update.message
    caption = f"📎 *Файл от менеджера*\n{msg.caption or ''}"

    try:
        if msg.document:
            sent = await context.bot.send_document(
                chat_id=user_id, document=msg.document.file_id,
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
        elif msg.photo:
            sent = await context.bot.send_photo(
                chat_id=user_id, photo=msg.photo[-1].file_id,
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
        elif msg.video:
            sent = await context.bot.send_video(
                chat_id=user_id, video=msg.video.file_id,
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
        elif msg.audio:
            sent = await context.bot.send_audio(
                chat_id=user_id, audio=msg.audio.file_id,
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
        elif msg.voice:
            sent = await context.bot.send_voice(
                chat_id=user_id, voice=msg.voice.file_id,
                caption=caption, parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "⚠️ Неподдерживаемый тип файла. Отправьте документ/фото/видео."
            )
            return
        clear_admin_state(update.effective_chat.id)
        await update.message.reply_text(f"✅ Файл отправлен клиенту {anon_id}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        clear_admin_state(update.effective_chat.id)


async def on_admin_reply_to_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если админ отвечает (reply) на пересланное сообщение — переслать клиенту."""
    if not is_admin(update.effective_chat.id):
        return
    msg = update.message
    if not msg.reply_to_message:
        return
    link = get_chat_link(msg.reply_to_message.message_id)
    if not link:
        return
    anon_id = link["anonymous_id"]
    user_id = link["user_chat_id"]
    direction = link["direction"]

    if direction != "user_to_admin":
        return  # отвечают на собственное сообщение админа — игнор

    caption_header = "💬 *Менеджер:*\n"
    try:
        if msg.text:
            sent = await context.bot.send_message(
                chat_id=user_id,
                text=caption_header + msg.text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        elif msg.document:
            cap = f"📎 *Файл от менеджера*\n{msg.caption or ''}"
            sent = await context.bot.send_document(
                chat_id=user_id, document=msg.document.file_id,
                caption=cap, parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        elif msg.photo:
            cap = f"📎 *Файл от менеджера*\n{msg.caption or ''}"
            sent = await context.bot.send_photo(
                chat_id=user_id, photo=msg.photo[-1].file_id,
                caption=cap, parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        elif msg.video:
            cap = f"📎 *Файл от менеджера*\n{msg.caption or ''}"
            sent = await context.bot.send_video(
                chat_id=user_id, video=msg.video.file_id,
                caption=cap, parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        elif msg.voice:
            sent = await context.bot.send_voice(
                chat_id=user_id, voice=msg.voice.file_id,
                caption="💬 *Менеджер (голосовое)*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        elif msg.audio:
            sent = await context.bot.send_audio(
                chat_id=user_id, audio=msg.audio.file_id,
                caption="💬 *Менеджер (аудио)*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        else:
            return
        save_chat_link(msg.message_id, user_id, anon_id, "admin_to_user")
        await msg.reply_text(f"✅ Доставлено клиенту {anon_id}.")
    except Exception as e:
        await msg.reply_text(f"❌ Ошибка доставки: {e}")


# ================================================================
#  ОБРАБОТЧИКИ АДМИНСКИХ INLINE-КНОПОК (на карточке заказа)
# ================================================================

async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки под карточкой заказа у админа."""
    if not is_admin(update.effective_chat.id):
        await update.callback_query.answer(
            "⛔ Вы не администратор.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("admin_reply_"):
        anon_id = data[len("admin_reply_"):]
        await query.edit_message_text(
            f"💬 Чтобы ответить клиенту `{anon_id}`:\n\n"
            "• Введите `/reply " + anon_id + " текст`\n"
            "• Или просто *ответьте (reply)* на любое пересланное сообщение клиента.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data.startswith("admin_file_"):
        anon_id = data[len("admin_file_"):]
        set_admin_state(update.effective_chat.id, "await_file", anon_id)
        await query.edit_message_text(
            f"📎 Отправьте файл для клиента `{anon_id}` следующим сообщением.\n"
            "Или /cancel для отмены.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data.startswith("admin_prepay_"):
        order_id = data[len("admin_prepay_"):]
        with get_db() as c:
            o = c.execute("SELECT * FROM orders WHERE order_id=?",
                          (order_id,)).fetchone()
        if not o:
            await query.answer("Заказ не найден.", show_alert=True)
            return
        update_order_status(int(order_id), "waiting_payment")
        # Отправляем клиенту запрос на предоплату
        user_id = o["user_id"]
        anon_id = o["anonymous_id"]
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"💳 *Запрос предоплаты 50%*\n\n"
                f"🆔 Заказ *#{order_id}*\n"
                f"📚 Тема: {o['topic']}\n\n"
                f"🏦 Банк: *{PAYMENT_BANK}*\n"
                f"💳 Карта: `{PAYMENT_CARD}`\n\n"
                "После оплаты пришлите чек в чат с менеджером "
                "(кнопка «💬 Чат с менеджером» ниже).",
                parse_mode=ParseMode.MARKDOWN
            ),
            reply_markup=chat_keyboard()
        )
        await query.edit_message_text(
            f"💳 Запрошена предоплата по заказу #{order_id}."
        )
        return

    if data.startswith("admin_take_"):
        order_id = data[len("admin_take_"):]
        update_order_status(int(order_id), "in_progress")
        with get_db() as c:
            o = c.execute("SELECT * FROM orders WHERE order_id=?",
                          (order_id,)).fetchone()
        if o:
            await context.bot.send_message(
                chat_id=o["user_id"],
                text=(
                    f"⚙️ *Заказ #{order_id} принят в работу!*\n\n"
                    "Менеджер приступил к выполнению. "
                    "Ориентировочные сроки и промежуточные результаты "
                    "будут приходить в этот чат."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        await query.edit_message_text(f"⚙️ Заказ #{order_id} — в работе.")
        return

    if data.startswith("admin_finish_"):
        order_id = data[len("admin_finish_"):]
        update_order_status(int(order_id), "done")
        with get_db() as c:
            o = c.execute("SELECT * FROM orders WHERE order_id=?",
                          (order_id,)).fetchone()
        if o:
            await context.bot.send_message(
                chat_id=o["user_id"],
                text=(
                    f"🏁 *Заказ #{order_id} готов!*\n\n"
                    "📂 Файлы с результатом будут отправлены в этот чат "
                    "после окончательной оплаты остатка 50%.\n\n"
                    "💳 Реквизиты для оплаты остатка:\n"
                    f"🏦 {PAYMENT_BANK}\n"
                    f"💳 `{PAYMENT_CARD}`\n\n"
                    "После оплаты пришлите чек — и получите готовую работу."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=chat_keyboard()
            )
        await query.edit_message_text(f"✅ Заказ #{order_id} — завершён.")
        return

    if data.startswith("admin_reject_"):
        order_id = data[len("admin_reject_"):]
        update_order_status(int(order_id), "rejected")
        with get_db() as c:
            o = c.execute("SELECT * FROM orders WHERE order_id=?",
                          (order_id,)).fetchone()
        if o:
            await context.bot.send_message(
                chat_id=o["user_id"],
                text=(
                    f"❌ К сожалению, заказ #{order_id} не может быть выполнен.\n\n"
                    "Причина будет уточнена в чате. "
                    "Приносим извинения за неудобства."
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard()
            )
        await query.edit_message_text(f"❌ Заказ #{order_id} — отклонён.")
        return


# ================================================================
#  ОБЩИЕ ОБРАБОТЧИКИ
# ================================================================

async def on_unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Все остальные текстовые сообщения вне Conversation."""
    if is_admin(update.effective_chat.id):
        # Если админ в режиме ожидания файла — уже обработано в on_admin_file_wait
        # Иначе предлагаем команды
        state = get_admin_state(update.effective_chat.id)
        if state and state.get("state") == "await_file":
            return  # обработает on_admin_file_wait
        await update.message.reply_text(
            "ℹ️ Команды администратора:\n"
            "/reply <ID> <текст>\n"
            "/sendfile <ID>\n"
            "/broadcast <текст>\n"
            "/orders\n"
            "/find <ID>\n\n"
            "Или ответьте (reply) на любое пересланное сообщение клиента."
        )
        return

    # Клиент вне Conversation — предлагаем меню
    if not context.user_data.get("chat_mode"):
        await update.message.reply_text(
            "ℹ️ Вы не в режиме заказа и не в чате.\n"
            "Нажмите «💬 Чат с менеджером» или «📝 Заказать работу» в меню.",
            reply_markup=main_menu_keyboard()
        )


async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}", exc_info=True)


# ================================================================
#  ВСПОМОГАТЕЛЬНЫЕ: загрузка админов при старте
# ================================================================

def load_admins() -> None:
    with get_db() as c:
        rows = c.execute("SELECT chat_id FROM admin_chats").fetchall()
        for r in rows:
            ADMIN_IDS.add(r["chat_id"])
    logger.info(f"Загружено админов: {len(ADMIN_IDS)}")


# ================================================================
#  TOOLS / SKIPPED COMMENTS HANDLER
# ================================================================

async def on_skip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок skip_comments / skip_contact."""
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "skip_comments":
        context.user_data["comments"] = "—"
        await _ask_contact_update(query, context)
        return STATE_CONTACT
    if data == "skip_contact":
        context.user_data["contact"] = "—"
        await _show_confirm_query(query, context)
        return STATE_CONFIRM


# ================================================================
#  РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ
# ================================================================

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # /start, /cancel, /help
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help", cmd_start))

    # Conversation: оформление заказа
    order_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern="^new_order$")],
        states={
            STATE_TOPIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_topic_input),
                CallbackQueryHandler(on_callback, pattern="^(svc_|cancel$|svc_done)")
            ],
            STATE_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_deadline_input),
                CallbackQueryHandler(on_callback, pattern="^cancel$")
            ],
            STATE_COMMENTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_comments_input),
                CallbackQueryHandler(on_skip_handler, pattern="^skip_comments$"),
                CallbackQueryHandler(on_callback, pattern="^cancel$")
            ],
            STATE_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_contact_input),
                CallbackQueryHandler(on_skip_handler, pattern="^skip_contact$"),
                CallbackQueryHandler(on_callback, pattern="^cancel$")
            ],
            STATE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_confirm_text),
                CallbackQueryHandler(on_callback, pattern="^(confirm_order|cancel|back_to_menu)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(order_conv)

    # Чат с менеджером (клиент)
    chat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern="^chat_start$")],
        states={
            CHAT_STATE_ACTIVE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.Document.ALL |
                     filters.VOICE | filters.AUDIO | filters.VIDEO |
                     filters.Sticker.ALL) & ~filters.COMMAND,
                    on_chat_message
                ),
                CallbackQueryHandler(on_callback, pattern="^(chat_end|back_to_menu)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(chat_conv)

    # Глобальные кнопки (не в Conversation)
    app.add_handler(CallbackQueryHandler(on_callback,
        pattern="^(back_to_menu|my_orders|payment|rules|help|chat_start|cancel|new_order)$"))
    app.add_handler(CallbackQueryHandler(on_skip_handler,
        pattern="^skip_(comments|contact)$"))

    # Админские команды
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("sendfile", cmd_sendfile))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("find", cmd_find))

    # Админские кнопки под заказом
    app.add_handler(CallbackQueryHandler(on_admin_callback,
        pattern="^admin_(reply|file|prepay|take|finish|reject)_"))

    # Админ: ожидание файла после /sendfile
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.VIDEO |
         filters.VOICE | filters.AUDIO) &
        filters.REPLY & filters.ChatType.PRIVATE,
        on_admin_file_wait
    ))

    # Админ: reply на пересланное сообщение
    app.add_handler(MessageHandler(
        filters.REPLY & filters.ChatType.PRIVATE,
        on_admin_reply_to_forwarded
    ))

    # Прочее
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                    on_unexpected_message))

    app.add_error_handler(on_error)
    return app


# ================================================================
#  TOOLS: инициализация админа при старте
# ================================================================

async def post_init(app: Application) -> None:
    """Подгружаем админов из базы."""
    load_admins()


# ================================================================
#  MAIN
# ================================================================

def main():
    init_db()
    load_admins()
    app = build_application()
    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
