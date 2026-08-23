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
BUFFER_USDT = 0.5 # Tabungan
TABEL = "orders"
TARGET_USDT_PER_BUY = 5

# ========== CONFIG ATR ==========
ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_JARAK = 250 # <--- MINIMAL JARAK
MAX_JARAK = 1000 # <--- MAKSIMAL JARAK

WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()

# ========== GLOBAL ==========
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": 500, "date": None, "atr": 0} # Default 500
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# ========== FUNGSI SUPABASE ==========
def sb_insert(data):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        return r.json()
    except: return []

def sb_select(filters=""):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        return r.json()
    except: return []

def sb_delete(order_id):
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, timeout=5)
    except: pass

def auto_create_table(): pass

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
        if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])

def cancel_order(order_id):
    signed_request("DELETE", "/api/v3/order", {"symbol":SYMBOL, "orderId": order_id})

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    return float(int(qty / step) * step)

def hitung_qty_aman(harga, target_usdt=TARGET_USDT_PER_BUY):
    qty = target_usdt / harga
    qty = format_qty(qty)
    if qty * harga < BINANCE_RULES['min_notional']: qty = format_qty(BINANCE_RULES['min_notional'] / harga)
    return qty

def hitung_butuh_modal(price, qty):
    modal = price * qty
    fee_buy = modal * 0.001
    fee_sell = modal * 0.001
    return modal + fee_buy + fee_sell + BUFFER_USDT # 1.002 + 0.5

# ========== FUNGSI TELEGRAM ==========
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

# ========== FUNGSI ATR CLAMP 250-1000 ==========
def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=5)
    data = r.json(); tr_list = []
    for i in range(1, len(data)):
        high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def update_atr_manager():
    global ATR_MANAGER, DAILY_STATS
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}

    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        jarak_mentah = atr_baru * ATR_MULTIPLIER
        jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK)) # <--- CLAMP DISINI

        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}
        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nMentah: {jarak_mentah:.2f}\nFinal: {jarak:.2f}")

# ========== LOGIKA TANPA GRID ==========
def is_price_exist(price):
    data = sb_select(f"price=eq.{price}&side=eq.BUY&status=eq.OPEN")
    return data[0] if len(data) > 0 else None

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    update_atr_manager()
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")

    if not FIRST_BUY_DONE and len(data_open) == 0 and time.time() - START_TIME > WAIT_FIRST_BUY:
        FIRST_BUY_DONE = True; return True, price

    if len(data_open) > 0:
        harga_buy_terakhir = data_open[0]['price']
        if price <= harga_buy_terakhir - jarak:
            if not is_price_exist(price):
                return True, price
    return False, 0

def cek_signal_sell(price):
    update_atr_manager()
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc&limit=1")
    if len(data_open) > 0:
        order_data = data_open[0]
        harga_beli = order_data['price']
        if price >= harga_beli + jarak:
            return True, price, order_data
    return False, 0, None

def cek_sell_instan_darurat(price):
    usdt, btc = get_all_balance()
    if btc > 0.00001:
        data_nyangkut = sb_select(f"status=eq.OPEN&side=eq.BUY")
        if len(data_nyangkut) == 0:
            qty = format_qty(btc)
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' in res:
                send_telegram(f"⚠️ <b>SELL DARURAT</b> {qty:.6f} BTC @ {price:.2f}")

def place_order_real(side, price_grid, qty, order_data=None):
    global NOTIF_FLAGS
    usdt, btc = get_all_balance()
    if side=="BUY":
        if is_price_exist(price_grid): return
        butuh = hitung_butuh_modal(price_grid, qty)
        if usdt < butuh:
            if not NOTIF_FLAGS["saldo_kurang"]: send_telegram(f"💰 SALDO KURANG. Butuh {butuh:.2f} USDT")
            NOTIF_FLAGS["saldo_kurang"]=True; return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
        if 'orderId' in res:
            insert_res = sb_insert({"price":price_grid, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": res['orderId']})
            if len(insert_res) == 0:
                cancel_order(res['orderId'])
                send_telegram("❌ Gagal catat ke DB. Order BUY di CANCEL")
                return
            send_telegram(f"🟢 <b>BUY</b> {price_grid:.2f}\nButuh: {butuh:.2f}\nQty: {qty}\nJarak: {ATR_MANAGER['jarak']:.2f}")
            NOTIF_FLAGS["saldo_kurang"]=False

    if side=="SELL":
        if btc < qty: return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res and order_data:
            harga_beli = order_data['price']
            harga_jual_net = price_grid * (1 - 0.001)
            harga_beli_net = harga_beli * (1 + 0.001)
            profit = (harga_jual_net - harga_beli_net) * qty
            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            send_telegram(f"🔴 <b>SELL TP</b> {price_grid:.2f}\nBeli: {harga_beli:.2f}\nProfit: {profit:.2f} USDT\nJarak: {ATR_MANAGER['jarak']:.2f}")

async def main():
    global START_TIME; START_TIME = time.time()
    auto_create_table(); get_binance_rules(SYMBOL)
    harga_sekarang = get_price(); update_atr_manager()
    saldo_usdt, saldo_btc = get_all_balance()
    send_telegram(f"🤖 <b>Bot V11.53 CLAMP 250-1000</b>\n<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    while True:
        try:
            price = get_price()
            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, order_data = cek_signal_sell(price)
            if signal_sell: place_order_real("SELL", grid_sell, hitung_qty_aman(order_data['price']), order_data)
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]: send_telegram(f"❌ <b>ERROR</b>\n{e}"); NOTIF_FLAGS["error"]=True
            await asyncio.sleep(5); NOTIF_FLAGS["error"]=False

if __name__ == "__main__":
    asyncio.run(main())
