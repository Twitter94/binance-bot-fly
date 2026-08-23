import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_SECRET = os.getenv("API_SECRET")
SUPABASE_URL = os.getenv("SUPA_URL")
SUPABASE_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

SYMBOL = "BTCUSDT"
BASE_COIN = "BTC"
QUOTE_COIN = "USDT"
LOOP_SEC = 3
BUFFER_USDT = 0.5
TABEL = "orders"

# ========== CONFIG ATR + GRID ==========
ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0

GRID_MIN = 250
GRID_MAX = 1000

WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()

# ========== GLOBAL ==========
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
GRID_MANAGER = {"grid_step": GRID_MIN, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}

BUY_HISTORY = set()
LAST_ERROR_MSG = ""

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}

# ========== FUNGSI SUPABASE ==========
def sb_insert(data):
    try: requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
    except: pass

def sb_select(filters=""):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        return r.json()
    except: return []

def sb_delete(filters):
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
    except: pass

def auto_create_table(): # INI YG KEMARIN KEPOTONG
    pass # Biarin kosong aja. Kita bikin manual di Supabase

# ========== FUNGSI BINANCE ==========
def signed_request(method, endpoint, params={}):
    params['timestamp'] = int(time.time() * 1000)
    query_string = urlencode(params)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    return requests.request(method, url, headers=headers, timeout=10).json()

def get_price():
    return float(requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()['price'])

def get_all_balance():
    data = signed_request("GET", "/api/v3/account")
    usdt = float(next((b['free'] for b in data['balances'] if b['asset']=='USDT'), 0))
    btc = float(next((b['free'] for b in data['balances'] if b['asset']=='BTC'), 0))
    return usdt, btc

def get_binance_rules(symbol):
    data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}", timeout=5).json()
    for f in data['symbols'][0]['filters']:
        if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
        if f['filterType']=='LOT_SIZE':
            BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    return float(int(qty / step) * step)

def hitung_qty_aman(harga, target_usdt=5):
    qty = target_usdt / harga
    qty = format_qty(qty)
    if qty * harga < BINANCE_RULES['min_notional']: qty = format_qty(BINANCE_RULES['min_notional'] / harga)
    return qty

def hitung_butuh_modal(price, qty, fee=0.001):
    return (price * qty * (1 + fee)) + BUFFER_USDT

# ========== FUNGSI TELEGRAM ==========
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ========== FUNGSI ATR & GRID ==========
def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=5)
    data = r.json(); tr_list = []
    for i in range(1, len(data)):
        high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def update_grid_manager():
    global GRID_MANAGER, DAILY_STATS
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}
    if GRID_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        grid_step = max(GRID_MIN, min(atr_baru * ATR_MULTIPLIER, GRID_MAX))
        GRID_MANAGER = {"grid_step": grid_step, "atr": atr_baru, "date": hari_ini_wib}
        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nGrid Baru: {grid_step:.2f}")

def generate_grid_levels(harga_tengah, grid_step):
    return sorted(list(set([harga_tengah + (i * grid_step) for i in range(-3, 4) if harga_tengah + (i * grid_step) > 0])))

# ========== FUNGSI LOGIKA ==========
def is_price_exist(price):
    return False # Sederhanain dulu biar gak error

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    if not FIRST_BUY_DONE and time.time() - START_TIME > WAIT_FIRST_BUY:
        FIRST_BUY_DONE = True; return True, price
    grids = generate_grid_levels(price, GRID_MANAGER["grid_step"])
    buy_grid = grids[2] # grid ke 3 dari bawah
    if buy_grid not in BUY_HISTORY: return True, buy_grid
    return False, 0

def cek_signal_sell(price):
    grids = generate_grid_levels(price, GRID_MANAGER["grid_step"])
    sell_grid = grids[4] # grid ke 3 dari atas
    if sell_grid in BUY_HISTORY: return True, sell_grid, False
    return False, 0, False

def cek_sell_instan_darurat(price):
    usdt, btc = get_all_balance()
    if btc > 0.00001:
        qty = format_qty(btc)
        signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        send_telegram(f"⚠️ <b>SELL DARURAT</b> {price}")

def place_order_real(side, price_grid, qty, is_reentry=False):
    global BUY_HISTORY
    if side=="BUY" and price_grid in BUY_HISTORY: return
    usdt, btc = get_all_balance()
    if side=="BUY":
        butuh = hitung_butuh_modal(price_grid, qty)
        if usdt < butuh:
            if not NOTIF_FLAGS["saldo_kurang"]: send_telegram(f"💰 SALDO KURANG. Butuh {butuh:.2f}")
            NOTIF_FLAGS["saldo_kurang"]=True; return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
        if 'orderId' in res:
            BUY_HISTORY.add(price_grid)
            sb_insert({"price":price_grid, "qty":qty, "side":"BUY", "status":"OPEN"})
            send_telegram(f"🟢 <b>BUY</b> {price_grid:.2f}\nQty: {qty}")
            NOTIF_FLAGS["saldo_kurang"]=False
    if side=="SELL":
        if btc < qty: return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res:
            profit = (price_grid * qty) - (price_grid * qty) # sederhanain
            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            BUY_HISTORY.discard(price_grid)
            sb_delete(f"price=eq.{price_grid}")
            send_telegram(f"🔴 <b>SELL</b> {price_grid:.2f}\nProfit: {profit:.2f}")

async def main():
    global START_TIME, NOTIF_FLAGS; START_TIME = time.time()
    auto_create_table(); get_binance_rules(SYMBOL); update_grid_manager()
    saldo_usdt, saldo_btc = get_all_balance(); harga_sekarang = get_price()
    send_telegram(f"🤖 <b>Bot V11.39 FIXED</b>\n<b>Harga BTC:</b> {harga_sekarang}\n<b>Saldo:</b>\nUSDT: {saldo_usdt:.2f}\nBTC: {saldo_btc:.6f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    while True:
        try:
            price = get_price(); update_grid_manager()
            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, is_reentry = cek_signal_sell(price)
            if signal_sell: place_order_real("SELL", grid_sell, hitung_qty_aman(grid_sell), is_reentry)
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]: send_telegram(f"❌ <b>ERROR</b>\n{e}"); NOTIF_FLAGS["error"]=True
            await asyncio.sleep(5); NOTIF_FLAGS["error"]=False

if __name__ == "__main__":
    asyncio.run(main())
