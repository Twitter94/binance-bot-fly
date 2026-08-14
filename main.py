import os, time, math, ccxt, schedule, asyncio, requests, threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client
import pytz

load_dotenv()
WIB = pytz.timezone('Asia/Jakarta')

# [8] ENV WAJIB
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PAIR = os.getenv('PAIR', 'BTCUSDT')
LOT = float(os.getenv('LOT', 5))
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
supabase: Client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# [1] SETTING ATR & GRID SESUAI ATURAN
ATR_PERIOD = 14; ATR_TIMEFRAME = '1h'; ATR_MULTIPLIER = 0.5 # [FIX]
ATR_UPDATE_HOUR = 0
MIN_GRID = 250; MAX_GRID = 1000 # [FIX]
FEE = 0.001; BUFFER = 0.003; MAX_GAGAL_SELL = 3

exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

harga_terakhir_shift = 0; grid_aktif = 0; total_profit = 0; total_sell = 0
slot_gagal_sell = {}
RUNNING = True
NOTIF_SALDO_0 = False

app = Flask(__name__)
@app.route("/")
def health(): return "OK", 200

def notif(msg):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def log_db(waktu, buy, sell, profit, alasan):
    supabase.table('logs').insert({"waktu": waktu, "buy": buy, "sell": sell, "profit": profit, "alasan": alasan}).execute()

def get_atr_grid():
    ohlcv = exchange.fetch_ohlcv(PAIR, ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    tr = [max(ohlcv[i][2]-ohlcv[i][3], abs(ohlcv[i][2]-ohlcv[i-1][4]), abs(ohlcv[i][3]-ohlcv[i-1][4])) for i in range(1, len(ohlcv))]
    atr = sum(tr)/ATR_PERIOD
    grid = atr * ATR_MULTIPLIER # [1] Multiplier 0.5
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50)) # [1] Dibulatkan ke 50

def hitung_qty(harga): return LOT / harga # [5]
def modal_potong(): return LOT * (1 + FEE + FEE + BUFFER) # [3]

def safe_order(side, qty, price):
    for i in range(3): # [2.6] RETRY 3X
        try:
            time.sleep(1.5) # [6.2] ANTI SPAM
            return exchange.create_limit_order(PAIR, side, qty, price)
        except Exception as e:
            if i==2: notif(f"❌ ORDER GAGAL 3X: {e}")
            time.sleep(2)
    return None

def binance_market_sell(qty):
    time.sleep(1.5) # [6.2]
    return exchange.create_market_sell_order(PAIR, qty)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE): # [7]
    keyboard = [[InlineKeyboardButton("📊 STATUS", callback_data='status')]]
    await update.message.reply_text("Bot v7.7 INFINITE GRID ON", reply_markup=InlineKeyboardMarkup(keyboard))

async def setlot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LOT
    try:
        LOT = float(context.args[0])
        notif(f"✅ LOT DIGANTI: ${LOT}")
    except: await update.message.reply_text("Format: /setlot 5")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING
    query = update.callback_query; await query.answer()
    if query.data == 'status': await kirim_status(context)

async def kirim_status(context: ContextTypes.DEFAULT_TYPE): # [7.2]
    saldo = exchange.fetch_balance(); usdt = saldo['USDT']['free']
    harga = exchange.fetch_ticker(PAIR)['last']
    positions = supabase.table('positions').select('*').execute().data
    txt = f"""**📊 STATUS BOT v7.7**
`Status:` {'🟢 JALAN' if RUNNING else '🔴 PAUSE'}
`Saldo USDT:` {usdt:.2f}
`Harga:` {harga}
`LOT:` {LOT}
`Total Buy:` {len(positions)}
`Total Sell:` {total_sell}
`Profit:` {total_profit:.2f}
`ATR:` {grid_aktif/ATR_MULTIPLIER:.2f}
`Grid_Aktif:` {grid_aktif}
`Posisi:` {[p['harga_buy'] for p in positions[:5]]}
"""
    await context.bot.send_message(CHAT_ID, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 STATUS", callback_data='status')]]))

def cek_shift_20(harga, grid_baru): # [1] ATR_AUTO_SHIFT_20%
    global harga_terakhir_shift, grid_aktif
    if harga_terakhir_shift == 0: harga_terakhir_shift = harga
    if abs(harga - harga_terakhir_shift) / harga_terakhir_shift >= 0.20:
        positions = supabase.table('positions').select('*').execute().data
        if harga > harga_terakhir_shift: # [1A] NAIK 20%
            for p in positions: safe_order('SELL', p['qty'], p['tp'])
            notif(f"⚡ NAIK 20%. SELL INSTAN SEMUA TP LAMA")
        else: # [1B] TURUN 20%
            for p in positions:
                new_tp = round(p['harga_buy'] + grid_baru, 2)
                supabase.table('positions').update({"tp": new_tp}).eq('id', p['id']).execute()
            notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
        for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)
        harga_terakhir_shift = harga
    grid_aktif = grid_baru

