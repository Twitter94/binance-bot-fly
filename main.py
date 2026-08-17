import os, time, asyncio, math
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from supabase import create_client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import pytz

# [8] ENV WAJIB
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT_ENV = float(os.getenv("LOT", 5))
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

# [1] SETTING ATR & GRID
ATR_PERIOD = 14
ATR_TIMEFRAME = Client.KLINE_INTERVAL_1HOUR
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
QTY_FIXED = 0.00001 # [3] SATPAM 2
BUFFER = 0.0005 # 0.05% buffer

wib = pytz.timezone("Asia/Jakarta")
binance = Client(API_KEY, API_SECRET)
supa = create_client(SUPA_URL, SUPA_KEY)

# Global
last_grid = 0
last_atr_check_price = 0
last_grid_update_day = 0
paused = False
symbol_info_cache = None

async def send_telegram(msg):
    try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
    except: pass

def get_area(price, grid): # [2.2.B] Kelipatan GRID
    return math.floor(price / grid) * grid

def get_positions(): # [6] AMBIL DARI SUPABASE
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return res.data

def save_position(area, buy_price, qty, lot, fee):
    data = {"pair": PAIR, "area": area, "buy_price": buy_price, "qty": qty, "lot": lot, "fee": fee}
    supa.table("positions").upsert(data, on_conflict="pair,area").execute()

def delete_position(area):
    supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

def get_atr(): # [1] HITUNG ATR
    klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    highs = [float(k[2]) for k in klines]; lows = [float(k[3]) for k in klines]; closes = [float(k[4]) for k in klines]
    trs = [max(h-l, abs(h-closes[i-1]), abs(l-closes[i-1])) for i,(h,l) in enumerate(zip(highs[1:], lows[1:]))]
    atr = sum(trs)/ATR_PERIOD
    grid = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
    return atr, grid

def get_symbol_info(): # Cache [3]
    global symbol_info_cache
    if symbol_info_cache is None: symbol_info_cache = binance.get_symbol_info(PAIR)
    return symbol_info_cache

def get_lot_and_fee(price): # [3] 2 SATPAM BINANCE
    info = get_symbol_info()
    lot_filter = next(f for f in info['filters'] if f['filterType'] == 'MIN_NOTIONAL')
    min_notional = float(lot_filter['minNotional']) # SATPAM 1: $5

    lot = LOT_ENV
    notional = price * QTY_FIXED
    if notional < min_notional: # Jika 0.00001 * harga < 5
        lot = math.ceil(min_notional / price / QTY_FIXED) * QTY_FIXED # Naikin lot

    fee = float(binance.get_trade_fee(symbol=PAIR)['tradeFee'][0]['maker']) / 100 # Fee rill
    return lot, fee

def retry_api(func, *args, **kwargs): # [2.6] RETRY 3X
    for i in range(3):
        try: return func(*args, **kwargs)
        except BinanceAPIException as e:
            time.sleep(2)
            if i==2: raise e
    return None

def get_price(): return float(retry_api(binance.get_symbol_ticker, symbol=PAIR)['price'])
def get_balance(asset): return float(retry_api(binance.get_asset_balance, asset=asset)['free'])

async def do_buy(price, area, grid):
    lot, fee = get_lot_and_fee(price) # [2.3] Ambil rill
    qty = QTY_FIXED
    usdt_need = lot + (lot * fee * 2) + (lot * BUFFER) # [3] MODAL_POTONG

    if get_balance("USDT") < usdt_need:
        global paused
        paused = True
        await send_telegram(f"PAUSE: SALDO KURANG. Butuh `{usdt_need:.2f}` USDT")
        return False

    order = retry_api(binance.order_market_buy, symbol=PAIR, quantity=qty)
    save_position(area, price, qty, lot, fee) # [6] SIMPAN SUPABASE
    await send_telegram(f"BUY @`{price:.2f}` | AREA: `{area:.2f}`") # [7.3]
    return True

