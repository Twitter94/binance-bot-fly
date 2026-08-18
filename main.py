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
QTY_FIXED = 0.00001
BUFFER = 0.0005
ATR_SHIFT_PCT = 0.20 # 20%
ATR_DEFAULT = 500 # DEFAULT KALAU ATR 0

wib = pytz.timezone("Asia/Jakarta")
binance = Client(API_KEY, API_SECRET)
supa = create_client(SUPA_URL, SUPA_KEY)

# ========== GLOBAL ==========
last_grid = 0
last_atr_check_price = 0
last_grid_update_day = 0
paused = False
symbol_info_cache = None
need_recovery = False

async def send_telegram(msg):
    try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
    except: pass

def get_area(price, grid):
    if grid <= 0: grid = MIN_GRID
    return math.floor(price / grid) * grid

def get_positions():
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return sorted(res.data, key=lambda x: x['buy_price'])

def save_position(area, buy_price, qty, fee, order_id):
    data = {"pair": PAIR, "area": area, "buy_price": buy_price, "qty": qty, "fee": fee, "order_id": str(order_id)}
    supa.table("positions").upsert(data, on_conflict="pair,area").execute()

def delete_position(area):
    supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

def update_all_positions_grid(new_grid, old_grid):
    positions = get_positions()
    diff = old_grid - new_grid
    if diff == 0: return
    for pos in positions:
        new_buy_price = pos['buy_price'] - diff
        supa.table("positions").update({"buy_price": new_buy_price}).eq("pair", PAIR).eq("area", pos['area']).execute()

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
    except Exception as e:
        asyncio.run(send_telegram(f"ERROR get_atr: {e}"))
        return ATR_DEFAULT, MIN_GRID

def get_symbol_info():
    global symbol_info_cache
    if symbol_info_cache is None: symbol_info_cache = retry_api(binance.get_symbol_info, PAIR)
    return symbol_info_cache

def get_qty_fee_usdtneed(price):
    if price <= 0: return 0,0,0
    info = get_symbol_info()
    min_notional = float(next(f for f in info['filters'] if f['filterType'] == 'MIN_NOTIONAL')['minNotional'])
    step_size = float(next(f for f in info['filters'] if f['filterType'] == 'LOT_SIZE')['stepSize'])
    qty = QTY_FIXED
    notional = price * qty
    if notional < min_notional:
        qty = math.ceil(min_notional / price / step_size) * step_size
    fee = float(retry_api(lambda: binance.get_trade_fee(symbol=PAIR))['tradeFee'][0]['maker']) / 100
    usdt_need = price * qty * (1 + fee*2 + BUFFER)
    return qty, fee, usdt_need

def bisa_buy_1_lot(price):
    qty, fee, usdt_need = get_qty_fee_usdtneed(price)
    if usdt_need == 0: return False
    return get_balance("USDT") >= usdt_need

def retry_api(func, *args, **kwargs):
    for i in range(3):
        try: return func(*args, **kwargs)
        except BinanceAPIException as e:
            time.sleep(2)
            if i==2: raise e
    return None

def get_price():
    try: return float(retry_api(binance.get_symbol_ticker, symbol=PAIR)['price'])
    except: return 0

def get_balance(asset):
    try: return float(retry_api(binance.get_asset_balance, asset=asset)['free'])
    except: return 0

async def check_ghost_order():
    asset = PAIR.replace("USDT","")
    bal = get_balance(asset)
    db_pos = get_positions()
    if bal > 0 and len(db_pos) == 0:
        await send_telegram(f"WARNING KRITIS: ADA `{bal}` {asset} DI BINANCE TAPI KOSONG DI DB!")
    elif bal == 0 and len(db_pos) > 0:
        await send_telegram(f"WARNING: DB ADA `{len(db_pos)}` POSISI TAPI SALDO {asset}=0. MUNGKIN SELL MANUAL?")

