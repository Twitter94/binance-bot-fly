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

NOTIF_MODE = "SILENT"

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
TABEL = "orders"
TABEL_STATE = "bot_state"
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
LAST_SYNC_CICILAN = 0
BUYING_LOCK = set()
SELL_LOCK = set()
SELL_LOCK_TIME= {}
PERLU_REENTRY = False
LAST_REENTRY_TIME = 0
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": None, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang_rill": False, "saldo_kurang_paper": False, "critical_msg": ""}
NOTIF_SENT = {"buy": None, "sell": None}
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

STATE = {"paper_mode": True, "paper_usdt": 100.0, "paper_btc": 0.0}

def log_only(msg):
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except:
        pass
    if NOTIF_MODE == "NORMAL":
        send_telegram(msg)

def log_error(e, ctx=""):
    import traceback
    err = traceback.format_exc()
    msg = f"[{ctx}] {repr(e)}\n{err}"
    log_only(msg)

def notif_penting(msg):
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except:
        pass
    send_telegram(msg)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except:
        pass

def kirim_keyboard():
    # FIX: CUMA ADA MODE DAN STATUS. RILL/PAPER HARUS KETIK MANUAL
    keyboard = {"keyboard": [[{"text": "MODE"}, {"text": "STATUS"}]], "resize_keyboard": True, "one_time_keyboard": False}
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": "✅ <b>Panel Kontrol Aktif</b>\n\nMODE = Ganti Silent/Normal\nSTATUS = Lihat Posisi\n\nKetik: RILL atau PAPER untuk ganti mode uang", "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}, timeout=5)
    except:
        pass

def load_state():
    global STATE
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5).json()
        if len(r) > 0: STATE = r[0]
        else: requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}", headers=SB_HEADERS, json={"id":1, "paper_mode":True, "paper_usdt":10000.0, "paper_btc":0.0})
    except: pass

def save_state():
    try: requests.patch(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, json=STATE)
    except: pass

def kirim_status_lengkap():
    usdt, btc = get_all_balance()
    price = get_price()
    jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else 0
    mode = "SILENT" if NOTIF_MODE == "SILENT" else "NORMAL"
    mode_uang = "🧪 PAPER" if STATE["paper_mode"] else "💰 REAL"
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc")

    flag_key = "saldo_kurang_paper" if STATE["paper_mode"] else "saldo_kurang_rill"
    status_txt = "PAUSE" if NOTIF_FLAGS[flag_key] else "JALAN"
    emoji_status = "🔴" if status_txt=="PAUSE" else "🟢"

    msg = f"""<b> SAFANA GRID MURNI FIX</b>

{emoji_status} {status_txt} | {mode} | {mode_uang}
Harga: ${price:.2f} | Grid: ${jarak:.2f}
Saldo: ${usdt:.2f} | Butuh: ${hitung_butuh_modal(price, hitung_qty_aman(price)):.2f}
Posisi: {len(data_open)} Grid | BTC: {btc:.8f}"""
    if len(data_open) > 0:
        msg += f"\n\nDETAIL POSISI {mode_uang}\n<code>--------------------\nNo | BUY | TP\n--------------------\n"
        no = 1
        for d in data_open:
            harga_buy = float(d['price'])
            tp = harga_buy + jarak
            msg += f"{no:2}.| ${harga_buy:8.2f}| ${tp:8.2f}\n"
            no+=1
        msg += "</code>"
    notif_penting(msg)

def cek_command_telegram():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates", timeout=3).json()
        if 'result' not in r or len(r['result']) == 0: return
        last_update = r['result'][-1]
        text = last_update['message'].get('text', '').upper()
        chat_id = str(last_update['message']['chat']['id'])
        if chat_id!= TELE_CHAT_ID: return
        global NOTIF_MODE

        if text == "STATUS": kirim_status_lengkap()
        elif text == "MODE":
            NOTIF_MODE = "NORMAL" if NOTIF_MODE == "SILENT" else "SILENT"
            txt = "🔊 MODE: NORMAL" if NOTIF_MODE == "NORMAL" else "🔇 MODE: SILENT"
            notif_penting(f"{txt} AKTIF")
        elif text == "RILL":
            STATE["paper_mode"] = False
            NOTIF_FLAGS["saldo_kurang_rill"] = False
            save_state()
            notif_penting("💰 <b>GANTI MODE: RILL</b>\nBot sekarang trading pake uang beneran. Hati-hati!")
        elif text == "PAPER":
            STATE["paper_mode"] = True
            NOTIF_FLAGS["saldo_kurang_paper"] = False
            save_state()
            notif_penting("🧪 <b>GANTI MODE: PAPER</b>\nBot sekarang trading pake saldo simulasi")
        requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={last_update['update_id']+1}")
    except: pass

