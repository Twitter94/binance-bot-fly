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
LOT = float(os.getenv("LOT") or 0)
FEE = 0.001
BUFFER = 0.003

# CEK ENV WAJIB
for k in ["BINANCE_API_KEY","BINANCE_API_SECRET","PAIR","LOT","TELE_TOKEN","TELE_CHAT_ID","SUPA_URL","SUPA_KEY"]:
    if not os.getenv(k): raise Exception(f"ENV {k} KOSONG!")

# ===== [1] SETTING ATR & GRID =====
ATR_PERIOD, ATR_TIMEFRAME, ATR_MULTIPLIER = 14, Client.KLINE_INTERVAL_1HOUR, 0.5
ATR_UPDATE_HOUR = 0
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
sent_notif = set()

# ===== FUNGSI UTIL SUPABASE =====
def supa_select(table, eq_key=None, eq_val=None):
    try:
        url = f"{SUPA_URL}/rest/v1/{table}?select=*"
        if eq_key: url += f"&{eq_key}=eq.{eq_val}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        return data if isinstance(data, list) else []
    except: return []

def supa_insert(table, data):
    try: requests.post(f"{SUPA_URL}/rest/v1/{table}", json=data, headers=HEADERS, timeout=10)
    except: pass

def supa_update(table, data, eq_key, eq_val):
    try: requests.patch(f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}", json=data, headers=HEADERS, timeout=10)
    except: pass

def supa_delete(table, eq_key, eq_val):
    try: requests.delete(f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}", headers=HEADERS, timeout=10)
    except: pass

# ===== FUNGSI UTIL =====
async def log_db(level, msg, data={}):
    supa_insert("bot_logs", {"level": level, "message": msg, "data": data})
    print(f"[{level}] {msg}")

async def send_tele(msg, key="umum"):
    global sent_notif
    if key in sent_notif: return
    try:
        await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard())
        sent_notif.add(key)
    except Exception as e: print("TELE ERROR:", e)

def keyboard(): return ReplyKeyboardMarkup([["STATUS"]], resize_keyboard=True)

def rapikan_ke_grid(harga, grid): return round(harga / grid) * grid

def get_atr():
    try:
        klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
        closes = [float(k[4]) for k in klines]
        trs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
        atr = sum(trs)/ATR_PERIOD
        return max(MIN_GRID, min(MAX_GRID, round((atr * ATR_MULTIPLIER) / 10) * 10))
    except: return MIN_GRID

def calc_modal(price): return LOT + (LOT * FEE * 2) + (LOT * BUFFER)

async def get_positions_db():
    res = supa_select("positions", "pair", PAIR)
    out = {}
    for r in res:
        try: out[float(r['buy_price'])] = {"qty": float(r['qty']), "tp": float(r['tp_price'])}
        except: continue
    return out

async def save_position(buy_price, qty, tp_price):
    supa_insert("positions", {"pair": PAIR, "buy_price": buy_price, "qty": qty, "tp_price": tp_price})

async def delete_position(buy_price):
    supa_delete("positions", f"pair=eq.{PAIR}&buy_price=eq.{buy_price}", "")

async def update_tp(buy_price, new_tp):
    supa_update("positions", {"tp_price": new_tp}, f"pair=eq.{PAIR}&buy_price=eq.{buy_price}", "")

async def update_stats(profit):
    res = supa_select("stats", "id", 1)
    if not res: return
    stats = res[0]
    new_profit = float(stats.get('total_profit',0)) + profit
    new_sell = int(stats.get('total_sell',0)) + 1
    supa_update("stats", {"total_profit": new_profit, "total_sell": new_sell}, "id", 1)

# ===== [2] [4] FUNGSI ORDER =====
async def check_existing_order(price):
    try: orders = binance.get_open_orders(symbol=PAIR)
    except: return False
    return any(abs(float(o['price']) - price) < 1 for o in orders)

async def place_buy(price):
    price = rapikan_ke_grid(price, grid_aktif)
    positions = await get_positions_db()
    if price in positions: return
    if await check_existing_order(price): return

    modal = calc_modal(price)
    try: balance = float(binance.get_asset_balance('USDT')['free'])
    except: balance = 0
    if balance < modal:
        await send_tele(f"⚠️ *SALDO KURANG*\nButuh: `{modal:.2f}` USDT\nBot PAUSE", key="SALDO")
        return

    qty = LOT / price
    for i in range(3):
        try:
            time.sleep(1.5)
            binance.order_market_buy(symbol=PAIR, quantity=qty)
            tp = price + grid_aktif
            await save_position(price, qty, tp)
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
        for buy_price in list(positions.keys()): await place_sell(buy_price, reason="ATR SHIFT UP")
    else:
        await send_tele(f"⚡ *ATR TURUN 20%*\nGrid: {grid_aktif} -> {new_grid}\n*RESET TP SEMUA POSISI*", key="ATR_DOWN")
        for buy_price, data in positions.items(): await update_tp(buy_price, buy_price + new_grid)
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
                if price >= data['tp']: await place_sell(buy_price, reason="TP HIT")

            now_wib = datetime.now(wib)
            if now_wib.hour == ATR_UPDATE_HOUR and now_wib.strftime("%H:%M")!= atr_last_check:
                atr_baru = get_atr()
                if atr_awal > 0:
                    perubahan = (atr_baru - atr_awal) / atr_awal
                    if abs(perubahan) >= 0.2: await handle_atr_shift(atr_baru)
                atr_awal = atr_baru
                atr_last_check = now_wib.strftime("%H:%M")

            lowest_buy = min(positions.keys()) if positions else rapikan_ke_grid(price, grid_aktif)
            if price <= lowest_buy - grid_aktif: await place_buy(price)

            await asyncio.sleep(2) # [GANTI time.sleep]
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n`{str(e)}`", key="ERROR")
            await asyncio.sleep(60) # [GANTI time.sleep]

# ===== [7] TELEGRAM =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = float(binance.get_asset_balance('USDT')['free'])
        price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
        positions = await get_positions_db()
        res = supa_select("stats", "id", 1)
        stats = res[0] if res else {"total_sell":0, "total_profit":0}
        msg = f"""*STATUS BOT v7.0*
`Saldo` : {balance:.2f} USDT
`Harga` : {price}
`LOT` : {LOT}
`Total Buy` : {len(positions)}
`Total Sell` : {stats['total_sell']}
`Profit` : {float(stats['total_profit']):.2f} USDT
`ATR/Grid` : {atr_awal:.2f} / {grid_aktif}"""
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"ERROR: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text.strip().upper()
    if text == "STATUS":
        await status(update, context)

async def main():
    app = Application.builder().token(os.getenv("TELE_TOKEN")).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    # [PENTING] jalanin 2 task bareng dalam 1 loop
    await asyncio.gather(
        app.run_polling(allowed_updates=Update.ALL_TYPES),
        main_loop()
    )

if __name__ == "__main__":
    asyncio.run(main()) # [GANTI SEMUA BAGIAN BAWAH]
