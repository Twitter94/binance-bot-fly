import os, asyncio, math, requests
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
MIN_LOT = float(os.getenv("LOT") or 5) # [MIN 5 SESUAI ATURAN]
BUFFER = 0.003 # [0.3%]

for k in ["BINANCE_API_KEY","BINANCE_API_SECRET","PAIR","LOT","TELE_TOKEN","TELE_CHAT_ID","SUPA_URL","SUPA_KEY"]:
    if not os.getenv(k): raise Exception(f"ENV {k} KOSONG!")

# ===== [1] SETTING ATR & GRID =====
ATR_PERIOD, ATR_TIMEFRAME, ATR_MULTIPLIER = 14, Client.KLINE_INTERVAL_1HOUR, 0.5
ATR_UPDATE_HOUR = 0 # [00:00 WIB]
MIN_GRID, MAX_GRID = 250, 1000

# ===== KONEKSI =====
binance = Client(API_KEY, API_SECRET, tld='com')
tele_bot = Bot(os.getenv("TELE_TOKEN"))
CHAT_ID = os.getenv("TELE_CHAT_ID")

SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

grid_aktif = MIN_GRID
atr_awal = 0
atr_last_check = ""
sent_notif = set()
is_paused = False
FEE_BINANCE = 0.001

# ===== FUNGSI: AMBIL FEE RILL DARI BINANCE =====
def get_fee_binance():
    global FEE_BINANCE
    try:
        info = binance.get_trade_fee(symbol=PAIR)
        FEE_BINANCE = float(info[0]['maker'])
        return FEE_BINANCE
    except Exception:
        return 0.001

# ===== FUNGSI UTIL SUPABASE =====
def supa_select(table, eq_key=None, eq_val=None):
    try:
        url = f"{SUPA_URL}/rest/v1/{table}?select=*"
        if eq_key: url += f"&{eq_key}=eq.{eq_val}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        return data if isinstance(data, list) else []
    except: return []

def supa_insert(table, data):
    try: requests.post(f"{SUPA_URL}/rest/v1/{table}", json=data, headers=HEADERS, timeout=15)
    except: pass

def supa_update(table, data, eq_key, eq_val):
    try: requests.patch(f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}", json=data, headers=HEADERS, timeout=15)
    except: pass

def supa_delete(table, eq_key, eq_val):
    try: requests.delete(f"{SUPA_URL}/rest/v1/{table}?{eq_key}=eq.{eq_val}", headers=HEADERS, timeout=15)
    except: pass

# ===== [6] LOG =====
async def log_db(level, msg, data={}):
    payload = {"time": datetime.now(wib).isoformat(), "level": level, "message": msg, "data": data}
    supa_insert("bot_logs", payload)
    print(f"[{level}] {msg}")

async def send_tele(msg, key="umum"):
    global sent_notif
    if key in sent_notif: return
    try:
        await tele_bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=keyboard())
        sent_notif.add(key)
        await asyncio.sleep(2)
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

def calc_modal(modal_lot): return modal_lot + (modal_lot * FEE_BINANCE * 2) + (modal_lot * BUFFER) # [RUMUS MODAL v7.0]

async def get_positions_db():
    res = supa_select("positions", "pair", PAIR)
    out = {}
    for r in res:
        try: out[float(r['buy_price'])] = {"qty": float(r['qty']), "tp": float(r['tp_price']), "lot": float(r.get('lot', MIN_LOT))}
        except: continue
    return out

async def save_position(buy_price, qty, tp_price, lot):
    supa_insert("positions", {"pair": PAIR, "buy_price": buy_price, "qty": qty, "tp_price": tp_price, "lot": lot})

async def delete_position(buy_price):
    supa_delete("positions", f"pair=eq.{PAIR}&buy_price=eq.{buy_price}", "")

async def update_tp(buy_price, new_tp):
    supa_update("positions", {"tp_price": new_tp}, f"pair=eq.{PAIR}&buy_price=eq.{buy_price}", "")

async def update_stats(profit):
    res = supa_select("stats", "id", 1)
    if not res: supa_insert("stats", {"id": 1, "total_sell": 0, "total_profit": 0}); return
    stats = res[0]
    new_profit = float(stats.get('total_profit',0)) + profit
    new_sell = int(stats.get('total_sell',0)) + 1
    supa_update("stats", {"total_profit": new_profit, "total_sell": new_sell}, "id", 1)

