import os
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance.client import Client

# Supabase optional - biar gak crash kalau gagal
try:
    from supabase import create_client, Client as SupaClient
    SUPA_URL = os.getenv("SUPA_URL")
    SUPA_KEY = os.getenv("SUPA_KEY")
    supabase = create_client(SUPA_URL, SUPA_KEY) if SUPA_URL and SUPA_KEY else None
    print("Supabase OK")
except Exception as e:
    print(f"Supabase skip: {e}")
    supabase = None

# Ambil dari Fly.io Secrets
TELE_TOKEN = os.getenv("TELE_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
LOT = float(os.getenv("LOT", 10))
PAIR = "BTCUSDT"

# Koneksi Binance
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=False)

def log_to_supabase(action, detail):
    if supabase:
        try:
            supabase.table("logs").insert({"pair": PAIR, "action": action, "detail": detail}).execute()
        except: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = f"""Bot BTCUSDT Aktif 🚀
/saldo - Cek saldo
/buy - Beli BTC {LOT} USDT
/sell - Jual semua BTC
/harga - Cek harga
"""
    await update.message.reply_text(msg)

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = binance.get_account()
    usdt = float([a for a in account['balances'] if a['asset'] == 'USDT'][0]['free'])
    btc = float([a for a in account['balances'] if a['asset'] == 'BTC'][0]['free'])
    log_to_supabase("CEK_SALDO", f"USDT:{usdt} BTC:{btc}")
    await update.message.reply_text(f"💰 Saldo\nUSDT: {usdt:.2f}\nBTC: {btc:.6f}")

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = binance.get_symbol_ticker(symbol=PAIR)['price']
    await update.message.reply_text(f"💲 Harga {PAIR}: ${float(price):,.2f}")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = binance.order_market_buy(symbol=PAIR, quoteOrderQty=LOT)
    log_to_supabase("BUY", f"Beli {order['executedQty']} BTC")
    await update.message.reply_text(f"✅ BUY BERHASIL\n{order['executedQty']} BTC")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    btc = float([a for a in binance.get_account()['balances'] if a['asset'] == 'BTC'][0]['free'])
    order = binance.order_market_sell(symbol=PAIR, quantity=btc)
    log_to_supabase("SELL", f"Jual {order['executedQty']} BTC")
    await update.message.reply_text(f"✅ SELL BERHASIL\n{order['executedQty']} BTC")

def main():
    if not TELE_TOKEN or not BINANCE_API_KEY:
        print("ERROR: Secret TELE_TOKEN atau BINANCE_API_KEY kosong")
        return
    app = Application.builder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("harga", harga))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))
    print("Bot jalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
