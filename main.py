import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
import sys
import json
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
NOTIF_MODE = "SILENT" # "SILENT" = cuma BUY/SELL/ERROR, "NORMAL" = semua notif

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
LOOP_SEC = 3
BUFFER_USDT = 0.5
TABEL = "orders"
RECOVERY_INTERVAL = 3600
RE_ENTRY_MODE = True
REENTRY_COOLDOWN = 60
ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_JARAK = 250
MAX_JARAK = 1000
JSON_FILE = "pending_orders.json"
WAIT_FIRST_BUY = 60
FIRST_BUY_DONE = False
START_TIME = time.time()
LAST_RECOVERY = 0
BUYING_LOCK = set()
PERLU_REENTRY = False
LAST_REENTRY_TIME = 0
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": None, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False, "critical_msg": ""}
NOTIF_SENT = {"buy": None, "sell": None}
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

def log_only(msg):
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except: pass
    if NOTIF_MODE == "NORMAL": send_telegram(msg)

def notif_penting(msg):
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except: pass
    send_telegram(msg)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def kirim_keyboard():
    keyboard = {"keyboard": [[{"text": "GANTI MODE"}, {"text": "STATUS"}]], "resize_keyboard": True, "one_time_keyboard": False}
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": "✅ <b>Panel Kontrol Aktif</b>", "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}, timeout=5)
    except: pass

def kirim_status_lengkap():
    usdt, btc = get_all_balance(); price = get_price(); jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else 0
    mode = "🔇 SILENT" if NOTIF_MODE == "SILENT" else "🔊 NORMAL"
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc")
    posisi = "TIDAK ADA POSISI"
    if len(data_open) > 0: posisi = f"BUY: {data_open[0]['price']:.2f} s/d {data_open[-1]['price']:.2f} | Total: {len(data_open)} grid"
    status_bot = "PAUSE - MENUNGGU SALDO" if NOTIF_FLAGS["saldo_kurang"] else "JALAN"
    msg = f"📊 <b>STATUS BOT V11.63.43 BINANCE RAJA</b>\n<b>Mode:</b> {mode}\n<b>Status:</b> {status_bot}\n<b>Harga:</b> {price:.2f}\n<b>ATR Jarak:</b> {jarak:.2f}\n<b>Saldo USDT:</b> {usdt:.2f}\n<b>Saldo BTC:</b> {btc:.8f}\n<b>Posisi:</b> {posisi}\n<b>Profit Hari Ini:</b> {DAILY_STATS['profit_usdt']:.4f} USDT"
    notif_penting(msg)

def cek_command_telegram():
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates"
        r = requests.get(url, timeout=3).json()
        if 'result' not in r or len(r['result']) == 0: return
        last_update = r['result'][-1]
        if 'message' not in last_update: return
        text = last_update['message'].get('text', '').lower()
        chat_id = str(last_update['message']['chat']['id'])
        if chat_id!= TELE_CHAT_ID: return
        global NOTIF_MODE
        if text == "ganti mode":
            NOTIF_MODE = "NORMAL" if NOTIF_MODE == "SILENT" else "SILENT"
            txt = "🔊 MODE NORMAL AKTIF" if NOTIF_MODE == "NORMAL" else "🔇 MODE SILENT AKTIF"
            notif_penting(txt)
            requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={last_update['update_id']+1}")
        elif text == "status":
            kirim_status_lengkap()
            requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={last_update['update_id']+1}")
    except: pass

def save_to_json(data):
    try:
        pending = []
        if os.path.exists(JSON_FILE):
            with open(JSON_FILE, 'r') as f: pending = json.load(f)
        pending.append(data)
        with open(JSON_FILE, 'w') as f: json.dump(pending, f)
    except Exception as e: notif_penting(f"❌ GAGAL SAVE JSON: {repr(e)}")

def load_and_clear_json():
    if not os.path.exists(JSON_FILE): return []
    try:
        with open(JSON_FILE, 'r') as f: pending = json.load(f)
        os.remove(JSON_FILE)
        return pending
    except: return []

