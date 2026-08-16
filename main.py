import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance.client import Client
from supabase import create_client, Client as SupaClient

# Ambil dari Environment Variable Fly.io
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API = os.getenv("BINANCE_API")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Koneksi
binance = Client(BINANCE_API, BINANCE_SECRET, testnet=False)
supabase: SupaClient = create_client(SUPABASE_URL, SUPABASE_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Binance jalan bos 🚀\nKetik /saldo buat cek saldo")

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = binance.get_account()
    usdt = [a for a in account['balances'] if a['asset'] == 'USDT'][0]
    
    # Simpan ke Supabase
    supabase.table("saldo").insert({
        "chat_id": update.effective_chat.id,
        "usdt": float(usdt['free'])
    }).execute()
    
    await update.message.reply_text(f"Saldo USDT: {usdt['free']}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.run_polling()

if __name__ == "__main__":
    main()
