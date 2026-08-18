import os, time, asyncio, math, httpx
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import pytz

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
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
BUFFER = 0.0005
ATR_SHIFT_PCT = 0.20
ATR_DEFAULT = 500

wib = pytz.timezone("Asia/Jakarta")
binance = Client(API_KEY, API_SECRET)

SUPA_HEADERS = {
    "apikey": SUPA_KEY,
    "Authorization": f"Bearer {SUPA_KEY}",
    "Content-Type": "application/json"
}

# GLOBAL
last_grid = 0
last_atr_check_price = 0
last_grid_update_day = 0
paused = False
symbol_info_cache = None
need_recovery = False
first_buy_base_price = 0
waiting_first_buy = True
first_run = True
last_error_time = 0 # [BARU] ANTI SPAM

async def send_telegram(msg):
    global last_error_time
    # [BARU] kalau ERROR cuma kirim 1x per 60 detik
    if "ERROR" in msg or "GAGAL" in msg or "KRITIS" in msg:
        now = time.time()
        if now - last_error_time < 60:
            return
        last_error_time = now
    try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
    except: pass

def get_area(price, grid):
    if grid <= 0: grid = MIN_GRID
    return math.floor(price / grid) * grid

def supa_select(table):
    url = f"{SUPA_URL}/rest/v1/{table}?pair=eq.{PAIR}&order=buy_price.asc"
    r = httpx.get(url, headers=SUPA_HEADERS, timeout=10)
    return r.json()

def supa_upsert(table, data):
    url = f"{SUPA_URL}/rest/v1/{table}"
    headers = {**SUPA_HEADERS, "Prefer": "resolution=merge-duplicates"}
    httpx.post(url, json=data, headers=headers, timeout=10)

def supa_update(table, where, data):
    url = f"{SUPA_URL}/rest/v1/{table}?{where}"
    httpx.patch(url, json=data, headers=SUPA_HEADERS, timeout=10)

def supa_delete(table, where):
    url = f"{SUPA_URL}/rest/v1/{table}?{where}"
    httpx.delete(url, headers=SUPA_HEADERS, timeout=10)

def get_positions():
    try: return sorted(supa_select("positions"), key=lambda x: x['buy_price'])
    except: return []

def save_position(area, buy_price, qty, fee, order_id):
    data = {"pair": PAIR, "area": area, "buy_price": buy_price, "qty": qty, "fee": fee, "order_id": str(order_id)}
    supa_upsert("positions", data)

def delete_position(area):
    where = f"pair=eq.{PAIR}&area=eq.{area}"
    supa_delete("positions", where)

def update_all_positions_grid(new_grid, old_grid):
    positions = get_positions()
    diff = old_grid - new_grid
    if diff == 0: return
    for pos in positions:
        new_buy_price = pos['buy_price'] - diff
        where = f"pair=eq.{PAIR}&area=eq.{pos['area']}"
        supa_update("positions", where, {"buy_price": new_buy_price})

def get_atr():
    try:
        klines = retry_api(lambda: binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1))
        if not klines or len(klines) < ATR_PERIOD: return ATR_DEFAULT, MIN_GRID
        highs = [float(k[2]) for k in klines]; lows = [float(k[3]) for k in klines]; closes = [float(k[4]) for k in klines]
        trs = [max(h-l, abs(h-closes[i-1]), abs(l-closes[i-1])) for i,(h,l) in enumerate(zip(highs[1:], lows[1:]))]
        if len(trs) == 0: return ATR_DEFAULT, MIN_GRID
        atr = sum(trs)/ATR_PERIOD
        if atr <= 0: atr = ATR_DEFAULT
        grid = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
        return atr, grid
    except: return ATR_DEFAULT, MIN_GRID

def get_symbol_info():
    global symbol_info_cache
    if symbol_info_cache is None: symbol_info_cache = retry_api(binance.get_symbol_info, PAIR)
    return symbol_info_cache

def get_trade_fee_safe(): # [BARU] ANTI CRASH
    try:
        res = retry_api(lambda: binance.get_trade_fee(symbol=PAIR))
        if not res or 'tradeFee' not in res: return 0.001
        return float(res['tradeFee'][0]['maker']) / 100
    except:
        return 0.001

