import os, time, math, requests, logging, signal, asyncio
from binance.client import Client
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
FEE = 0.001
SELISIH_TOLERANSI = 0.00001

binance = Client(API_KEY, API_SECRET, {"timeout": 3})
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; app = None
is_executing = False; mode_flexible = True
last_status_msg = ""

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def supa_req(m,u,**k):
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=3,**k)
    except: return None
def get_positions():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&order=buy_price.asc")
    return r.json() if r and r.status_code==200 else []
def area_aktif(area, positions): return any(p['area'] == area for p in positions)
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]
def get_balance(asset):
    try: return float(binance.get_asset_balance(asset)['free'])
    except: return 0
def get_price():
    try: return float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    except: return 0
def get_binance_balance_coin():
    try: return float(binance.get_asset_balance(PAIR.replace("USDT",""))['free'])
    except: return 0
def get_atr_grid():
    try:
        k = binance.get_klines(symbol=PAIR, interval=Client.KLINE_INTERVAL_1HOUR, limit=ATR_PERIOD+1)
        tr = [abs(float(k[i][4])-float(k[i-1][4])) for i in range(1,len(k))]
        atr = sum(tr)/len(tr) if tr else 500
        return max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
    except: return MIN_GRID
def get_qty(price):
    try:
        info = binance.get_symbol_info(PAIR)
        min_n = float(next(f['minNotional'] for f in info['filters'] if f['filterType']=='MIN_NOTIONAL'))
        step = float(next(f['stepSize'] for f in info['filters'] if f['filterType']=='LOT_SIZE'))
        qty = QTY_FIXED
        if price*qty < min_n: qty = math.ceil(min_n/price/step)*step
        return round(qty, 8)
    except: return QTY_FIXED

KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("STATUS")]], resize_keyboard=True, one_time_keyboard=False)

async def notif_event(msg): # 1. LAPORAN SPAM
    if app and TELE_CHAT_ID:
        try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
        except: pass

async def notif_status(msg): # 2. PERINGATAN 1X
    global last_status_msg
    if not app or not TELE_CHAT_ID: return
    if msg == last_status_msg: return
    try:
        await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
        last_status_msg = msg
    except: pass

async def sinkron_db_dengan_binance():
    positions_db = get_positions()
    balance_coin = get_binance_balance_coin()
    total_qty_db = sum(p['qty'] for p in positions_db)
    selisih = abs(balance_coin - total_qty_db)
    if selisih > SELISIH_TOLERANSI and total_qty_db > balance_coin:
        await notif_status(f"SYNC: DB `{total_qty_db:.8f}` > BINANCE `{balance_coin:.8f}`. RESET DB")
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
    elif selisih > SELISIH_TOLERANSI and balance_coin > total_qty_db:
        await notif_status(f"SYNC: BINANCE `{balance_coin:.8f}` > DB `{total_qty_db:.8f}`. RESET DB")
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")

