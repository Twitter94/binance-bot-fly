import os, time, math, requests, logging, signal, asyncio, gc
from binance.client import Client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.WARNING) # WARNING biar log kecil

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 300; MAX_GRID = 800; QTY_FIXED = 0.00001 
MIN_USDT = 5
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
DELAY_FIRST_BUY = 1800

binance = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; app = None
is_executing = False; mode_flexible = True
bot_start_time = time.time()

# CACHE CUMA 3 INI DOANG
cached_price = 0; cached_price_time = 0
cached_atr_grid = 500
cached_symbol_info = None

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0

def supa_req(m,u,**k): # TIMEOUT DIPERKECIL
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=3,**k)
    except: return None

def get_positions():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&select=area,buy_price,qty") # SELECT KOLOM DOANG
    return r.json() if r and r.status_code==200 else []

def get_balance(asset):
    try: return float(binance.get_asset_balance(asset)['free'])
    except: return 0

def get_price():
    try: return float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    except: return 0

def get_price_cache(): # CACHE 3 DETIK
    global cached_price, cached_price_time
    if time.time() - cached_price_time < 3:
        return cached_price
    cached_price = get_price()
    cached_price_time = time.time()
    return cached_price

def get_symbol_info_cache():
    global cached_symbol_info
    if cached_symbol_info: return cached_symbol_info
    try:
        cached_symbol_info = binance.get_symbol_info(PAIR)
        return cached_symbol_info
    except: return None

def get_atr_grid():
    global cached_atr_grid
    if cached_atr_grid!= 500: return cached_atr_grid
    try:
        k = binance.get_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=ATR_PERIOD+1)
        tr = [abs(float(k[i][4])-float(k[i-1][4])) for i in range(1,len(k))]
        atr = sum(tr)/len(tr) if tr else 500
        cached_atr_grid = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
        return cached_atr_grid
    except: return 500

def get_qty_aman(price):
    try:
        info = get_symbol_info_cache()
        step = float(next(f['stepSize'] for f in info['filters'] if f['filterType']=='LOT_SIZE'))
        qty_by_usdt = math.ceil(MIN_USDT/price/step)*step
        return round(max(qty_by_usdt, QTY_FIXED), 8)
    except: return QTY_FIXED

def get_fee_binance(): return 0.001, 0.001 # DI HARDCODE BIAR GAK NEMBAK API

async def notif_event(msg):
    if app and TELE_CHAT_ID:
        try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg) # TANPA PARSE MODE
        except: pass

async def satpam_buy(price, area):
    global is_executing, mode_flexible
    if is_executing: return
    is_executing = True
    try:
        qty = get_qty_aman(price)
        _, taker_fee = get_fee_binance()
        usdt_need = price * qty * (1 + taker_fee + BUFFER)
        if get_balance("USDT") < usdt_need: return

        await notif_event(f"BUY {area} {price}")
        order = binance.order_market_buy(symbol=PAIR, quantity=qty)
        if order['status']== 'FILLED':
            supa_req("POST", f"{SUPA_URL}/rest/v1/positions",
                     json={"pair":PAIR,"area":area,"buy_price":price,"qty":qty},
                     headers={**SUPA_HEADERS,"Prefer":"resolution=merge-duplicates"})
            mode_flexible = False
    except: pass
    finally: is_executing = False

async def satpam_sell_instansemua(all_positions, price):
    global is_executing
    if is_executing: return
    is_executing = True
    try:
        total_qty = sum(p['qty'] for p in all_positions)
        await notif_event(f"SELL INSTAN {price}")
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        if order['status']== 'FILLED':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
            await asyncio.sleep(1)
            await satpam_buy(price, get_area(price, last_grid))
    except: pass
    finally: is_executing = False

async def scout_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, base_price_start, mode_flexible
    if is_executing: return
    try:
        price = get_price_cache()
        if price == 0: return
        positions = get_positions() # GAK PAKE CACHE BIAR RAM TURUN

        if not positions:
            if mode_flexible and (time.time() - bot_start_time) >= DELAY_FIRST_BUY:
                await satpam_buy(price, get_area(price, last_grid))
            return

        area_tertinggi = max(p['area'] for p in positions)
        if price >= area_tertinggi + last_grid:
            await satpam_sell_instansemua(positions, price)
            return

    finally:
        del positions
        gc.collect()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price_cache(); usdt = get_balance("USDT")
    await update.message.reply_text(f"V29.14 LITE\nPrice: {price}\nUSDT: {usdt}")

async def main():
    while True:
        try:
            global app, last_grid, base_price_start, mode_flexible, binance
            await asyncio.sleep(20) # KASIH JEDA LEBIH LAMA

            app = ApplicationBuilder().token(TELE_TOKEN).build()
            app.add_handler(CommandHandler("start", status))

            binance = Client(API_KEY, API_SECRET, {"timeout": 3})
            binance.ping()

            db = get_positions(); last_grid = get_atr_grid(); base_price_start = get_price_cache()
            if len(db) > 0: mode_flexible = False

            app.job_queue.run_repeating(scout_loop, interval=4, first=10) # JADI 4 DETIK
            await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
            await notif_event("V29.14 LITE ON")

            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            await stop.wait(); await app.stop(); await app.shutdown()
            break
        except Exception as e:
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
