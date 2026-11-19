import os
import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
import requests

TOKEN = os.environ["BOT_TOKEN"]

GOLD_URL = "https://api.gold-api.com/price/XAU"
SILVER_URL = "https://api.gold-api.com/price/XAG"
BACKUP_URL = "https://xaus.com/api/v1/spot?compact=1"

INTERVAL = 60
MAX_AGE = 180

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

subscribers = set()
last_prices = {}
last_errors = set()


def parse_time(value):
    if not value:
        raise ValueError("Missing timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def valid_quote(data):
    price = float(data["price"])
    timestamp = parse_time(data["updatedAt"])
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()

    if price <= 0 or age < -30 or age > MAX_AGE:
        raise ValueError("Invalid or stale quote")

    return price, timestamp


def get_primary(url):
    response = requests.get(url, timeout=12)
    response.raise_for_status()
    return valid_quote(response.json())


def get_gold():
    try:
        price, timestamp = get_primary(GOLD_URL)
        return price, timestamp, "Gold API"
    except Exception as error:
        log.warning("Primary gold API failed: %s", error)

    try:
        response = requests.get(BACKUP_URL, timeout=12)
        response.raise_for_status()
        data = response.json()

        state = data.get("data_state", {})
        if state.get("status") != "fresh":
            raise ValueError("XAUS gold data is not fresh")

        timestamp = parse_time(data["updated_at"])
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        price = float(data["spot_usd_oz"])

        if price <= 0 or age < -30 or age > MAX_AGE:
            raise ValueError("XAUS quote is stale")

        if data.get("stale") is True:
            raise ValueError("XAUS marks quote stale")

        return price, timestamp, "XAUS backup"

    except Exception as error:
        log.error("Gold backup failed: %s", error)
        raise RuntimeError("No verified gold quote available")


def get_silver():
    price, timestamp = get_primary(SILVER_URL)
    return price, timestamp, "Gold API"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribers.add(chat_id)

    await update.message.reply_text(
        "Gold & Silver Monitor activated!\n"
        "Updates are checked every 60 seconds.\n"
        "Commands: /prices /stop /status"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.discard(update.effective_chat.id)
    await update.message.reply_text("Price notifications stopped.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Monitoring interval: {INTERVAL} seconds\n"
        f"Subscribers: {len(subscribers)}\n"
        "Use /prices to request current quotes."
    )


async def send_prices(context, force=False):
    if not subscribers:
        return

    results = {}

    for name, function in (
        ("Gold", get_gold),
        ("Silver", get_silver)
    ):
        try:
            price, timestamp, source = function()
            results[name] = (price, timestamp, source)
            last_errors.discard(name)
        except Exception as error:
            log.exception("%s quote unavailable", name)
            results[name] = None

            if name not in last_errors:
                last_errors.add(name)
                for chat_id in list(subscribers):
                    try:
                        await context.bot.send_message(
                            chat_id,
                            f"WARNING: {name} price unavailable.\n"
                            "No stale price is being reported as live."
                        )
                    except Exception:
                        log.exception("Telegram warning failed")

    for name, quote in results.items():
        if quote is None:
            continue

        price, timestamp, source = quote
        changed = last_prices.get(name) != price

        if force or changed:
            message = (
                f"{'GOLD' if name == 'Gold' else 'SILVER'} "
                f"({ 'XAU' if name == 'Gold' else 'XAG' }/USD)\n"
                f"Price: ${price:,.2f} per troy ounce\n"
                f"Source: {source}\n"
                f"Quote time: {timestamp.isoformat()}\n"
                f"Checked: {datetime.now(timezone.utc).isoformat()}"
            )

            for chat_id in list(subscribers):
                try:
                    await context.bot.send_message(chat_id, message)
                except Exception:
                    log.exception("Telegram message failed")

            last_prices[name] = price


async def prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subscribers.add(update.effective_chat.id)
    await send_prices(context, force=True)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    await send_prices(context)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("prices", prices))

    app.job_queue.run_repeating(
        scheduled_check,
        interval=INTERVAL,
        first=5
    )

    log.info("Gold and Silver monitor starting")
    app.run_polling()


if __name__ == "__main__":
    main()
