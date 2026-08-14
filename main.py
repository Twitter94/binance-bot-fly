import os, time, math, ccxt, schedule, asyncio, requests
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from supabase import create_client, Client # [FIX 1] Tambah,Client

load_dotenv()

# [8] ENV
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PAIR = os.getenv('PAIR')
LOT = float(os.getenv('LOT'))
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# [11] SUPABASE [FIX 2] Tambah :Client biar gak error proxy
supabase: Client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# [1] SETTING
ATR_PERIOD = 14
ATR_MULTIPLIER = 0.5
MIN_GRID = 250
MAX_GRID = 1000
FEE = 0.001
BUFFER = 0.003

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

harga_terakhir_shift = 0
grid_aktif = 0
total_profit = 0
total_sell = 0

def log_db(waktu, buy, sell, profit, alasan):
    try:
        supabase.table('logs').insert({
            "waktu": waktu, "buy": buy, "sell": sell, "profit": profit, "alasan": alasan
        }).execute()
    except Exception as e: notif(f"❌ ERROR LOG DB: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 STATUS", callback_data='status')]]
    await update.message.reply_text("Bot v7.0 ON", reply_markup=InlineKeyboardMarkup(keyboard))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'status': await kirim_status(context)

async def kirim_status(context: ContextTypes.DEFAULT_TYPE):
    try:
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
    except Exception as e: notif(f"❌ ERROR STATUS: {e}")

def notif(msg):
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": msg})
    except: pass

def get_atr_grid():
    ohlcv = exchange.fetch_ohlcv(PAIR, '1h', limit=ATR_PERIOD+1)
    tr = []
    for i in range(1, len(ohlcv)):
        h,l,c,pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4], ohlcv[i-1][4]
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(tr)/ATR_PERIOD
    grid = atr * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50))

def hitung_qty(harga): return LOT / harga
def modal_potong(): return LOT * (1 + FEE + FEE + BUFFER)

def safe_order(side, qty, price):
    for i in range(3):
        try:
            time.sleep(1.5)
            return exchange.create_limit_order(PAIR, side, qty, price)
        except Exception as e:
            if i==2: notif(f"❌ ERROR ORDER: {e}")
            time.sleep(2)
    return None

def cancel_all():
    for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)

def cek_shift_20(harga, grid_baru):
    global harga_terakhir_shift, grid_aktif
    if harga_terakhir_shift == 0: harga_terakhir_shift = harga
    if abs(harga - harga_terakhir_shift) / harga_terakhir_shift >= 0.20:
        positions = supabase.table('positions').select('*').execute().data
        if harga > harga_terakhir_shift:
            for p in positions: safe_order('SELL', p['qty'], p['tp'])
            notif(f"⚡ NAIK 20%. SELL INSTAN SEMUA TP LAMA")
        else:
            for p in positions:
                new_tp = round(p['harga_buy'] + grid_baru, 2)
                supabase.table('positions').update({"tp": new_tp, "grid": grid_baru}).eq('id', p['id']).execute()
            notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
        cancel_all()
        harga_terakhir_shift = harga
    grid_aktif = grid_baru

def loop_utama():
    global total_profit, total_sell
    try:
        harga = exchange.fetch_ticker(PAIR)['last']
        grid = get_atr_grid()
        buy_rapi = math.floor(harga / grid) * grid

        cek_shift_20(harga, grid)

        open_orders = {float(o['price']) for o in exchange.fetch_open_orders(PAIR)}
        positions = {p['harga_buy']: p for p in supabase.table('positions').select('*').execute().data}
        usdt = exchange.fetch_balance()['USDT']['free']
        modal = modal_potong()

        for i in range(50):
            harga_buy = round(buy_rapi - (i * grid), 2)
            if harga <= harga_buy and harga_buy not in open_orders and harga_buy not in positions:
                if usdt >= modal:
                    qty = hitung_qty(harga_buy)
                    if safe_order('BUY', qty, harga_buy):
                        tp = round(harga_buy + grid, 2)
                        supabase.table('positions').insert({"harga_buy": harga_buy, "qty": qty, "tp": tp, "grid": grid}).execute()
                        notif(f"✅ BUY `{harga_buy}` -> TP `{tp}`")
                        usdt -= modal
                else:
                    notif("⚠️ SALDO KURANG. PAUSE")
                    break

        for o in exchange.fetch_closed_orders(PAIR, limit=20):
            if o['side'] == 'SELL' and o['status'] == 'closed':
                harga_sell = float(o['price'])
                positions = supabase.table('positions').select('*').execute().data
                for p in positions:
                    if abs(p['tp'] - harga_sell) < grid/2:
                        profit = (LOT * BUFFER) + (p['qty'] * p['grid'])
                        total_profit += profit; total_sell += 1
                        log_db(datetime.now().isoformat(), p['harga_buy'], harga_sell, profit, "TP")
                        notif(f"🎯 TP `{p['harga_buy']}` -> `{harga_sell}` Profit `{profit:.2f}`")
                        safe_order('BUY', p['qty'], harga_sell)
                        supabase.table('positions').delete().eq('id', p['id']).execute()
    except Exception as e: notif(f"❌ ERROR LOOP: {e}")

def init():
    global grid_aktif
    grid_aktif = get_atr_grid()
    orders = exchange.fetch_open_orders(PAIR)
    for o in orders:
        if o['side'] == 'SELL':
            hb = float(o['price']) - grid_aktif
            cek = supabase.table('positions').select('*').eq('harga_buy', hb).execute().data
            if len(cek) == 0:
                supabase.table('positions').insert({
                    "harga_buy": hb, "qty": float(o['amount']), "tp": float(o['price']), "grid": grid_aktif
                }).execute()

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))

    init()
    schedule.every().day.at("00:00").do(lambda: cek_shift_20(exchange.fetch_ticker(PAIR)['last'], get_atr_grid()))
    schedule.every(3).seconds.do(loop_utama)

    async def run():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        while True: schedule.run_pending(); await asyncio.sleep(1)

    asyncio.run(run())

if __name__ == "__main__": main()