async def sync_db_with_binance():
    global need_recovery
    if last_grid == 0:
        need_recovery = False
        return
    try:
        trades = retry_api(binance.get_my_trades, symbol=PAIR, limit=100)
        db_positions = get_positions()
        db_map = {p['area']: p for p in db_positions}
        found_ghost = False
        for trade in trades:
            order_id = str(trade['orderId']); side = trade['side']; price = float(trade['price']); qty = float(trade['qty']); area = get_area(price, last_grid)
            if side == 'BUY' and area not in db_map:
                fee = float(trade['commission']); save_position(area, price, qty, fee, order_id)
                await send_telegram(f"AUTO RECOVERY: BUY `{order_id}` ketemu. DB di sync. Area:`{area}`"); found_ghost = True
            if side == 'SELL' and area in db_map:
                pos = db_map[area]
                if abs(pos['buy_price'] + last_grid - price) < 10:
                    delete_position(area)
                    await send_telegram(f"AUTO RECOVERY: SELL `{order_id}` ketemu. DB dihapus. Area:`{area}`"); found_ghost = True
        if not found_ghost: await send_telegram("AUTO RECOVERY: SELESAI. TIDAK ADA HANTU DITEMUKAN")
    except Exception as e: await send_telegram(f"ERROR AUTO RECOVERY: {e}")
    need_recovery = False

async def do_buy(price, area, grid):
    global paused, need_recovery
    qty, fee, usdt_need = get_qty_fee_usdtneed(price)
    if usdt_need == 0 or get_balance("USDT") < usdt_need:
        paused = True
        await send_telegram(f"PAUSE: SALDO KURANG. Butuh `{usdt_need:.2f}` USDT")
        return False
    try: order = retry_api(binance.order_market_buy, symbol=PAIR, quantity=qty); order_id = order['orderId']
    except Exception as e: await send_telegram(f"BUY GAGAL: {e}"); return False
    try: save_position(area, price, qty, fee, order_id)
    except Exception as e:
        need_recovery = True
        await send_telegram(f"KRITIS: BUY SUKSES TAPI DB GAGAL! OrderID:`{order_id}`. AUTO RECOVERY AKAN DIJALANKAN!")
        return False
    usdt_terpakai = price * qty
    await send_telegram(f"BUY @`{price:.2f}`\nQTY: `{qty}`\nLOT: `~{usdt_terpakai:.2f}` USDT\nAREA: `{area:.2f}`\nID: `{order_id}`")
    return True

async def do_sell(pos, price, grid, reason="TP"):
    try: order = retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['qty'])
    except Exception as e: await send_telegram(f"SELL GAGAL: {e}"); return False
    try: delete_position(pos['area'])
    except Exception as e:
        global need_recovery; need_recovery = True
        await send_telegram(f"KRITIS: SELL SUKSES TAPI DB GAGAL HAPUS! Area:`{pos['area']}`")

    profit = BUFFER + (pos['qty'] * grid)
    await send_telegram(f"SELL {reason} @`{price:.2f}` +`{profit:.2f}` | AREA:`{pos['area']}`")

    re_area = get_area(price, grid)
    positions = get_positions()
    area_aktif = any(p['area'] == re_area for p in positions)
    if not area_aktif:
        await do_buy(price, re_area, grid)
        await send_telegram(f"-> RE-ENTRY BUY @`{re_area:.2f}`")
    else:
        await send_telegram(f"| AREA MASIH AKTIF. SKIP RE-ENTRY")
    return True

async def sell_all_instant(price, grid, reason):
    positions = get_positions()
    if len(positions) == 0: return
    await send_telegram(f"ATR SHIFT {reason} 20%! SELL INSTAN SEMUA `{len(positions)}` POSISI")
    for pos in positions:
        await do_sell(pos, price, grid, reason=f"ATR-{reason}")
        await asyncio.sleep(0.5)

