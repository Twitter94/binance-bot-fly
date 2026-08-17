import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from binance.client import Client

TELE_TOKEN = os.getenv("TELE_TOKEN")
BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")

print("=== BOT START ===")
print("TELE_TOKEN:", "ADA" if TELE_TOKEN else "KOSONG")

binance = Client(BINANCE_KEY, BINANCE_SECRET)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("BOT HIDUP 100% 🚀\nKetik /saldo")

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        acc = binance.get_account()
        usdt = float(next(a['free'] for a in acc['balances'] if a['asset'] == 'USDT'))
        await update.message.reply_text(f"💰 Saldo USDT: {usdt:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def main():
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    
    # INI KUNCINYA: WEBHOOK
    app.run_webhook(
        listen="0.0.0.0",
        port=8080,
        url_path=TELE_TOKEN,
        webhook_url=f"https://bahaya.fly.dev/{TELE_TOKEN}"
    )

if __name__ == "__main__":
    main()
