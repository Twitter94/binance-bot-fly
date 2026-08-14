import os, time, math, ccxt, requests, gc
from datetime import datetime
from dotenv import load_dotenv
import pytz

load_dotenv()
WIB = pytz.timezone('Asia/Jakarta')

API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
PAIR = os.getenv('PAIR', 'BTCUSDT')
LOT = float(os.getenv('LOT', 5))
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SUPA_URL = os.getenv('SUPABASE_URL')
SUPA_KEY = os.getenv('SUPABASE_KEY')
HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}

ATR_PERIOD = 14; ATR_TIMEFRAME = '1h'; ATR_MULTIPLIER = 0.5
MIN_GRID = 250; MAX_GRID = 1000
FEE = 0.001; BUFFER = 0.003

exchange = ccxt.binance({'apiKey': API_KEY, 'secret': API_SECRET, 'enableRateLimit': True})

grid_aktif = 0; total_profit = 0; total_sell = 0
RUNNING = True; harga_shift = 0; last_atr_day = 0
harga = 0; usdt = 0

def supa_get(t): 
    try: return requests.get(f"{SUPA_URL}/rest/v1/{t}?select=*", headers=HEADERS, timeout=5).json()
    except: return []
def supa_ins(t,d): requests.post(f"{SUPA_URL}/rest/v1/{t}", headers=HEADERS, json=d, timeout=5)
def supa_up(t,i,d): requests.patch(f"{SUPA_URL}/rest/v1/{t}?id=eq.{i}", headers=HEADERS, json=d, timeout=5)
def supa_del(t,i): requests.delete(f"{SUPA_URL}/rest/v1/{t}?id=eq.{i}", headers=HEADERS, timeout=5)

def notif(m): 
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": m, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def safe_order(side, qty, price):
    for i in range(3): # [2.6] RETRY 3X
        try:
            time.sleep(1.5) # [6.2] ANTI SPAM
            if side == 'BUY': return exchange.create_limit_buy_order(PAIR, qty, price)
            else: return exchange.create_limit_sell_order(PAIR, qty, price)
        except: 
            if i==2: notif(f"❌ ORDER GAGAL 3X")
            time.sleep(2)
    return None

def get_atr():
    ohlcv = exchange.fetch_ohlcv(PAIR, ATR_TIMEFRAME, limit=ATR_PERIOD+1)
    tr = [max(ohlcv[i][2]-ohlcv[i][3], abs(ohlcv[i][2]-ohlcv[i-1][4]), abs(ohlcv[i][3]-ohlcv[i-1][4])) for i in range(1, len(ohlcv))]
    grid = sum(tr)/ATR_PERIOD * ATR_MULTIPLIER
    return max(MIN_GRID, min(MAX_GRID, round(grid / 50) * 50))

def qty(h): return round(LOT / h, 5)
def modal(): return LOT * (1 + FEE + FEE + BUFFER) # [3]

def status(): # [7.1] TOMBOL CUMA BUAT CEK
    pos = supa_get('positions')
    txt = f"""**📊 STATUS SEKARANG**
`Harga:` ${harga:,.2f}
`Saldo:` ${usdt:.2f}
`LOT:` ${LOT}
`Grid ATR:` ${grid_aktif}
`Total Buy:` {len(pos)}
`Total Sell:` {total_sell}
`Profit:` ${total_profit:.2f}
`Posisi:` {', '.join([str(p['harga_buy']) for p in pos[:5]])}
"""
    kb = {"keyboard":[["📊 STATUS"]],"resize_keyboard":True}
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": txt, "reply_markup": kb})

def cek_tele():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=1&limit=1").json()
        if r.get('ok') and r.get('result') and r['result'][0]['message']['text'] == "📊 STATUS":
            status()
            requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={r['result'][0]['update_id']+1}")
    except: pass

notif("🤖 Bot v7.6 PENGAWAS ON. Pantau 24 jam")

