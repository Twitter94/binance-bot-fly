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
    if not os.getenv(v): sys.exit(f"FATAL: {v} belum di set")

SYMBOL = "BTCUSDT"
GRID_JARAK = 250
LOOP_SEC = 3
TABEL = "orders"
TABEL_STATE = "bot_state"
JSON_FILE = "pending_orders.json"
WAIT_FIRST_BUY = 60
NOTIF_MODE = "SILENT"
BASE_URL = "https://api.binance.com"
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
SB_HEADERS_DELETE = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
WIB = timezone(timedelta(hours=7))

# ========== GLOBAL ==========
STATE = {
    "paper_mode": True,
    "paper_usdt": 10000.0,
    "paper_btc": 0.0,
    "last_buy_time": 0
}
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
BUYING_LOCK = set(); SELL_LOCK = set(); START_TIME = time.time(); LAST_SYNC = 0

# ========== UTIL ==========
def log(msg):
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}")
    try: open("bot_log.txt", "a", encoding="utf-8").write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except: pass

def notif(msg):
    log(msg)
    if NOTIF_MODE == "NORMAL" or "❌" in msg or "🟢" in msg or "🔴" in msg:
        try: requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
        except: pass

def signed_request(method, endpoint, params=None):
    if params is None: params = {}
    params['timestamp'] = int(time.time() * 1000); params['recvWindow'] = 60000
    query_string = urlencode(params)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    r = requests.request(method, url, headers={'X-MBX-APIKEY': BINANCE_API_KEY}, timeout=10)
    return r.json() if r.status_code == 200 else {}

def get_price(): return float(requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()['price'])
def format_qty(qty): step = BINANCE_RULES['step_size']; qty = math.floor(qty / step) * step; return f"{qty:.8f}".rstrip('0').rstrip('.')
def hitung_qty_aman(harga):
    step = BINANCE_RULES['step_size']
    qty = 5.1 / harga
    while harga * float(format_qty(qty)) < 5.01:
        qty += step
    return format_qty(qty)
def hitung_butuh_modal(price, qty): modal = price * float(qty); fee_1x = modal * 0.001; buffer = fee_1x * 5; return modal + fee_1x + fee_1x + buffer

# TAMBAHAN 1: BULETIN HARGA KE GRID
def to_grid(harga):
    return round(round(harga / GRID_JARAK) * GRID_JARAK, 2)

# ========== SUPABASE ==========
def auto_setup_supabase():
    log("CEK TABEL SUPABASE...")
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?limit=1", headers=SB_HEADERS, timeout=5)
    if r.status_code!= 200:
        requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json={"price":0,"qty":0,"side":"INIT","status":"INIT","binance_order_id":"INIT","fee":0,"mode":"INIT"})
        requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=neq.0", headers=SB_HEADERS_DELETE)
        notif("✅ Tabel `orders` siap")
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5)
    if r.status_code!= 200 or not r.json():
        requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}", headers=SB_HEADERS, json={"id":1, "data": STATE})
        notif("✅ Tabel `bot_state` siap")

def load_state():
    global STATE
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5).json()
        if r:
            data = r[0]['data']
            STATE["paper_mode"] = data.get("paper_mode", True)
            STATE["paper_usdt"] = data.get("paper_usdt", 10000.0)
            STATE["paper_btc"] = data.get("paper_btc", 0.0)
            STATE["last_buy_time"] = data.get("last_buy_time", 0)
            log("STATE LOADED")
    except Exception as e: log(f"LOAD STATE ERROR: {e}")

def save_state():
    try:
        data = {"id": 1, "data": STATE}
        requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}", headers=SB_HEADERS, data=json.dumps(data), timeout=5)
    except Exception as e: log(f"SAVE STATE ERROR: {e}")

def sb_select(filters=""):
    try:
        mode = "PAPER" if STATE["paper_mode"] else "RILL"
        if filters: filters += f"&mode=eq.{mode}&status=neq.ZOMBIE"
        else: filters = f"mode=eq.{mode}&status=neq.ZOMBIE"
        return requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5).json()
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
            if requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS_DELETE, timeout=10).status_code == 204: return True
        except: time.sleep(1)
    try: requests.patch(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, json={"status":"ZOMBIE"})
    except: pass
    return False

def save_json(data):
    pending = json.load(open(JSON_FILE)) if os.path.exists(JSON_FILE) else []
    pending.append(data); json.dump(pending, open(JSON_FILE, 'w'))

def load_json():
    if not os.path.exists(JSON_FILE): return []
    data = json.load(open(JSON_FILE)); os.remove(JSON_FILE); return data