def recovery_sync():
    log_only("🔄 MENJALANKAN RECOVERY SYNC")
    sync_3_sumber()
    bersihin_sampah()
    log_only("✅ RECOVERY SELESAI")

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

def bersihin_sampah():
    tujuh_hari_lalu = int(time.time()) - (7 * 24 * 3600)
    try:
        r = requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?status=neq.OPEN&time=lt.{tujuh_hari_lalu}", headers=SB_HEADERS, timeout=5)
        if r.status_code == 204: log_only("🧹 BERSIHIN SAMPAH SUKSES")
    except Exception as e: log_only(f"❌ GAGAL BERSIHIN SAMPAH: {repr(e)}")
    gc.collect()

def sb_delete(order_id):
    mode_sekarang = "PAPER" if STATE["paper_mode"] else "RILL"
    for i in range(5):
        try:
            r = requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}&mode=eq.{mode_sekarang}", headers=SB_HEADERS, timeout=10)
            if r.status_code == 204:
                log_only(f"✅ HAPUS DB SUKSES: {order_id}")
                return True
            else:
                log_only(f"⚠️ HAPUS DB GAGAL {i+1}/5: {r.status_code} {r.text}")
        except Exception as e:
            log_only(f"⚠️ HAPUS DB CRASH {i+1}/5: {repr(e)}")
        time.sleep(2)
    return False

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
        if r.status_code!= 200:
            log_only(f"❌ BINANCE ERROR {r.status_code}\n{r.text}")
            return {}
        return r.json()
    except Exception as e:
        log_only(f"❌ SIGNED_REQUEST CRASH\n{repr(e)}")
        return {}

def get_price():
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
        r.raise_for_status()
        return float(r.json()['price'])
    except:
        log_only("❌ GAGAL GET PRICE. RETRY 10 DETIK")
        time.sleep(10)
        return get_price()

def sb_select(filters="", pakai_filter_mode=True):
    try:
        if pakai_filter_mode:
            mode_sekarang = "PAPER" if STATE["paper_mode"] else "RILL"
            if filters!= "":
                filters += f"&mode=eq.{mode_sekarang}"
            else:
                filters = f"mode=eq.{mode_sekarang}"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        if r.status_code!= 200: return []
        data = r.json()
        return data if isinstance(data, list) else []
    except: return []

def sb_insert(data):
    try:
        data['mode'] = "PAPER" if STATE["paper_mode"] else "RILL"
        data['price'] = float(data['price'])
        data['qty'] = float(data['qty'])
        data['fee'] = float(data.get('fee', 0))
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        if r.status_code not in [200, 201]:
            notif_penting(f"❌ SB_INSERT GAGAL {r.status_code}: {r.text}")
            return []
        return r.json()
    except Exception as e:
        notif_penting(f"❌ SB_INSERT CRASH: {repr(e)}")
        return []

def get_all_balance():
    if STATE["paper_mode"]: return STATE["paper_usdt"], STATE["paper_btc"]
    data = signed_request("GET", "/api/v3/account")
    if 'balances' not in data: return 0,0
    usdt = float(next((b['free'] for b in data['balances'] if b['asset']=='USDT'), 0))
    btc = float(next((b['free'] for b in data['balances'] if b['asset']=='BTC'), 0))
    return usdt, btc

def get_binance_rules(symbol):
    global BINANCE_RULES
    try:
        data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}", timeout=5).json()
        for f in data['symbols'][0]['filters']:
            if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
            if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])
    except Exception as e: notif_penting(f"❌ GAGAL AMBIL RULE BINANCE: {repr(e)}")

def get_binance_fee():
    try:
        data = signed_request("GET", "/api/v3/account")
        if 'takerCommission' in data: return float(data['takerCommission']) / 10000
        return 0.001
    except: return 0.001

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    qty = math.floor(qty / step) * step
    return f"{qty:.8f}".rstrip('0').rstrip('.')

def hitung_qty_aman(harga):
    step = BINANCE_RULES['step_size']; min_notional = BINANCE_RULES['min_notional']; qty = 5.0 / harga
    while True:
        qty_str = format_qty(qty); nilai = harga * float(qty_str)
        if nilai >= min_notional + 0.01: break
        qty += step
        if qty > 0.01: break
    return qty_str

def hitung_butuh_modal(price, qty):
    fee = get_binance_fee()
    modal = price * float(qty)
    fee_buy = modal * fee
    fee_sell = modal * fee
    buffer = modal * (fee * 5)
    total_butuh = modal + fee_buy + fee_sell + buffer
    return total_butuh