while True:
    try:
        cek_tele() # Cuma cek tele, gak makan RAM
        
        # [KUNCI] 1 LOOP = 1 KALI PANTAU. 5 DETIK SEKALI
        harga = exchange.fetch_ticker(PAIR)['last'] # 1. PANTAU HARGA
        time.sleep(1)
        
        usdt = exchange.fetch_balance()['USDT']['free'] # 2. PANTAU SALDO
        mod = modal()
        if usdt < mod and RUNNING: notif(f"⚠️ PAUSE. SALDO ${usdt:.2f}"); RUNNING = False # [2.3]
        if usdt >= mod and not RUNNING: RUNNING = True
        time.sleep(1)

        now = datetime.now(WIB)
        if now.hour == 0 and now.day!= last_atr_day: # [1] ATR_UPDATE 00:00 WIB
            grid_baru = get_atr()
            if harga_shift == 0: harga_shift = harga
            if abs(harga - harga_shift) / harga_shift >= 0.20: # [1] ATR_AUTO_SHIFT_20%
                pos = supa_get('positions')
                if harga > harga_shift: # A. NAIK 20%
                    for p in pos: 
                        exchange.create_market_sell_order(PAIR, p['qty'])
                        notif(f"⚡ NAIK 20%. SELL INSTAN `{p['harga_buy']}`")
                        supa_del('positions', p['id'])
                else: # B. TURUN 20%
                    for p in pos:
                        supa_up('positions', p['id'], {"tp": round(p['harga_buy'] + grid_baru, 2)})
                    notif(f"⚡ TURUN 20%. RESET TP KE GRID {grid_baru}")
                harga_shift = harga
            grid_aktif = grid_baru
            last_atr_day = now.day
            gc.collect()
        
        if grid_aktif == 0: grid_aktif = get_atr()
        buy_rapi = math.floor(harga / grid_aktif) * grid_aktif # [1] BUY_AWAL_RAPI

        if RUNNING:
            pos_db = supa_get('positions') # [11]
            harga_pos = {float(p['harga_buy']) for p in pos_db}
            
            # [2.1] PANTAU BUY TIAP GRID TURUN
            for i in range(8): # Cuma pantau 8 grid ke bawah
                hb = round(buy_rapi - (i * grid_aktif), 2)
                if harga <= hb and hb not in harga_pos and usdt >= mod:
                    q = qty(hb)
                    if safe_order('BUY', q, hb):
                        tp = round(hb + grid_aktif, 2) # [4.1]
                        supa_ins('positions', {"harga_buy": hb, "qty": q, "tp": tp})
                        notif(f"✅ AKU BUY di BINANCE `{hb:,.2f}` -> TP `{tp:,.2f}`") # [13]
                        usdt -= mod
                    break # dapet 1 langsung stop

            # [4.3] PANTAU SELL TP
            for p in pos_db:
                if harga >= float(p['tp']):
                    r = exchange.create_market_sell_order(PAIR, p['qty']) # [4.2]
                    if 'id' in r:
                        profit = BUFFER + (float(p['qty']) * grid_aktif) # [5]
                        total_profit += profit; total_sell += 1
                        supa_ins('logs', {"waktu": datetime.now().isoformat(), "buy": p['harga_buy'], "sell": p['tp'], "profit": profit, "alasan": "TP"}) # [6.4]
                        notif(f"🎯 AKU SELL di BINANCE `{p['tp']:,.2f}` Profit `${profit:.2f}`") # [13]
                        safe_order('BUY', p['qty'], p['tp']) # [2.4] RE-ENTRY
                        supa_del('positions', p['id'])
                    break # dapet 1 langsung stop

        gc.collect()
        time.sleep(5) # Jedanya 5 detik. Aman dari limit binance 1200/menit

    except Exception as e:
        notif(f"❌ ERROR: {str(e)[:200]}")
        time.sleep(30)
