import os, time, math, ccxt, requests, gc, csv
from datetime import datetime
from dotenv import load_dotenv
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
SUPA_URL = os.getenv('SUPABASE_URL')
SUPA_KEY = os.getenv('SUPABASE_KEY')
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

# [1] SETTING ATR & GRID
ATR_PERIOD = 14; ATR_TIMEFRAME = '1h'; ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_GRID = 250; MAX_GRID = 1000
FEE = 0.001; BUFFER = 0.003

exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True,
    'options': {'defaultType': 'spot'} # [9] RILL BINANCE
})

harga_shift = 0; grid_aktif = 0; total_profit = 0; total_sell = 0
RUNNING = True; last_00 = 0

# [6.4] LOG + [11] SUPABASE REST BIAR IRIT
def supa_get(table): 
    try: return requests.get(f"{SUPA_URL}/rest/v1/{table}?select=*", headers=HEADERS, timeout=5).json()
    except: return []
def supa_ins(table, data): requests.post(f"{SUPA_URL}/rest/v1/{table}", headers=HEADERS, json=data, timeout=5)
def supa_up(table, id, data): requests.patch(f"{SUPA_URL}/rest/v1/{table}?id=eq.{id}", headers=HEADERS, json=data, timeout=5)
def supa_del(table, id): requests.delete(f"{SUPA_URL}/rest/v1/{table}?id=eq.{id}", headers=HEADERS, timeout=5)
def log_csv(waktu, buy, sell, profit, alasan):
    with open('logs.csv', 'a', newline='') as f: csv.writer(f).writerow([waktu, buy, sell, profit, alasan])

def notif(msg): 
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def safe_order(side, qty, price):
    for i in range(3): # [2.6] RETRY 3X
        try:
            time.sleep(1.5) # [6.2] ANTI SPAM
            if side == 'BUY': return exchange.create_limit_buy_order(PAIR, qty, price)
            else: return exchange.create_limit_sell_order(PAIR, qty, price)
        except Exception as e:
            if i==2: notif(f"❌ ORDER GAGAL 3X: {str(e)[:100]}")
            time.sleep(2)
    return None

def get_atr():
    ohlcv = exchange.fetch_ohlcv(PAIR, ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    tr = [max(ohlcv[i][2]-ohlcv[i][3], abs(ohlcv[i][2]-ohlcv[i-1][4]), abs(ohlcv[i][3]-ohlcv[i-1][4])) for i in range(1, len(ohlcv))]
    grid = sum(tr)/ATR_PERIOD * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50))

def qty(harga): return round(LOT / harga, 5)
def modal(): return LOT * (1 + FEE + FEE + BUFFER) # [3] RUMUS MODAL

# [7] TELEGRAM
def status():
    saldo = exchange.fetch_balance()['USDT']['free']
    harga = exchange.fetch_ticker(PAIR)['last']
    pos = supa_get('positions')
    txt = f"""**📊 STATUS BOT v7.0 PROFESIONAL**
`Status:` {'🟢 JALAN' if RUNNING else '🔴 PAUSE'}
`Saldo USDT:` ${saldo:.2f}
`Harga:` ${harga:,.2f}
`LOT:` ${LOT}
`Total Buy:` {len(pos)}
`Total Sell:` {total_sell}
`Profit:` ${total_profit:.2f}
`ATR/Grid:` ${grid_aktif}
`Posisi:` {', '.join([f"{p['harga_buy']}" for p in pos[:8]])}
"""
    kb = {"keyboard":[["📊 STATUS"]],"resize_keyboard":True} # [7.4]
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": txt, "reply_markup": kb})

def cek_tele():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=1&limit=1").json()
        if r.get('ok') and r.get('result') and r['result'][0]['message']['text'] == "📊 STATUS":
            status()
            requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={r['result'][0]['update_id']+1}")
    except: pass

