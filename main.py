import os, time, math, ccxt, schedule, asyncio, requests, threading
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client

load_dotenv()

# [ENV]
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PAIR = os.getenv('PAIR')
LOT = float(os.getenv('LOT'))
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# [SETTING GABUNGAN]
ATR_PERIOD = 14; ATR_MULTIPLIER = 1; MIN_GRID = 1.5; MAX_GRID = 5
FEE = 0.001; BUFFER = 0.003; MAX_GAGAL_SELL = 3

exchange = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True, 'options': {'defaultType': 'spot'}})

harga_terakhir_shift = 0; grid_aktif = 0; total_profit = 0; total_sell = 0
slot_gagal_sell = {}
RUNNING = True

app = Flask(__name__)
@app.route("/")
def health(): return "OK", 200

def notif(msg):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})

def log_db(waktu, buy, sell, profit, alasan):
    supabase.table('logs').insert({"waktu": waktu, "buy": buy, "sell": sell, "profit": profit, "alasan": alasan}).execute()

def get_atr_grid():
    ohlcv = exchange.fetch_ohlcv(PAIR, '1h', limit=ATR_PERIOD+1)
    tr = [max(ohlcv[i][2]-ohlcv[i][3], abs(ohlcv[i][2]-ohlcv[i-1][4]), abs(ohlcv[i][3]-ohlcv[i-1][4])) for i in range(1, len(ohlcv))]
    atr = sum(tr)/ATR_PERIOD
    grid = atr * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid, 2))) # dari v5.52

def hitung_qty(harga): return LOT / harga
def modal_potong(): return LOT * (1 + FEE + FEE + BUFFER)

def safe_order(side, qty, price):
    for i in range(3):
        try: return exchange.create_limit_order(PAIR, side, qty, price)
        except Exception as e: 
            if i==2: notif(f"❌ ORDER GAGAL 3X: {e}")
            time.sleep(2)
    return None

def binance_market_sell(qty): # dari v5.52
    return exchange.create_market_sell_order(PAIR, qty)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 STATUS", callback_data='status')],[InlineKeyboardButton("⏯️ START/STOP", callback_data='toggle')]]
    await update.message.reply_text("Bot v7.5 ON", reply_markup=InlineKeyboardMarkup(keyboard))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global RUNNING
    query = update.callback_query; await query.answer()
    if query.data == 'status': await kirim_status(context)
    if query.data == 'toggle': 
        RUNNING = not RUNNING
        await query.edit_message_text(f"Bot {'JALAN' if RUNNING else 'PAUSE'}")

async def kirim_status(context: ContextTypes.DEFAULT_TYPE):
    saldo = exchange.fetch_balance(); usdt = saldo['USDT']['free']
    harga = exchange.fetch_ticker(PAIR)['last']
    positions = supabase.table('positions').select('*').execute().data
    txt = f"**📊 STATUS v7.5**\n`Status:` {'🟢 JALAN' if RUNNING else '🔴 PAUSE'}\n`Saldo:` {usdt:.2f}\n`Harga:` {harga}\n`Posisi:` {len(positions)}\n`Profit:` {total_profit:.2f}"
    await context.bot.send_message(CHAT_ID, txt, parse_mode='Markdown')

def cek_shift_20(harga, grid_baru):
    global harga_terakhir_shift, grid_aktif
    if harga_terakhir_shift == 0: harga_terakhir_shift = harga
    if abs(harga - harga_terakhir_shift) / harga_terakhir_shift >= 0.20:
        positions = supabase.table('positions').select('*').execute().data
        if harga > harga_terakhir_shift:
            for p in positions: safe_order('SELL', p['qty'], p['tp']) # [v5.52] Sell instan
            notif(f"⚡ NAIK 20%. SELL INSTAN SEMUA")
        else:
            for p in positions: supabase.table('positions').update({"tp": round(p['harga_buy'] + grid_baru, 2)}).eq('id', p['id']).execute()
            notif(f"⚡ TURUN 20%. RESET TP")
        for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)
        harga_terakhir_shift = harga
    grid_aktif = grid_baru

def loop_utama():
    global total_profit, total_sell
    if not RUNNING: return
    try:
        harga = exchange.fetch_ticker(PAIR)['last']
        grid = get_atr_grid()
        buy_rapi = math.floor(harga / grid) * grid
        cek_shift_20(harga, grid)

        open_orders = {float(o['price']) for o in exchange.fetch_open_orders(PAIR)}
        positions = {p['harga_buy']: p for p in supabase.table('positions').select('*').execute().data}
        usdt = exchange.fetch_balance()['USDT']['free']
        modal = modal_potong()

        # [BUY 50 GRID]
        for i in range(50):
            harga_buy = round(buy_rapi - (i * grid), 2)
            if harga <= harga_buy and harga_buy not in open_orders and harga_buy not in positions:
                if usdt >= modal:
                    qty = hitung_qty(harga_buy)
                    if safe_order('BUY', qty, harga_buy):
                        tp = round(harga_buy + grid, 2)
                        supabase.table('positions').insert({"harga_buy": harga_buy, "qty": qty, "tp": tp}).execute()
                        notif(f"✅ BUY `{harga_buy}` -> TP `{tp}`")
                        usdt -= modal
                else: notif("⚠️ SALDO KURANG. PAUSE"); break

        # [CEK TP + SELL MARKET INSTAN] dari v5.52
        for o in exchange.fetch_closed_orders(PAIR, limit=20):
            if o['side'] == 'SELL' and o['status'] == 'closed':
                harga_sell = float(o['price'])
                positions = supabase.table('positions').select('*').execute().data
                for p in positions:
                    if abs(p['tp'] - harga_sell) < grid/2:
                        profit = (LOT * BUFFER) + (p['qty'] * grid)
                        total_profit += profit; total_sell += 1
                        log_db(datetime.now().isoformat(), p['harga_buy'], harga_sell, profit, "TP")
                        notif(f"🎯 TP `{p['harga_buy']}` -> `{harga_sell}` Profit `{profit:.2f}`")
                        safe_order('BUY', p['qty'], harga_sell) # Re-entry
                        supabase.table('positions').delete().eq('id', p['id']).execute()

        # [CEK SLOT HANTU] dari v5.52
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
    app_telegram.add_handler(CallbackQueryHandler(button))
    threading.Thread(target=lambda: app_telegram.run_polling(), daemon=True).start()
    
    schedule.every(3).seconds.do(loop_utama)
    while True: schedule.run_pending(); time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=8080) # [v5.52] Health check ben Fly gak sleep