def get_qty_fee_usdtneed(price):
    if price <= 0: return 0,0,0
    info = get_symbol_info()
    if not info: return 0,0,0 # [PENGAMAN]
    try:
        min_notional = float(next(f for f in info['filters'] if f['filterType'] == 'MIN_NOTIONAL')['minNotional'])
        step_size = float(next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')['stepSize'])
    except: return 0,0,0
    qty = QTY_FIXED
    notional = price * qty
    if notional < min_notional: qty = math.ceil(min_notional / price / step_size) * step_size
    fee = get_trade_fee_safe() # [PAKE YG BARU]
    usdt_need = price * qty * (1 + fee*2 + BUFFER)
    return qty, fee, usdt_need

def bisa_buy_1_lot(price):
    qty, fee, usdt_need = get_qty_fee_usdtneed(price)
    if usdt_need == 0: return False
    return get_balance("USDT") >= usdt_need

def retry_api(func, *args, **kwargs):
    for i in range(3):
        try: return func(*args, **kwargs)
        except: time.sleep(2)
    return None

def get_price():
    try: return float(retry_api(binance.get_symbol_ticker, symbol=PAIR)['price'])
    except: return 0

def get_balance(asset):
    try: res = retry_api(binance.get_asset_balance, asset=asset)
    except: res = None
    if not res: return 0
    return float(res['free'])

async def do_buy(price, area, grid):
    global paused, need_recovery
    qty, fee, usdt_need = get_qty_fee_usdtneed(price)
    if usdt_need == 0 or get_balance("USDT") < usdt_need:
        paused = True; await send_telegram(f"PAUSE: SALDO KURANG. Butuh `{usdt_need:.2f}` USDT"); return False
    try: order = retry_api(binance.order_market_buy, symbol=PAIR, quantity=qty); order_id = order['orderId']
    except Exception as e: await send_telegram(f"BUY GAGAL: {e}"); return False
    try: save_position(area, price, qty, fee, order_id)
    except: need_recovery = True; await send_telegram(f"KRITIS: BUY SUKSES TAPI DB GAGAL!"); return False
    await send_telegram(f"BUY @`{price:.2f}`\nLOT: `~{price*qty:.2f}` USDT\nAREA: `{area:.2f}`"); return True

async def do_sell(pos, price, grid, reason="TP"):
    global need_recovery
    try: retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['qty'])
    except Exception as e: await send_telegram(f"SELL GAGAL: {e}"); return False
    try: delete_position(pos['area'])
    except: need_recovery = True
    profit = BUFFER + (pos['qty'] * grid)
    await send_telegram(f"SELL {reason} @`{price:.2f}` +`{profit:.2f}`")
    re_area = get_area(price, grid)
    if not any(p['area'] == re_area for p in get_positions()):
        await do_buy(price, re_area, grid)
    return True

async def sell_all_instant(price, grid):
    for pos in get_positions():
        await do_sell(pos, price, grid, "ATR-NAIK")
        await asyncio.sleep(0.5)

async def trading_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, last_atr_check_price, last_grid_update_day, paused, need_recovery
    global first_buy_base_price, waiting_first_buy, first_run
    
    try:
        now = datetime.now(wib); price = get_price()
        if price <= 0: return
        positions = get_positions()
        if first_run: first_run = False
        if len(positions) > 0: waiting_first_buy = False

        grid = last_grid; area = get_area(price, grid)
        for pos in positions[:]:
            if price >= pos['buy_price'] + grid:
                await do_sell(pos, pos['buy_price'] + grid, grid)
                positions = get_positions()

        update_grid = False; reason = ""; atr_shift_dir = None
        if now.hour == 0 and now.minute == 0 and last_grid_update_day!= now.day:
            update_grid = True; reason = "ATR SHIFT 00:00"; last_grid_update_day = now.day
        if last_grid > 0 and last_atr_check_price > 0:
            change = (price - last_atr_check_price) / last_atr_check_price
            if change >= ATR_SHIFT_PCT: update_grid = True; atr_shift_dir = "NAIK"; reason = "ATR SHIFT 20% NAIK"
            elif change <= -ATR_SHIFT_PCT: update_grid = True; atr_shift_dir = "TURUN"; reason = "ATR SHIFT 20% TURUN"

        if update_grid or last_grid == 0:
            old_grid = last_grid
            _, new_grid = get_atr()
            if atr_shift_dir == "NAIK": await sell_all_instant(price, old_grid); positions = []; waiting_first_buy = True; first_buy_base_price = price
            elif atr_shift_dir == "TURUN": update_all_positions_grid(new_grid, old_grid)
            last_grid = new_grid; last_atr_check_price = price
            await send_telegram(f"{reason} | GRID BARU: `{last_grid}`")
            if first_buy_base_price == 0: first_buy_base_price = price
            grid = last_grid; area = get_area(price, grid)

        if not paused and grid > 0:
            area_aktif = any(p['area'] == area for p in positions)
            if waiting_first_buy and len(positions) == 0:
                if (price >= first_buy_base_price + grid or price <= first_buy_base_price - grid) and not area_aktif:
                    if await do_buy(price, area, grid): waiting_first_buy = False
            elif not waiting_first_buy:
                buy_trigger = min([p['buy_price'] for p in positions]) - grid
                if price <= buy_trigger and not area_aktif: await do_buy(price, area, grid)

        if paused and bisa_buy_1_lot(price): paused = False; await send_telegram(f"LANJUT: SALDO CUKUP")

    except Exception as e: await send_telegram(f"ERROR: {e}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price(); usdt = get_balance("USDT"); positions = get_positions()
    profit = sum([BUFFER + (p['qty'] * last_grid) for p in positions])
    txt = f"*JALAN* | Harga: `{price:.2f}`\nSaldo: `{usdt:.2f}` USDT\nGRID: `{last_grid}` | Posisi: `{len(positions)}`\nProfit TP: `~{profit:.2f}` USDT"
    await update.message.reply_text(txt, parse_mode="Markdown")

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(CommandHandler("start", status))
app.job_queue.run_repeating(trading_loop, interval=3, first=3)

if __name__ == "__main__":
    app.run_polling()
