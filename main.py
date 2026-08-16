import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance.client import Client

TELE_TOKEN = os.getenv("TELE_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

print("TELE_TOKEN ada:", bool(TELE_TOKEN))

if not TELE_TOKEN:
    print("ERROR FATAL: TELE_TOKEN KOSONG!")
    exit(1)

binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Bot Aktif 100% 🚀")

def main():
    print("Starting polling...")
    app = Application.builder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(drop_pending_updates=True) # ini biar gak langsung mati

if __name__ == "__main__": main()
