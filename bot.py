"""
Telegram Digital File Shop — personal KHQR + manual payment verification.

Python 3.11+
Install: py -m pip install -r requirements.txt

Required environment variables (PowerShell):
  $env:BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
  $env:ADMIN_IDS="123456789,987654321"

Optional:
  $env:SHOP_NAME="My Digital Shop"

Put your own KHQR image at khqr.png next to this script.
A personal KHQR image does not provide automatic payment verification. Admin must check the actual banking app/account before approving.
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).resolve().with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


def parse_admin_ids(value: str) -> set[int]:
    admin_ids = set()
    invalid_values = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.isdecimal():
            invalid_values.append(entry)
        else:
            admin_ids.add(int(entry))
    if invalid_values:
        raise SystemExit(
            "Invalid ADMIN_IDS. Use comma-separated numeric Telegram user IDs."
        )
    return admin_ids


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
SHOP_NAME = os.getenv("SHOP_NAME", "Digital File Shop")
PAYMENT_INSTRUCTIONS = os.getenv(
    "PAYMENT_INSTRUCTIONS",
    "Scan the KHQR and pay the exact amount. Keep your transaction record. "
    "Your file will be sent after an admin verifies the payment in the banking app."
)
DB_PATH = Path("shop.db")
(
    UPLOAD_FILE,
    PRODUCT_PHOTO,
    PRODUCT_NAME,
    PRODUCT_DESCRIPTION,
    PRODUCT_PRICE,
    PRODUCT_CURRENCY,
    PRODUCT_QR,
) = range(7)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("file-shop")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL CHECK(price > 0),
                currency TEXT NOT NULL CHECK(currency IN ('USD','KHR')),
                file_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                photo_file_id TEXT,
                qr_file_id TEXT,
                qr_type TEXT,
                description TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_payment',
                proof_file_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                qr_generated_at TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)
        # Add columns safely for databases created by older versions.
        cols = {row["name"] for row in con.execute("PRAGMA table_info(products)").fetchall()}
        if "photo_file_id" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN photo_file_id TEXT")
        if "description" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "qr_file_id" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN qr_file_id TEXT")
        if "qr_type" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN qr_type TEXT")
        order_cols = {
            row["name"] for row in con.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "qr_generated_at" not in order_cols:
            con.execute("ALTER TABLE orders ADD COLUMN qr_generated_at TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def product_button_label(name: str, amount: float, currency: str) -> str:
    price_label = f" — {money(amount, currency)}"
    available_units = 64 - len(price_label.encode("utf-16-le")) // 2
    name_units = len(name.encode("utf-16-le")) // 2
    if name_units <= available_units:
        return name + price_label

    truncated = []
    used_units = 0
    for character in name:
        character_units = len(character.encode("utf-16-le")) // 2
        if used_units + character_units > available_units - 1:
            break
        truncated.append(character)
        used_units += character_units
    return "".join(truncated) + "…" + price_label


def money(amount: float, currency: str) -> str:
    if currency == "KHR":
        return f"{amount:,.0f} KHR"
    return f"${amount:,.2f} USD"


def description_without_title(name: str, description: str | None) -> str:
    description = (description or "").strip()
    if description[:len(name)].casefold() == name.casefold():
        remainder = description[len(name):]
        if not remainder or remainder[0].isspace() or remainder[0] in ":：-–—|":
            return remainder.lstrip(" \t\r\n:：-–—|")
    return description


def main_keyboard():
    rows = [
        [InlineKeyboardButton("🛍 Browse products", callback_data="shop")],
        [InlineKeyboardButton("📦 My orders", callback_data="myorders")],
    ]
    rows.append([InlineKeyboardButton(
        "✉️ Contact owner",
        url="https://t.me/bun_meng_heng_kh",
    )])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Please open me in a private chat to shop.")
        return
    await update.effective_message.reply_text(
        f"Welcome to {SHOP_NAME}!\nChoose an option below.",
        reply_markup=main_keyboard(),
    )


async def display_shop_message(query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup):
    message = query.message
    if message.photo or message.document:
        try:
            await message.delete()
        except TelegramError:
            log.exception("Could not remove the previous shop media message")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=text,
            reply_markup=reply_markup,
        )
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    with db() as con:
        products = con.execute(
            "SELECT id,name,price,currency FROM products WHERE active=1 ORDER BY id DESC"
        ).fetchall()
    if not products:
        await display_shop_message(
            query,
            context,
            "There are no products yet. Please check back later.",
            main_keyboard(),
        )
        return
    rows = [
        [InlineKeyboardButton(
            product_button_label(p["name"], p["price"], p["currency"]),
            callback_data=f"product:{p['id']}"
        )]
        for p in products
    ]
    rows.append([InlineKeyboardButton("⬅️ Main menu", callback_data="home")])
    await display_shop_message(
        query,
        context,
        "Choose a product:",
        InlineKeyboardMarkup(rows),
    )


async def product_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        product_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    with db() as con:
        p = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
    if not p:
        await display_shop_message(
            query,
            context,
            "This product is unavailable.",
            main_keyboard(),
        )
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Buy this file", callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton("⬅️ Back to products", callback_data="shop")],
    ])
    description = description_without_title(p["name"], p["description"])
    details = (
        f"📄 {p['name']}\n💵 Price: {money(p['price'], p['currency'])}\n\n"
        "After ordering, scan the seller's KHQR and pay the exact amount. "
        "The file is delivered only after the admin verifies the payment."
    )
    if p["photo_file_id"]:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=p["photo_file_id"],
            caption=details[:1024],
            reply_markup=keyboard,
        )
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        await query.edit_message_text(details, reply_markup=keyboard)


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        product_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    user = query.from_user
    with db() as con:
        p = con.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
        if not p:
            await display_shop_message(
                query,
                context,
                "This product is no longer available.",
                main_keyboard(),
            )
            return
        stamp = now()
        cur = con.execute("""
            INSERT INTO orders
            (user_id, username, product_id, product_name, price, currency, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?)
        """, (
            user.id, user.username, p["id"], p["name"], p["price"],
            p["currency"], stamp, stamp
        ))
        order_id = cur.lastrowid

    context.user_data["active_order"] = order_id
    text = (
        f"🧾 Order #{order_id}\n"
        f"Product: {p['name']}\n"
        f"Amount due: {money(p['price'], p['currency'])}\n\n"
        "Tap Generate QR to display this product's payment QR. The Pay ready button appears "
        "after one minute. An admin must verify your payment before the download is available."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Generate QR", callback_data=f"generate_qr:{order_id}")],
        [InlineKeyboardButton(
            "❌ Cancel order & browse products",
            callback_data=f"cancel_order:{order_id}",
        )],
        [InlineKeyboardButton("📦 My orders", callback_data="myorders")],
        [InlineKeyboardButton("🏠 Main menu", callback_data="home")],
    ])
    if query.message.photo:
        await query.edit_message_caption(caption=text, reply_markup=keyboard)
    else:
        await query.edit_message_text(text, reply_markup=keyboard)
    await notify_admins_new_order(context, order_id, user, p)


def elapsed_since(timestamp: str) -> timedelta:
    then = datetime.fromisoformat(timestamp)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then.astimezone(timezone.utc)


async def reveal_pay_ready_button(bot, chat_id: int, message_id: int, order_id: int):
    await asyncio.sleep(60)
    with db() as con:
        order = con.execute(
            "SELECT status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
    if not order or order["status"] != "awaiting_payment":
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Pay ready", callback_data=f"pay_ready:{order_id}")],
        [InlineKeyboardButton(
            "❌ Cancel order & browse products",
            callback_data=f"cancel_order:{order_id}",
        )],
    ])
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=keyboard
        )
    except Exception:
        log.exception("Could not show Pay ready for order %s", order_id)


async def reveal_download_button(bot, chat_id: int, message_id: int, order_id: int):
    await asyncio.sleep(20)
    with db() as con:
        order = con.execute(
            "SELECT status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
    if not order or order["status"] not in {"awaiting_review", "paid"}:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Download", callback_data=f"download:{order_id}")
    ]])
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=keyboard
        )
    except Exception:
        log.exception("Could not show Download for order %s", order_id)


async def send_download_wait_message(context, chat_id: int, order_id: int):
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"Order #{order_id} submitted. The Download button will appear in 20 seconds. "
            "The file is available only after an admin verifies payment."
        ),
    )
    context.application.create_task(
        reveal_download_button(
            context.bot, chat_id, message.message_id, order_id
        )
    )


async def generate_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        order_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order.", show_alert=True)
        return

    with db() as con:
        order = con.execute(
            "SELECT orders.*,products.qr_file_id,products.qr_type "
            "FROM orders JOIN products ON products.id=orders.product_id WHERE orders.id=?",
            (order_id,),
        ).fetchone()
    if not order or order["user_id"] != query.from_user.id:
        await query.answer("This order doesn't belong to you.", show_alert=True)
        return
    if order["status"] != "awaiting_payment":
        await query.answer("This order is no longer awaiting payment.", show_alert=True)
        return
    if order["qr_generated_at"]:
        await query.answer("The QR was already generated for this order.", show_alert=True)
        return

    text = (
        f"🧾 Order #{order_id}\n"
        f"Product: {order['product_name']}\n"
        f"Amount due: {money(order['price'], order['currency'])}\n\n"
        f"{PAYMENT_INSTRUCTIONS}\n\n"
        "Pay the exact amount. The Pay ready button will appear in one minute. "
        "An admin must verify the transfer; screenshots or waiting do not confirm payment."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "❌ Cancel order & browse products",
            callback_data=f"cancel_order:{order_id}",
        )
    ]])
    qr_file_id = order["qr_file_id"]
    qr_type = order["qr_type"]
    if qr_file_id and qr_type == "photo":
        message = await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=qr_file_id,
            caption=text,
            reply_markup=keyboard,
        )
    elif qr_file_id and qr_type == "document":
        message = await context.bot.send_document(
            chat_id=query.from_user.id,
            document=qr_file_id,
            caption=text,
            reply_markup=keyboard,
        )
    else:
        await query.answer(
            "This product has no payment QR configured. Please contact the shop admin.",
            show_alert=True,
        )
        return

    with db() as con:
        con.execute(
            "UPDATE orders SET qr_generated_at=? "
            "WHERE id=? AND user_id=? AND status='awaiting_payment' "
            "AND qr_generated_at IS NULL",
            (now(), order_id, query.from_user.id),
        )
    await query.answer("QR generated. Pay ready appears in one minute.")
    context.application.create_task(
        reveal_pay_ready_button(
            context.bot, query.from_user.id, message.message_id, order_id
        )
    )


async def pay_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        order_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order.", show_alert=True)
        return

    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order or order["user_id"] != query.from_user.id:
            await query.answer("This order doesn't belong to you.", show_alert=True)
            return
        if order["status"] != "awaiting_payment":
            await query.answer("This order is no longer awaiting payment.", show_alert=True)
            return
        if not order["qr_generated_at"]:
            await query.answer("Generate the QR first.", show_alert=True)
            return
        if elapsed_since(order["qr_generated_at"]) < timedelta(minutes=1):
            await query.answer("Please wait until the one-minute QR timer finishes.", show_alert=True)
            return

    try:
        await query.message.delete()
    except Exception:
        log.exception("Could not hide the QR message for order %s", order_id)
        await query.answer("Could not hide the QR. Please try again.", show_alert=True)
        return

    with db() as con:
        cursor = con.execute(
            "UPDATE orders SET status='awaiting_review', updated_at=? "
            "WHERE id=? AND user_id=? AND status='awaiting_payment'",
            (now(), order_id, query.from_user.id),
        )
    if cursor.rowcount == 0:
        await query.answer("This order is no longer awaiting payment.", show_alert=True)
        return

    await query.answer("Payment submitted for admin review.")
    context.user_data["active_order"] = order_id
    await send_download_wait_message(context, query.from_user.id, order_id)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔎 Customer says payment was made for order #{order_id}. "
                "Verify the transaction in your banking app before approving.",
            )
            await send_review_controls(context, admin_id, order_id)
        except Exception:
            log.warning("Could not send review request to admin %s", admin_id)


async def download_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        order_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order.", show_alert=True)
        return

    with db() as con:
        order = con.execute(
            "SELECT * FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order or order["user_id"] != query.from_user.id:
            await query.answer("This order doesn't belong to you.", show_alert=True)
            return
        if order["status"] != "paid":
            await query.answer(
                "Payment has not been verified by the admin yet.",
                show_alert=True,
            )
            return
        product = con.execute(
            "SELECT file_id,file_name,description FROM products WHERE id=?",
            (order["product_id"],),
        ).fetchone()
    if not product:
        await query.answer("Product file not found. Contact the shop admin.", show_alert=True)
        return

    await query.answer("Sending your file.")
    description = description_without_title(
        order["product_name"], product["description"]
    )
    caption_parts = [
        f"✅ Payment verified!\nOrder #{order_id}\n{order['product_name']}"
    ]
    if description:
        caption_parts.append(description)
    caption_parts.append("Thank you for your purchase.")
    await context.bot.send_document(
        chat_id=query.from_user.id,
        document=product["file_id"],
        filename=product["file_name"],
        caption="\n".join(caption_parts)[:1024],
    )


async def notify_admins_new_order(context, order_id, user, product):
    if not ADMIN_IDS:
        log.error("ADMIN_IDS is empty; no admin can review orders.")
        return
    msg = (
        f"🛒 New order #{order_id}\n"
        f"Customer ID: {user.id}\n"
        f"Username: @{user.username or 'not set'}\n"
        f"Product: {product['name']}\n"
        f"Amount: {money(product['price'], product['currency'])}\n"
        "Status: awaiting payment"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, msg)
        except Exception:
            log.warning("Could not notify admin %s; they may need to /start the bot.", admin_id)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        order_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order.", show_alert=True)
        return

    with db() as con:
        order = con.execute(
            "SELECT user_id,status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if not order or order["user_id"] != query.from_user.id:
            await query.answer("This order doesn't belong to you.", show_alert=True)
            return
        if order["status"] != "awaiting_payment":
            await query.answer(
                "This order can no longer be cancelled. Contact the shop admin if needed.",
                show_alert=True,
            )
            return
        con.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE id=?",
            (now(), order_id),
        )

    if context.user_data.get("active_order") == order_id:
        context.user_data.pop("active_order", None)
    await query.answer("Order cancelled.")
    message = query.message
    text = f"Order #{order_id} cancelled. Browse the shop to choose another product."
    try:
        await message.delete()
    except TelegramError:
        log.exception("Could not remove the cancelled order message for order %s", order_id)
        text += "\nThe previous QR message could not be removed."
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=text,
        reply_markup=main_keyboard(),
    )


async def paid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        order_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid order.", show_alert=True)
        return
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["user_id"] != query.from_user.id:
            await query.answer("This order doesn't belong to you.", show_alert=True)
            return
        if order["status"] != "awaiting_payment":
            await query.answer("This order is no longer awaiting payment.", show_alert=True)
            return
        con.execute(
            "UPDATE orders SET status='awaiting_review', updated_at=? "
            "WHERE id=? AND status='awaiting_payment'",
            (now(), order_id),
        )
    await query.answer("Payment submitted for admin review.")
    context.user_data["active_order"] = order_id
    await send_download_wait_message(context, query.from_user.id, order_id)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🔎 Customer says payment was made for order #{order_id}. "
                "Verify the transaction in your banking app before approving."
            )
            await send_review_controls(context, admin_id, order_id)
        except Exception:
            log.warning("Could not send review request to admin %s", admin_id)


async def handle_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    order_id = context.user_data.get("active_order")
    if not order_id:
        await update.message.reply_text("First create an order and press “I have paid”. Use /myorders to see orders.")
        return
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document
    file_id = photo.file_id if photo else (document.file_id if document else None)
    if not file_id:
        return
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["user_id"] != user.id:
            await update.message.reply_text("I couldn't match that proof to your order. Use /myorders and select the order.")
            return
        if order["status"] == "paid":
            await update.message.reply_text("This order is already paid.")
            return
        if order["status"] in {"cancelled", "rejected"}:
            await update.message.reply_text(
                "This order is no longer active. Create a new order to continue."
            )
            return
        if order["status"] != "awaiting_review":
            await update.message.reply_text(
                "Wait for the Pay ready button, tap it, then send your payment proof."
            )
            return
        con.execute(
            "UPDATE orders SET proof_file_id=?, status='awaiting_review', updated_at=? WHERE id=?",
            (file_id, now(), order_id)
        )
    await update.message.reply_text(
        f"Proof received for order #{order_id}. This does not confirm payment; an admin will verify it."
    )
    await send_download_wait_message(context, user.id, order_id)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📷 Payment proof submitted for order #{order_id} by user {user.id}. "
                "Check the real transaction in the bank app before approval."
            )
            await context.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=user.id,
                message_id=update.message.message_id,
            )
            await send_review_controls(context, admin_id, order_id)
        except Exception:
            log.warning("Could not forward proof to admin %s", admin_id)


async def send_review_controls(context, admin_id, order_id):
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Verify & deliver", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order_id}"),
    ]])
    await context.bot.send_message(
        admin_id,
        f"Review order #{order_id}\nCustomer: {order['user_id']}\n"
        f"Product: {order['product_name']}\nAmount: {money(order['price'], order['currency'])}\n"
        f"Status: {order['status']}\n\nApprove only after confirming the actual incoming payment.",
        reply_markup=keyboard,
    )


async def review_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    await query.answer()
    action, raw_id = query.data.split(":", 1)
    order_id = int(raw_id)
    if action == "approve":
        with db() as con:
            order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                await query.message.reply_text("Order not found.")
                return
            if order["status"] == "paid":
                await query.message.reply_text("This order has already been approved.")
                return
            if order["status"] == "rejected":
                await query.message.reply_text("This order was rejected. It cannot be approved.")
                return
            if order["status"] == "cancelled":
                await query.message.reply_text("This order was cancelled and cannot be approved.")
                return
            product = con.execute(
                "SELECT file_id,file_name,description FROM products WHERE id=?",
                (order["product_id"],)
            ).fetchone()
            if not product:
                await query.message.reply_text("Product file is missing; cannot deliver.")
                return
            con.execute(
                "UPDATE orders SET status='paid', updated_at=? WHERE id=?",
                (now(), order_id)
            )
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=(
                    f"✅ Payment verified for order #{order_id}. "
                    "The Download button will appear after the 20-second wait."
                ),
            )
            await query.message.reply_text(
                f"Order #{order_id} approved. The customer can download the file."
            )
        except Exception:
            log.exception("Could not notify customer for approved order %s", order_id)
            await query.message.reply_text(
                f"Payment verified, but customer notification failed for order #{order_id}. "
                f"Use /resend {order_id} if needed."
            )
    else:
        with db() as con:
            order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                await query.message.reply_text("Order not found.")
                return
            if order["status"] == "paid":
                await query.message.reply_text("Already paid; rejection is blocked.")
                return
            con.execute(
                "UPDATE orders SET status='rejected', updated_at=? WHERE id=?",
                (now(), order_id)
            )
        try:
            await context.bot.send_message(
                order["user_id"],
                f"❌ Payment for order #{order_id} was not approved. "
                "Please contact the shop admin if you believe this is a mistake."
            )
        except Exception:
            pass
        await query.message.reply_text(f"Order #{order_id} rejected.")


async def myorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    user_id = update.effective_user.id
    with db() as con:
        orders = con.execute(
            "SELECT id,product_name,price,currency,status,created_at FROM orders "
            "WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_id,)
        ).fetchall()
    if not orders:
        text = "You have no orders yet."
    else:
        lines = ["📦 Your recent orders:"]
        for order in orders:
            lines.append(
                f"\n#{order['id']} — {order['product_name']}\n"
                f"Amount: {money(order['price'], order['currency'])}\n"
                f"Status: {order['status'].replace('_', ' ')}"
            )
        text = "\n".join(lines)

    if query:
        await display_shop_message(query, context, text, main_keyboard())
        return
    await update.effective_message.reply_text(text, reply_markup=main_keyboard())


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await display_shop_message(
        query,
        context,
        f"{SHOP_NAME}\nChoose an option:",
        main_keyboard(),
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Upload product", callback_data="admin_upload")],
        [InlineKeyboardButton("📋 List products", callback_data="admin_products")],
        [InlineKeyboardButton("🧾 Pending orders", callback_data="admin_pending")],
    ])
    await update.effective_message.reply_text("Admin menu:", reply_markup=keyboard)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    await query.answer()
    if query.data == "admin_upload":
        await query.message.reply_text(
            "Send the product file as a Telegram document. Then send an optional product photo "
            "(or /skipphoto), enter the name and description, and set its price and currency."
        )
        context.user_data["awaiting_product_file"] = True
    elif query.data == "admin_products":
        with db() as con:
            rows = con.execute("SELECT id,name,price,currency,active,file_name FROM products ORDER BY id DESC").fetchall()
        if not rows:
            await query.message.reply_text("No products yet.")
        else:
            for row in rows:
                keyboard = None
                if row["active"]:
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🗑 Delete product",
                            callback_data=f"product_delete:{row['id']}",
                        )
                    ]])
                await query.message.reply_text(
                    f"#{row['id']} | {row['name']} | "
                    f"{money(row['price'], row['currency'])} | "
                    f"{'active' if row['active'] else 'hidden'} | {row['file_name']}",
                    reply_markup=keyboard,
                )
    elif query.data == "admin_pending":
        with db() as con:
            rows = con.execute(
                "SELECT id,user_id,product_name,price,currency,status FROM orders "
                "WHERE status IN ('awaiting_payment','awaiting_review') ORDER BY id"
            ).fetchall()
        if not rows:
            await query.message.reply_text("No pending orders.")
        for r in rows:
            await query.message.reply_text(
                f"Order #{r['id']} | user {r['user_id']} | {r['product_name']} | "
                f"{money(r['price'],r['currency'])} | {r['status']}"
            , reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Review", callback_data=f"review:{r['id']}")
            ]]))


async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    action, raw_product_id = query.data.split(":", 1)
    product_id = int(raw_product_id)
    if action == "product_delete":
        with db() as con:
            product = con.execute(
                "SELECT name,active FROM products WHERE id=?", (product_id,)
            ).fetchone()
        if not product or not product["active"]:
            await query.answer("That product is not active.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Confirm delete",
                callback_data=f"product_delete_confirm:{product_id}",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=f"product_delete_cancel:{product_id}",
            ),
        ]]))
        return

    if action == "product_delete_cancel":
        await query.answer("Deletion cancelled.")
        await query.edit_message_reply_markup(reply_markup=None)
        return

    with db() as con:
        cursor = con.execute(
            "UPDATE products SET active=0 WHERE id=? AND active=1",
            (product_id,),
        )
    if cursor.rowcount == 0:
        await query.answer("Product not found or already deleted.", show_alert=True)
        return

    await query.answer("Product deleted from the shop.")
    await query.edit_message_text(
        f"Product #{product_id} deleted from the shop. Existing order records are preserved."
    )


async def review_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    await query.answer()
    order_id = int(query.data.split(":", 1)[1])
    await send_review_controls(context, query.message.chat_id, order_id)


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get("awaiting_product_file"):
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send the file as a document (not as a photo).")
        return
    context.user_data["new_product_file_id"] = doc.file_id
    context.user_data["new_product_file_name"] = doc.file_name or f"file_{doc.file_unique_id}"
    context.user_data["awaiting_product_file"] = False
    context.user_data["product_upload_stage"] = PRODUCT_PHOTO
    await update.message.reply_text(
        "Optional: send a product photo to show in the shop. Or type /skipphoto to continue without a photo."
    )
    return PRODUCT_PHOTO


async def product_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    photo = update.message.photo[-1]
    context.user_data["new_product_photo_id"] = photo.file_id
    context.user_data["product_upload_stage"] = PRODUCT_NAME
    await update.message.reply_text("Photo saved. Now enter the product name:")
    return PRODUCT_NAME


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data["product_upload_stage"] = PRODUCT_NAME
    await update.effective_message.reply_text("No photo. Now enter the product name:")
    return PRODUCT_NAME


async def product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name or len(name) > 256:
        await update.message.reply_text(
            "Name must be 1–256 characters. Enter the product name again."
        )
        return PRODUCT_NAME
    context.user_data["new_product_name"] = name
    context.user_data["product_upload_stage"] = PRODUCT_DESCRIPTION
    await update.message.reply_text("Enter a product caption/description (up to 800 characters):")
    return PRODUCT_DESCRIPTION


async def product_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()
    if len(description) > 800:
        await update.message.reply_text("Description must be 800 characters or fewer. Please enter it again.")
        return PRODUCT_DESCRIPTION
    context.user_data["new_product_description"] = description
    context.user_data["product_upload_stage"] = PRODUCT_PRICE
    await update.message.reply_text("Enter the price as a number, e.g. 2.50 or 10000:")
    return PRODUCT_PRICE


async def product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", "")
    try:
        price = float(raw)
        if not (0 < price <= 1_000_000_000):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Enter a number greater than 0.")
        return PRODUCT_PRICE
    context.user_data["new_product_price"] = price
    context.user_data["product_upload_stage"] = PRODUCT_CURRENCY
    await update.message.reply_text(
        "Choose currency:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("USD", callback_data="currency:USD"),
            InlineKeyboardButton("KHR", callback_data="currency:KHR"),
        ]])
    )
    return PRODUCT_CURRENCY


async def product_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return ConversationHandler.END
    currency = query.data.split(":", 1)[1]
    await query.answer()
    context.user_data["new_product_currency"] = currency
    context.user_data["product_upload_stage"] = PRODUCT_QR
    await query.message.reply_text(
        "Now send the payment QR for this product as a photo or image document. "
        "Each product needs its own QR."
    )
    return PRODUCT_QR


async def product_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    photo = update.message.photo[-1] if update.message.photo else None
    document = update.message.document
    if photo:
        qr_file_id, qr_type = photo.file_id, "photo"
    elif document and (document.mime_type or "").startswith("image/"):
        qr_file_id, qr_type = document.file_id, "document"
    else:
        await update.message.reply_text(
            "That is not an image. Send this product's QR as a photo or image document."
        )
        return PRODUCT_QR

    saved_name = context.user_data["new_product_name"]
    saved_price = context.user_data["new_product_price"]
    saved_currency = context.user_data["new_product_currency"]
    with db() as con:
        cur = con.execute("""
            INSERT INTO products(
                name,price,currency,file_id,file_name,photo_file_id,
                qr_file_id,qr_type,description,active,created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,1,?)
        """, (
            saved_name,
            saved_price,
            saved_currency,
            context.user_data["new_product_file_id"],
            context.user_data["new_product_file_name"],
            context.user_data.get("new_product_photo_id"),
            qr_file_id,
            qr_type,
            context.user_data.get("new_product_description", ""),
            now(),
        ))
        product_id = cur.lastrowid
    for key in ("new_product_name", "new_product_price", "new_product_file_id",
                "new_product_file_name", "new_product_photo_id", "new_product_description",
                "new_product_currency", "awaiting_product_file", "product_upload_stage"):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        f"✅ Product added!\nID: {product_id}\n"
        f"Name: {saved_name}\nPrice: {money(saved_price, saved_currency)}\n"
        "Customers can now see it in the shop."
    )
    return ConversationHandler.END


async def resume_product_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    stage = context.user_data.get("product_upload_stage")
    if stage == PRODUCT_PHOTO:
        await update.effective_message.reply_text(
            "Send a product photo, or type /skipphoto to continue without one."
        )
    elif stage == PRODUCT_NAME:
        await product_name(update, context)
    elif stage == PRODUCT_DESCRIPTION:
        await product_description(update, context)
    elif stage == PRODUCT_PRICE:
        await product_price(update, context)
    elif stage == PRODUCT_QR:
        await product_qr(update, context)
    else:
        return


async def resume_product_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("Admin only.", show_alert=True)
        return
    if context.user_data.get("product_upload_stage") != PRODUCT_CURRENCY:
        return
    await product_currency(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in list(context.user_data):
        if key.startswith("new_product_") or key in {
            "awaiting_product_file",
                "product_upload_stage",
        }:
            context.user_data.pop(key, None)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /resend ORDER_ID")
        return
    order_id = int(context.args[0])
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "paid":
            await update.effective_message.reply_text("Order not found or not approved as paid.")
            return
        product = con.execute(
            "SELECT file_id,file_name,description FROM products WHERE id=?",
            (order["product_id"],)
        ).fetchone()
    if not product:
        await update.effective_message.reply_text("Product file not found.")
        return
    description = description_without_title(
        order["product_name"], product["description"]
    )
    caption = f"📦 Resent file for paid order #{order_id}."
    if description:
        caption += f"\n{description}"
    await context.bot.send_document(
        chat_id=order["user_id"],
        document=product["file_id"],
        filename=product["file_name"],
        caption=caption[:1024]
    )
    await update.effective_message.reply_text(f"File resent for order #{order_id}.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception: %s", context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN. Add a replacement token to .env before running.")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", BOT_TOKEN):
        raise SystemExit(
            "Invalid BOT_TOKEN format. Check .env and remove any stale PowerShell "
            "BOT_TOKEN override."
        )
    if not ADMIN_IDS:
        raise SystemExit("Missing ADMIN_IDS. Set at least your numeric Telegram user ID.")
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    upload_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE & filters.User(user_id=list(ADMIN_IDS)), upload_file)],
        states={
            PRODUCT_PHOTO: [
                MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, product_photo),
                CommandHandler("skipphoto", skip_photo),
            ],
            PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_name)],
            PRODUCT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_description)],
            PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_price)],
            PRODUCT_CURRENCY: [CallbackQueryHandler(product_currency, pattern=r"^currency:(USD|KHR)$")],
            PRODUCT_QR: [
                MessageHandler(
                    (filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE,
                    product_qr,
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myorders", myorders))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("resend", resend))
    app.add_handler(upload_conversation)
    app.add_handler(MessageHandler(
        filters.TEXT
        & ~filters.COMMAND
        & filters.ChatType.PRIVATE
        & filters.User(user_id=list(ADMIN_IDS)),
        resume_product_upload,
    ))
    app.add_handler(CallbackQueryHandler(
        resume_product_currency,
        pattern=r"^currency:(USD|KHR)$",
    ))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.IMAGE)
        & filters.ChatType.PRIVATE
        & filters.User(user_id=list(ADMIN_IDS)),
        resume_product_upload,
    ))
    app.add_handler(CallbackQueryHandler(shop, pattern=r"^shop$"))
    app.add_handler(CallbackQueryHandler(myorders, pattern=r"^myorders$"))
    app.add_handler(CallbackQueryHandler(home, pattern=r"^home$"))
    app.add_handler(CallbackQueryHandler(product_details, pattern=r"^product:\d+$"))
    app.add_handler(CallbackQueryHandler(buy, pattern=r"^buy:\d+$"))
    app.add_handler(CallbackQueryHandler(generate_qr, pattern=r"^generate_qr:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_ready, pattern=r"^pay_ready:\d+$"))
    app.add_handler(CallbackQueryHandler(download_order, pattern=r"^download:\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern=r"^cancel_order:\d+$"))
    app.add_handler(CallbackQueryHandler(paid_button, pattern=r"^paid:\d+$"))
    app.add_handler(CallbackQueryHandler(review_order, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(review_button, pattern=r"^review:\d+$"))
    app.add_handler(CallbackQueryHandler(
        delete_product,
        pattern=r"^product_delete(?:_confirm|_cancel)?:\d+$",
    ))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_(upload|products|pending)$"))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.ChatType.PRIVATE,
        handle_proof
    ))
    app.add_error_handler(error_handler)
    log.info("Starting %s", SHOP_NAME)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