def cek_tabel_supabase():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?limit=1", headers=SB_HEADERS, timeout=5)
        if r.status_code == 200:
            notif_penting("✅ Koneksi Supabase OK. Tabel `orders` ada")
            pending = load_and_clear_json()
            if len(pending) > 0:
                notif_penting(f"🔄 Menemukan {len(pending)} order di JSON. Mencoba insert ke DB...")
                for p in pending: sb_insert(p)
        else:
            notif_penting(f"⚠️ Supabase Error: {r.status_code}. Retry 5 detik")
            time.sleep(5)
    except Exception as e:
        notif_penting(f"⚠️ Gagal konek Supabase: {repr(e)}. Retry 5 detik")
        time.sleep(5)

def sb_insert(data):
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        if r.status_code not in [200,201]: return []
        return r.json()
    except: return []

def sb_select(filters=""):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        if r.status_code!= 200: return []
        data = r.json()
        return data if isinstance(data, list) else []
    except: return []

def sb_delete(order_id):
    try: requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, timeout=5)
    except: pass

def bersihin_sampah():
    tujuh_hari_lalu = int(time.time()) - (7 * 24 * 3600)
    try:
        requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?status=neq.OPEN&time=lt.{tujuh_hari_lalu}", headers=SB_HEADERS, timeout=5)
        log_only("🧹 Sampah DB CLOSED>7hari dihapus")
    except: pass
    global NOTIF_FLAGS
    NOTIF_FLAGS = {"error": False, "saldo_kurang": False, "critical_msg": NOTIF_FLAGS["critical_msg"]}
    gc.collect()

def sync_3_sumber():
    global PERLU_REENTRY
    count = 0
    data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 500})
    data_db = sb_select(f"status=eq.OPEN")
    data_json = load_and_clear_json()
    if not isinstance(data_binance, list): data_binance = []
    if len(data_json) > 0:
        for p in data_json: sb_insert(p)
        data_db = sb_select(f"status=eq.OPEN")
    db_dict = {str(d['binance_order_id']): d for d in data_db if 'binance_order_id' in d}
    for o in data_binance:
        order_id = str(o['orderId']); ada_di_db = order_id in db_dict
        if o['side'] == 'BUY' and o['status'] == 'FILLED' and o.get('fills'):
            harga = float(o['fills'][0]['price']); qty = float(o['executedQty']); fee_buy = sum([float(f['commission']) * float(f['price']) for f in o['fills']])
            if not ada_di_db:
                sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(time.time())})
                count += 1; log_only(f"⚠️ SYNC: Ketemu BUY Floating di {harga:.2f}")
        if o['side'] == 'SELL' and o['status'] == 'FILLED':
            if ada_di_db: sb_delete(db_dict[order_id]['id']); count += 1
    for order_id, d in db_dict.items():
        ketemu = any(str(o['orderId']) == order_id for o in data_binance)
        if not ketemu: sb_delete(d['id']); count += 1
    if count > 0: log_only(f"🔄 Sync: {count} data diperbaiki")
    cek_sell_instan_darurat(get_price())

def signed_request(method, endpoint, params=None):
    if params is None: params = {}
    try:
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 60000
        query_string = urlencode(params)
        signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
        headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
        r = requests.request(method, url, headers=headers, timeout=10)
        if r.status_code!= 200: return {}
        return r.json()
    except: return {}

def get_price():
    try: return float(requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()['price'])
    except: time.sleep(10); return 0

def get_all_balance():
    data = signed_request("GET", "/api/v3/account")
    if 'balances' not in data: return 0,0
    usdt = float(next((b['free'] for b in data['balances'] if b['asset']=='USDT'), 0))
    btc = float(next((b['free'] for b in data['balances'] if b['asset']=='BTC'), 0))
    return usdt, btc

def get_binance_rules(symbol):
    try:
        data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}", timeout=5).json()
        for f in data['symbols'][0]['filters']:
            if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
            if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])
    except: pass

def get_binance_fee():
    try:
        data = signed_request("GET", "/api/v3/account")
        return float(data['takerCommission']) / 10000 if 'takerCommission' in data else 0.001
    except: return 0.001

def format_qty(qty):
    step = BINANCE_RULES['step_size']; min_qty = BINANCE_RULES['min_qty']
    qty_floored = int(qty / step) * step
    if qty_floored < min_qty: qty_floored = min_qty
    return f"{qty_floored:.8f}"

