import os, time, asyncio, math
from datetime import datetime
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import Bot, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()
wib = pytz.timezone('Asia/Jakarta')

# ===== CONFIG =====
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR")
LOT = float(os.getenv("LOT"))
FEE = 0.001 # 0.1%
BUFFER = 0.003 # 0.3%
ATR_PERIOD, ATR_MULT = 14, 0.5
MIN_GRID, MAX_GRID = 250, 1000

# ===== KONEKSI =====
binance = Client(API_KEY, API_SECRET)
tele_bot = Bot(os.getenv("TELE_TOKEN"))
supa = create_client(os.getenv("SUPA_URL"), os.getenv("SUPA_KEY"))
CHAT_ID = os.getenv("TELE_CHAT_ID")

positions = {} # {harga_buy: qty}
grid_aktif = MIN_GRID
atr_last_update = ""

# ===== FUNGSI INTI =====
async def log_db(level, msg, data={}):
    supa.table("bot_logs").insert({
        "time": datetime.now(wib).isoformat(),
        "level": level, "message": msg, "data": data
    }).execute()

async def send_tele(msg):
    await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard())

def keyboard():
    return ReplyKeyboardMarkup([["STATUS"]], resize_keyboard=True)

def get_atr(): # Poin [1] - Sederhanain dulu pake ATR dari 14 candle 1h
    klines = binance.get_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=ATR_PERIOD+1)
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    trs = [max(h-l, abs(h-closes[i-1]), abs(l-closes[i-1])) for i,(h,l) in enumerate(zip(highs[1:], lows[1:]))]
    atr = sum(trs)/ATR_PERIOD
    new_grid = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULT / 10) * 10))
    return new_grid

def calc_modal_potongan(price): # Poin [3]
    fee_total = LOT * FEE * 2 # buy + sell
    buffer = LOT * BUFFER
    return LOT + fee_total + buffer

async def place_buy(price):
    if price in positions: return # Poin [2.2] Anti dobel
    modal = calc_modal_potongan(price)
    balance = float(binance.get_asset_balance('USDT')['free'])
    if balance < modal:
        await send_tele(f"⚠️ *SALDO KURANG*\nButuh: {modal:.2f} USDT\nPause buy")
        await log_db("PAUSE", "Saldo kurang")
        return

    qty = LOT / price
    for i in range(3): # Poin [2.6] Retry 3x
        try:
            order = binance.order_market_buy(symbol=PAIR, quantity=qty)
            positions[price] = qty
            await log_db("BUY", f"Buy {PAIR} {qty:.6f} @ {price}", {"price": price})
            await send_tele(f"🟢 *BUY*\n{PAIR} @ {price}\nQty: {qty:.6f}")
            break
        except Exception as e: time.sleep(2)

async def place_sell(price_buy):
    qty = positions.pop(price_buy)
    tp_price = price_buy + grid_aktif # Poin [4.1]
    for i in range(3):
        try:
            order = binance.order_market_sell(symbol=PAIR, quantity=qty)
            profit = BUFFER + (qty * grid_aktif) # Poin [5]
            await log_db("SELL", f"Sell {PAIR} {qty:.6f} @ TP", {"profit": profit})
            await send_telegram(f"🔴 *SELL/TP*\n{PAIR} @ {tp_price}\nProfit: {profit:.2f} USDT")
            await place_buy(tp_price) # Poin [2.4] Re-entry
            break
        except Exception as e: time.sleep(2)

# ===== LOOP UTAMA =====
async def main_loop():
    global grid_aktif
    await send_tele("✅ *BOT v7.0 JALAN*")
    while True:
        try:
            price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])

            # Cek ATR shift 20% tiap jam 00:00 WIB
            #... logika ATR_AUTO_SHIFT_20% taruh di sini...

            # Cek TP: harga >= buy + grid
            for buy_price in list(positions.keys()):
                if price >= buy_price + grid_aktif:
                    await place_sell(buy_price)

            # Cek Buy: harga turun 1 grid
            lowest_buy = min(positions.keys()) if positions else price
            if price <= lowest_buy - grid_aktif:
                await place_buy(price)

            time.sleep(2) # Poin [6.2] Anti spam
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n{str(e)}")
            time.sleep(60)

# ===== TELEGRAM COMMAND =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = binance.get_asset_balance('USDT')['free']
    price = binance.get_symbol_ticker(symbol=PAIR)['price']
    msg = f"""*STATUS BOT v7.0*
Saldo: {float(balance):.2f} USDT
Harga: {price}
LOT: {LOT}
Grid Aktif: {grid_aktif}
Total Buy: {len(positions)}
Profit: -
ATR: -
Posisi: {list(positions.keys())}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

app = Application.builder().token(os.getenv("TELE_TOKEN")).build()
app.add_handler(CommandHandler("status", status))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex('STATUS'), status))

asyncio.gather(main_loop(), app.run_polling())