def shift_20(harga, grid_baru):
    global harga_shift, grid_aktif
    if harga_shift == 0: harga_shift = harga
    if abs(harga - harga_shift) / harga_shift >= 0.20: # [1] ATR_AUTO_SHIFT_20%
        pos = supa_get('positions')
        if harga > harga_shift: # A. NAIK 20%: SELL INSTAN semua
            for p in pos: 
                exchange.create_market_sell_order(PAIR, p['qty'])
                notif(f"⚡ NAIK 20%. SELL INSTAN `{p['harga_buy']}`")
                supa_del('positions', p['id'])
        else: # B. TURUN 20%: RESET TP semua
            for p in pos:
                new_tp = round(p['harga_buy'] + grid_baru, 2)
                supa_up('positions', p['id'], {"tp": new_tp})
            notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
        for o in exchange.fetch_open_orders(PAIR): exchange.cancel_order(o['id'], PAIR)
        harga_shift = harga
    grid_aktif = grid_baru

notif("🤖 Bot v7.0 PROFESIONAL ON. 1 PAIR | 256MB MODE")

while True:
    try:
        cek_tele()
        harga = exchange.fetch_ticker(PAIR)['last']
        usdt = exchange.fetch_balance()['USDT']['free']

        now = datetime.now(WIB)
        if now.hour == ATR_UPDATE_HOUR and now.minute < 2 and now.day!= last_00: # [1] ATR_UPDATE 00:00 WIB
            grid_baru = get_atr()
            shift_20(harga, grid_baru)
            last_00 = now.day

        grid = get_atr(); grid_aktif = grid
        buy_rapi = math.floor(harga / grid) * grid # [1] BUY_AWAL_RAPI KELIPATAN GRID
        shift_20(harga, grid)

        mod = modal()
        if usdt < mod and RUNNING: notif(f"⚠️ PAUSE. SALDO ${usdt:.2f} < MODAL ${mod:.2f}"); RUNNING = False # [2.3]
        if usdt >= mod and not RUNNING: RUNNING = True; notif(f"✅ AUTO RESUME. Saldo: ${usdt:.2f}")

        if RUNNING:
            pos_db = supa_get('positions') # [11] SLOT DI SUPABASE
            harga_pos = {float(p['harga_buy']) for p in pos_db}
            open_ord = {float(o['price']) for o in exchange.fetch_open_orders(PAIR)} # [2.5] ANTI DOBEL ORDER

            # [2.1] BUY TIAP GRID TURUN TANPA MAX POSISI
            for i in range(15): # dibatesi 15 biar aman RAM
                hb = round(buy_rapi - (i * grid), 2)
                if harga <= hb and hb not in open_ord and hb not in harga_pos and usdt >= mod:
                    q = qty(hb)
                    if safe_order('BUY', q, hb):
                        tp = round(hb + grid, 2) # [4.1] TP
                        supa_ins('positions', {"harga_buy": hb, "qty": q, "tp": tp})
                        notif(f"✅ BUY `{hb:,.2f}` -> TP `{tp:,.2f}`") # [7.3]
                        usdt -= mod

            # [4.3] JIKA BOT OFF/ON LAGI DAN HARGA SUDAH LEWAT TP: SELL INSTAN
            for p in pos_db:
                if harga >= float(p['tp']): # [4.1] TP
                    r = exchange.create_market_sell_order(PAIR, p['qty']) # [4.2] JUAL FULL
                    if 'id' in r:
                        profit = BUFFER + (float(p['qty']) * grid) # [5] RUMUS PROFIT
                        total_profit += profit; total_sell += 1
                        log_csv(datetime.now().isoformat(), p['harga_buy'], p['tp'], profit, "TP") # [6.4]
                        supa_ins('logs', {"waktu": datetime.now().isoformat(), "buy": p['harga_buy'], "sell": p['tp'], "profit": profit, "alasan": "TP"})
                        notif(f"🎯 TP `{p['harga_buy']:,.2f}` -> `{p['tp']:,.2f}` Profit `${profit:.2f}`") # [7.3]
                        safe_order('BUY', p['qty'], p['tp']) # [2.4] RE-ENTRY
                        supa_del('positions', p['id'])

        gc.collect()
        time.sleep(10)

    except Exception as e:
        notif(f"❌ ERROR: {str(e)[:200]}") # [7.3]
        time.sleep(30)
