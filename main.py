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
sent_notif = set() # [SPAM 1X]

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

async def send_tele(msg, key="umum"): # [SPAM 1X]
    global sent_notif
    if key in sent_notif: return
    try:
        await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard())
        sent_notif.add(key)
    except: pass

def keyboard(): return ReplyKeyboardMarkup([["STATUS"]], resize_keyboard=True) # [HAPUS START]

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
    if not isinstance(res, list): res = []
    return {float(r['buy_price']): {"qty": float(r['qty']), "tp": float(r['tp_price'])} for r in res}

async def save_position(buy_price, qty, tp_price):
    supa_insert("positions", {"pair": PAIR, "buy_price": buy_price, "qty": qty, "tp_price": tp_price})

async def delete_position(buy_price):
    url = f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&buy_price=eq.{buy_price}"
    requests.delete(url, headers=HEADERS)

async def update_tp(buy_price, new_tp):
    url = f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&buy_price=eq.{buy_price}"
    requests.patch(url, json={"tp_price": new_tp}, headers=HEADERS)

async def update_stats(profit):
    res = supa_select("stats", "id", 1)
    if not res: return
    stats = res[0]
    new_profit = float(stats['total_profit']) + profit
    new_sell = int(stats['total_sell']) + 1
    supa_update("stats", {"total_profit": new_profit, "total_sell": new_sell}, "id", 1)

# ===== [2] [4] FUNGSI ORDER =====
async def check_existing_order(price):
    orders = binance.get_open_orders(symbol=PAIR)
    return any(abs(float(o['price']) - price) < 1 for o in orders)

async def place_buy(price):
    price = rapikan_ke_grid(price, grid_aktif)
    positions = await get_positions_db()
    if price in positions: return
    if await check_existing_order(price): return

    modal = calc_modal(price)
    balance = float(binance.get_asset_balance('USDT')['free'])
    if balance < modal:
        await send_tele(f"⚠️ *SALDO KURANG*\nButuh: `{modal:.2f}` USDT\nBot PAUSE", key="SALDO")
        await log_db("PAUSE", "Saldo kurang")
        return

    qty = LOT / price
    for i in range(3):
        try:
            time.sleep(1.5)
            binance.order_market_buy(symbol=PAIR, quantity=qty)
            tp = price + grid_aktif
            await save_position(price, qty, tp)
            await log_db("BUY", f"Buy {qty:.6f} @ {price}", {"price": price})
            await send_tele(f"🟢 *BUY*\n`{PAIR}` @ `{price}`\nQty: `{qty:.6f}`\nTP: `{tp}`", key=f"BUY_{price}")
            return
        except Exception as e: await log_db("ERROR", f"Buy Gagal: {e}"); time.sleep(3)

async def place_sell(buy_price, reason="TP"):
    data = (await get_positions_db()).get(buy_price)
    if not data: return
    qty = data['qty']
    for i in range(3):
        try:
            time.sleep(1.5)
            binance.order_market_sell(symbol=PAIR, quantity=qty)
            profit = BUFFER + (qty * grid_aktif)
            await delete_position(buy_price)
            await log_db("SELL", f"Sell @ {buy_price} Alasan: {reason}", {"profit": profit})
            await update_stats(profit)
            await send_tele(f"🔴 *SELL/TP*\n`{PAIR}` @ Market\nAlasan: `{reason}`\nProfit: `+{profit:.2f}` USDT", key=f"SELL_{buy_price}")
            await place_buy(buy_price)
            return
        except Exception as e: await log_db("ERROR", f"Sell Gagal: {e}"); time.sleep(3)

# ===== [1] ATR SHIFT =====
async def handle_atr_shift(new_grid):
    global grid_aktif
    positions = await get_positions_db()
    if new_grid > grid_aktif:
        await send_tele(f"⚡ *ATR NAIK 20%*\nGrid: {grid_aktif} -> {new_grid}\n*SELL INSTAN {len(positions)} POSISI*", key="ATR_UP")
        for buy_price in list(positions.keys()):
            await place_sell(buy_price, reason="ATR SHIFT UP")
    else:
        await send_tele(f"⚡ *ATR TURUN 20%*\nGrid: {grid_aktif} -> {new_grid}\n*RESET TP SEMUA POSISI*", key="ATR_DOWN")
        for buy_price, data in positions.items():
            new_tp = buy_price + new_grid
            await update_tp(buy_price, new_tp)
    grid_aktif = new_grid

# ===== LOOP UTAMA =====
async def main_loop():
    global grid_aktif, atr_awal, atr_last_check
    grid_aktif = get_atr()
    atr_awal = get_atr()
    await send_tele(f"✅ *BOT v7.0 JALAN*\nGrid Awal: `{grid_aktif}`", key="START")

    while True:
        try:
            price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
            positions = await get_positions_db()

            for buy_price, data in list(positions.items()):
                if price >= data['tp']:
                    await place_sell(buy_price, reason="TP HIT")

            now_wib = datetime.now(wib)
            if now_wib.hour == ATR_UPDATE_HOUR and now_wib.strftime("%H:%M")!= atr_last_check:
                atr_baru = get_atr()
                if atr_awal > 0:
                    perubahan = (atr_baru - atr_awal) / atr_awal
                    if abs(perubahan) >= 0.2:
                        await handle_atr_shift(atr_baru)
                atr_awal = atr_baru
                atr_last_check = now_wib.strftime("%H:%M")

            lowest_buy = min(positions.keys()) if positions else rapikan_ke_grid(price, grid_aktif)
            if price <= lowest_buy - grid_aktif:
                await place_buy(price)

            time.sleep(2)
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n`{str(e)}`", key="ERROR")
            time.sleep(60)

# ===== [7] TELEGRAM =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = float(binance.get_asset_balance('USDT')['free'])
    price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    positions = await get_positions_db()
    stats = supa_select("stats", "id", 1)
    if not stats: stats = [{"total_sell":0, "total_profit":0}]
    else: stats = stats[0]
    msg = f"""*STATUS BOT v7.0*
`Saldo` : {balance:.2f} USDT
`Harga` : {price}
`LOT` : {LOT}
`Total Buy` : {len(positions)}
`Total Sell` : {stats['total_sell']}
`Profit` : {stats['total_profit']:.2f} USDT
`ATR/Grid` : {atr_awal:.2f} / {grid_aktif}
`Posisi` : {len(positions)}"""
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE): # [FIX STATUS]
    if update.message.text == "STATUS":
        await status(update, context)

app = Application.builder().token(os.getenv("TELE_TOKEN")).build()
app.add_handler(MessageHandler(filters.TEXT, handle_message)) # [FIX] tangkep semua text

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(main_loop())
    app.run_polling()
