import os, time, asyncio, math, requests
from datetime import datetime
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import Bot, ReplyKeyboardMarkup, Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()
wib = pytz.timezone('Asia/Jakarta')

# ===== [8] CONFIG =====
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR")
LOT = float(os.getenv("LOT"))
FEE = 0.001 # 0.1% Spot Binance
BUFFER = 0.003 # 0.3%

# ===== [1] SETTING ATR & GRID =====
ATR_PERIOD, ATR_TIMEFRAME, ATR_MULTIPLIER = 14, Client.KLINE_INTERVAL_1HOUR, 0.5
ATR_UPDATE_HOUR = 0 # 00:00 WIB
MIN_GRID, MAX_GRID = 250, 1000

# ===== KONEKSI =====
binance = Client(API_KEY, API_SECRET)
tele_bot = Bot(os.getenv("TELE_TOKEN"))
CHAT_ID = os.getenv("TELE_CHAT_ID")

# [GANTI SUPABASE]
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

grid_aktif = MIN_GRID
atr_awal = 0
atr_last_check = ""

# ===== FUNGSI UTIL SUPABASE =====
def supa_select(table, eq_key=None, eq_val=None):
    url = f"{SUPA_URL}/rest/v1/{table}?select=*"
    if eq_key: url += f"&{eq_key}=eq.{eq_val}"
    r = requests.get(url, headers=HEADERS)
    return r.json()

def supa_insert(table, data):
    url = f"{SUPA_URL}/rest/v1/{table}"
    requests.post(url, json=data, headers=HEADERS)

def supa_update(table, data, eq_key, eq_val):
    url = f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}"
    requests.patch(url, json=data, headers=HEADERS)

def supa_delete(table, eq_key, eq_val):
    url = f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}"
    requests.delete(url, headers=HEADERS)

# ===== FUNGSI UTIL =====
async def log_db(level, msg, data={}):
    supa_insert("bot_logs", {"level": level, "message": msg, "data": data})
    print(f"[{level}] {msg}")

async def send_tele(msg):
    try: await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard())
    except: pass

def keyboard(): return ReplyKeyboardMarkup([["STATUS"]], resize_keyboard=True)

def rapikan_ke_grid(harga, grid): return round(harga / grid) * grid # [1] BUY_AWAL_RAPI

def get_atr():
    klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    closes = [float(k[4]) for k in klines]
    trs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    atr = sum(trs)/ATR_PERIOD
    return max(MIN_GRID, min(MAX_GRID, round((atr * ATR_MULTIPLIER) / 10) * 10))

def calc_modal(price): # [3] RUMUS MODAL
    fee = LOT * FEE * 2
    buffer = LOT * BUFFER
    return LOT + fee + buffer

async def get_positions_db():
    res = supa_select("positions", "pair", PAIR)
    return {float(r['buy_price']): {"qty": float(r['qty']), "tp": float(r['tp_price'])} for r in res}

async def save_position(buy_price, qty, tp_price):
    supa_insert("positions", {"pair": PAIR, "buy_price": buy_price, "qty": qty, "tp_price": tp_price})

async def delete_position(buy_price):
    supa_delete("positions", "and(pair.eq."+PAIR+",buy_price.eq."+str(buy_price)+")", "")

async def update_tp(buy_price, new_tp):
    supa_update("positions", {"tp_price": new_tp}, "and(pair.eq."+PAIR+",buy_price.eq."+str(buy_price)+")", "")

async def update_stats(profit):
    stats = supa_select("stats", "id", 1)[0]
    new_profit = stats['total_profit'] + profit
    new_sell = stats['total_sell'] + 1
    supa_update("stats", {"total_profit": new_profit, "total_sell": new_sell}, "id", 1)

# ===== [2] [4] FUNGSI ORDER =====
async def check_existing_order(price):
    orders = binance.get_open_orders(symbol=PAIR) # [2.5] ANTI DOBEL CEK EXCHANGE
    return any(abs(float(o['price']) - price) < 1 for o in orders)

async def place_buy(price):
    price = rapikan_ke_grid(price, grid_aktif)
    positions = await get_positions_db()
    if price in positions: return # [2.2] TIDAK DOBEL
    if await check_existing_order(price): return # [2.5] ANTI DOBEL EXCHANGE

    modal = calc_modal(price)
    balance = float(binance.get_asset_balance('USDT')['free'])
    if balance < modal: # [2.3] SALDO KURANG = PAUSE
        await send_tele(f"⚠️ *SALDO KURANG*\nButuh: `{modal:.2f}` USDT\nBot PAUSE")
        await log_db("PAUSE", "Saldo kurang")
        return

    qty = LOT / price
    for i in range(3): # [2.6] RETRY 3x
        try:
            time.sleep(1.5) # [6.2] ANTI SPAM
            binance.order_market_buy(symbol=PAIR, quantity=qty)
            tp = price + grid_aktif # [4.1]
            await save_position(price, qty, tp)
            await log_db("BUY", f"Buy {qty:.6f} @ {price}", {"price": price})
            await send_tele(f"🟢 *BUY*\n`{PAIR}` @ `{price}`\nQty: `{qty:.6f}`\nTP: `{tp}`")
            return
        except Exception as e: await log_db("ERROR", f"Buy Gagal: {e}"); time.sleep(3)

