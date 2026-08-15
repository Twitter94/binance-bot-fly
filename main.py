import os, time, math, asyncio, traceback
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from supabase import create_client, Client as SupaClient
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import pandas as pd
import ta

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT_SETTING = float(os.getenv("LOT", 5))
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
QTY_FIXED = 0.00001 # [5]
BUFFER = 0.001

binance = Client(API_KEY, API_SECRET)
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY)
app = None
GRID_ATR_AKTIF = MIN_GRID
LAST_ATR_UPDATE = 0
PAUSE_BOT = False
sent_notif_cache = set()

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

async def send_tele(text):
    global app
    if text in sent_notif_cache or not app: return
    try:
        await app.bot.send_message(chat_id=TELE_CHAT_ID, text=text, parse_mode="Markdown")
        sent_notif_cache.add(text)
        await asyncio.sleep(1.5) # [7] Anti spam
    except Exception as e: log(f"Tele error: {e}")

def get_area_grid(price, grid): return math.floor(price / grid) * grid # [2.B]

async def get_grid_atr():
    global GRID_ATR_AKTIF, LAST_ATR_UPDATE
    now_wib = datetime.now()
    if now_wib.hour == 0 and now_wib.minute < 5 and time.time() - LAST_ATR_UPDATE > 82800: # [9] Update 00:00 WIB
        klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
        df = pd.DataFrame(klines, columns=['t','o','h','l','c','v','ct','qv','n','tbv','tqv','x'])
        df[['h','l','c']] = df[['h','l','c']].astype(float)
        atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=ATR_PERIOD).average_true_range().iloc[-1]
        grid_hitung = atr * ATR_MULTIPLIER
        GRID_ATR_AKTIF = max(MIN_GRID, min(MAX_GRID, round(grid_hitung))) # [1]
        LAST_ATR_UPDATE = time.time()
        log(f"GRID UPDATE 00:00: {GRID_ATR_AKTIF}")
    return GRID_ATR_AKTIF

async def get_fee_lot(): # [3]
    try:
        info = binance.get_symbol_info(PAIR)
        lot_min = float([f for f in info['filters'] if f['filterType']=='LOT_SIZE'][0]['minQty'])
        fee = float(binance.get_trade_fee(symbol=PAIR)['tradeFee'][0]['taker']) / 100
        return max(lot_min, QTY_FIXED), fee
    except: return QTY_FIXED, 0.001

def get_price(): return float(binance.get_symbol_ticker(symbol=PAIR)['price'])
def get_balance(): return float(binance.get_asset_balance(asset='USDT')['free'])

def supa_get_positions():
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return res.data

def supa_upsert_position(pos): # [6]
    supa.table("positions").upsert(pos, on_conflict="pair,area").execute()

def supa_delete_position(area): supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

def retry_api(func, *args, retries=3): # [2.6]
    for i in range(retries):
        try: return func(*args)
        except: 
            if i == retries-1: raise
            time.sleep(2)

async def can_buy(price):
    grid = await get_grid_atr()
    area = get_area_grid(price, grid)
    return not any(p['area'] == area for p in supa_get_positions()) # [2.2.B]

async def place_buy(price):
    global PAUSE_BOT
    grid = await get_grid_atr(); area = get_area_grid(price, grid)
    lot, fee = await get_fee_lot() # [3] Ambil rill saat order
    
    modal_butuh = lot + (lot * fee * 2) + (lot * BUFFER) # [3]
    if get_balance() < modal_butuh: # [4]
        if not PAUSE_BOT:
            await send_tele(f"*PAUSE* Saldo kurang. Butuh: `${modal_butuh:.2f}`")
            PAUSE_BOT = True
        return False

    for _ in range(3): # [2.6]
        try:
            retry_api(binance.order_market_buy, symbol=PAIR, quantity=lot) # [2.7] Market Buy
            supa_upsert_position({"pair": PAIR, "area": area, "buy_price": price, "lot": lot, "fee": fee, "grid": grid, "time": datetime.now().isoformat()}) # [6]
            await send_tele(f"*BUY* `@{price}`\n*AREA:* `{area}` | *GRID:* `{grid}` | *LOT:* `{lot:.5f}`") # [7]
            PAUSE_BOT = False; return True
        except Exception as e: log(f"Buy gagal: {e}")
    return False

async def check_tp():
    price = get_price(); grid = await get_grid_atr()
    for pos in supa_get_positions():
        tp_price = pos['buy_price'] + grid # [4.1]
        if price >= tp_price:
            lot, fee = await get_fee_lot() # [4.2] Ambil rill
            try:
                retry_api(binance.order_market_sell, symbol=PAIR, quantity=pos['lot'])
                supa_delete_position(pos['area'])
                profit = BUFFER + (QTY_FIXED * grid) # [5]

                area_reentry = get_area_grid(tp_price, grid)
                area_masih_aktif = any(p['area'] == area_reentry for p in supa_get_positions()) # [4.3]

                if not area_masih_aktif: # [4.3.A]
                    await place_buy(tp_price)
                    await send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}`\n-> *RE-ENTRY BUY* `@{tp_price}`")
                else: # [4.3.B]
                    await send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}`\n*AREA MASIH AKTIF. SKIP RE-ENTRY*")
                return
            except Exception as e: log(f"Sell gagal: {e}")

async def start_mode(): # [11]
    grid = await get_grid_atr(); price = get_price()
    target_bawah = math.floor(price / grid) * grid # [11] BUY RAPI
    target_atas = math.ceil(price / grid) * grid
    await send_tele(f"🚀 *BOT v9.0.5 START*\n*Mode:* `Cari Grid`\n*Harga:* `{price}`\n*Target:* `{target_bawah}` atau `{target_atas}`")

    while len(supa_get_positions()) == 0:
        price = get_price()
        if price <= target_bawah: await place_buy(target_bawah); break
        if price >= target_atas: await place_buy(target_atas); break
        await asyncio.sleep(2)

async def main_loop():
    if len(supa_get_positions()) == 0: await start_mode()

    while True:
        try:
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
            log(traceback.format_exc())
            await asyncio.sleep(10)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE): # [7]
    price = get_price(); grid = await get_grid_atr(); lot, fee = await get_fee_lot()
    positions = supa_get_positions(); saldo = get_balance()
    status_txt = "PAUSE" if PAUSE_BOT else "JALAN"
    
    pos_text = "\n".join([f"`BUY {p['buy_price']}` -> TP `{p['buy_price']+grid}` AREA `{p['area']}`" for p in positions]) or "Tidak ada posisi"
    msg = f"""*STATUS {status_txt}*
*Harga:* `${price}`
*Saldo:* `${saldo:.4f}`
*GRID:* `${grid}(ATR)` | *LOT:* `{LOT_SETTING}`
*Butuh:* `${LOT_SETTING*1.004:.2f}` | *Fee:* `{fee*100:.3f}%`
*Posisi:* `{len(positions)}`

*DAFTAR POSISI:*
{pos_text}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

async def main():
    global app
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    asyncio.create_task(main_loop()) # Jalanin loop
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