# ========== CORE ==========
def get_balance():
    if STATE["paper_mode"]: return STATE["paper_usdt"], STATE["paper_btc"]
    try:
        data = signed_request("GET", "/api/v3/account")
        usdt = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='USDT'), 0))
        btc = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='BTC'), 0))
        return usdt, btc
    except:
        return 0.0, 0.0

def sync_binance():
    global LAST_SYNC
    if time.time() - LAST_SYNC < 10: return
    LAST_SYNC = time.time()
    if STATE["paper_mode"]: return

    data_db = sb_select("status=eq.OPEN&side=eq.BUY")
    db_ids = {str(d['binance_order_id']): d for d in data_db if str(d['binance_order_id']).isdigit()}
    binance_orders = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 500})
    binance_ids = {str(o['orderId']) for o in binance_orders}

    for oid, data in db_ids.items():
        if oid not in binance_ids:
            if sb_delete(data['id']):
                notif(f"🧹 AUTO CLEANUP: Hapus order nyangkut {data['price']:.2f}")

    for o in binance_orders:
        oid = str(o['orderId'])
        if o['side']=='BUY' and o['status']=='FILLED' and oid not in db_ids:
            harga = float(o['fills'][0]['price']); qty = float(o['executedQty']); fee = sum([float(f['commission']) for f in o['fills']])
            if sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": oid, "fee": fee, "time": int(o['time']/1000)}):
                notif(f"[RILL] RECOVERY: BUY {harga:.2f}")

def cek_buy(harga_asli):
    global STATE
    harga = to_grid(harga_asli) # UBAH 1: BULETIN DULU

    if len(sb_select(f"status=eq.OPEN&side=eq.BUY&price=eq.{harga}")) > 0: return # Cegah dobel

    data_atas = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
    if not data_atas:
        if time.time() - STATE["last_buy_time"] > WAIT_FIRST_BUY: place_buy(harga)
        return

    if float(data_atas[0]['price']) - harga >= GRID_JARAK: # Turun 250 dari atas
        place_buy(harga)

def cek_sell(price):
    to_sell = []
    for order in sb_select("status=eq.OPEN&side=eq.BUY"):
        if price >= float(order['price']) + GRID_JARAK:
            data_atas = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
            is_top = order['id'] == data_atas[0]['id']
            to_sell.append({"order": order, "sell_price": price, "is_top": is_top})
    return to_sell

def place_buy(price):
    price = to_grid(price) # UBAH 2: BULETIN JUGA DISINI
    if price in BUYING_LOCK: return False
    BUYING_LOCK.add(price)
    try:
        if len(sb_select(f"status=eq.OPEN&side=eq.BUY&price=eq.{price}")) > 0: return False
        qty = hitung_qty_aman(price)
        usdt, _ = get_balance()
        butuh = hitung_butuh_modal(price, qty)
        if usdt < butuh:
            log(f"Saldo kurang. Butuh ${butuh:.2f} Punya ${usdt:.2f}")
            return False
        order_id = f"PAPER_{int(time.time())}"; fee = float(qty)*price*0.001
        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res: return False
            order_id = res['orderId']; qty = res['executedQty']; fee = sum([float(f['commission']) for f in res['fills']])
        if STATE["paper_mode"]:
            STATE["paper_usdt"] -= float(qty)*price;
            STATE["paper_btc"] += float(qty)
        STATE["last_buy_time"] = time.time(); save_state()
        if not sb_insert({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee}):
            save_json({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee})
        notif(f"🟢 <b>BUY TERISI</b>\nHarga: ${price:.2f}\nQty: {qty}\nButuh: ${butuh:.2f}")
        return True
    finally: BUYING_LOCK.discard(price)

def place_sell(data):
    global STATE
    oid = data['order']['id']
    if oid in SELL_LOCK: return
    SELL_LOCK.add(oid); delete_ok = False
    harga_jual = data['sell_price']
    try:
        _, btc = get_balance(); qty = format_qty(float(data['order']['qty']))
        if float(btc) < float(qty): return
        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res: return
            fee = sum([float(f['commission']) for f in res['fills']])
        else:
            fee = float(qty) * harga_jual * 0.001
            STATE["paper_usdt"] += float(qty) * harga_jual - fee; STATE["paper_btc"] -= float(qty)
        save_state()
        delete_ok = sb_delete(oid)
        profit = (harga_jual - float(data['order']['price'])) * float(qty) - fee - float(data['order']['fee'])
        notif(f"🔴 <b>SELL TP</b>\nBuy: ${float(data['order']['price']):.2f} -> Sell: ${harga_jual:.2f}\nProfit: ${profit:.4f}")
        if data['is_top']: # INI KUNCI GRID INFINITE
            place_buy(to_grid(harga_jual)) # UBAH 3: BULETIN HARGA JUAL JUGA
    finally:
        if delete_ok: SELL_LOCK.discard(oid)