async def do_sell(pos, price, grid):
    lot, fee = get_lot_and_fee(price) # Ambil rill
    order = retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['qty'])
    profit = BUFFER + (pos['qty'] * grid) # [5] RUMUS PROFIT
    delete_position(pos['area']) # [6] HAPUS SUPABASE

    # [4.3] RE-ENTRY BERSYARAT
    re_area = get_area(price, grid)
    positions = get_positions()
    area_aktif = any(p['area'] == re_area for p in positions)

    if not area_aktif: # 3A: KOSONG
        await do_buy(price, re_area, grid)
        await send_telegram(f"SELL @`{price:.2f}` +`{profit:.2f}` -> RE-ENTRY BUY @`{re_area:.2f}`")
    else: # 3B: MASIH AKTIF
        await send_telegram(f"SELL @`{price:.2f}` +`{profit:.2f}` | AREA MASIH AKTIF. SKIP RE-ENTRY")

async def trading_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, last_atr_check_price, last_grid_update_day, paused
    first_buy_base_price = 0 # [11] PATOKAN HARGA START
    waiting_first_buy = True

    while True:
        try:
            now = datetime.now(wib)
            price = get_price()
            positions = get_positions()

            update_grid = False
            reason = ""

            # [1] SATPAM 1: 00:00 WIB
            if now.hour == 0 and now.minute == 0 and last_grid_update_day!= now.day:
                update_grid = True; reason = "ATR SHIFT 00:00"; last_grid_update_day = now.day

            # [1] SATPAM 2: 20%
            if last_grid > 0 and last_atr_check_price > 0:
                change = abs(price - last_atr_check_price) / last_atr_check_price
                if change >= 0.20:
                    update_grid = True; reason = f"ATR SHIFT 20%"

            if update_grid or last_grid == 0: # [11] JIKA BARU START ATAU GANTI GRID
                atr, new_grid = get_atr()
                last_grid = new_grid
                last_atr_check_price = price
                if reason: await send_telegram(f"{reason} | GRID BARU: `{last_grid}`")
                if first_buy_base_price == 0: first_buy_base_price = price # CATAT HARGA START

            grid = last_grid
            area = get_area(price, grid)

            # [4.4] CEK TP JIKA BOT OFF/ON
            for pos in positions[:]: # pakai [:] biar aman pas delete
                tp_price = pos['buy_price'] + grid
                if price >= tp_price:
                    await do_sell(pos, tp_price, grid)
                    positions = get_positions() # refresh setelah sell

            # [2] ATURAN BUY + [11] START FLEKSIBEL
            if not paused and grid > 0:
                area_aktif = any(p['area'] == area for p in positions)

                # [11] FASE 1: BELUM PERNAH BUY SAMA SEKALI
                if waiting_first_buy and len(positions) == 0:
                    buy_up = first_buy_base_price + grid # naik 1 grid dari harga start
                    buy_down = first_buy_base_price - grid # turun 1 grid dari harga start

                    if price >= buy_up or price <= buy_down: # NUNGGU LARI 1 GRID DULU
                        if await do_buy(price, area, grid):
                            waiting_first_buy = False # SETELAH INI MASUK MODE KAKU

                # [2] FASE 2: SETELAH BUY PERTAMA = MODE KAKU NORMAL
                elif not waiting_first_buy:
                    buy_trigger_price = min([p['buy_price'] for p in positions]) - grid # [2.1] TURUN 1 GRID
                    if price <= buy_trigger_price and not area_aktif: # [2.2.A][2.2.B]
                        await do_buy(price, area, grid)

            # [4] UNPAUSE JIKA SALDO ADA
            if paused and get_balance("USDT") > LOT_ENV * 1.1:
                paused = False; await send_telegram("LANJUT: SALDO SUDAH DIISI")

        except Exception as e: await send_telegram(f"ERROR: {e}")
        await asyncio.sleep(30) # Hemat RAM

# [7] TELEGRAM
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price(); usdt = get_balance("USDT")
    positions = get_positions()
    profit = sum([BUFFER + (p['qty'] * last_grid) for p in positions])
    status_txt = "JALAN" if not paused else "PAUSE"
    txt = f"*{status_txt}* | Harga: `{price:.2f}`\nSaldo: `{usdt:.2f}` USDT\nGrid: `{last_grid}` | LOT: `{LOT_ENV}` | Profit: `{profit:.2f}`\nPosisi: `{len(positions)}`"
    keyboard = [[InlineKeyboardButton("STATUS", callback_data='status')]]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await status(update, context)

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(CommandHandler("start", status))
app.add_handler(CallbackQueryHandler(button))
app.job_queue.run_repeating(trading_loop, interval=30, first=5)

if __name__ == "__main__":
    app.run_polling()
