import os, time, math, ccxt, schedule, asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client

load_dotenv()

# [8] ENV
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PAIR = os.getenv('PAIR')
LOT = float(os.getenv('LOT'))
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# [11] SUPABASE
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# [1] SETTING
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
FEE = 0.001 # [9] FEE RILL 0.1%
BUFFER = 0.003 # 0.3%

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True, # [6.2] ANTI SPAM
    'options': {'defaultType': 'spot'}
})

harga_terakhir_shift = 0
grid_aktif = 0
total_profit = 0
total_sell = 0

# [6.4] LOG KE SUPABASE
def log_db(waktu, buy, sell, profit, alasan):
    supabase.table('logs').insert({
        "waktu": waktu, "buy": buy, "sell": sell, "profit": profit, "alasan": alasan
    }).execute()

# [7] TELEGRAM
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 STATUS", callback_data='status')]]
    await update.message.reply_text("Bot v7.0 ON", reply_markup=InlineKeyboardMarkup(keyboard))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query.data == 'status': await kirim_status(context)

async def kirim_status(context: ContextTypes.DEFAULT_TYPE):
    saldo = exchange.fetch_balance()
    usdt = saldo['USDT']['free']
    harga = exchange.fetch_ticker(PAIR)['last']
    positions = supabase.table('positions').select('*').execute().data
    total_buy = len(positions)

    txt = f"""**📊 STATUS BOT v7.0**
`Saldo USDT :` {usdt:.2f}
`Harga :` {harga}
`LOT :` {LOT}
`Total Buy :` {total_buy}
`Total Sell :` {total_sell}
`Profit :` {total_profit:.2f}
`ATR :` {grid_aktif/ATR_MULTIPLIER:.2f}
`Grid_Aktif :` {grid_aktif}
`Posisi :` {[p['harga_buy'] for p in positions]}
"""
    keyboard = [[InlineKeyboardButton("📊 STATUS", callback_data='status')]]
    await context.bot.send_message(CHAT_ID, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

def notif(msg):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})

# [1] HITUNG GRID ATR
def get_atr_grid():
    ohlcv = exchange.fetch_ohlcv(PAIR, '1h', limit=ATR_PERIOD+1)
    tr = []
    for i in range(1, len(ohlcv)):
        h,l,c,pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4], ohlcv[i-1][4]
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(tr)/ATR_PERIOD
    grid = atr * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50))

# [3] RUMUS MODAL
def hitung_qty(harga): return LOT / harga
def modal_potong(): return LOT * (1 + FEE + FEE + BUFFER)

# [6.6] RETRY 3X
def safe_order(side, qty, price):
    for i in range(3):
        try: return exchange.create_limit_order(PAIR, side, qty, price)
        except: time.sleep(2)
    notif(f"❌ ERROR ORDER GAGAL 3X")
    return None

def cancel_all():
    for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)

# [1] CEK SHIFT 20%
def cek_shift_20(harga, grid_baru):
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
                supabase.table('positions').update({"tp": new_tp, "grid": grid_baru}).eq('id', p['id']).execute()
            notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
        cancel_all()
        harga_terakhir_shift = harga
    grid_aktif = grid_baru

# [2][4] LOOP UTAMA
def loop_utama():
    global total_profit, total_sell
    harga = exchange.fetch_ticker(PAIR)['last']
    grid = get_atr_grid()
    buy_rapi = math.floor(harga / grid) * grid # [1] BUY_AWAL_RAPI

    cek_shift_20(harga, grid)

    # [2] ATURAN BUY
    open_orders = {float(o['price']) for o in exchange.fetch_open_orders(PAIR)}
    positions = {p['harga_buy']: p for p in supabase.table('positions').select('*').execute().data}
    usdt = exchange.fetch_balance()['USDT']['free']
    modal = modal_potong()

    for i in range(50): # cek 50 grid kebawah
        harga_buy = round(buy_rapi - (i * grid), 2)
        if harga <= harga_buy and harga_buy not in open_orders and harga_buy not in positions:
            if usdt >= modal:
                qty = hitung_qty(harga_buy)
                if safe_order('BUY', qty, harga_buy):
                    tp = round(harga_buy + grid, 2) # [4.1]
                    supabase.table('positions').insert({"harga_buy": harga_buy, "qty": qty, "tp": tp, "grid": grid}).execute()
                    notif(f"✅ BUY `{harga_buy}` -> TP `{tp}`") # [7]
                    usdt -= modal
            else:
                notif("⚠️ SALDO KURANG. PAUSE") # [2.3]
                break

    # [4] CEK TP & RE-ENTRY
    for o in exchange.fetch_closed_orders(PAIR, limit=20):
        if o['side'] == 'SELL' and o['status'] == 'closed':
            harga_sell = float(o['price'])
            positions = supabase.table('positions').select('*').execute().data
            for p in positions:
                if abs(p['tp'] - harga_sell) < grid/2:
                    profit = (LOT * BUFFER) + (p['qty'] * p['grid']) # [5]
                    total_profit += profit; total_sell += 1
                    log_db(datetime.now().isoformat(), p['harga_buy'], harga_sell, profit, "TP") # [6.4]
                    notif(f"🎯 TP `{p['harga_buy']}` -> `{harga_sell}` Profit `{profit:.2f}`")
                    safe_order('BUY', p['qty'], harga_sell) # [2.4] RE-ENTRY
                    supabase.table('positions').delete().eq('id', p['id']).execute()

# [6.3] AUTO RESUME
def init():
    orders = exchange.fetch_open_orders(PAIR)
    for o in orders:
        if o['side'] == 'SELL':
            hb = float(o['price']) - grid_aktif
            supabase.table('positions').insert({
                "harga_buy": hb, "qty": float(o['amount']), "tp": float(o['price']), "grid": grid_aktif
            }).execute()

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    init() # [6.3]
    schedule.every().day.at("00:00").do(lambda: cek_shift_20(exchange.fetch_ticker(PAIR)['last'], get_atr_grid())) # [1]
    schedule.every(3).seconds.do(loop_utama)

    asyncio.run(app.initialize())
    app.run_polling()
    while True: schedule.run_pending(); time.sleep(1)

if __name__ == "__main__": main()