def laporan_harian():
    notif(f"📈 *LAPORAN HARIAN*\n`Total Sell:` {total_sell}\n`Total Profit:` {total_profit:.2f} USDT")

def loop_utama():
    global total_profit, total_sell, NOTIF_SALDO_0
    try:
        harga = exchange.fetch_ticker(PAIR)['last']
        usdt = exchange.fetch_balance()['USDT']['free']

        # [2.3] AUTO PAUSE/RESUME
        if usdt < modal_potong() and RUNNING:
            if not NOTIF_SALDO_0: notif(f"⚠️ PAUSE. SALDO ${usdt:.2f} < MODAL ${modal_potong():.2f}"); NOTIF_SALDO_0 = True
            RUNNING = False; return
        if usdt >= modal_potong() and not RUNNING:
            RUNNING = True; notif(f"✅ AUTO RESUME. Saldo: ${usdt:.2f}"); NOTIF_SALDO_0 = False

        if not RUNNING: return

        grid = get_atr_grid()
        buy_rapi = math.floor(harga / grid) * grid # [1] BUY_AWAL_RAPI
        cek_shift_20(harga, grid)

        # [2.5] ANTI DOBEL ORDER
        open_orders = {float(o['price']) for o in exchange.fetch_open_orders(PAIR)}
        positions = {p['harga_buy']: p for p in supabase.table('positions').select('*').execute().data}
        modal = modal_potong()

        # [2.1] BUY TIAP GRID TURUN
        for i in range(50):
            harga_buy = round(buy_rapi - (i * grid), 2)
            if harga <= harga_buy and harga_buy not in open_orders and harga_buy not in positions:
                if usdt >= modal:
                    qty = hitung_qty(harga_buy)
                    if safe_order('BUY', qty, harga_buy):
                        tp = round(harga_buy + grid, 2) # [4.1]
                        supabase.table('positions').insert({"harga_buy": harga_buy, "qty": qty, "tp": tp}).execute()
                        notif(f"✅ BUY `{harga_buy}` -> TP `{tp}`") # [7.3]
                        usdt -= modal

        # [4.3] JIKA HARGA SUDAH LEWAT TP: SELL INSTAN
        for o in exchange.fetch_closed_orders(PAIR, limit=20):
            if o['side'] == 'SELL' and o['status'] == 'closed':
                harga_sell = float(o['price'])
                positions = supabase.table('positions').select('*').execute().data
                for p in positions:
                    if abs(p['tp'] - harga_sell) < grid/2:
                        profit = (LOT * BUFFER) + (p['qty'] * grid) # [5]
                        total_profit += profit; total_sell += 1
                        log_db(datetime.now().isoformat(), p['harga_buy'], harga_sell, profit, "TP") # [6.4]
                        notif(f"🎯 TP `{p['harga_buy']}` -> `{harga_sell}` Profit `{profit:.2f}`")
                        safe_order('BUY', p['qty'], harga_sell) # [2.4] RE-ENTRY
                        supabase.table('positions').delete().eq('id', p['id']).execute()

        # [CEK SLOT HANTU]
        positions = supabase.table('positions').select('*').execute().data
        for p in positions:
            if harga >= p['tp']:
                r = binance_market_sell(p['qty'])
                if 'id' not in r:
                    slot_gagal_sell[p['harga_buy']] = slot_gagal_sell.get(p['harga_buy'], 0) + 1
                    if slot_gagal_sell[p['harga_buy']] >= MAX_GAGAL_SELL:
                        supabase.table('positions').delete().eq('id', p['id']).execute()
                        notif(f"🗑️ HAPUS SLOT HANTU {p['harga_buy']}")

    except Exception as e: notif(f"❌ ERROR LOOP: {e}")

def run_bot():
    app_telegram = Application.builder().token(TOKEN).build()
    app_telegram.add_handler(CommandHandler("start", start))
    app_telegram.add_handler(CommandHandler("setlot", setlot))
    app_telegram.add_handler(CallbackQueryHandler(button))
    threading.Thread(target=lambda: app_telegram.run_polling(), daemon=True).start()

    schedule.every(3).seconds.do(loop_utama)
    schedule.every().day.at("00:00").do(lambda: cek_shift_20(exchange.fetch_ticker(PAIR)['last'], get_atr_grid())) # [1] ATR_UPDATE 00:00
    schedule.every().day.at("00:00").do(laporan_harian)
    while True: schedule.run_pending(); time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
