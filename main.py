import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
import sys
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_SECRET = os.getenv("API_SECRET")
SUPABASE_URL = os.getenv("SUPA_URL")
SUPABASE_KEY = os.getenv("SUPA_KEY") # HARUS sb_secret_...
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

for v in ["API_KEY", "API_SECRET", "SUPA_URL", "SUPA_KEY", "TELE_TOKEN", "TELE_CHAT_ID"]:
    if not os.getenv(v):
        print(f"FATAL: {v} belum di set")
        sys.exit(1)

SYMBOL = "BTCUSDT"
LOOP_SEC = 3
BUFFER_USDT = 0.5
TABEL = "orders"
TARGET_USDT_PER_BUY = 5

ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_JARAK = 250
MAX_JARAK = 1000

WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()
FLAG_FILE = "/tmp/table_created.flag"

BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": 500, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

# ========== AUTO CREATE TABLE 1X - FIX ANTI 404 ==========
def auto_create_table():
    if os.path.exists(FLAG_FILE):
        return

    # Cara baru: INSERT dummy. Paling ampuh buat bikin tabel
    data_dummy = {
        "price": 0,
        "qty": 0,
        "side": "INIT",
        "status": "INIT",
        "binance_order_id": 999999
    }

    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data_dummy, timeout=10)

        if r.status_code in [200, 201]:
            send_telegram("✅ Tabel `orders` auto dibuat 1x")
            # Hapus data dummy
            requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?binance_order_id=eq.999999", headers=SB_HEADERS, timeout=5)
            with open(FLAG_FILE, "w") as f: f.write("done")
        else:
            send_telegram(f"⚠️ GAGAL BUAT TABEL. Cek: 1.SUPA_KEY sb_secret 2.RLS mati\nErr: {r.status_code} {r.text[:100]}")

    except Exception as e:
        send_telegram(f"⚠️ Auto create tabel error: {e}")

# ========== FUNGSI SUPABASE ==========
def sb_insert(data):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if not NOTIF_FLAGS["error"]: send_telegram(f"❌ SB INSERT: {e}")
        return []

def sb_select(filters=""):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if not NOTIF_FLAGS["error"]: send_telegram(f"❌ SB SELECT: {e}")
        return []

def sb_delete(order_id):
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, timeout=5)
    except: pass

# ========== FUNGSI BINANCE ==========
def signed_request(method, endpoint, params={}):
    try:
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 60000
        query_string = urlencode(params)
        signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
        headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
        r = requests.request(method, url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if not NOTIF_FLAGS["error"]: send_telegram(f"❌ BINANCE API: {e}")
        return {}

def get_price():
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
        r.raise_for_status()
        return float(r.json()['price'])
    except Exception as e:
        if not NOTIF_FLAGS["error"]: send_telegram(f"❌ GET PRICE: {e}")
        time.sleep(10)
        return get_price()

def get_all_balance():
    data = signed_request("GET", "/api/v3/account")
    if 'balances' not in data: return 0,0
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
    qty_floored = int(qty / step) * step
    return f"{qty_floored:.8f}"

def hitung_qty_aman(harga, target_usdt=TARGET_USDT_PER_BUY):
    qty = target_usdt / harga
    qty = format_qty(qty)
    if float(qty) * harga < BINANCE_RULES['min_notional']: qty = format_qty(BINANCE_RULES['min_notional'] / harga)
    return qty

def hitung_butuh_modal(price, qty):
    modal = price * float(qty)
    return modal * 1.002 + BUFFER_USDT

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

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
        jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK))
        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}
        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nFinal: {jarak:.2f}")

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
                send_telegram(f"⚠️ <b>SELL DARURAT</b> {qty} BTC @ {price:.2f}")

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
            insert_res = sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": res['orderId']})
            if len(insert_res) == 0:
                cancel_order(res['orderId'])
                send_telegram("❌ Gagal catat ke DB. Order BUY di CANCEL")
                return
            send_telegram(f"🟢 <b>BUY</b> {price_grid:.2f}\nButuh: {butuh:.2f}\nQty: {qty}\nJarak: {ATR_MANAGER['jarak']:.2f}")
            NOTIF_FLAGS["saldo_kurang"]=False
    if side=="SELL":
        if float(btc) < float(qty): return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res and order_data:
            harga_beli = order_data['price']
            profit = ((price_grid * 0.999) - (harga_beli * 1.001)) * float(qty)
            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            send_telegram(f"🔴 <b>SELL TP</b> {price_grid:.2f}\nProfit: {profit:.2f} USDT\nJarak: {ATR_MANAGER['jarak']:.2f}")

async def main():
    global START_TIME; START_TIME = time.time()
    auto_create_table()
    get_binance_rules(SYMBOL)
    send_telegram("🤖 <b>Bot V11.61 START</b> FIX 404")
    harga_sekarang = get_price(); update_atr_manager()
    saldo_usdt, saldo_btc = get_all_balance()
    send_telegram(f"<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    while True:
        try:
            price = get_price()
            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, order_data = cek_signal_sell(price)
            if signal_sell: place_order_real("SELL", grid_sell, hitung_qty_aman(order_data['price']), order_data)
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            gc.collect(); NOTIF_FLAGS["error"]=False; await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]:
                send_telegram(f"❌ <b>CRITICAL</b>\n<code>{repr(e)}</code>")
                NOTIF_FLAGS["error"]=True
            await asyncio.sleep(15)

if __name__ == "__main__":
    asyncio.run(main())