async def satpam_buy(price, area, reason="GRID"):
    global is_executing, mode_flexible
    if is_executing: return # SATPAM 1
    is_executing = True
    await sinkron_db_dengan_binance() # SYNC PAS MAU BUY
    await notif_event(f"BUY [{reason}] @`{price:.2f}` AREA `{area}`")
    try:
        qty = get_qty(price)
        usdt_need = price * qty * (1 + FEE*2 + BUFFER)
        if get_balance("USDT") < usdt_need:
            if not await cek_dana_dan_jual(usdt_need, price): return

        order = binance.order_market_buy(symbol=PAIR, quantity=qty)
        order_status = binance.get_order(symbol=PAIR, orderId=order['orderId'])
        if order_status['status']!= 'FILLED': return

        # BUY SUKSES -> SIMPAN KE SUPABASE
        supa_req("POST", f"{SUPA_URL}/rest/v1/positions",
                 json={"pair":PAIR,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['orderId'])},
                 headers={**SUPA_HEADERS,"Prefer":"resolution=merge-duplicates"})
        await notif_event(f"BUY SUKSES QTY `{qty}`")
        mode_flexible = False
    except Exception as e:
        await notif_status(f"BUY GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_area(area, positions_in_area, price, mode="BIASA"):
    global is_executing
    if is_executing: return # SATPAM 1
    is_executing = True
    await sinkron_db_dengan_binance() # SYNC PAS MAU SELL
    total_qty = sum(p['qty'] for p in positions_in_area)
    await notif_event(f"SELL [{mode}] AREA `{area}` @`{price:.2f}` QTY `{total_qty:.8f}`")
    try:
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        order_status = binance.get_order(symbol=PAIR, orderId=order['orderId'])
        if order_status['status']!= 'FILLED': return
        # SELL SUKSES -> HAPUS DARI SUPABASE
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&area=eq.{area}")
        avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty
        profit = (price - avg_buy) * total_qty * (1 - BUFFER)
        await notif_event(f"SELL SELESAI. PROFIT `~{profit:.2f}`")
    except Exception as e:
        await notif_status(f"SELL GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_instansemua(all_positions, price):
    global is_executing
    if is_executing: return # SATPAM 1
    is_executing = True
    await sinkron_db_dengan_binance() # SYNC PAS MAU SELL INSTAN
    total_qty = sum(p['qty'] for p in all_positions)
    await notif_event(f"SELL INSTAN [LUAR GRID] @`{price:.2f}` QTY `{total_qty:.8f}`")
    try:
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        order_status = binance.get_order(symbol=PAIR, orderId=order['orderId'])
        if order_status['status']!= 'FILLED': return
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
        avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty
        profit = (price - avg_buy) * total_qty
        await notif_event(f"SELL INSTAN SELESAI. PROFIT `~{profit:.2f}`")
    except Exception as e:
        await notif_status(f"SELL INSTAN GAGAL: `{e}`")
    finally: is_executing = False

async def cek_dana_dan_jual(usdt_need, price):
    positions = get_positions()
    if not positions: return False
    await notif_status(f"SALDO KURANG. COBA ROLING DANA...")
    for area in set(p['area'] for p in positions):
        pos_in_area = get_pos_by_area(area, positions)
        buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
        if price >= buy_terendah_area + last_grid:
            await satpam_sell_area(area, pos_in_area, price, mode="ROLING")
            await asyncio.sleep(2)
            if get_balance("USDT") >= usdt_need:
                await notif_event(f"ROLING SUKSES. LANJUT BUY")
                return True
    await notif_status(f"ROLING GAGAL: GAK ADA POSISI YG BISA DI TP")
    return False

async def scout_loop(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, base_price_start, mode_flexible
    if is_executing: return
    # TIDAK SYNC DI SINI. CUMA PANTAU HARGA RINGAN
    price = get_price()
    if price == 0: return
    positions = get_positions()
    if not positions:
        if mode_flexible and abs(price - base_price_start) >= last_grid:
            await satpam_buy(price, get_area(price, last_grid), reason="FLEX-START")
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

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sinkron_db_dengan_binance() # SYNC PAS KLIK STATUS
    price = get_price(); usdt = get_balance("USDT"); pos = get_positions()
    mode = "FLEXIBLE" if mode_flexible else "GRID-KLASIK"
    txt = f"*BOT V28.9 HEMAT-RAM*\nMode: `{mode}`\nHarga: `{price:.2f}` | Grid: `{last_grid}`\nSaldo: `{usdt:.2f}` | Posisi: `{len(pos)}`"
    await update.message.reply_text(txt, reply_markup=KEYBOARD, parse_mode="Markdown")

async def main():
    global app, last_grid, base_price_start, mode_flexible
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^STATUS$'), status))
    db = get_positions(); last_grid = get_atr_grid(); base_price_start = get_price()
    if len(db) > 0: mode_flexible = False
    app.job_queue.run_repeating(scout_loop, interval=1, first=3) # CUMA PANTAU 1 DETIK
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    logging.info("BOT V28.9 JALAN...")
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait(); await app.stop(); await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
