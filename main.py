import os, time, math, ccxt, requests
from datetime import datetime
from dotenv import load_dotenv
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
ATR_PERIOD = 14; ATR_TIMEFRAME = '1h'; ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_GRID = 250; MAX_GRID = 1000
FEE = 0.001; BUFFER = 0.003; MAX_GAGAL_SELL = 3

# [OPTIMAL] CCXT CUMA LOAD 1 PAIR DOANG BIAR HEMAT RAM
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True,
    'options': {'defaultType': 'spot', 'fetchMarkets': False}
})
exchange.load_markets({PAIR: {}})

harga_terakhir_shift = 0; grid_aktif = 0; total_profit = 0; total_sell = 0
slot_gagal_sell = {}
RUNNING = True
last_daily_report = 0
LAST_TELE_CHECK = 0

def notif(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def log_db(waktu, buy, sell, profit, alasan):
    try: supabase.table('logs').insert({"waktu": waktu, "buy": buy, "sell": sell, "profit": profit, "alasan": alasan}).execute()
    except: pass

def get_atr_grid():
    ohlcv = exchange.fetch_ohlcv(PAIR, ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    tr = [max(ohlcv[i][2]-ohlcv[i][3], abs(ohlcv[i][2]-ohlcv[i-1][4]), abs(ohlcv[i][3]-ohlcv[i-1][4])) for i in range(1, len(ohlcv))]
    atr = sum(tr)/ATR_PERIOD
    grid = atr * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50))

def hitung_qty(harga): return LOT / harga
def modal_potong(): return LOT * (1 + FEE + FEE + BUFFER) # [3] RUMUS MODAL

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
    time.sleep(1.5)
    return exchange.create_market_sell_order(PAIR, qty)

def kirim_status():
    saldo = exchange.fetch_balance(); usdt = saldo['USDT']['free']
    harga = exchange.fetch_ticker(PAIR)['last']
    positions = supabase.table('positions').select('*').execute().data
    txt = f"""**📊 STATUS BOT v7.0 PROFESIONAL**
`Status:` {'🟢 JALAN' if RUNNING else '🔴 PAUSE'}
`Saldo USDT:` {usdt:.2f}
`Harga:` {harga}
`LOT:` {LOT}
`Total Buy:` {len(positions)}
`Total Sell:` {total_sell}
`Profit:` {total_profit:.2f}
`Grid_Aktif:` {grid_aktif}
"""
    keyboard = {"keyboard":[["📊 STATUS"]],"resize_keyboard":True}
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": txt, "parse_mode": "Markdown", "reply_markup": keyboard})

def cek_tele(): # [7] TOMBOL BAWAH MANUAL
    global RUNNING
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=1", timeout=5).json()
        if r['ok'] and r['result']:
            update = r['result'][-1]
            if 'message' in update and update['message']['text'] == "📊 STATUS":
                kirim_status()
            requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={update['update_id']+1}")
    except: pass

def cek_shift_20(harga, grid_baru):
    global harga_terakhir_shift, grid_aktif
    if harga_terakhir_shift == 0: harga_terakhir_shift = harga
    if abs(harga - harga_terakhir_shift) / harga_terakhir_shift >= 0.20: # [1] ATR_AUTO_SHIFT_20%
        positions = supabase.table('positions').select('*').execute().data
        if harga > harga_terakhir_shift: # A. NAIK 20%
            for p in positions: safe_order('SELL', p['qty'], p['tp']) # SELL INSTAN
            notif(f"⚡ NAIK 20%. SELL INSTAN SEMUA TP LAMA")
        else: # B. TURUN 20%
            for p in positions:
                new_tp = round(p['harga_buy'] + grid_baru, 2) # RESET TP KE GRID BARU
                supabase.table('positions').update({"tp": new_tp}).eq('id', p['id']).execute()
            notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
        for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)
        harga_terakhir_shift = harga
    grid_aktif = grid_baru

def laporan_harian():
    global total_sell, total_profit
    notif(f"📈 *LAPORAN HARIAN*\n`Total Sell:` {total_sell}\n`Total Profit:` {total_profit:.2f} USDT")

