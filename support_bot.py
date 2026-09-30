
import os
import logging
import asyncio
from datetime import datetime
from flask import Flask
from threading import Thread
import psycopg
from psycopg.rows import dict_row
from groq import Groq

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-120b").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("DANATER_SUPPORT")

SYSTEM_PROMPT = """
You are DanaterShop Support AI.

You are a real customer-support assistant for a Free Fire shop.
Reply naturally in the user's language (Tajik or Russian; English when needed).

IMPORTANT:
- Never invent an order status, payment status, delivery status, refund, or reason.
- If database information is available, use only that information.
- If there is not enough information, say so and ask for the order ID or relevant details.
- Never ask for passwords, Telegram login codes, SMS codes, PINs, CVV, or bank credentials.
- Do not claim that diamonds were delivered unless the database has a real delivery event saying so.
- For payment disputes, refunds, or unresolved cases, offer to create a support ticket.
- Be concise, polite and practical.
- You are not the payment provider and cannot promise a refund yourself.
"""

groq = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                subject TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id BIGSERIAL PRIMARY KEY,
                ticket_id BIGINT,
                user_id BIGINT NOT NULL,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_support_messages_user
            ON support_messages(user_id, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_support_tickets_user_status
            ON support_tickets(user_id, status)
        """)
        conn.commit()

def save_user(user):
    with db() as conn:
        conn.execute("""
            INSERT INTO support_users(user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                updated_at = CURRENT_TIMESTAMP
        """, (user.id, user.username, user.first_name))
        conn.commit()

def get_open_ticket(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM support_tickets
            WHERE user_id=%s AND status='open'
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()

def create_ticket(user_id, subject="Support request"):
    existing = get_open_ticket(user_id)
    if existing:
        return existing
    with db() as conn:
        row = conn.execute("""
            INSERT INTO support_tickets(user_id, subject)
            VALUES (%s, %s)
            RETURNING *
        """, (user_id, subject)).fetchone()
        conn.commit()
        return row

def save_message(user_id, sender, message, ticket_id=None):
    with db() as conn:
        conn.execute("""
            INSERT INTO support_messages(ticket_id, user_id, sender, message)
            VALUES (%s, %s, %s, %s)
        """, (ticket_id, user_id, sender, message))
        conn.commit()

def get_history(user_id, limit=12):
    with db() as conn:
        rows = conn.execute("""
            SELECT sender, message
            FROM support_messages
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
        """, (user_id, limit)).fetchall()
    return list(reversed(rows))

def get_order_context(user_id, order_id=None):
    with db() as conn:
        if order_id:
            order = conn.execute("""
                SELECT *
                FROM orders
                WHERE id=%s AND user_id=%s
                LIMIT 1
            """, (order_id, user_id)).fetchone()
        else:
            order = conn.execute("""
                SELECT *
                FROM orders
                WHERE user_id=%s
                ORDER BY created_at DESC
                LIMIT 1
            """, (user_id,)).fetchone()

        if not order:
            return None

        payments = conn.execute("""
            SELECT * FROM payment_events
            WHERE order_id=%s
            ORDER BY created_at ASC
        """, (order["id"],)).fetchall() if table_exists(conn, "payment_events") else []

        deliveries = conn.execute("""
            SELECT * FROM delivery_events
            WHERE order_id=%s
            ORDER BY created_at ASC
        """, (order["id"],)).fetchall() if table_exists(conn, "delivery_events") else []

        return {
            "order": dict(order),
            "payment_events": [dict(x) for x in payments],
            "delivery_events": [dict(x) for x in deliveries],
        }

def table_exists(conn, name):
    row = conn.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
        ) AS exists
    """, (name,)).fetchone()
    return bool(row["exists"])

def detect_order_id(text):
    words = text.replace("#", " ").split()
    for word in words:
        clean = word.strip(".,:;()[]")
        if clean.isdigit() and 5 <= len(clean) <= 30:
            return clean
    return None

def format_context(ctx):
    if not ctx:
        return "No verified order information was found."

    order = ctx["order"]
    lines = [
        f"Order ID: {order.get('id')}",
        f"Product: {order.get('product_name')}",
        f"Amount: {order.get('final_price')}",
        f"Payment method: {order.get('payment_method')}",
        f"Order status: {order.get('status')}",
        f"Created: {order.get('created_at')}",
    ]
    if ctx["payment_events"]:
        lines.append("Payment events:")
        for x in ctx["payment_events"]:
            lines.append(f"- {x.get('status')} | {x.get('note') or ''}")
    if ctx["delivery_events"]:
        lines.append("Delivery events:")
        for x in ctx["delivery_events"]:
            lines.append(f"- {x.get('status')} | {x.get('provider') or ''} | {x.get('note') or ''}")
    return "\n".join(lines)

def ai_reply(user_id, user_text):
    history = get_history(user_id)
    order_id = detect_order_id(user_text)
    ctx = get_order_context(user_id, order_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if ctx:
        messages.append({
            "role": "system",
            "content": "VERIFIED DATABASE CONTEXT:\n" + format_context(ctx)
        })
    for h in history:
        role = "assistant" if h["sender"] == "ai" else "user"
        messages.append({"role": role, "content": h["message"]})
    messages.append({"role": "user", "content": user_text})

    if not groq:
        return "⚠️ AI ҳоло фаъол нест. Оператор ба шумо ҷавоб медиҳад."

    try:
        result = groq.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=700,
        )
        return result.choices[0].message.content.strip()
    except Exception:
        logger.exception("Groq request failed")
        return "⚠️ Ҳоло AI ҷавоб дода натавонист. Ман масъалаатонро ба оператор мефиристам."

def support_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎫 Оператор", callback_data="ticket"),
            InlineKeyboardButton("📦 Фармоиши охирин", callback_data="last_order"),
        ],
        [InlineKeyboardButton("❌ Бастани тикет", callback_data="close_ticket")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)
    text = (
        "🤖 <b>DanaterShop Поддержка</b>\n\n"
        "Салом! Ман ёвари AI-и DanaterShop ҳастам. 👋\n\n"
        "Метавонед саволатонро оддӣ нависед. Масалан:\n"
        "• «Фармоишам куҷост?»\n"
        "• «Ман пул пардохт кардам»\n"
        "• «Алмазҳо наомаданд»\n\n"
        "Агар масъала ба фармоиш дахл дошта бошад, <b>ID-и фармоишро</b> ҳам фиристед."
    )
    await update.message.reply_text(text, reply_markup=support_kb(), parse_mode=ParseMode.HTML)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
    save_user(user)
    text = update.message.text.strip()
    if not text:
        return

    save_message(user.id, "user", text)

    reply = await asyncio.to_thread(ai_reply, user.id, text)
    save_message(user.id, "ai", reply)

    await update.message.reply_text(
        reply,
        reply_markup=support_kb(),
        parse_mode=ParseMode.HTML
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    save_user(user)
    ticket = create_ticket(user.id, "Payment screenshot")
    caption = update.message.caption or "Payment screenshot"
    save_message(user.id, "user", "[PHOTO] " + caption, ticket["id"])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=update.message.photo[-1].file_id,
                caption=(
                    f"🎫 <b>Support ticket #{ticket['id']}</b>\n"
                    f"👤 @{user.username or 'no_username'}\n"
                    f"🆔 <code>{user.id}</code>\n"
                    f"💬 {caption}"
                ),
                parse_mode=ParseMode.HTML
            )
        except TelegramError:
            logger.exception("Could not notify admin")

    await update.message.reply_text(
        "📸 Скриншот қабул шуд.\n\n"
        "🎫 Тикет кушода шуд. Оператор онро мебинад.",
        reply_markup=support_kb()
    )

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "ticket":
        ticket = create_ticket(uid, "Customer support")
        await q.message.reply_text(
            f"🎫 <b>Тикет #{ticket['id']} кушода шуд.</b>\n"
            "Масъалаатонро нависед ё скриншот фиристед.",
            parse_mode=ParseMode.HTML
        )
        return

    if q.data == "last_order":
        ctx = get_order_context(uid)
        if not ctx:
            await q.message.reply_text("📦 Барои шумо фармоиш ёфт нашуд.")
            return
        await q.message.reply_text(
            "📦 <b>Маълумоти тасдиқшуда:</b>\n\n<code>" +
            format_context(ctx).replace("<", "&lt;").replace(">", "&gt;") +
            "</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if q.data == "close_ticket":
        with db() as conn:
            conn.execute("""
                UPDATE support_tickets
                SET status='closed', updated_at=CURRENT_TIMESTAMP
                WHERE user_id=%s AND status='open'
            """, (uid,))
            conn.commit()
        await q.message.reply_text("✅ Тикети кушода баста шуд.")
        return

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await update.message.reply_text("Истифода: /reply USER_ID матн")
        return
    uid = int(parts[1])
    message = parts[2]
    save_message(uid, "operator", message)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text="👨‍💻 <b>Оператор:</b>\n\n" + message,
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text("✅ Ҷавоб фиристода шуд.")
    except TelegramError as ex:
        await update.message.reply_text(f"❌ Фиристодан нашуд: {ex}")

flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "DanaterShop Support is running", 200

@flask_app.get("/health")
def health():
    return {"status": "ok", "service": "danatershop-support"}, 200

def run_web():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

async def post_init(application):
    await application.bot.delete_webhook(drop_pending_updates=False)
    me = await application.bot.get_me()
    logger.info("Support bot: @%s | ID=%s", me.username, me.id)

def main():
    if not TOKEN:
        raise RuntimeError("SUPPORT_BOT_TOKEN лозим аст.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL лозим аст.")
    init_db()
    Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reply", admin_reply))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

if __name__ == "__main__":
    main()
