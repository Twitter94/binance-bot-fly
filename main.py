import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from binance.client import Client

TELE_TOKEN = os.getenv("TELE_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
LOT = float(os.getenv("LOT", 10))
PAIR = "BTCUSDT"

binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"Bot Aktif 🚀\n/saldo /buy /sell /harga")

async def saldo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    acc = binance.get_account()
    usdt = float(next(a['free'] for a in acc['balances'] if a['asset'] == 'USDT'))
    btc = float(next(a['free'] for a in acc['balances'] if a['asset'] == 'BTC'))
    await u.message.reply_text(f"💰 USDT: {usdt:.2f}\nBTC: {btc:.6f}")

async def harga(u: Update, c: ContextTypes.DEFAULT_TYPE):
    price = binance.get_symbol_ticker(symbol=PAIR)['price']
    await u.message.reply_text(f"💲 {PAIR}: ${float(price):,.2f}")

async def buy(u: Update, c: ContextTypes.DEFAULT_TYPE):
    order = binance.order_market_buy(symbol=PAIR, quoteOrderQty=LOT)
    await u.message.reply_text(f"✅ BUY {order['executedQty']} BTC")

async def sell(u: Update, c: ContextTypes.DEFAULT_TYPE):
    btc = float(next(a['free'] for a in binance.get_account()['balances'] if a['asset'] == 'BTC'))
    order = binance.order_market_sell(symbol=PAIR, quantity=btc)
    await u.message.reply_text(f"✅ SELL {order['executedQty']} BTC")

def main():
    app = Application.builder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("harga", harga))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("sell", sell))
    print("Bot jalan...")
    app.run_polling()

if __name__ == "__main__": main()