def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    try:
        r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=10)
        r.raise_for_status(); data = r.json(); tr_list = []
        for i in range(1, len(data)): high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4]); tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
        return sum(tr_list[-period:]) / period
    except Exception as e: notif_penting(f"❌ ERROR GET ATR: {repr(e)}"); return 0

def update_atr_manager():
    global ATR_MANAGER, DAILY_STATS, NOTIF_SENT
    now_wib = datetime.now(WIB)
    hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}; NOTIF_SENT = {"buy": None, "sell": None}
    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        if atr_baru == 0: return
        jarak_mentah = atr_baru * ATR_MULTIPLIER; jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK))
        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}

def is_price_exist(price):
    jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else MIN_JARAK
    data = sb_select(f"side=eq.BUY&status=eq.OPEN", pakai_filter_mode=True)
    for d in data:
        if abs(float(d['price']) - price) < jarak:
            return d
    return None

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc")

    if not FIRST_BUY_DONE and len(data_open) == 0 and time.time() - START_TIME > WAIT_FIRST_BUY:
        FIRST_BUY_DONE = True
        return True, price

    if len(data_open) > 0:
        grid_diatas = [d for d in data_open if float(d['price']) > price]
        if len(grid_diatas) > 0:
            harga_grid_terdekat = min([float(d['price']) for d in grid_diatas])
            if price <= harga_grid_terdekat - jarak:
                if not is_price_exist(price):
                    return True, price
    return False, 0

def cek_signal_sell_murni(price):
    global ATR_MANAGER, SELL_LOCK, SELL_LOCK_TIME
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return None
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc", pakai_filter_mode=True)
    if len(data_open) == 0: return None
    data_tertinggi = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1", pakai_filter_mode=True)
    id_grid_teratas = data_tertinggi[0]['id'] if len(data_tertinggi) > 0 else None

    for order_data in data_open:
        harga_beli = float(order_data['price'])
        tp_harga = harga_beli + jarak
        order_id = order_data['id']
        if order_id in SELL_LOCK:
            if time.time() - SELL_LOCK_TIME.get(order_id, 0) > 60:
                SELL_LOCK.discard(order_id)
            else: continue
        if price >= tp_harga:
            is_top_grid = (order_id == id_grid_teratas)
            log_only(f"🎯 GRID MURNI TP: Buy@{harga_beli:.2f} -> TP@{tp_harga:.2f} | Now@{price:.2f}")
            return order_data, price, is_top_grid
    return None

def sync_3_sumber():
    global LAST_SYNC_CICILAN
    sekarang = time.time()
    if sekarang - LAST_SYNC_CICILAN < 3: return
    LAST_SYNC_CICILAN = sekarang
    mode_txt = "[PAPER]" if STATE["paper_mode"] else "[RILL]"
    log_only(f"{mode_txt} SYNC CICILAN: Ambil 10 order terbaru")
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY", pakai_filter_mode=True)
    data_json = load_and_clear_json()
    _, btc_total = get_all_balance()
    if len(data_json) > 0:
        log_only(f"{mode_txt} Ada {len(data_json)} order di JSON. Pindahin ke DB...")
        for p in data_json: sb_insert(p)
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY", pakai_filter_mode=True)
    db_dict = {str(d['binance_order_id']): d for d in data_db if 'binance_order_id' in d}
    count_tambah = 0; count_hapus = 0
    if not STATE["paper_mode"] and btc_total > 0.00001 and len(data_db) == 0:
        log_only(f"{mode_txt} DARURAT: Ada BTC {btc_total:.8f} tapi DB kosong. Scan 50 order terakhir...")
        data_scan = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 50})
        if isinstance(data_scan, list):
            for o in reversed(data_scan):
                if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills'):
                    harga = float(o['fills'][0]['price']); qty = float(o['executedQty']); order_id = str(o['orderId']); fee_buy = sum([float(f['commission']) for f in o['fills']])
                    sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(o['time']/1000)})
                    notif_penting(f"{mode_txt} RECOVERY DARURAT: Ketemu BUY di {harga:.2f}")
                    count_tambah += 1; break
    if not STATE["paper_mode"]:
        data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 10})
        if isinstance(data_binance, list):
            for o in data_binance:
                order_id = str(o['orderId'])
                if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills') and len(o['fills']) > 0 and order_id not in db_dict:
                    harga = float(o['fills'][0]['price']); qty = float(o['executedQty']); fee_buy = sum([float(f['commission']) for f in o['fills']])
                    sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(o['time']/1000)})
                    count_tambah += 1; log_only(f"{mode_txt} TAMBAH: Ketemu BUY baru di {harga:.2f}")
                elif order_id in db_dict:
                    try:
                        cek_detail = signed_request("GET", "/api/v3/order", {"symbol":SYMBOL, "orderId": order_id}); time.sleep(0.1)
                        if cek_detail.get('status') == 'FILLED' and cek_detail.get('side') == 'SELL':
                            sb_delete(db_dict[order_id]['id']); count_hapus += 1; log_only(f"{mode_txt} HAPUS: Order {db_dict[order_id]['price']:.2f} sudah TP di Binance")
                        elif cek_detail.get('status')!= 'FILLED':
                            sb_delete(db_dict[order_id]['id']); count_hapus += 1; log_only(f"{mode_txt} HAPUS: Order {db_dict[order_id]['price']:.2f} sudah CANCEL di Binance")
                    except Exception as e: log_only(f"{mode_txt} SKIP CEK TP: Order {order_id} error {repr(e)}")
    if count_tambah > 0: notif_penting(f"{mode_txt} RECOVERY: +{count_tambah} order baru")
    if count_hapus > 0: notif_penting(f"{mode_txt} CLEAN: -{count_hapus} order TP/Cancel")
    log_only(f"{mode_txt} Sync Selesai")