def hitung_qty_aman(harga):
    qty = max(BINANCE_RULES['min_qty'], BINANCE_RULES['min_notional'] / harga)
    qty_formatted = float(format_qty(qty))
    if harga * qty_formatted < BINANCE_RULES['min_notional']: qty_formatted += BINANCE_RULES['step_size']
    return format_qty(qty_formatted)

def hitung_butuh_modal(price, qty): fee = get_binance_fee(); modal = price * float(qty); return modal + (modal * fee * 2) + BUFFER_USDT

def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    try:
        data = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=10).json()
        tr_list = [max(float(data[i][2])-float(data[i][3]), abs(float(data[i][2])-float(data[i-1][4])), abs(float(data[i][3])-float(data[i-1][4]))) for i in range(1, len(data))]
        return sum(tr_list[-period:]) / period
    except: return 0

def update_atr_manager():
    global ATR_MANAGER, DAILY_STATS, NOTIF_SENT
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}; NOTIF_SENT = {"buy": None, "sell": None}
    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        if atr_baru > 0: jarak = max(MIN_JARAK, min(atr_baru * ATR_MULTIPLIER, MAX_JARAK)); ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}; notif_penting(f"📊 <b>ATR UPDATE</b>\nATR: {atr_baru:.2f}\nJarak: {jarak:.2f}")

def is_price_exist(price):
    jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else MIN_JARAK
    return len(sb_select(f"price=gte.{price-jarak/2}&price=lte.{price+jarak/2}&side=eq.BUY&status=eq.OPEN")) > 0

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
    if not FIRST_BUY_DONE and len(data_open) == 0 and time.time() - START_TIME > WAIT_FIRST_BUY: FIRST_BUY_DONE = True; return True, price
    if len(data_open) > 0 and price <= data_open[0]['price'] - jarak and not is_price_exist(price): return True, price
    return False, 0

def cek_signal_sell(price):
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0, None, False
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc&limit=1")
    if len(data_open) > 0 and price >= data_open[0]['price'] + ATR_MANAGER["jarak"]:
        data_tertinggi = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
        return True, price, data_open[0], len(data_tertinggi) > 0 and data_tertinggi[0]['id'] == data_open[0]['id']
    return False, 0, None, False

def cek_sell_instan_darurat(price):
    _, btc = get_all_balance()
    if btc < BINANCE_RULES['min_qty']: return
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY")
    if len(data_db) > 0:
        try: harga_buy_pertama = min([d['price'] for d in data_db])
        except: harga_buy_pertama = 0
        if harga_buy_pertama > 0 and price > harga_buy_pertama:
            qty = hitung_qty_aman(price); nilai_jual = price * float(qty); butuh_min = hitung_butuh_modal(price, qty)
            if nilai_jual >= butuh_min:
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
                if 'orderId' in res:
                    for d in data_db: sb_delete(d['id'])
                    profit = (price - harga_buy_pertama) * float(qty)
                    notif_penting(f"✅ MODE 1 SUKSES\nJual {qty} @ {price:.2f}\nProfit Kotor: {profit:.4f} USDT")

def recovery_sync(): sync_3_sumber()

def cek_order_binance_sudah_ada(price_target):
    data = signed_request("GET", "/api/v3/openOrders", {"symbol":SYMBOL})
    return any(abs(float(o['price']) - price_target) < 0.01 for o in data) if isinstance(data, list) else False