async def trading_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, last_atr_check_price, last_grid_update_day, paused, need_recovery
    first_buy_base_price = 0; waiting_first_buy = True; first_run = True

    while True:
        try:
            now = datetime.now(wib); price = get_price()
            if price <= 0: await asyncio.sleep(3); continue
            positions = get_positions()
            if first_run: await check_ghost_order(); first_run = False
            if len(positions) > 0: waiting_first_buy = False

            if need_recovery:
                await send_telegram("MENJALANKAN AUTO RECOVERY SEKARANG...")
                await sync_db_with_binance(); await asyncio.sleep(5); positions = get_positions()

            grid = last_grid; area = get_area(price, grid)
            for pos in positions[:]:
                tp_price = pos['buy_price'] + grid
                if price >= tp_price:
                    await do_sell(pos, tp_price, grid)
                    positions = get_positions()
                    grid = last_grid; area = get_area(price, grid)

            update_grid = False; reason = ""; atr_shift_dir = None
            if now.hour == 0 and now.minute == 0 and last_grid_update_day!= now.day:
                update_grid = True; reason = "ATR SHIFT 00:00"; last_grid_update_day = now.day
            if last_grid > 0 and last_atr_check_price > 0:
                change = (price - last_atr_check_price) / last_atr_check_price
                if change >= ATR_SHIFT_PCT:
                    update_grid = True; atr_shift_dir = "NAIK"; reason = "ATR SHIFT 20% NAIK"
                elif change <= -ATR_SHIFT_PCT:
                    update_grid = True; atr_shift_dir = "TURUN"; reason = "ATR SHIFT 20% TURUN"

            if update_grid or last_grid == 0:
                old_grid = last_grid
                _, new_grid = get_atr()
                if atr_shift_dir == "NAIK":
                    await sell_all_instant(price, old_grid, "NAIK")
                    positions = []; waiting_first_buy = True; first_buy_base_price = price
                elif atr_shift_dir == "TURUN":
                    await send_telegram(f"ATR SHIFT TURUN 20%! Grid: `{old_grid}` -> `{new_grid}`. UPDATE TP SEMUA POSISI")
                    update_all_positions_grid(new_grid, old_grid)
                last_grid = new_grid
                last_atr_check_price = price
                await send_telegram(f"{reason} | GRID BARU: `{last_grid}`")
                if first_buy_base_price == 0: first_buy_base_price = price
                grid = last_grid; area = get_area(price, grid)

            if not paused and grid > 0:
                area_aktif = any(p['area'] == area for p in positions)
                if waiting_first_buy and len(positions) == 0:
                    buy_up = first_buy_base_price + grid; buy_down = first_buy_base_price - grid
                    if (price >= buy_up or price <= buy_down) and not area_aktif:
                        if await do_buy(price, area, grid): waiting_first_buy = False
                elif not waiting_first_buy:
                    buy_trigger_price = min([p['buy_price'] for p in positions]) - grid
                    if price <= buy_trigger_price and not area_aktif: await do_buy(price, area, grid)

            if paused and bisa_buy_1_lot(price): paused = False; await send_telegram(f"LANJUT: SALDO CUKUP | GRID: `{grid}`")

        except Exception as e: await send_telegram(f"ERROR LOOP: {e}")
        await asyncio.sleep(3)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price(); usdt = get_balance("USDT"); positions = get_positions()
    profit = sum([BUFFER + (p['qty'] * last_grid) for p in positions])
    status_txt = "JALAN" if not paused else "PAUSE"
    lot_dibutuhkan = len(positions) + 10 if len(positions) > 0 else 20
    txt = f"*{status_txt}* | Harga: `{price:.2f}`\n"
    txt += f"Saldo: `{usdt:.2f}` USDT\n"
    txt += f"GRID AKTIF: `{last_grid}` | Posisi: `{len(positions)}`\n"
    txt += f"Lot Dibutuhkan: `~{lot_dibutuhkan:.2f}` LOT\n"
    txt += f"Profit TP: `~{profit:.2f}` USDT"
    keyboard = [[InlineKeyboardButton("REFRESH STATUS", callback_data='status')]]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await status(update, context)

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(CommandHandler("start", status))
app.add_handler(CallbackQueryHandler(button))
app.job_queue.run_repeating(trading_loop, interval=3, first=3)

if __name__ == "__main__":
    app.run_polling()
