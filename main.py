import os, time, math, requests, logging, signal, asyncio, gc
from binance.client import Client
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
# HAPUS HTTPXRequest

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
MIN_USDT = 5
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
SELISIH_TOLERANSI = 0.00001
DELAY_FIRST_BUY = 1800

binance = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; app = None
is_executing = False; mode_flexible = True
last_status_msg = ""
bot_start_time = time.time()

# CACHE GLOBAL
last_fee_check = 0; cached_taker_fee = 0.001
cached_price = 0; cached_price_time = 0
cached_positions = []; cached_pos_time = 0
cached_symbol_info = None; cached_atr_grid = 500

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def supa_req(m,u,**k):
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=5,**k)
    except: return None

def get_positions():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&order=buy_price.asc")
    return r.json() if r and r.status_code==200 else []

def get_positions_cache():
    global cached_positions, cached_pos_time
    if time.time() - cached_pos_time < 2:
        return cached_positions
    cached_positions = get_positions()
    cached_pos_time = time.time()
    return cached_positions

def area_aktif(area, positions): return any(p['area'] == area for p in positions)
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]
def get_balance(asset):
    try: return float(binance.get_asset_balance(asset)['free'])
    except: return 0

def get_price():
    try: return float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    except: return 0

def get_price_cache():
    global cached_price, cached_price_time
    if time.time() - cached_price_time < 2:
        return cached_price
    cached_price = get_price()
    cached_price_time = time.time()
    return cached_price

def get_binance_balance_coin():
    try: return float(binance.get_asset_balance(PAIR.replace("USDT",""))['free'])
    except: return 0

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
        if not info: return QTY_FIXED
        step = float(next(f['stepSize'] for f in info['filters'] if f['filterType']=='LOT_SIZE'))
        qty_by_usdt = math.ceil(MIN_USDT/price/step)*step
        qty = max(qty_by_usdt, QTY_FIXED)
        return round(qty, 8)
    except: return QTY_FIXED

def get_fee_binance():
    global last_fee_check, cached_taker_fee
    if time.time() - last_fee_check < 3600:
        return 0.001, cached_taker_fee
    try:
        info = binance.get_trade_fee(symbol=PAIR)
        cached_taker_fee = float(info['tradeFee'][0]['taker'])
        last_fee_check = time.time()
        return 0.001, cached_taker_fee
    except:
        return 0.001, 0.001

KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("STATUS")]], resize_keyboard=True)

async def notif_event(msg):
    if app and TELE_CHAT_ID:
        try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
        except: pass

async def notif_status(msg):
    global last_status_msg
    if not app or not TELE_CHAT_ID: return
    if msg == last_status_msg: return
    try:
        await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=KEYBOARD)
        last_status_msg = msg
    except: pass

async def sinkron_db_dengan_binance():
    positions_db = get_positions_cache()
    balance_coin = get_binance_balance_coin()
    total_qty_db = sum(p['qty'] for p in positions_db)
    if abs(balance_coin - total_qty_db) > SELISIH_TOLERANSI:
        await notif_status(f"SYNC: DB `{total_qty_db:.8f}` vs BINANCE `{balance_coin:.8f}`. RESET DB")
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")

async def satpam_buy(price, area, reason="GRID"):
    global is_executing, mode_flexible
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance()
        qty = get_qty_aman(price)
        _, taker_fee = get_fee_binance()
        usdt_need = price * qty * (1 + taker_fee + BUFFER)
        await notif_event(f"🟢 BUY [{reason}] @`{price:.2f}` AREA `{area}` | QTY `{qty}`")
        if get_balance("USDT") < usdt_need:
            if not await cek_dana_dan_jual(usdt_need, price): return
        order = binance.order_market_buy(symbol=PAIR, quantity=qty)
        if order['status']== 'FILLED':
            supa_req("POST", f"{SUPA_URL}/rest/v1/positions",
                     json={"pair":PAIR,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['orderId'])},
                     headers={**SUPA_HEADERS,"Prefer":"resolution=merge-duplicates"})
            await notif_event(f"🟢 BUY SUKSES QTY `{qty}`")
            mode_flexible = False
    except Exception as e:
        await notif_status(f"⚠️ BUY GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_area(area, positions_in_area, price, mode="BIASA"):
    global is_executing
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance()
        total_qty = sum(p['qty'] for p in positions_in_area)
        _, taker_fee = get_fee_binance()
        await notif_event(f"🔴 SELL [{mode}] AREA `{area}` @`{price:.2f}`")
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        if order['status']== 'FILLED':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&area=eq.{area}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee - BUFFER)
            await notif_event(f"🔴 SELL SELESAI. PROFIT `~{profit:.2f}`")
    except Exception as e:
        await notif_status(f"⚠️ SELL GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_instansemua(all_positions, price):
    global is_executing
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance()
        total_qty = sum(p['qty'] for p in all_positions)
        _, taker_fee = get_fee_binance()
        await notif_event(f"🔴 SELL INSTAN @`{price:.2f}`")
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        if order['status']== 'FILLED':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee)
            await notif_event(f"🔴 SELL INSTAN SELESAI. PROFIT `~{profit:.2f}`")
            await asyncio.sleep(1)
            await satpam_buy(price, get_area(price, last_grid), reason="REENTRY-INSTAN")
    except Exception as e:
        await notif_status(f"⚠️ SELL INSTAN GAGAL: `{e}`")
    finally: is_executing = False