def place_order_real(side, price_grid, qty, order_data=None, is_top_grid=False):
    global NOTIF_FLAGS, NOTIF_SENT, BUYING_LOCK, PERLU_REENTRY, LAST_REENTRY_TIME
    if side=="BUY":
        if price_grid in BUYING_LOCK or is_price_exist(price_grid) or cek_order_binance_sudah_ada(price_grid): return
        BUYING_LOCK.add(price_grid)
        try:
            usdt, btc = get_all_balance(); butuh = hitung_butuh_modal(price_grid, qty)
            if usdt < butuh:
                if not NOTIF_FLAGS["saldo_kurang"]: notif_penting(f"💰 <b>SALDO KURANG</b>\nUSDT: {usdt:.2f} | Butuh: {butuh:.2f}")
                NOTIF_FLAGS["saldo_kurang"]=True; return
            if NOTIF_FLAGS["saldo_kurang"]: notif_penting(f"✅ <b>SALDO SUDAH CUKUP</b>"); NOTIF_FLAGS["saldo_kurang"]=False
            if PERLU_REENTRY: notif_penting(f"✅ <b>RE-ENTRY BERHASIL</b>"); PERLU_REENTRY = False
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
            if 'orderId' in res:
                order_id = res['orderId']; fee_buy = sum([float(f['commission']) * float(f['price']) for f in res['fills']])
                if len(sb_select(f"binance_order_id=eq.{order_id}")) == 0:
                    if len(sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(time.time())})) == 0:
                        save_to_json({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(time.time())})
                if NOTIF_SENT["buy"]!= price_grid: notif_penting(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}"); NOTIF_SENT["buy"] = price_grid; NOTIF_SENT["sell"] = None
        except: pass
        finally: BUYING_LOCK.discard(price_grid)
    if side=="SELL":
        qty_db = format_qty(float(order_data['qty']))
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_db})
        if 'orderId' in res and order_data:
            harga_beli = order_data['price']; fee_buy_db = order_data.get('fee', 0); qty_fill = float(res['executedQty']); fee_sell = sum([float(f['commission']) * float(f['price']) for f in res['fills']])
            profit = (price_grid * qty_fill) - (harga_beli * qty_fill) - fee_buy_db - fee_sell
            DAILY_STATS["profit_usdt"] += profit; sb_delete(order_data['id']); usdt, _ = get_all_balance()
            if NOTIF_SENT["sell"]!= price_grid: notif_penting(f"🔴 <b>SELL TP</b>\nProfit: {profit:.4f} USDT"); NOTIF_SENT["sell"] = price_grid; NOTIF_SENT["buy"] = None
            if RE_ENTRY_MODE and is_top_grid:
                if time.time() - LAST_REENTRY_TIME < REENTRY_COOLDOWN: return
                price_reentry = price_grid; qty_reentry = hitung_qty_aman(price_reentry); butuh = hitung_butuh_modal(price_reentry, qty_reentry); usdt_cek, _ = get_all_balance()
                if usdt_cek >= butuh: LAST_REENTRY_TIME = time.time(); place_order_real("BUY", price_reentry, qty_reentry)
                else: PERLU_REENTRY = True

async def main():
    notif_penting("🤖 <b>Bot V11.63.43 BINANCE RAJA START</b>")
    global START_TIME, LAST_RECOVERY, PERLU_REENTRY
    START_TIME = time.time(); cek_tabel_supabase(); get_binance_rules(SYMBOL)
    while ATR_MANAGER["jarak"] is None: update_atr_manager(); await asyncio.sleep(2)
    sync_3_sumber(); LAST_RECOVERY = time.time(); kirim_keyboard()
    cek_sell_instan_darurat(get_price()); await asyncio.sleep(3)
    while True:
        try:
            sync_3_sumber()
            bersihin_sampah()
            cek_command_telegram()
            if time.time() - LAST_RECOVERY > RECOVERY_INTERVAL: recovery_sync(); LAST_RECOVERY = time.time()
            if PERLU_REENTRY:
                price_sekarang = get_price()
                if price_sekarang!= 0:
                    _, btc_cek = get_all_balance()
                    if btc_cek < 0.00001:
                        qty_market = hitung_qty_aman(price_sekarang); usdt_cek, _ = get_all_balance(); butuh = hitung_butuh_modal(price_sekarang, qty_market)
                        if usdt_cek >= butuh: notif_penting(f"🔄 <b>EKSEKUSI RE-ENTRY</b>"); place_order_real("BUY", price_sekarang, qty_market); PERLU_REENTRY = False; continue
            price = get_price()
            if price > 0:
                signal_buy, grid_buy = cek_signal_buy(price); signal_sell, grid_sell, order_data, is_top = cek_signal_sell(price)
                if signal_sell: place_order_real("SELL", grid_sell, format_qty(float(order_data['qty'])), order_data, is_top)
                if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]: NOTIF_FLAGS["error"]=True; notif_penting(f"❌ <b>CRITICAL ERROR</b>\n<code>{repr(e)}</code>")

if __name__ == "__main__":
    asyncio.run(main())
