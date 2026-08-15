import os, asyncio, math, requests
from datetime import datetime
import pytz
from binance.client import Client
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
binance = Client(API_KEY, API_SECRET)
tele_bot = Bot(os.getenv("TELE_TOKEN"))
CHAT_ID = os.getenv("TELE_CHAT_ID")

SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

grid_aktif = MIN_GRID
atr_awal = 0
atr_last_check = ""
sent_notif = set()
is_paused = False # [UNTUK ATURAN SALDO KURANG=PAUSE]
FEE_BINANCE = 0.001 # [DEFAULT. NANTI DIUPDATE]

# ===== FUNGSI BARU: AMBIL FEE DARI BINANCE =====
def get_fee_binance():
    global FEE_BINANCE
    try:
        info = binance.get_trade_fee(symbol=PAIR)
        FEE_BINANCE = float(info[0]['maker']) # [AMBIL FEE MAKER. KALO PAKE BNB JADI 0.00075]
        return FEE_BINANCE
    except:
        return 0.001 # [FALLBACK 0.1%]

#... semua fungsi supabase, log, keyboard sama persis...

def rapikan_ke_grid(harga, grid): return round(harga / grid) * grid # [BUY_AWAL_RAPI]

def get_atr():
    try:
        klines = binance.get_klines(symbol=PAIR, interval=ATR_TIMEFRAME, limit=ATR_PERIOD+1)
        closes = [float(k[4]) for k in klines]
        trs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
        atr = sum(trs)/ATR_PERIOD
        return max(MIN_GRID, min(MAX_GRID, round((atr * ATR_MULTIPLIER) / 10) * 10))
    except: return MIN_GRID

def calc_modal(modal_lot): return modal_lot + (modal_lot * FEE_BINANCE * 2) + (modal_lot * BUFFER) # [PAKE FEE BINANCE]

#... semua fungsi db sama...

async def place_buy(price):
    global is_paused
    price = rapikan_ke_grid(price, grid_aktif)
    positions = await get_positions_db()
    if price in positions: return
    if await check_existing_order(price): return

    MIN_QTY_BINANCE = 0.00001

    # ===== [LOT FLEKSIBEL MIN 5] =====
    lot_hitung = price * MIN_QTY_BINANCE
    LOT = max(MIN_LOT, lot_hitung) # [INI YG KITA TAMPILIN]
    qty = MIN_QTY_BINANCE
    # =================================

    modal = calc_modal(LOT)
    try: balance = float(binance.get_asset_balance('USDT')['free'])
    except: balance = 0

    if balance < modal:
        if not is_paused:
            await send_tele(f"🔴 *PAUSE* | Harga: ${price:.2f}\nSALDO: ${balance:.4f}\nButuh LOT: ${LOT:.2f}", key="SALDO")
            is_paused = True
        return
    if is_paused:
        is_paused = False; await send_tele("✅ *SALDO CUKUP - BOT LANJUT BUY*", key="SALDO_OK")

    for i in range(3):
        try:
            await asyncio.sleep(1.5)
            order = binance.order_market_buy(symbol=PAIR, quantity=qty)
            real_price = float(order['fills'][0]['price']) if order['fills'] else price
            tp = real_price + grid_aktif
            await save_position(real_price, qty, tp)
            await log_db("BUY", f"Buy {qty:.8f} @ {real_price}", {"price": real_price, "qty": qty, "lot": LOT})
            await send_tele(f"🟢 *BUY DI BINANCE*\n`{PAIR}` @ `{real_price}`\nLOT: `${LOT:.2f}`\nQty: `{qty:.8f}`\nTP: `{tp}`", key=f"BUY_{real_price}")
            return
        except Exception as e:
            await log_db("ERROR", f"Buy Gagal Retry {i+1}: {e}")
            await asyncio.sleep(3)

#... place_sell dan fungsi lain sama...

async def main_loop():
    global grid_aktif, atr_awal, atr_last_check
    get_fee_binance() # [AMBIL FEE PAS START]
    grid_aktif = get_atr()
    atr_awal = get_atr()
    price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
    await check_and_sell_passed_tp(price)
    await send_tele(f"✅ *BOT v7.2 INFINITE GRID JALAN*\nGrid Awal: `{grid_aktif}`\nLOT Min: `{MIN_LOT}`", key="START")

    while True:
        try:
            get_fee_binance() # [UPDATE FEE TIAP LOOP]
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

            await asyncio.sleep(2)
        except Exception as e:
            await log_db("ERROR", str(e))
            await send_tele(f"❌ *ERROR*\n`{str(e)}`", key="ERROR")
            await asyncio.sleep(60)

# ===== [7] TELEGRAM STATUS BARU =====
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        get_fee_binance() # [UPDATE FEE DULU]
        balance = float(binance.get_asset_balance('USDT')['free'])
        price = float(binance.get_symbol_ticker(symbol=PAIR)['price'])
        positions = await get_positions_db()
        res = supa_select("stats", "id", 1)
        stats = res[0] if res else {"total_sell":0, "total_profit":0}

        # [HITUNG LOT SAAT INI]
        lot_sekarang = max(MIN_LOT, price * 0.00001)

        status_text = "PAUSE" if is_paused else "JALAN"
        emoji = "🔴" if is_paused else "🟢"

        posisi_text = "\n".join([f"BUY ${p:.2f} -> TP ${d['tp']:.2f}" for p,d in sorted(positions.items())]) or "Tidak ada posisi"

        msg = f"""{emoji} *{status_text}* | *Harga:* ${price:.2f}
*SALDO:* ${balance:.4f}
*GRID:* ${grid_aktif:.2f} | *LOT:* ${lot_sekarang:.2f} | *Min:* ${MIN_LOT:.1f}
*Fee:* {FEE_BINANCE*100:.3f}% | *Posisi:* {len(positions)}

📌 *POSISI*
{posisi_text}"""
        await update.message.reply_text(msg, parse_mode="Markdown")
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
