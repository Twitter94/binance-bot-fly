import os, time, math, traceback, threading, asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
from supabase import create_client, Client as SupaClient, ClientOptions # FIX SUPA
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
import pandas as pd
import ta

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT_USDT = float(os.getenv("LOT", 5)) # [3] LOT 5 USDT
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
BUFFER = 0.001
SHIFT_THRESHOLD = 0.20

binance = Client(API_KEY, API_SECRET, requests_params={'timeout': 30})
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY, options=ClientOptions(timeout=30)) # FIX
app = None
GRID_ATR_AKTIF = MIN_GRID
LAST_ATR_UPDATE = 0
PAUSE_BOT = False
sent_notif_cache = set()
BASE_GRID_FOR_SHIFT = MIN_GRID

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

async def send_tele(text):
    global app
    if text in sent_notif_cache or not app: return
    for i in range(3): # [2.6]
        try:
            await app.bot.send_message(chat_id=TELE_CHAT_ID, text=text, parse_mode="Markdown")
            sent_notif_cache.add(text)
            await asyncio.sleep(1.5) # [7]
            return
        except Exception as e: 
            log(f"Tele retry {i+1}/3 error: {e}")
            await asyncio.sleep(3)

def get_area_grid(price, grid): return math.floor(price / grid) * grid # [2.B]

async def get_grid_atr(force=False):
    global GRID_ATR_AKTIF, LAST_ATR_UPDATE, BASE_GRID_FOR_SHIFT
    now = datetime.utcnow() + timedelta(hours=7) # WIB
    update_waktu = now.hour == 0 and now.minute < 5 and time.time() - LAST_ATR_UPDATE > 82800 # [9]

    if update_waktu or force:
        for i in range(3):
            try:
                klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
                df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qv','n','tbv','tqv','x'])
                df[['h','l','c']] = df[['h','l','c']].astype(float)
                atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=ATR_PERIOD).average_true_range().iloc[-1]
                grid_baru = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER))) # [1]

                # [1] ATR_AUTO_SHIFT_20%
                if BASE_GRID_FOR_SHIFT > 0 and len(supa_get_positions()) > 0:
                    perubahan = abs(grid_baru - BASE_GRID_FOR_SHIFT) / BASE_GRID_FOR_SHIFT
                    if perubahan >= SHIFT_THRESHOLD:
                        await send_tele(f"*ATR SHIFT 20%* GRID `{BASE_GRID_FOR_SHIFT}` -> `{grid_baru}`\n*SELL & RESET SEMUA POSISI*")
                        for pos in supa_get_positions():
                            try: retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['qty'])
                            except: pass
                            supa_delete_position(pos['area'])

                GRID_ATR_AKTIF = grid_baru
                BASE_GRID_FOR_SHIFT = grid_baru
                LAST_ATR_UPDATE = time.time()
                log(f"GRID UPDATE: {GRID_ATR_AKTIF}")
                return GRID_ATR_AKTIF
            except Exception as e:
                log(f"Get ATR retry {i+1}/3 error: {e}")
                await asyncio.sleep(5)
    return GRID_ATR_AKTIF

async def get_order_params(price): # [3]
    try:
        info = retry_api(binance.get_symbol_info, PAIR)
        lot_step = float([f for f in info['filters'] if f['filterType']=='LOT_SIZE'][0]['stepSize'])
        fee = float(retry_api(binance.get_trade_fee, symbol=PAIR)['tradeFee'][0]['taker']) / 100

        # SATPAM 1: MIN NOTIONAL $5 -> QTY = LOT_USDT / PRICE
        qty = LOT_USDT / price
        qty = math.ceil(qty / lot_step) * lot_step # dibulatkan ke ATAS
        # SATPAM 2: MIN QTY 0.00001
        qty = max(qty, QTY_FIXED)

        modal_butuh = LOT_USDT + (LOT_USDT * fee * 2) + (LOT_USDT * BUFFER) # [3]
        return qty, fee, modal_butuh
    except: return QTY_FIXED, 0.001, LOT_USDT * 1.005

def get_price(): return float(retry_api(binance.get_symbol_ticker, symbol=PAIR)['price'])
def get_balance(): return float(retry_api(binance.get_asset_balance, asset='USDT')['free'])
def supa_get_positions(): 
    try: return supa.table("positions").select("*").eq("pair", PAIR).execute().data
    except: return []
def supa_upsert_position(pos): # [6]
    try: supa.table("positions").upsert(pos, on_conflict="pair,area").execute()
    except Exception as e: log(f"Supa upsert error: {e}")
def supa_delete_position(area): 
    try: supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()
    except Exception as e: log(f"Supa delete error: {e}")

def retry_api(func, *args, retries=3): # [2.6]
    for i in range(retries):
        try: return func(*args)
        except Exception as e: 
            log(f"API retry {i+1}/3 error: {e}")
            if i == retries-1: raise
            time.sleep(3)

