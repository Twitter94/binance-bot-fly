import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
import sys
import json
import math
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_SECRET = os.getenv("API_SECRET")
SUPABASE_URL = os.getenv("SUPA_URL")
SUPABASE_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
for v in ["API_KEY", "API_SECRET", "SUPA_URL", "SUPA_KEY", "TELE_TOKEN", "TELE_CHAT_ID"]:
    if not os.getenv(v):
        print(f"FATAL: {v} belum di set")
        sys.exit(1)

SYMBOL = "BTCUSDT"
GRID_JARAK = 250 # FIX 250 USDT
LOOP_SEC = 3
TABEL = "orders"
TABEL_STATE = "bot_state"
JSON_FILE = "pending_orders.json"
WAIT_FIRST_BUY = 60

BASE_URL = "https://api.binance.com"
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
WIB = timezone(timedelta(hours=7))

# ========== GLOBAL ==========
STATE = {"paper_mode": True, "paper_usdt": 10000.0, "paper_btc": 0.0}
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
BUYING_LOCK = set()
SELL_LOCK = set()
START_TIME = time.time()
FIRST_BUY_DONE = False
LAST_SYNC = 0

# ========== UTIL ==========
def log(msg):
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}")
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f: f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except: pass

def notif(msg):
    log(msg)
    try: requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def signed_request(method, endpoint, params=None):
    if params is None: params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 60000
    query_string = urlencode(params)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    r = requests.request(method, url, headers=headers, timeout=10)
    return r.json() if r.status_code == 200 else {}

def get_price(): return float(requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()['price'])

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    qty = math.floor(qty / step) * step
    return f"{qty:.8f}".rstrip('0').rstrip('.')

def hitung_qty_aman(harga):
    step = BINANCE_RULES['step_size']
    qty = 5.1 / harga # target 5.1 biar aman
    while harga * float(format_qty(qty)) < 5.01: qty += step
    return format_qty(qty)

# ========== DB & STATE ==========
def load_state():
    global STATE
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5).json()
        if r: STATE = r[0]
        else: requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}", headers=SB_HEADERS, json={"id":1, **STATE})
    except: pass

def save_state():
    try: requests.patch(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, json=STATE)
    except: pass

def sb_select(filters=""):
    try:
        mode = "PAPER" if STATE["paper_mode"] else "RILL"
        if filters: filters += f"&mode=eq.{mode}"
        else: filters = f"mode=eq.{mode}"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        return r.json() if r.status_code == 200 else []
    except: return []

def sb_insert(data):
    try:
        data['mode'] = "PAPER" if STATE["paper_mode"] else "RILL"
        data['price'] = float(data['price']); data['qty'] = float(data['qty']); data['fee'] = float(data.get('fee', 0))
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        return r.json() if r.status_code in [200, 201] else []
    except: return []

def sb_delete(order_id):
    for i in range(5):
        try:
            if requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, timeout=10).status_code == 204: return True
        except: time.sleep(1)
    notif(f"❌ FATAL: GAGAL HAPUS DB {order_id}")
    return False

def save_json(data):
    pending = json.load(open(JSON_FILE)) if os.path.exists(JSON_FILE) else []
    pending.append(data)
    json.dump(pending, open(JSON_FILE, 'w'))

def load_json():
    if not os.path.exists(JSON_FILE): return []
    data = json.load(open(JSON_FILE)); os.remove(JSON_FILE); return data

# ========== CORE LOGIC ==========
def get_balance():
    if STATE["paper_mode"]: return STATE["paper_usdt"], STATE["paper_btc"]
    data = signed_request("GET", "/api/v3/account")
    usdt = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='USDT'), 0))
    btc = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='BTC'), 0))
    return usdt, btc

def sync_binance():
    global LAST_SYNC
    if time.time() - LAST_SYNC < 5: return
    LAST_SYNC = time.time()
    if STATE["paper_mode"]: return

    data_db = {str(d['binance_order_id']): d for d in sb_select("status=eq.OPEN&side=eq.BUY") if 'binance_order_id' in d}
    for o in signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 10}):
        oid = str(o['orderId'])
        if o['side']=='BUY' and o['status']=='FILLED' and oid not in data_db:
            harga = float(o['fills'][0]['price']); qty = float(o['executedQty']); fee = sum([float(f['commission']) for f in o['fills']])
            sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": oid, "fee": fee, "time": int(o['time']/1000)})
            notif(f"[RILL] RECOVERY: Ketemu BUY di {harga:.2f}")
        elif oid in data_db:
            detail = signed_request("GET", "/api/v3/order", {"symbol":SYMBOL, "orderId": oid})
            if detail.get('status')!= 'FILLED': sb_delete(data_db[oid]['id'])

