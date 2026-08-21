import os, time, math, requests, logging, signal, asyncio, gc, resource
import ccxt.async_support as ccxt
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
PAIR = os.getenv("PAIR", "BTC/USDT")
PAIR_BINANCE = "BTCUSDT"
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
MIN_USDT = 5
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
SELISIH_TOLERANSI = 0.00001
DELAY_FIRST_BUY = 1800
FEE_KASAR = 0.0011

binance = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; app = None
is_executing = False; mode_flexible = True
last_status_msg = ""
bot_start_time = time.time()

last_fee_check = 0; cached_taker_fee = 0.0011
cached_price = 0; cached_price_time = 0
cached_positions = []; cached_pos_time = 0
cached_atr_grid = 500
last_status_cache = ""; last_status_cache_time = 0

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def supa_req(m,u,**k):
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=5,**k)
    except: return None

def get_positions():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&order=buy_price.asc")
    return r.json() if r and r.status_code==200 else []

def get_positions_full():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&order=buy_price.asc")
    return r.json() if r and r.status_code==200 else []

def get_positions_cache():
    global cached_positions, cached_pos_time
    if time.time() - cached_pos_time < 3: # DARI 10 DETIK TURUN JADI 3 DETIK
        return cached_positions
    cached_positions = get_positions()
    cached_pos_time = time.time()
    return cached_positions

def area_aktif(area, positions): return any(p['area'] == area for p in positions)
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]

async def get_balance(asset):
    try:
        bal = await binance.fetch_balance()
        return float(bal[asset]['free'])
    except: return 0

async def get_price_cache():
    global cached_price, cached_price_time
    if time.time() - cached_price_time < 2: # DARI 5 DETIK TURUN JADI 2 DETIK
        return cached_price
    try: 
        ticker = await binance.fetch_ticker(PAIR)
        cached_price = float(ticker['last'])
    except: cached_price = 0
    cached_price_time = time.time()
    return cached_price

async def get_binance_balance_coin():
    try:
        bal = await binance.fetch_balance()
        coin = PAIR.split('/')[0]
        return float(bal[coin]['free'])
    except: return 0

async def get_atr_grid():
    global cached_atr_grid
    if cached_atr_grid!= 500: return cached_atr_grid
    try:
        ohlcv = await binance.fetch_ohlcv(PAIR, '1h', limit=ATR_PERIOD+1)
        closes = [c[4] for c in ohlcv]
        tr = [abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
        atr = sum(tr)/len(tr) if tr else 500
        cached_atr_grid = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
        return cached_atr_grid
    except: return 500

async def get_qty_aman(price):
    try:
        market = binance.market(PAIR)
        step = market['limits']['amount']['min']
        qty_by_usdt = math.ceil(MIN_USDT/price/step)*step
        qty = max(qty_by_usdt, QTY_FIXED)
        return round(qty, 8)
    except: return QTY_FIXED

async def get_fee_binance():
    global last_fee_check, cached_taker_fee
    if time.time() - last_fee_check < 3600:
        return 0.0011, cached_taker_fee
    try:
        fee = await binance.fetch_trading_fee(PAIR)
        cached_taker_fee = float(fee['taker'])
        last_fee_check = time.time()
        return 0.0011, cached_taker_fee
    except:
        return 0.0011, 0.0011

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
    balance_coin = await get_binance_balance_coin()
    total_qty_db = sum(p['qty'] for p in positions_db)
    if abs(balance_coin - total_qty_db) > SELISIH_TOLERANSI:
        await notif_status(f"SYNC: DB `{total_qty_db:.8f}` vs BINANCE `{balance_coin:.8f}`. RESET DB")
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}")