def cek_order_binance_sudah_ada(price_target):
    data = signed_request("GET", "/api/v3/openOrders", {"symbol":SYMBOL})
    if not isinstance(data, list): return False
    for o in data:
        if abs(float(o['price']) - price_target) < 0.01: return True # FIX: o bukan d
    return False
    
def place_order_real(side, price_grid, qty, order_data=None, is_top_grid=False):
    global NOTIF_FLAGS, NOTIF_SENT, BUYING_LOCK, SELL_LOCK, SELL_LOCK_TIME, PERLU_REENTRY, LAST_REENTRY_TIME
    mode_txt = "[PAPER]" if STATE["paper_mode"] else "[RILL]"
    flag_key = "saldo_kurang_paper" if STATE["paper_mode"] else "saldo_kurang_rill"

    if side=="BUY":
        if price_grid in BUYING_LOCK: return
        if is_price_exist(price_grid) or cek_order_binance_sudah_ada(price_grid): return
        BUYING_LOCK.add(price_grid)
        try:
            usdt, btc = get_all_balance()
            butuh = hitung_butuh_modal(price_grid, qty)
            if usdt < butuh:
                if not NOTIF_FLAGS[flag_key]: notif_penting(f"💰 <b>SALDO KURANG {mode_txt}</b>\nUSDT: {usdt:.2f} | Butuh: {butuh:.2f}")
                NOTIF_FLAGS[flag_key]=True; return
            if NOTIF_FLAGS[flag_key] == True: notif_penting(f"✅ <b>SALDO SUDAH CUKUP {mode_txt}</b>\nUSDT: {usdt:.2f}\nLanjut Trading..."); NOTIF_FLAGS[flag_key]=False
            if PERLU_REENTRY: notif_penting(f"✅ <b>RE-ENTRY BERHASIL {mode_txt}</b>\nGrid sudah ketutup di {price_grid:.2f}"); PERLU_REENTRY = False

            nilai_beli = price_grid * float(qty)
            if nilai_beli < BINANCE_RULES['min_notional']: log_only(f"❌ GAGAL BUY {mode_txt}: Nilai {nilai_beli:.2f} < Min 5 USDT"); return

            fee_buy = 0; order_id = int(time.time())
            if STATE["paper_mode"]:
                STATE["paper_usdt"] -= nilai_beli; STATE["paper_btc"] += float(qty); save_state()
                fee_buy = nilai_beli * 0.001; order_id = f"PAPER_{order_id}"
            else:
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
                if 'orderId' not in res: raise Exception(f"BINANCE BUY FAIL: {res}")
                order_id = res['orderId']; qty = res['executedQty']; fee_buy = sum([float(f['commission']) for f in res.get('fills',[])])

            sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy})
            usdt, _ = get_all_balance()
            if NOTIF_SENT["buy"]!= price_grid: notif_penting(f"{mode_txt} 🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nSaldo USDT: {usdt:.2f}"); NOTIF_SENT["buy"] = price_grid; NOTIF_SENT["sell"] = None
        except Exception as e: log_error(e, "PLACE_BUY")
        finally: BUYING_LOCK.discard(price_grid)

    if side=="SELL":
        order_id_db = order_data['id']
        if order_id_db in SELL_LOCK:
            if time.time() - SELL_LOCK_TIME.get(order_id_db, 0) > 60:
                log_only(f"⚠️ BUKA PAKSA LOCK: {order_id_db}")
                SELL_LOCK.discard(order_id_db)
                SELL_LOCK_TIME.pop(order_id_db, None)
            else: return

        SELL_LOCK.add(order_id_db)
        SELL_LOCK_TIME[order_id_db] = time.time()

        try:
            if not sb_delete(order_id_db):
                raise Exception("ABORT SELL KARENA GAGAL HAPUS DB")

            _, btc_total = get_all_balance()
            qty_db = float(order_data['qty'])
            qty_str = format_qty(qty_db)
            nilai_jual = price_grid * float(qty_str)
            harga_beli = float(order_data['price'])
            fee_sell = 0

            if STATE["paper_mode"]:
                STATE["paper_btc"] -= float(qty_str)
                STATE["paper_usdt"] += nilai_jual - (nilai_jual * 0.001)
                save_state()
                fee_sell = nilai_jual * 0.001
                order_id_binance = f"PAPER_SELL_{int(time.time())}"
            else:
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_str})
                if 'orderId' not in res:
                    sb_insert(order_data)
                    raise Exception(f"BINANCE SELL FAIL: {res}")
                order_id_binance = res['orderId']
                qty_str = res['executedQty']
                fee_sell = sum([float(f['commission']) for f in res.get('fills',[])])

            profit = nilai_jual - (harga_beli * float(qty_str))
            DAILY_STATS["profit_usdt"] += profit
            DAILY_STATS["trade_count"] += 1

            usdt, _ = get_all_balance()
            if NOTIF_SENT["sell"]!= price_grid:
                notif_penting(f"{mode_txt} 🔴 <b>SELL TERISI</b>\nBuy: {harga_beli:.2f} -> Sell: {price_grid:.2f}\nProfit: {profit:.4f} USDT\nSaldo USDT: {usdt:.2f}")
                NOTIF_SENT["sell"] = price_grid
                NOTIF_SENT["buy"] = None

            if is_top_grid and RE_ENTRY_MODE:
                time.sleep(1)
                usdt_cek, _ = get_all_balance()
                qty_reentry = hitung_qty_aman(price_grid)
                butuh = hitung_butuh_modal(price_grid, qty_reentry)
                if usdt_cek >= butuh:
                    place_order_real("BUY", price_grid, qty_reentry)

        except Exception as e:
            log_error(e, "PLACE_SELL")
        finally:
            SELL_LOCK.discard(order_id_db)
            SELL_LOCK_TIME.pop(order_id_db, None)