async def can_buy(price):
    grid = await get_grid_atr()
    area = get_area_grid(price, grid)
    return not any(p['area'] == area for p in supa_get_positions()) # [2.2.B]

async def place_buy(price):
    global PAUSE_BOT
    grid = await get_grid_atr(); area = get_area_grid(price, grid)
    qty, fee, modal_butuh = await get_order_params(price)

    if get_balance() < modal_butuh: # [4]
        if not PAUSE_BOT:
            await send_tele(f"*PAUSE* Saldo kurang. Butuh: `${modal_butuh:.2f}`")
            PAUSE_BOT = True
        return False

    for _ in range(3): # [2.6]
        try:
            retry_api(binance.order_market_buy, symbol=PAIR, quantity=qty) # [2.7]
            supa_upsert_position({"pair": PAIR, "area": area, "buy_price": price, "qty": qty, "lot_usdt": LOT_USDT, "fee": fee, "grid": grid, "time": datetime.now().isoformat()}) # [6]
            await send_tele(f"*BUY* `@{price}` | *AREA:* `{area}`") # [7]
            PAUSE_BOT = False; return True
        except Exception as e: log(f"Buy gagal: {e}")
    return False

async def check_tp():
    price = get_price(); grid = await get_grid_atr()
    for pos in supa_get_positions():
        tp_price = pos['buy_price'] + grid # [4.1]
        if price >= tp_price: # [4.4]
            qty, fee, _ = await get_order_params(tp_price) # [4.2]
            try:
                retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['qty'])
                supa_delete_position(pos['area'])
                profit = BUFFER + (QTY_FIXED * grid) # [5]

                area_reentry = get_area_grid(tp_price, grid)
                area_masih_aktif = any(p['area'] == area_reentry for p in supa_get_positions()) # [4.3]

                if not area_masih_aktif: # [4.3.A]
                    await place_buy(tp_price)
                    await send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}` -> *RE-ENTRY BUY* `@{tp_price}`")
                else: # [4.3.B]
                    await send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}` | *AREA MASIH AKTIF. SKIP RE-ENTRY*")
                return
            except Exception as e: log(f"Sell gagal: {e}")

async def start_mode(): # [11]
    grid = await get_grid_atr(force=True); price = get_price()
    target_bawah = math.floor(price / grid) * grid # [11] BUY RAPI
    target_atas = math.ceil(price / grid) * grid
    await send_tele(f"🚀 *BOT v9.0.14 START*\n*Mode:* `Cari Grid`\n*Harga:* `{price}`\n*Target:* `{target_bawah}` atau `{target_atas}`")

    while len(supa_get_positions()) == 0:
        price = get_price()
        if price <= target_bawah: await place_buy(target_bawah); break
        if price >= target_atas: await place_buy(target_atas); break
        time.sleep(2)

async def main_loop():
    if len(supa_get_positions()) == 0: await start_mode()
    while True:
        try:
            await get_grid_atr() # Cek update 00:00
            await check_tp()
            price = get_price(); grid = await get_grid_atr(); positions = supa_get_positions()

            if positions:
                last_buy_area = min([p['area'] for p in positions])
                target_buy = last_buy_area - grid # [2.1] Turun 1 grid
            else:
                target_buy = get_area_grid(price, grid)

            if price <= target_buy and await can_buy(target_buy): # [2.1]
                await place_buy(target_buy)

            await asyncio.sleep(3)
        except Exception as e: 
            log("CRASH MAIN LOOP: " + traceback.format_exc())
            await asyncio.sleep(10)

def run_trading_loop():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            log("TRADING LOOP CRASH. RESTART 10s: " + str(e))
            time.sleep(10)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE): # [7]
    price = get_price(); grid = await get_grid_atr(); qty, fee, modal = await get_order_params(price)
    positions = supa_get_positions(); saldo = get_balance()
    status_txt = "PAUSE" if PAUSE_BOT else "JALAN"
    total_profit = sum([BUFFER + (QTY_FIXED * p['grid']) for p in positions])

    pos_text = "\n".join([f"`BUY {p['buy_price']}` -> TP `{p['buy_price']+grid}` AREA `{p['area']}`" for p in positions]) or "Tidak ada posisi"
    msg = f"""*STATUS {status_txt}*
*Harga:* `${price}`
*Saldo:* `${saldo:.4f}`
*GRID:* `${grid}(ATR)` | *LOT:* `{LOT_USDT} USDT`
*Fee:* `{fee*100:.3f}%` | *Profit:* `{total_profit:.4f}`
*Posisi:* `{len(positions)}`

*DAFTAR POSISI:*
{pos_text}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    global app
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0) # ANTI TIMEOUT
    app = ApplicationBuilder().token(TELE_TOKEN).request(request).build()
    app.add_handler(CommandHandler("status", status))
    threading.Thread(target=run_trading_loop, daemon=True).start()
    log("BOT v9.0.14 START POLLING")
    app.run_polling()

if __name__ == "__main__":
    main()