def cek_buy(price):
    global FIRST_BUY_DONE
    data_open = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc")
    if not FIRST_BUY_DONE and len(data_open)==0 and time.time()-START_TIME>WAIT_FIRST_BUY:
        FIRST_BUY_DONE=True; return True, price
    if data_open:
        harga_tertinggi = float(data_open[0]['price'])
        if price <= harga_tertinggi - GRID_JARAK: return True, price
    return False, 0

def cek_sell(price):
    data_open = sb_select("status=eq.OPEN&side=eq.BUY&order=price.asc&limit=1")
    if not data_open: return None
    order = data_open[0]
    tp = float(order['price']) + GRID_JARAK
    if price >= tp:
        data_tertinggi = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
        is_top = order['id'] == data_tertinggi[0]['id'] if data_tertinggi else False
        return {"order": order, "sell_price": price, "is_top": is_top}
    return None

def place_buy(price):
    if price in BUYING_LOCK: return
    BUYING_LOCK.add(price)
    try:
        qty = hitung_qty_aman(price)
        usdt, _ = get_balance()
        butuh = float(qty) * price * 1.006 # modal + fee + buffer
        if usdt < butuh: return notif(f"💰 SALDO KURANG: {usdt:.2f} < {butuh:.2f}")

        order_id = f"PAPER_{int(time.time())}"; fee = butuh * 0.001
        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res: return notif(f"❌ BUY GAGAL: {res}")
            order_id = res['orderId']; qty = res['executedQty']; fee = sum([float(f['commission']) for f in res['fills']])
            STATE["paper_usdt"] -= float(qty)*price; STATE["paper_btc"] += float(qty)
        else:
            STATE["paper_usdt"] -= float(qty)*price; STATE["paper_btc"] += float(qty)
        save_state()

        if not sb_insert({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee}):
            save_json({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee})
        notif(f"🟢 BUY: {price:.2f} | Qty: {qty}")
    finally: BUYING_LOCK.discard(price)

def place_sell(data):
    oid = data['order']['id']
    if oid in SELL_LOCK: return
    SELL_LOCK.add(oid)
    delete_ok = False
    try:
        _, btc = get_balance()
        qty = format_qty(float(data['order']['qty']))
        if float(btc) < float(qty): return

        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res: return
            fee = sum([float(f['commission']) for f in res['fills']])
        else:
            fee = float(qty) * data['sell_price'] * 0.001
            STATE["paper_usdt"] += float(qty) * data['sell_price'] - fee
            STATE["paper_btc"] -= float(qty)
        save_state()

        delete_ok = sb_delete(oid)
        if not delete_ok: return notif(f"❌ LOCK PERMANEN: {oid}")

        profit = (data['sell_price'] - float(data['order']['price'])) * float(qty) - fee - float(data['order']['fee'])
        notif(f"🔴 SELL TP: {data['sell_price']:.2f} | Profit: {profit:.4f}")

        if data['is_top']: # RE-ENTRY
            place_buy(data['sell_price'])
    finally:
        if delete_ok: SELL_LOCK.discard(oid)

# ========== TELEGRAM ==========
def cek_tele():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates", timeout=3).json()
        if not r.get('result'): return
        u = r['result'][-1]; text = u['message']['text'].upper()
        if str(u['message']['chat']['id'])!= TELE_CHAT_ID: return
        if text == "STATUS":
            usdt, btc = get_balance(); price = get_price()
            notif(f"<b>STATUS</b>\nMode: {'PAPER' if STATE['paper_mode'] else 'RILL'}\nHarga: {price:.2f}\nSaldo: {usdt:.2f} | {btc:.8f}\nGrid: {len(sb_select('status=eq.OPEN&side=eq.BUY'))}")
        elif text == "RILL": STATE["paper_mode"]=False; save_state(); notif("💰 MODE RILL AKTIF")
        elif text == "PAPER": STATE["paper_mode"]=True; save_state(); notif("🧪 MODE PAPER AKTIF")
        requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={u['update_id']+1}")
    except: pass

# ========== MAIN ==========
async def main():
    load_state(); get_balance() # trigger ambil rule
    data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={SYMBOL}").json()
    for f in data['symbols'][0]['filters']:
        if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
        if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])

    notif(f"🤖 BOT V14.0.0 START\nGrid: {GRID_JARAK} | Mode: {'PAPER' if STATE['paper_mode'] else 'RILL'}")

    while True:
        try:
            sync_binance(); cek_tele()
            for d in load_json(): sb_insert(d)

            price = get_price()
            buy_sig, buy_price = cek_buy(price)
            if buy_sig: place_buy(buy_price)

            sell_sig = cek_sell(price)
            if sell_sig: place_sell(sell_sig)

            gc.collect()
            await asyncio.sleep(LOOP_SEC)
        except Exception as e: notif(f"❌ ERROR: {repr(e)}"); await asyncio.sleep(10)

if __name__ == "__main__": asyncio.run(main())
