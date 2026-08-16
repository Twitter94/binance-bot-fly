import os, time
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

print("TOKEN KEDETEKSI:", "ADA" if TELE_TOKEN else "KOSONG")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("BOT HIDUP v9.0.26")

def main():
    print("BOT v9.0.26 START POLLING")
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    app.run_polling()

if __name__ == "__main__":
    main()
