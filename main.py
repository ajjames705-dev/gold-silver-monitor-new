import os
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------------- SETTINGS ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", "60"))

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "subscribers.sqlite3"

GOLD_API = "https://api.gold-api.com/price"
FX_API = "https://api.frankfurter.dev/v2/rate/USD/INR"

GRAMS_PER_TROY_OUNCE = 31.1034768
MAX_QUOTE_AGE = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger(__name__)
http = requests.Session()

fx_cache = {
    "rate": None,
    "date": None,
    "saved_at": None,
}


# ---------------- DATABASE ----------------

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)
        db.commit()


def add_subscriber(chat_id):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT OR IGNORE INTO subscribers VALUES (?, ?)",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def remove_subscriber(chat_id):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "DELETE FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        db.commit()


def get_subscribers():
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT chat_id FROM subscribers"
        ).fetchall()

    return [row[0] for row in rows]


# ---------------- PRICE APIS ----------------

def get_json(url):
    response = http.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def get_metal_price(symbol):
    data = get_json(f"{GOLD_API}/{symbol}")

    price = float(data["price"])
    updated = datetime.fromisoformat(
        data["updatedAt"].replace("Z", "+00:00")
    )

    age = (
        datetime.now(timezone.utc) - updated
    ).total_seconds()

    if price <= 0 or age < -60 or age > MAX_QUOTE_AGE:
        raise ValueError(f"{symbol} price is invalid or stale")

    return price, updated


def get_usd_inr():
    now = datetime.now(timezone.utc)

    # Reuse the daily exchange rate for six hours.
    if (
        fx_cache["rate"] is not None
        and fx_cache["saved_at"] is not None
        and now - fx_cache["saved_at"] < timedelta(hours=6)
    ):
        return fx_cache["rate"], fx_cache["date"]

    try:
        data = get_json(FX_API)

        rate = float(data["rate"])
        if rate <= 0:
            raise ValueError("Invalid USD-INR rate")

        fx_cache["rate"] = rate
        fx_cache["date"] = data["date"]
        fx_cache["saved_at"] = now

        return rate, data["date"]

    except Exception:
        if fx_cache["rate"] is not None:
            log.warning("Using previously cached USD-INR rate")
            return fx_cache["rate"], fx_cache["date"]

        raise


def get_all_rates():
    gold, gold_time = get_metal_price("XAU")
    silver, silver_time = get_metal_price("XAG")
    usd_inr, fx_date = get_usd_inr()

    gold_inr_gram = (
        gold * usd_inr / GRAMS_PER_TROY_OUNCE
    )

    silver_inr_kg = (
        silver * usd_inr
        / GRAMS_PER_TROY_OUNCE * 1000
    )

    return {
        "gold": gold,
        "silver": silver,
        "gold_inr": gold_inr_gram,
        "silver_inr": silver_inr_kg,
        "usd_inr": usd_inr,
        "fx_date": fx_date,
        "gold_time": gold_time,
        "silver_time": silver_time,
    }


# ---------------- MESSAGE ----------------

def format_message(data):
    return (
        "🥇 GOLD & SILVER LIVE SPOT RATES\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🌍 INTERNATIONAL PRICES\n"
        f"Gold: ${data['gold']:,.2f} / troy oz\n"
        f"Silver: ${data['silver']:,.2f} / troy oz\n\n"
        "🇮🇳 INDIA — INR CONVERTED SPOT\n"
        f"Gold: ₹{data['gold_inr']:,.2f} / gram\n"
        f"Silver: ₹{data['silver_inr']:,.2f} / kg\n\n"
        "💱 USD/INR reference\n"
        f"₹{data['usd_inr']:,.4f} "
        f"(rate date: {data['fx_date']})\n\n"
        "🕒 Quote times (UTC)\n"
        f"Gold: {data['gold_time'].strftime('%H:%M:%S')}\n"
        f"Silver: {data['silver_time'].strftime('%H:%M:%S')}\n\n"
        "⚠️ India prices are converted international spot "
        "estimates, NOT MCX futures or jeweller rates. "
        "Taxes, duties and local premiums are excluded."
    )


# ---------------- TELEGRAM COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    add_subscriber(chat_id)

    await update.message.reply_text(
        "✅ Gold & Silver Monitor activated!\n\n"
        f"You will receive a new rate message every "
        f"{UPDATE_INTERVAL} seconds.\n\n"
        "Commands:\n"
        "/rates - Get prices now\n"
        "/status - Check bot status\n"
        "/stop - Stop automatic messages"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "Automatic messages stopped. "
        "Send /start to subscribe again."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = len(get_subscribers())

    await update.message.reply_text(
        "✅ Bot is running\n"
        f"Update interval: {UPDATE_INTERVAL} seconds\n"
        f"Subscribers: {count}\n"
        f"Database: {DB_PATH}"
    )


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = get_all_rates()
        await update.message.reply_text(format_message(data))

    except Exception:
        log.exception("Price request failed")
        await update.message.reply_text(
            "Prices temporarily unavailable. Please try again."
        )


# ---------------- AUTOMATIC UPDATES ----------------

async def scheduled_update(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = get_subscribers()

    if not chat_ids:
        return

    try:
        data = get_all_rates()
        message = format_message(data)

    except Exception:
        log.exception("Scheduled price update failed")
        return

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
            )
        except Exception:
            log.exception(
                "Could not send message to %s", chat_id
            )


# ---------------- START BOT ----------------

async def post_init(application):
    init_db()

    application.job_queue.run_repeating(
        scheduled_update,
        interval=UPDATE_INTERVAL,
        first=10,
        name="automatic-rates",
    )

    log.info("Automatic price updates started")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("rates", rates))
    app.add_handler(CommandHandler("prices", rates))

    log.info("Gold and Silver Telegram Bot starting")

    app.run_polling()


if __name__ == "__main__":
    main()
        