def loop_utama():
    global total_profit, total_sell, last_daily_report, grid_aktif, RUNNING
    try:
        harga = exchange.fetch_ticker(PAIR)['last']
        usdt = exchange.fetch_balance()['USDT']['free']

        now = datetime.now(WIB)
        if now.hour == 0 and now.minute < 1 and last_daily_report!= now.day: # [1] ATR_UPDATE 00:00
            laporan_harian()
            grid_baru = get_atr_grid()
            cek_shift_20(harga, grid_baru)
            last_daily_report = now.day
            grid_aktif = grid_baru

        # [2.3] SALDO KURANG = PAUSE
        modal = modal_potong()
        if usdt < modal and RUNNING:
            notif(f"⚠️ PAUSE. SALDO ${usdt:.2f} < MODAL ${modal:.2f}")
            RUNNING = False
        if usdt >= modal and not RUNNING:
            RUNNING = True
            notif(f"✅ AUTO RESUME. Saldo: ${usdt:.2f}") # [2.3] LANJUT BUY DI HARGA SEKARANG

        if not RUNNING: return

        grid = get_atr_grid(); grid_aktif = grid
        buy_rapi = math.floor(harga / grid) * grid # [1] BUY_AWAL_RAPI KELIPATAN GRID
        cek_shift_20(harga, grid)

        positions_db = supabase.table('positions').select('*').execute().data # [11] SLOT DI SUPABASE
        positions = {p['harga_buy']: p for p in positions_db}
        open_orders_binance = exchange.fetch_open_orders(PAIR)
        open_orders = {float(o['price']) for o in open_orders_binance}

        # [2.1] BUY TIAP GRID TURUN
        for i in range(50): # TANPA MAX POSISI
            harga_buy = round(buy_rapi - (i * grid), 2)
            if harga <= harga_buy and harga_buy not in open_orders and harga_buy not in positions: # [2.2] TIDAK DOBEL
                if usdt >= modal:
                    qty = hitung_qty(harga_buy)
                    if safe_order('BUY', qty, harga_buy):
                        tp = round(harga_buy + grid, 2)
                        supabase.table('positions').insert({"harga_buy": harga_buy, "qty": qty, "tp": tp}).execute()
                        positions[harga_buy] = {"harga_buy": harga_buy, "qty": qty, "tp": tp}
                        notif(f"✅ BUY `{harga_buy}` -> TP `{tp}`") # [7.3] NOTIF BUY
                        usdt -= modal

        # [4.3] JIKA HARGA SUDAH LEWAT TP: SELL INSTAN
        for o in exchange.fetch_closed_orders(PAIR, limit=10):
            if o['side'] == 'SELL' and o['status'] == 'closed':
                harga_sell = float(o['price'])
                for p_id, p in list(positions.items()):
                    if abs(p['tp'] - harga_sell) < grid/2:
                        profit = (LOT * BUFFER) + (p['qty'] * grid) # [5] RUMUS PROFIT
                        total_profit += profit; total_sell += 1
                        log_db(datetime.now().isoformat(), p['harga_buy'], harga_sell, profit, "TP") # [6.4] LOG
                        notif(f"🎯 TP `{p['harga_buy']}` -> `{harga_sell}` Profit `{profit:.2f}`") # [7.3] NOTIF TP
                        safe_order('BUY', p['qty'], harga_sell) # [2.4] RE-ENTRY
                        supabase.table('positions').delete().eq('id', p['id']).execute()
                        del positions[p_id]

        # [CEK SLOT HANTU]
        for p_id, p in list(positions.items()):
            if harga >= p['tp']:
                r = binance_market_sell(p['qty'])
                if 'id' not in r:
                    slot_gagal_sell[p['harga_buy']] = slot_gagal_sell.get(p['harga_buy'], 0) + 1
                    if slot_gagal_sell[p['harga_buy']] >= MAX_GAGAL_SELL:
                        supabase.table('positions').delete().eq('id', p['id']).execute()
                        del positions[p_id]
                        notif(f"🗑️ HAPUS SLOT HANTU {p['harga_buy']}")

    except Exception as e:
        notif(f"❌ ERROR: {e}") # [7.3] NOTIF ERROR

if __name__ == "__main__":
    notif("🤖 Bot v7.0 PROFESIONAL ON. 256MB MODE")
    keyboard = {"keyboard":[["📊 STATUS"]],"resize_keyboard":True}
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": "Bot ON. Klik STATUS", "reply_markup": keyboard})

    while True:
        cek_tele() # Cek tombol tiap loop
        loop_utama()
        time.sleep(3) # 5 detik. Cepet tapi aman di 256mb