# ========== STATUS CANTIK ==========
def kirim_status_cantik():
    usdt, btc = get_balance()
    price = get_price()
    data_open = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc")
    status_txt = "PAUSE" if usdt < hitung_butuh_modal(price, hitung_qty_aman(price)) else "JALAN"
    emoji_status = "🔴" if status_txt=="PAUSE" else "🟢"
    mode_txt = "SILENT" if NOTIF_MODE == "SILENT" else "NORMAL"
    mode_db = "PAPER" if STATE["paper_mode"] else "RILL"
    tanggal = datetime.now(WIB).strftime("%d_%m_%Y")

    msg = f"""<b>SAFANA GRID {tanggal}</b>

{emoji_status} <i>{status_txt}</i> | Mode: <i>{mode_txt}</i> | DB: <i>{mode_db}</i>
Harga: ${price:.2f} | Grid: ${GRID_JARAK:.2f}
Saldo: ${usdt:.2f} | Butuh: ${hitung_butuh_modal(price, hitung_qty_aman(price)):.2f}
Posisi: {len(data_open)} Grid | BTC: {btc:.8f}"""

    if len(data_open) > 0:
        msg += "\n\n<i>DETAIL POSISI</i>\n"
        msg += "<code>--------------------\nNo | BUY | TP\n--------------------\n"
        no = 1
        for d in data_open:
            tp = float(d['price']) + GRID_JARAK
            msg += f"{no:2}.| ${float(d['price']):8.2f}| ${tp:8.2f}\n"
            no += 1
        msg += "</code>"
    notif(msg)

# ========== TELEGRAM ==========
def kirim_keyboard():
    keyboard = {"keyboard": [[{"text": "STATUS"}]], "resize_keyboard": True}
    requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={
        "chat_id": TELE_CHAT_ID,
        "text": "✅ <b>Panel Kontrol Grid</b>\n\n<b>Perintah Ketik:</b>\n`PAPER` = Mode Simulasi\n`RILL` = Mode Real\n`SILENT` = Notif Penting\n`NORMAL` = Notif Lengkap\n`STATUS` = Lihat Posisi",
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    })

def cek_tele():
    global NOTIF_MODE
    try:
        r = requests.get(f"{TELE_TOKEN}/getUpdates", timeout=3).json()
        if not r.get('result'): return
        u = r['result'][-1]; text = u['message']['text'].strip().upper()
        if str(u['message']['chat']['id'])!= TELE_CHAT_ID: return

        if text == "STATUS":
            kirim_status_cantik()
        elif text == "PAPER":
            STATE["paper_mode"]=True; save_state(); notif("🧪 <b>MODE PAPER AKTIF</b>\nSaldo Virtual: $10.000")
        elif text == "RILL":
            usdt, _ = get_balance()
            STATE["paper_mode"]=False; save_state(); notif(f"💰 <b>MODE RILL AKTIF</b>\nSaldo: ${usdt:.2f}\nHATI-HATI INI UANG BENERAN")
        elif text == "SILENT":
            NOTIF_MODE = "SILENT"; notif("🔇 <b>MODE SILENT</b>\nCuma notif BUY/SELL/ERROR")
        elif text == "NORMAL":
            NOTIF_MODE = "NORMAL"; notif("🔊 <b>MODE NORMAL</b>\nNotifikasi Lengkap Aktif")
        else:
            notif("❓ Perintah tidak dikenal\nKetik: PAPER / RILL / SILENT / NORMAL / STATUS")

        requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={u['update_id']+1}")
    except Exception as e:
        log(f"TELE ERROR: {repr(e)}")

# ========== MAIN ==========
async def main():
    auto_setup_supabase()
    load_state()
    data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={SYMBOL}").json()
    for f in data['symbols'][0]['filters']:
        if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
        if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])

    notif(f"🤖 <b>BOT V14.4.10 ANTI DOBEL</b>\nGrid: ${GRID_JARAK} | Mode: {'PAPER' if STATE['paper_mode'] else 'RILL'}")
    kirim_keyboard()

    while True:
        try:
            sync_binance(); cek_tele()
            for d in load_json(): sb_insert(d)
            price = get_price()
            cek_buy(price)
            for sig in cek_sell(price): place_sell(sig)
            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e: notif(f"❌ <b>ERROR</b>\n<code>{repr(e)}</code>"); await asyncio.sleep(10)

if __name__ == "__main__": asyncio.run(main())