# ===== [2] [4] FUNGSI ORDER SPOT =====
async def check_existing_order(price):
    try: orders = binance.get_open_orders(symbol=PAIR)
    except: return False
    return any(abs(float(o['price']) - price) < 1 for o in orders)

async def place_buy(price): # [ATURAN BUY v7.0]
    global is_paused
    price = rapikan_ke_grid(price, grid_aktif)
    positions = await get_positions_db()
    if price in positions: return # [TIDAK DOBEL]
    if await check_existing_order(price): return # [ANTI DOBEL ORDER]

    # ===== [RUMUS LOT + QTY v7.0] =====
    lot_hitung = price * 0.00001 # [CEK MIN NOTIONAL]
    LOT = max(MIN_LOT, lot_hitung) # [MIN 5 USDT]
    qty = LOT / price # [RUMUS QTY = LOT / HARGA]
    # ==================================

    modal = calc_modal(LOT) # [MODAL = LOT + FEE*2 + BUFFER]
    try: balance = float(binance.get_asset_balance('USDT')['free'])
    except: balance = 0

    if balance < modal: # [SALDO KURANG = PAUSE]
        if not is_paused:
            await send_tele(f"🔴 *PAUSE* | Harga: ${price:.2f}\nSALDO: ${balance:.4f}\nButuh: `${modal:.2f}` | LOT: `${LOT:.2f}`", key="SALDO")
            is_paused = True
        return
    if is_paused: # [LANJUT OTOMATIS]
        is_paused = False; await send_tele("✅ *SALDO CUKUP - BOT LANJUT BUY*", key="SALDO_OK")

    for i in range(3): # [RETRY 3X]
        try:
            await asyncio.sleep(1.5) # [ANTI SPAM]
            order = binance.order_market_buy(symbol=PAIR, quantity=qty)
            real_price = float(order['fills'][0]['price']) if order['fills'] else price
            real_qty = float(order['executedQty']) # [PAKE QTY REAL]
            tp = real_price + grid_aktif # [TP = BUY + GRID]
            await save_position(real_price, real_qty, tp, LOT)
            await log_db("BUY", f"Buy {real_qty:.8f} @ {real_price}", {"price": real_price, "qty": real_qty, "lot": LOT})
            await send_tele(f"🟢 *BUY DI BINANCE*\n`{PAIR}` @ `{real_price:.2f}`\nLOT: `${LOT:.2f}`\nQty: `{real_qty:.8f}`\nTP: `{tp:.2f}`", key=f"BUY_{real_price}")
            return
        except BinanceAPIException as e:
            await log_db("ERROR", f"Buy Gagal: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            await log_db("ERROR", f"Buy Gagal Retry {i+1}: {e}")
            await asyncio.sleep(3)

async def place_sell(buy_price, reason="TP"): # [ATURAN SELL v7.0]
    data = (await get_positions_db()).get(buy_price)
    if not data: return
    qty = data['qty']
    lot_buy = data['lot']

    for i in range(3): # [RETRY 3X]
        try:
            await asyncio.sleep(1.5) # [ANTI SPAM]
            binance.order_market_sell(symbol=PAIR, quantity=qty) # [JUAL FULL]
            profit = BUFFER + (qty * grid_aktif) # [RUMUS PROFIT = BUFFER + QTY*GRID]
            await delete_position(buy_price)
            await update_stats(profit)
            await log_db("SELL", f"Sell {qty:.8f}", {"profit": profit, "reason": reason, "lot": lot_buy})
            await send_tele(f"🔴 *SELL DI BINANCE*\n`{PAIR}` @ Market\nAlasan: `{reason}`\nLOT: `${lot_buy:.2f}`\nProfit: `+{profit:.2f}` USDT", key=f"SELL_{buy_price}")
            await place_buy(buy_price) # [RE-ENTRY]
            return
        except BinanceAPIException as e:
            await log_db("ERROR", f"Sell Gagal: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            await log_db("ERROR", f"Sell Gagal Retry {i+1}: {e}")
            await asyncio.sleep(3)

# ===== [1] ATR SHIFT 20% =====
async def handle_atr_shift(new_grid):
    global grid_aktif
    positions = await get_positions_db()
    if new_grid > grid_aktif: # [NAIK 20% = SELL INSTAN]
        await send_tele(f"⚡ *ATR NAIK 20%*\nGrid: {grid_aktif} -> {new_grid}\n*SELL INSTAN {len(positions)} POSISI*", key="ATR_UP")
        for buy_price in list(positions.keys()): await place_sell(buy_price, reason="ATR SHIFT UP")
    else: # [TURUN 20% = RESET TP]
        await send_tele(f"⚡ *ATR TURUN 20%*\nGrid: {grid_aktif} -> {new_grid}\n*RESET TP SEMUA POSISI*", key="ATR_DOWN")
        for buy_price, data in positions.items(): await update_tp(buy_price, buy_price + new_grid)
    grid_aktif = new_grid

# ===== [4.3] AUTO RESUME =====
async def check_and_sell_passed_tp(price):
    positions = await get_positions_db()
    for buy_price, data in list(positions.items()):
        if price >= data['tp']: await place_sell(buy_price, reason="TP LEWAT SAAT START")

# ===== LOOP UTAMA =====
async def main_loop():
    global grid_aktif, atr_awal, atr_last_check
    get_fee_binance()
    grid_aktif = get_atr()
    atr_awal = get_atr()
    price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    await check_and_sell_passed_tp(price) # [AUTO RESUME DULU]
    await send_tele(f"✅ *BOT v7.6.2 INFINITE GRID JALAN*\nGrid Awal: `{grid_aktif}`\nLOT Min: `{MIN_LOT}`", key="START")

    while True:
        try:
            get_fee_binance() # [UPDATE FEE RILL]
            price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
            positions = await get_positions_db()

            for buy_price, data in list(positions.items()): # [CEK TP]
                if price >= data['tp']: await place_sell(buy_price, reason="TP HIT")

            now_wib = datetime.now(wib)
            if now_wib.hour == ATR_UPDATE_HOUR and now_wib.strftime("%H:%M")!= atr_last_check: # [00:00 WIB]
                atr_baru = get_atr()
                if atr_awal > 0:
                    perubahan = (atr_baru - atr_awal) / atr_awal
                    if abs(perubahan) >= 0.2: await handle_atr_shift(atr_baru) # [SHIFT 20%]
                atr_awal = atr_baru
                atr_last_check = now_wib.strftime("%H:%M")

            lowest_buy = min(positions.keys()) if positions else rapikan_ke_grid(price, grid_aktif)
            if price <= lowest_buy - grid_aktif: await place_buy(price) # [BUY TIAP TURUN 1 GRID]

            await asyncio.sleep(2)
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n`{str(e)}`", key="ERROR")
            await asyncio.sleep(60)

# ===== [7] TELEGRAM MONITORING - FORMAT KAYA GAMBAR =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        get_fee_binance()
        balance = float(binance.get_asset_balance('USDT')['free'])
        price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
        positions = await get_positions_db()
        res = supa_select("stats", "id", 1)
        stats = res[0] if res else {"total_sell":0, "total_profit":0}

        lot_sekarang = max(MIN_LOT, price * 0.00001)
        modal_sekarang = calc_modal(lot_sekarang)

        status_text = "JALAN" if not is_paused else "PAUSE"
        status_warna = "STATUS JALAN" if not is_paused else "STATUS PAUSE"

        # [FORMAT KAYA GAMBAR PAKE MONOSPACE]
        posisi_text = ""
        if positions:
            for p,d in sorted(positions.items()):
                posisi_text += f"BUY ${p:.2f} -> TP ${d['tp']:.2f}\nLOT AWAL: ${d['lot']:.2f}\n\n"
        else:
            posisi_text = "Tidak ada posisi"

        msg = f"""`{status_warna}

Harga: ${price:.2f}

Saldo: ${balance:.4f}
GRID: ${grid_aktif:.2f} | LOT: ${lot_sekarang:.2f}
Butuh: ${modal_sekarang:.2f} | Fee: {FEE_BINANCE*100:.3f}%
Posisi: {len(positions)} | Sell: {stats['total_sell']} | Profit: ${float(stats['total_profit']):.2f}

DAFTAR POSISI:
{posisi_text}`"""
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard())
    except Exception as e: await update.message.reply_text(f"ERROR: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    text = update.message.text.strip().upper()
    if text == "STATUS":
        await status(update, context)

async def on_startup(app: Application):
    asyncio.create_task(main_loop())

def main():
    app = Application.builder().token(os.getenv("TELE_TOKEN")).post_init(on_startup).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