async def place_sell(buy_price, reason="TP"):
    data = (await get_positions_db()).get(buy_price)
    if not data: return
    qty = data['qty']
    for i in range(3):
        try:
            time.sleep(1.5)
            binance.order_market_sell(symbol=PAIR, quantity=qty) # [4.2] JUAL FULL
            profit = BUFFER + (qty * grid_aktif) # [5] RUMUS PROFIT
            await delete_position(buy_price)
            await log_db("SELL", f"Sell @ {buy_price} Alasan: {reason}", {"profit": profit})
            await update_stats(profit)
            await send_tele(f"🔴 *SELL/TP*\n`{PAIR}` @ Market\nAlasan: `{reason}`\nProfit: `+{profit:.2f}` USDT")
            await place_buy(buy_price) # [2.4] RE-ENTRY
            return
        except Exception as e: await log_db("ERROR", f"Sell Gagal: {e}"); time.sleep(3)

# ===== [1] ATR SHIFT =====
async def handle_atr_shift(new_grid):
    global grid_aktif
    positions = await get_positions_db()
    if new_grid > grid_aktif: # A. NAIK 20%: SELL INSTAN semua
        await send_tele(f"⚡ *ATR NAIK 20%*\nGrid: {grid_aktif} -> {new_grid}\n*SELL INSTAN {len(positions)} POSISI*")
        for buy_price in list(positions.keys()):
            await place_sell(buy_price, reason="ATR SHIFT UP")
    else: # B. TURUN 20%: RESET TP
        await send_tele(f"⚡ *ATR TURUN 20%*\nGrid: {grid_aktif} -> {new_grid}\n*RESET TP SEMUA POSISI*")
        for buy_price, data in positions.items():
            new_tp = buy_price + new_grid
            await update_tp(buy_price, new_tp)
    grid_aktif = new_grid

# ===== LOOP UTAMA =====
async def main_loop():
    global grid_aktif, atr_awal, atr_last_check
    grid_aktif = get_atr() # Set grid awal
    atr_awal = get_atr()
    await send_tele(f"✅ *BOT v7.0 JALAN*\nGrid Awal: `{grid_aktif}`")

    while True:
        try:
            price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
            positions = await get_positions_db()

            # [4.3] AUTO RESUME + CEK TP LEWAT
            for buy_price, data in list(positions.items()):
                if price >= data['tp']:
                    await place_sell(buy_price, reason="TP HIT")

            # [1] ATR_UPDATE jam 00:00 WIB
            now_wib = datetime.now(wib)
            if now_wib.hour == ATR_UPDATE_HOUR and now_wib.strftime("%H:%M")!= atr_last_check:
                atr_baru = get_atr()
                if atr_awal > 0:
                    perubahan = (atr_baru - atr_awal) / atr_awal
                    if abs(perubahan) >= 0.2: # 20%
                        await handle_atr_shift(atr_baru)
                atr_awal = atr_baru
                atr_last_check = now_wib.strftime("%H:%M")

            # [2.1] BUY TIAP GRID TURUN
            lowest_buy = min(positions.keys()) if positions else rapikan_ke_grid(price, grid_aktif)
            if price <= lowest_buy - grid_aktif:
                await place_buy(price)

            time.sleep(2)
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n`{str(e)}`")
            time.sleep(60)

# ===== [7] TELEGRAM =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = float(binance.get_asset_balance('USDT')['free'])
    price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    positions = await get_positions_db()
    stats = supa_select("stats", "id", 1)[0]
    msg = f"""*STATUS BOT v7.0*
`Saldo` : {balance:.2f} USDT
`Harga` : {price}
`LOT` : {LOT}
`Total Buy` : {len(positions)}
`Total Sell` : {stats['total_sell']}
`Profit` : {stats['total_profit']:.2f} USDT
`ATR/Grid` : {atr_awal:.2f} / {grid_aktif}
`Posisi TP` : {list(positions.values())}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

app = Application.builder().token(os.getenv("TELE_TOKEN")).build()
app.add_handler(MessageHandler(filters.TEXT & filters.Regex('STATUS'), status))

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main_loop()) # jalanin grid bot di background
    app.run_polling() # telegram pegang loop utama