async def satpam_buy(price, area, reason="GRID"):
    global is_executing, mode_flexible
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance()
        qty = await get_qty_aman(price)
        _, taker_fee_asli = await get_fee_binance()
        usdt_need = price * qty * (1 + taker_fee_asli + taker_fee_asli + BUFFER)
        await notif_event(f"🟢 BUY [{reason}] @`{price:.2f}` AREA `{area}` | QTY `{qty}` | Fee `{taker_fee_asli*100:.3f}%`")
        if await get_balance("USDT") < usdt_need:
            if not await cek_dana_dan_jual(usdt_need, price): return
        order = await binance.create_market_buy_order(PAIR, qty)
        if order['status']== 'closed':
            supa_req("POST", f"{SUPA_URL}/rest/v1/positions",
                     json={"pair":PAIR_BINANCE,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['id'])},
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
        _, taker_fee_asli = await get_fee_binance()
        await notif_event(f"🔴 SELL [{mode}] AREA `{area}` @`{price:.2f}` | Fee `{taker_fee_asli*100:.3f}%`")
        order = await binance.create_market_sell_order(PAIR, total_qty)
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&area=eq.{area}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee_asli - BUFFER)
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
        _, taker_fee_asli = await get_fee_binance()
        await notif_event(f"🔴 SELL INSTAN @`{price:.2f}` | Fee `{taker_fee_asli*100:.3f}%`")
        order = await binance.create_market_sell_order(PAIR, total_qty)
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee_asli)
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
            if await get_balance("USDT") >= usdt_need:
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
        price = await get_price_cache()
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
    global last_status_cache, last_status_cache_time
    if time.time() - last_status_cache_time < 5 and last_status_cache!= "": # DARI 30 DETIK TURUN JADI 5 DETIK
        await update.message.reply_text(last_status_cache, reply_markup=KEYBOARD, parse_mode="Markdown")
        return
    try:
        price = await get_price_cache()
        pos = get_positions_full()
        mode = "FLEXIBLE" if mode_flexible else "GRID-KLASIK"
        usdt = await get_balance("USDT")

        qty_kasar = await get_qty_aman(price)
        modal_butuh_kasar = price * qty_kasar * (1 + FEE_KASAR + FEE_KASAR + BUFFER)

        posisi_txt = ""
        if pos:
            buy_list = sorted(pos, key=lambda x: x['buy_price'], reverse=True)
            for p in buy_list:
                b = p['buy_price']
                s = b + last_grid
                area = p['area']
                qty = p['qty']
                posisi_txt += f"`B{b:,.0f} - S{s:,.0f}` | A:`{area:,.0f}` | Q:`{qty}`\n"
        else:
            posisi_txt = "`- Belum ada posisi -`"

        saldo_status = "✅ AMAN" if usdt >= modal_butuh_kasar else "⚠️ KURANG"

        txt = (
            f"*BOT V30.0.1 NGEBUT*\n"
            f"_Mode: {mode}_\n\n"
            f"*Harga:* `${price:,.2f}` | *Grid:* `${last_grid:,.0f}`\n"
            f"*Saldo USDT:* `{usdt:.2f}` {saldo_status}\n"
            f"*Modal/Layer:* `~{modal_butuh_kasar:.2f}`\n\n"
            f"*POSISI:* `{len(pos)}`\n"
            f"{posisi_txt}"
        )
        last_status_cache = txt
        last_status_cache_time = time.time()
        await update.message.reply_text(txt, reply_markup=KEYBOARD, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Gagal ambil status: {e}")

async def main():
    resource.setrlimit(resource.RLIMIT_AS, (180 * 1024 * 1024, 180 * 1024 * 1024))
    while True:
        try:
            global app, last_grid, base_price_start, mode_flexible, binance
            logging.info("BOT V30.0.1 NGEBUT START...")
            await asyncio.sleep(15)

            app = ApplicationBuilder().token(TELE_TOKEN).build()
            app.add_handler(CommandHandler("start", status))
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^STATUS$'), status))

            binance = ccxt.binance({
                'apiKey': API_KEY,
                'secret': API_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'}
            })

            await sinkron_db_dengan_binance()
            db = get_positions_cache(); last_grid = await get_atr_grid(); base_price_start = await get_price_cache()
            if len(db) > 0: mode_flexible = False

            await cek_pengaman_restart(base_price_start, db)

            app.job_queue.run_repeating(scout_loop, interval=3, first=3) # INI YG DIUBAH JADI 3 DETIK
            await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)

            await notif_status("✅ *BOT V30.0.1 NGEBUT JALAN*")
            logging.info("BOT V30.0.1 NGEBUT JALAN...")

            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            await stop.wait(); await app.stop(); await app.shutdown()
            await binance.close()
            break
        except Exception as e:
            logging.error(f"CRASH: {e}. RESTART 10 DETIK")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
