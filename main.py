import os, time, math, traceback, threading, asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

REQUIRED = ["BINANCE_API_KEY","BINANCE_API_SECRET","TELE_TOKEN","TELE_CHAT_ID","SUPA_URL","SUPA_KEY"]
for key in REQUIRED:
    if not os.getenv(key): 
        print(f"FATAL: ENV {key} KOSONG. BOT MATI.")
        exit(1)

from binance.client import Client
from supabase import create_client, Client as SupaClient
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, Application
from telegram.request import HTTPXRequest
import pandas as pd
import ta

LOCK_FILE = "/tmp/bot.lock"
if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
with open(LOCK_FILE, "w") as f: f.write(str(os.getpid()))

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT_USDT = float(os.getenv("LOT", 5))
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

ATR_PERIOD = 14
ATR_TIMEFRAME = Client.KLINE_INTERVAL_1HOUR
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
QTY_FIXED = 0.00001
BUFFER = 0.001

binance = Client(API_KEY, API_SECRET, requests_params={'timeout': 30})
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY)
bot: Bot = None
application: Application = None
GRID_ATR_AKTIF = MIN_GRID
SUDAH_START = False
PAUSE_BOT = False
RUNNING = True

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

async def send_tele(text):
    global bot
    if not bot: return
    try: await bot.send_message(chat_id=TELE_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e: log(f"Tele error: {e}")

def get_area_grid(price, grid): return math.floor(price / grid) * grid
def get_price():
    try: return float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    except: return 0
def get_balance():
    try: return float(binance.get_asset_balance(asset='USDT')['free'])
    except: return 0
def supa_get_positions():
    try: return supa.table("positions").select("*").eq("pair", PAIR).execute().data
    except: return []

async def get_grid_atr():
    global GRID_ATR_AKTIF
    try:
        klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
        df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qv','n','tbv','tqv','x'])
        df[['h','l','c']] = df[['h','l','c']].astype(float)
        atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=ATR_PERIOD).average_true_range().iloc[-1]
        GRID_ATR_AKTIF = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
    except Exception as e: log(f"Get ATR error: {e}")
    return GRID_ATR_AKTIF

async def main_loop():
    await asyncio.sleep(3)
    await send_tele(f"🚀 *BOT v9.0.29 START*\n*Status:* `JALAN NORMAL`")
    log("BOT v9.0.29 START POLLING")
    
    while RUNNING:
        try:
            await get_grid_atr()
            await asyncio.sleep(5)
        except Exception as e: 
            log("ERROR DI LOOP: " + traceback.format_exc())
            await asyncio.sleep(10)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price(); saldo = get_balance()
    await update.message.reply_text(f"*BOT HIDUP v9.0.29*\n*Harga:* `{price}`\n*Saldo:* `${saldo:.4f}`", parse_mode="Markdown")

async def handle_updates():
    global bot, application
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    application = ApplicationBuilder().token(TELE_TOKEN).request(request).build()
    bot = application.bot
    application.add_handler(CommandHandler("status", status))
    
    await application.initialize()
    await application.start()
    
    offset = 0
    while RUNNING:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for update in updates:
                await application.process_update(update)
                offset = update.update_id + 1
        except Exception as e:
            log(f"Get updates error: {e}")
            await asyncio.sleep(5)

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    threading.Thread(target=lambda: loop.run_until_complete(main_loop()), daemon=True).start()
    loop.run_until_complete(handle_updates())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        RUNNING = False
    finally:
        if os.path.exists(LOCK_FILE): os.remove(LOCK_FILE)