async def cek_dana_dan_jual(usdt_need, price):
    positions = get_positions_cache()
    if not positions: return False
    await notif_status(f"ROLING: SALDO KURANG")
    for area in set(p['area'] for p in positions):
        pos_in_area = get_pos_by_area(area, positions)
        buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
        if price >= buy_terendah_area + last_grid:
            await satpam_sell_area(area, pos_in_area, price, mode="ROLING")
            await asyncio.sleep(2)
            if get_balance("USDT") >= usdt_need:
                await notif_event(f"ROLING SUKSES")
                return True
    return False

async def cek_pengaman_restart(price, positions):
    if not positions: return
    area_tertinggi = max(p['area'] for p in positions)
    area_terendah = min(p['area'] for p in positions)
    if price >= area_tertinggi + last_grid:
        await notif_status(f"⚠️ PENGAMAN: SELL INSTAN")
        await satpam_sell_instansemua(positions, price)
        return
    buy_trigger = area_terendah - last_grid
    if price <= buy_trigger:
        area = get_area(price, last_grid)
        if not area_aktif(area, positions):
            await satpam_buy(price, area, reason="RESTART-DIP")

async def scout_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, base_price_start, mode_flexible
    if is_executing: return
    try:
        price = get_price_cache()
        if price == 0: return
        positions = get_positions_cache()

        if not positions:
            if mode_flexible and (time.time() - bot_start_time) >= DELAY_FIRST_BUY:
                await satpam_buy(price, get_area(price, last_grid), reason="AUTO-START")
            return

        area_tertinggi = max(p['area'] for p in positions)
        if price >= area_tertinggi + last_grid:
            await satpam_sell_instansemua(positions, price)
            return

        for area in set(p['area'] for p in positions):
            pos_in_area = get_pos_by_area(area, positions)
            buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
            if price >= buy_terendah_area + last_grid:
                area_atas = get_area(price + last_grid, last_grid)
                if area_aktif(area_atas, positions):
                    await satpam_sell_area(area, pos_in_area, price, mode="BIASA")
                else:
                    await satpam_sell_area(area, pos_in_area, price, mode="REENTRY")
                    await satpam_buy(price, get_area(price, last_grid), reason="RE-ENTRY")
                return

        buy_trigger = min([p['buy_price'] for p in positions]) - last_grid
        if price <= buy_trigger:
            area = get_area(price, last_grid)
            if not area_aktif(area, positions):
                await satpam_buy(price, area, reason="GRID")
    finally:
        gc.collect()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sinkron_db_dengan_binance()
    price = get_price_cache(); usdt = get_balance("USDT"); pos = get_positions_cache()
    mode = "FLEXIBLE" if mode_flexible else "GRID-KLASIK"
    txt = f"*BOT V29.16 AIOHTTP*\n*Mode:* `{mode}`\n*Harga:* `${price:,.2f}` | *GRID:* `${last_grid:,.2f}`\n*Saldo:* `{usdt:.2f}` | *Posisi:* `{len(pos)}`"
    await update.message.reply_text(txt, reply_markup=KEYBOARD, parse_mode="Markdown")

async def main():
    while True:
        try:
            global app, last_grid, base_price_start, mode_flexible, binance
            logging.info("BOT V29.16 START...")
            await asyncio.sleep(15)

            # FIX: KEMBALI KE DEFAULT AIOHTTP. JANGAN PAKE HTTPXREQUEST
            app = ApplicationBuilder().token(TELE_TOKEN).build()
            
            app.add_handler(CommandHandler("start", status))
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^STATUS$'), status))

            binance = Client(API_KEY, API_SECRET, {"timeout": 5})
            binance.ping()

            await sinkron_db_dengan_binance()
            db = get_positions_cache(); last_grid = get_atr_grid(); base_price_start = get_price_cache()
            if len(db) > 0: mode_flexible = False

            await cek_pengaman_restart(base_price_start, db)

            app.job_queue.run_repeating(scout_loop, interval=3, first=5)
            await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)

            await notif_status("✅ *BOT V29.16 AIOHTTP JALAN*")
            logging.info("BOT V29.16 JALAN...")

            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            await stop.wait(); await app.stop(); await app.shutdown()
            break
        except Exception as e:
            logging.error(f"CRASH: {e}. RESTART 10 DETIK")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
