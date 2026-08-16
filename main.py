import os
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance.client import Client
from supabase import create_client, Client as SupaClient

# Ambil dari Fly.io Secrets
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
LOT = float(os.getenv("LOT", 10)) # default 10 USDT
PAIR = "BTCUSDT"

# Koneksi
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=False)
supabase: SupaClient = create_client(SUPA_URL, SUPA_KEY)

def log_to_supabase(action, detail):
    supabase.table("logs").insert({
        "pair": PAIR,
        "action": action,
        "detail": detail,
        "created_at": datetime.now().isoformat()
    }).execute()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = f"""Bot BTCUSDT Aktif 🚀
/saldo - Cek saldo USDT
/buy - Beli BTC senilai {LOT} USDT
/sell - Jual semua BTC
/harga - Cek harga BTC sekarang
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
    try:
        order = binance.order_market_buy(
            symbol=PAIR,
            quoteOrderQty=LOT # Beli senilai LOT USDT
        )
        detail = f"Beli {order['executedQty']} BTC @ {order['fills'][0]['price']}"
        log_to_supabase("BUY", detail)
        await update.message.reply_text(f"✅ BUY BERHASIL\n{detail}")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal Buy: {e}")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        btc = float([a for a in binance.get_account()['balances'] if a['asset'] == 'BTC'][0]['free'])
        order = binance.order_market_sell(
            symbol=PAIR,
            quantity=btc
        )
        detail = f"Jual {order['executedQty']} BTC @ {order['fills'][0]['price']}"
        log_to_supabase("SELL", detail)
        await update.message.reply_text(f"✅ SELL BERHASIL\n{detail}")
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal Sell: {e}")

def main():
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
