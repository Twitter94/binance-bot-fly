import os, time, math, asyncio, traceback
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException
from supabase import create_client, Client as SupaClient
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
import pandas as pd
import numpy as np
import ta

load_dotenv()

# ========== [8] ENV WAJIB ==========
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
LOT_SETTING = float(os.getenv("LOT", 5))
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

# ========== [1] SETTING ATR & GRID ==========
ATR_PERIOD = 14
ATR_TIMEFRAME = Client.KLINE_INTERVAL_1HOUR
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
QTY_FIXED = 0.00001
BUFFER = 0.001 # 0.1% buffer

binance = Client(API_KEY, API_SECRET)
supa: SupaClient = create_client(SUPA_URL, SUPA_KEY)
bot = Bot(token=TELE_TOKEN)

# Global
GRID_ATR_AKTIF = MIN_GRID
LAST_ATR_UPDATE = 0
sent_notif_cache = set() # [7] Anti spam

# ========== UTILS ==========
def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

async def send_tele(text):
    if text in sent_notif_cache: return
    try:
        await bot.send_message(chat_id=TELE_CHAT_ID, text=text, parse_mode="Markdown")
        sent_notif_cache.add(text)
        await asyncio.sleep(1.5) # [6.2] Anti spam
    except: pass

def get_area_grid(price, grid): return math.floor(price / grid) * grid

def get_grid_atr():
    global GRID_ATR_AKTIF, LAST_ATR_UPDATE
    if time.time() - LAST_ATR_UPDATE < 86400: return GRID_ATR_AKTIF # [1] update 00:00
    klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    df = pd.DataFrame(klines, columns=['time','o','h','l','c','v','ct','qv','n','tbv','tqv','x'])
    df['h'] = df['h'].astype(float); df['l'] = df['l'].astype(float); df['c'] = df['c'].astype(float)
    atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=ATR_PERIOD).average_true_range().iloc[-1]
    grid_hitung = atr * ATR_MULTIPLIER
    GRID_ATR_AKTIF = max(MIN_GRID, min(MAX_GRID, round(grid_hitung)))
    LAST_ATR_UPDATE = time.time()
    log(f"GRID BARU: {GRID_ATR_AKTIF}")
    return GRID_ATR_AKTIF

def get_fee_lot():
    info = binance.get_symbol_info(PAIR)
    lot_min = float([f for f in info['filters'] if f['filterType']=='LOT_SIZE'][0]['minQty'])
    fee = float(binance.get_trade_fee(symbol=PAIR)['tradeFee'][0]['taker'])
    return lot_min, fee

def get_price(): return float(binance.get_symbol_ticker(symbol=PAIR)['price'])

def supa_get_positions():
    res = supa.table("positions").select("*").eq("pair", PAIR).execute()
    return res.data

def supa_upsert_position(pos):
    supa.table("positions").upsert(pos, on_conflict="pair,area").execute()

def supa_delete_position(area):
    supa.table("positions").delete().eq("pair", PAIR).eq("area", area).execute()

# ========== [2] ATURAN BUY ==========
def can_buy(price):
    grid = get_grid_atr()
    area = get_area_grid(price, grid)
    positions = supa_get_positions()
    for p in positions:
        if p['area'] == area: return False # [2.2.B] Area Aktif
    return True

def place_buy(price):
    grid = get_grid_atr()
    area = get_area_grid(price, grid)
    lot_min, fee = get_fee_lot() # [2.3]
    lot = max(LOT_SETTING, QTY_FIXED)
    try:
        order = binance.order_market_buy(symbol=PAIR, quantity=lot)
        supa_upsert_position({"pair": PAIR, "area": area, "buy_price": price, "lot": lot, "fee": fee})
        asyncio.run(send_tele(f"*BUY* `@{price}`\n*AREA:* `{area}` | *GRID:* `{grid}` | *LOT:* `{lot}`"))
        log(f"BUY @ {price}")
        return True
    except BinanceAPIException as e:
        log(f"Buy gagal: {e}")
        return False

# ========== [4] ATURAN SELL + RE-ENTRY BERSYARAT ==========
def check_tp():
    price = get_price()
    grid = get_grid_atr()
    positions = supa_get_positions()
    for pos in positions:
        tp_price = pos['buy_price'] + grid
        if price >= tp_price:
            area_sell = pos['area']
            lot, fee = pos['lot'], pos['fee']
            try:
                binance.order_market_sell(symbol=PAIR, quantity=lot)
                supa_delete_position(area_sell)
                profit = BUFFER + (QTY_FIXED * grid)

                # [4.3] RE-ENTRY BERSYARAT
                area_reentry = get_area_grid(tp_price, grid)
                area_masih_aktif = any(p['area'] == area_reentry for p in supa_get_positions())

                if not area_masih_aktif:
                    place_buy(tp_price) # RE-ENTRY
                    asyncio.run(send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}`\n-> *RE-ENTRY BUY* `@{tp_price}`"))
                else:
                    asyncio.run(send_tele(f"*SELL* `@{tp_price}` +`{profit:.4f}`\n*AREA MASIH AKTIF. SKIP RE-ENTRY*"))
                return
            except Exception as e: log(f"Sell gagal: {e}")

# ========== [11] ATURAN START AWAL FLEKSIBEL ==========
def start_mode():
    log("MODE: MENCARI GRID AWAL")
    grid = get_grid_atr()
    price = get_price()
    target_bawah = math.floor(price / grid) * grid
    target_atas = math.ceil(price / grid) * grid
    asyncio.run(send_tele(f"*BOT START - MODE CARI GRID*\n*Harga:* `{price}`\n*Target:* `{target_bawah}` atau `{target_atas}`"))

    while True:
        price = get_price()
        if price <= target_bawah:
            place_buy(target_bawah); break
        if price >= target_atas:
            place_buy(target_atas); break
        time.sleep(2)

# ========== LOOP UTAMA ==========
async def main_loop():
    await send_tele("🚀 *BOT v9.0.2 START*")
    if len(supa_get_positions()) == 0:
        start_mode()

    while True:
        try:
            check_tp()
            price = get_price()
            grid = get_grid_atr()
            last_buy_area = 0
            positions = supa_get_positions()
            if positions: last_buy_area = min([p['area'] for p in positions])
            target_buy = last_buy_area - grid if positions else get_area_grid(price, grid)

            if price <= target_buy and can_buy(price):
                place_buy(target_buy)

            await asyncio.sleep(3)
        except Exception as e:
            log(traceback.format_exc())
            await asyncio.sleep(10)

# ========== [7] TELEGRAM STATUS ==========
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_price()
    grid = get_grid_atr()
    positions = supa_get_positions()
    pos_text = "\n".join([f"BUY `{p['buy_price']}` -> TP `{p['buy_price']+grid}`" for p in positions]) or "Kosong"
    msg = f"""*STATUS BOT v9.0.2*
*Harga:* `{price}`
*Grid:* `{grid}(ATR)`
*Posisi:* `{len(positions)}`
{pos_text}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    app = Application.builder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("status", status))
    asyncio.get_event_loop().create_task(main_loop())
    app.run_polling()

if __name__ == "__main__":
    main()