async def main():
    global LAST_RECOVERY, DAILY_STATS
    load_state()
    kirim_keyboard()
    get_binance_rules(SYMBOL)
    cek_tabel_supabase()
    LAST_RECOVERY = time.time()

    notif_penting("🤖 <b>BOT GRID MURNI START</b>\nMode: PAPER" if STATE["paper_mode"] else "🤖 <b>BOT GRID MURNI START</b>\nMode: REAL")
    log_only("Bot Grid Murni Dimulai")
    await asyncio.sleep(2)
    kirim_status_lengkap()

    while True:
        try:
            sync_3_sumber()
            bersihin_sampah()
            cek_command_telegram()

            if time.time() - LAST_RECOVERY > RECOVERY_INTERVAL:
                recovery_sync()
                LAST_RECOVERY = time.time()

            now = datetime.now(WIB)
            if DAILY_STATS["date"] is None: DAILY_STATS["date"] = now.strftime("%Y-%m-%d")
            if now.date() > datetime.strptime(DAILY_STATS["date"], "%Y-%m-%d").date():
                DAILY_STATS = {"profit_usdt": 0.0, "trade_count": 0, "date": now.strftime("%Y-%m-%d")}
                log_only("🔄 STATS HARIAN DI RESET")

            price = get_price()
            if price is None:
                await asyncio.sleep(LOOP_SEC)
                continue

            jarak_txt = f"{ATR_MANAGER['jarak']:.2f}" if ATR_MANAGER['jarak'] else "N/A"
            log_only(f"🔍 CEK: Harga={price:.2f} | Jarak={jarak_txt}")

            signal_buy, grid_buy = cek_signal_buy(price)
            if signal_buy:
                place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
                await asyncio.sleep(0.5)

            hasil_sell = cek_signal_sell_murni(price)
            if hasil_sell:
                order_data, harga_sell, is_top = hasil_sell
                place_order_real("SELL", harga_sell, hitung_qty_aman(harga_sell), order_data, is_top)
                await asyncio.sleep(0.5)

            gc.collect()
            await asyncio.sleep(LOOP_SEC)

        except Exception as e:
            log_error(e, "MAIN_LOOP")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
