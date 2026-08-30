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
PAPER_MODE = True # True = Paper, False = Real
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

STATE = {"paper_mode": True, "paper_usdt": 10000.0, "paper_btc": 0.0}

def log_only(msg):
    try:
        with open("bot_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except:
        pass
    if NOTIF_MODE == "NORMAL":
        send_telegram(msg)

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
    keyboard = {"keyboard": [[{"text": "STATUS"}], [{"text": "PAKSA PAPER"}, {"text": "PAKSA RILL"}]], "resize_keyboard": True, "one_time_keyboard": False}
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": "✅ <b>Panel Kontrol Aktif</b>", "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}, timeout=5)
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
    status_txt = "PAUSE" if NOTIF_FLAGS["saldo_kurang"] else "JALAN"
    emoji_status = "🔴" if status_txt=="PAUSE" else "🟢"
    msg = f"""<b> SAFANA 09_04_2025</b>

{emoji_status} {status_txt} | Mode: {mode} | Uang: {mode_uang}
Harga: ${price:.2f} | Grid: ${jarak:.2f}
Saldo: ${usdt:.2f} | Butuh: ${hitung_butuh_modal(price, hitung_qty_aman(price)):.2f}
Posisi: {len(data_open)} Grid | BTC: {btc:.8f}"""
    if len(data_open) > 0:
        msg += "\n\nDETAIL POSISI\n<code>--------------------\nNo | BUY | TP\n--------------------\n"
        no = 1
        for d in data_open:
            tp = d['price'] + jarak
            msg += f"{no:2}.| ${d['price']:8.2f}| ${tp:8.2f}\n"
            no+=1
        msg += "</code>"
    notif_penting(msg)

def cek_command_telegram():
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates", timeout=3).json()
        if 'result' not in r or len(r['result']) == 0: return
        last_update = r['result'][-1]
        text = last_update['message'].get('text', '').lower()
        chat_id = str(last_update['message']['chat']['id'])
        if chat_id!= TELE_CHAT_ID: return
        global NOTIF_MODE
        if text == "ganti mode":
            NOTIF_MODE = "NORMAL" if NOTIF_MODE == "SILENT" else "SILENT"
            txt = "🔊 MODE NORMAL AKTIF" if NOTIF_MODE == "NORMAL" else "🔇 MODE SILENT AKTIF"
            log_only(txt)
        elif text == "status": kirim_status_lengkap()
        elif text == "paksa paper":
            STATE["paper_mode"] = True
            save_state()
            notif_penting(f"🧪 DIPAKSA KE MODE: PAPER")
        elif text == "paksa rill":
            STATE["paper_mode"] = False
            save_state()
            notif_penting(f"💰 DIPAKSA KE MODE: RILL")
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
    try:
        r = requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, timeout=5)
        return r.status_code == 204
    except: return False

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

def sb_select(filters=""):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5)
        if r.status_code!= 200: return []
        data = r.json()
        return data if isinstance(data, list) else []
    except: return []

def sb_insert(data):
    try:
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
    fee = get_binance_fee(); modal = price * float(qty); fee_buy = modal * fee; fee_sell = modal * fee; total_fee = fee_buy + fee_sell; buffer = modal * (fee * 5)
    return modal + total_fee + buffer

def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    try:
        r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=10)
        r.raise_for_status(); data = r.json(); tr_list = []
        for i in range(1, len(data)): high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4]); tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
        return sum(tr_list[-period:]) / period
    except Exception as e: notif_penting(f"❌ ERROR GET ATR: {repr(e)}"); return 0

def update_atr_manager():
    global ATR_MANAGER, DAILY_STATS, NOTIF_SENT
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}; NOTIF_SENT = {"buy": None, "sell": None}
    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        if atr_baru == 0: return
        jarak_mentah = atr_baru * ATR_MULTIPLIER; jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK))
        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}
        notif_penting(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nJarak: {jarak:.2f}")

def is_price_exist(price):
    jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else MIN_JARAK
    data = sb_select(f"side=eq.BUY&status=eq.OPEN")
    for d in data:
        if abs(d['price'] - price) < jarak: # KALAU KURANG DARI JARAK = DITOLAK
            return d
    return None

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc")
    
    # FIRST BUY
    if not FIRST_BUY_DONE and len(data_open) == 0 and time.time() - START_TIME > WAIT_FIRST_BUY:
        FIRST_BUY_DONE = True
        return True, price
    
    # BUY SELANJUTNYA: HARUS TURUN >= JARAK DARI GRID TERDEKAT DI ATASNYA
    if len(data_open) > 0:
        # cari grid terdekat yg di atas harga sekarang
        grid_diatas = [d for d in data_open if d['price'] > price]
        if len(grid_diatas) > 0:
            harga_grid_terdekat = min([d['price'] for d in grid_diatas])
            if price <= harga_grid_terdekat - jarak:
                if not is_price_exist(price): # CEK APAKAH SUDAH ADA DI JARAK ITU
                    return True, price
    
    return False, 0

def cek_signal_sell(price):
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0, None, False
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc") # AMBIL SEMUA URUT DARI BAWAH

    if len(data_open) > 0:
        for order_data in data_open: # CEK 1 PER 1 DARI YG PALING BAWAH
            harga_beli = order_data['price']
            if price >= harga_beli + jarak:
                data_tertinggi = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
                is_top_grid = False
                if len(data_tertinggi) > 0 and data_tertinggi[0]['id'] == order_data['id']:
                    is_top_grid = True
                return True, price, order_data, is_top_grid # TP GRID PERTAMA YG KESENTUH

    return False, 0, None, False


def cek_sell_instan_darurat(price):
    _, btc = get_all_balance()
    if btc < BINANCE_RULES['min_qty']: return
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY"); data_json = load_and_clear_json()
    log_only(f"🔍 CEK DARURAT\nBTC: {btc:.8f}\nDB: {len(data_db)}\nJSON: {len(data_json)}\nHarga: {price:.2f}")
    harga_buy_pertama = 0; harga_buy_terakhir = 0; tp_terakhir = 0
    if len(data_db) > 0:
        try: harga_buy_pertama = min([d['price'] for d in data_db]); harga_buy_terakhir = max([d['price'] for d in data_db]); tp_terakhir = data_db[0].get('tp', harga_buy_terakhir + ATR_MANAGER['jarak'])
        except: pass
    grid = ATR_MANAGER['jarak']
    if len(data_db) > 0 and price > tp_terakhir + (grid * 0.5):
        qty = btc; step = BINANCE_RULES['step_size']; qty_str = ""; nilai_jual = 0
        while True:
            qty_str = format_qty(qty); nilai_jual = price * float(qty_str)
            if nilai_jual >= BINANCE_RULES['min_notional'] + 0.01: break
            qty += step
            if qty > btc + 0.001: log_only("GAGAL MODE 3: Qty mentok safety"); break
        notif_penting(f"🚨 <b>MODE 3: HARGA KELEWAT TP</b>\nBUY: {harga_buy_pertama:.2f}\nTP: {tp_terakhir:.2f}\nSekarang: {price:.2f}\nJUAL LGSG: {qty_str} @ {price:.2f}\nDapat: {nilai_jual:.2f} USDT")
        try:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_str})
            if 'orderId' in res:
                for d in data_db: sb_delete(d['id'])
                profit = (price - harga_buy_pertama) * float(qty_str); notif_penting(f"✅ MODE 3 SUKSES TP PAKSA\nProfit Kotor: {profit:.4f} USDT")
        except Exception as e: log_only(f"GAGAL MODE 3 SELL: {repr(e)}")
        return
    if len(data_db) == 0 and len(data_json) == 0:
        log_only(f"⚠️ MODE 2: DETEKSI COIN TANPA CATATAN. MENCARI HARGA BELI DI BINANCE...")
        data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 500}); harga_beli_asli = 0; qty_asli = 0
        if isinstance(data_binance, list):
            for o in data_binance:
                try:
                    if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills') and len(o['fills']) > 0: harga_beli_asli = float(o['fills'][0]['price']); qty_asli = float(o['executedQty']); break
                except: continue
        if harga_beli_asli > 0:
           recovery_id = int(time.time()) # ID ANGKA BIAR LOLOS DB
           insert_res = sb_insert({"price":harga_beli_asli, "qty":qty_asli, "side":"BUY", "status":"OPEN", "binance_order_id": recovery_id, "fee": 0})
           if len(insert_res) > 0: log_only(f"✅ MODE 2A BERHASIL: Order dicatat ke DB di {harga_beli_asli:.2f}. Lanjut trading normal"); return
        qty_str = format_qty(btc); nilai_jual = price * float(qty_str); butuh_min = hitung_butuh_modal(price, qty_str)
        log_only(f"MODE 2A CEK: Qty={qty_str} | Nilai={nilai_jual:.2f} | Butuh Min={butuh_min:.2f}")
        if nilai_jual < butuh_min: log_only(f"🛑 MODE 2A DITAHAN: Nilai {nilai_jual:.2f} < Butuh {butuh_min:.2f}")
        elif price > harga_beli_asli:
            try:
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_str})
                if 'orderId' in res: profit = (price - harga_beli_asli) * float(qty_str); notif_penting(f"🚨 MODE 2A SELL PROFIT\nJual {qty_str} @ {price:.2f}\nProfit: {profit:.4f} USDT")
            except Exception as e: log_only(f"GAGAL MODE 2A SELL: {repr(e)}")
    elif len(data_db) > 0:
        if harga_buy_pertama > 0 and price > harga_buy_pertama:
            qty_str = format_qty(float(data_db[0]['qty'])); nilai_jual = price * float(qty_str); butuh_min = hitung_butuh_modal(price, qty_str)
            log_only(f"MODE 1 CEK: Qty={qty_str} | Nilai={nilai_jual:.2f} | Butuh Min={butuh_min:.2f}")
            if nilai_jual < butuh_min: log_only(f"🛑 MODE 1 DITAHAN: Nilai {nilai_jual:.2f} < Butuh {butuh_min:.2f}")
            else:
                log_only(f"🚨 MODE 1: HARGA DIATAS BUY PERTAMA {harga_buy_pertama:.2f}. EKSEKUSI SELL DARURAT PROFIT")
                try:
                    res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_str})
                    if 'orderId' in res:
                        for d in data_db: sb_delete(d['id'])
                        profit = (price - harga_buy_pertama) * float(qty_str); notif_penting(f"✅ MODE 1 SUKSES\nJual {qty_str} @ {price:.2f}\nProfit Kotor: {profit:.4f} USDT")
                except Exception as e: log_only(f"GAGAL MODE 1 SELL: {repr(e)}")
        else: log_only(f"🛑 MODE 1 DITAHAN: Harga {price:.2f} < Buy Pertama {harga_buy_pertama:.2f}")

def sync_3_sumber():
    global LAST_SYNC_CICILAN
    sekarang = time.time()
    if sekarang - LAST_SYNC_CICILAN < 3: return
    LAST_SYNC_CICILAN = sekarang
    log_only("SYNC CICILAN: Ambil 10 order terbaru")
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY")
    data_json = load_and_clear_json()
    _, btc_total = get_all_balance()
    if len(data_json) > 0: log_only(f"Ada {len(data_json)} order di JSON. Pindahin ke DB...")
    for p in data_json: sb_insert(p)
    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY")
    db_dict = {str(d['binance_order_id']): d for d in data_db if 'binance_order_id' in d}
    count_tambah = 0
    count_hapus = 0
    if btc_total > 0.00001 and len(data_db) == 0:
        log_only(f"DARURAT: Ada BTC {btc_total:.8f} tapi DB kosong. Scan 50 order terakhir...")
        data_scan = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 50})
        if isinstance(data_scan, list):
            for o in reversed(data_scan):
                if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills'):
                    harga = float(o['fills'][0]['price'])
                    qty = float(o['executedQty'])
                    order_id = str(o['orderId'])
                    fee_buy = sum([float(f['commission']) * float(f['price']) for f in o['fills']])
                    sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(o['time']/1000)})
                    notif_penting(f"RECOVERY DARURAT: Ketemu BUY di {harga:.2f}")
                    count_tambah += 1
                    break
    data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 10})
    if isinstance(data_binance, list):
        for o in data_binance:
            order_id = str(o['orderId'])
            if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills') and len(o['fills']) > 0 and order_id not in db_dict:
                harga = float(o['fills'][0]['price'])
                qty = float(o['executedQty'])
                fee_buy = sum([float(f['commission']) * float(f['price']) for f in o['fills']])
                sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy, "time": int(o['time']/1000)})
                count_tambah += 1
                log_only(f"TAMBAH: Ketemu BUY baru di {harga:.2f}")
            elif order_id in db_dict:
                try:
                    cek_detail = signed_request("GET", "/api/v3/order", {"symbol":SYMBOL, "orderId": order_id})
                    time.sleep(0.1)
                    if cek_detail.get('status') == 'FILLED' and cek_detail.get('side') == 'SELL':
                        sb_delete(db_dict[order_id]['id'])
                        count_hapus += 1
                        log_only(f"HAPUS: Order {db_dict[order_id]['price']:.2f} sudah TP di Binance")
                    elif cek_detail.get('status')!= 'FILLED':
                        sb_delete(db_dict[order_id]['id'])
                        count_hapus += 1
                        log_only(f"HAPUS: Order {db_dict[order_id]['price']:.2f} sudah CANCEL di Binance")
                except Exception as e: log_only(f"SKIP CEK TP: Order {order_id} error {repr(e)}")
    if count_tambah > 0: notif_penting(f"RECOVERY: +{count_tambah} order baru")
    if count_hapus > 0: notif_penting(f"CLEAN: -{count_hapus} order TP/Cancel")
    log_only("Sync Selesai")

def cek_order_binance_sudah_ada(price_target):
    data = signed_request("GET", "/api/v3/openOrders", {"symbol":SYMBOL})
    if not isinstance(data, list): return False
    for o in data:
        if abs(float(o['price']) - price_target) < 0.01: return True
    return False

def place_order_real(side, price_grid, qty, order_data=None, is_top_grid=False):
    global NOTIF_FLAGS, NOTIF_SENT, BUYING_LOCK, PERLU_REENTRY, LAST_REENTRY_TIME
    if side=="BUY":
        if price_grid in BUYING_LOCK: return
        if is_price_exist(price_grid) or cek_order_binance_sudah_ada(price_grid): return
        BUYING_LOCK.add(price_grid)
        try:
            usdt, btc = get_all_balance()
            butuh = hitung_butuh_modal(price_grid, qty)
            if usdt < butuh:
                if not NOTIF_FLAGS["saldo_kurang"]: notif_penting(f"💰 <b>SALDO KURANG</b>\nUSDT: {usdt:.2f} | Butuh: {butuh:.2f}")
                NOTIF_FLAGS["saldo_kurang"]=True
                return
            if NOTIF_FLAGS["saldo_kurang"] == True: notif_penting(f"✅ <b>SALDO SUDAH CUKUP</b>\nUSDT: {usdt:.2f}\nLanjut Trading..."); NOTIF_FLAGS["saldo_kurang"]=False
            if PERLU_REENTRY: notif_penting(f"✅ <b>RE-ENTRY BERHASIL</b>\nGrid sudah ketutup di {price_grid:.2f}"); PERLU_REENTRY = False

            nilai_beli = price_grid * float(qty)
            if nilai_beli < BINANCE_RULES['min_notional']: log_only(f"❌ GAGAL BUY: Nilai {nilai_beli:.2f} < Min 5 USDT. Qty: {qty}"); return

            if STATE["paper_mode"]:
               STATE["paper_usdt"] -= nilai_beli
               STATE["paper_btc"] += float(qty)
               save_state()
               paper_id = int(time.time()) # ID ANGKA BIAR LOLOS DB
               res = {"orderId": paper_id, "executedQty": qty, "fills": [{"commission": "0", "commissionAsset": "USDT", "price": str(price_grid)}]}
               notif_penting(f"🧪 [PAPER] BUY TERISI\nHarga: {price_grid:.2f}\nQty: {qty}\nID: {paper_id}\nSisa USDT: {STATE['paper_usdt']:.2f}")
            else:
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})

            if 'orderId' not in res: notif_penting(f"❌ BUY GAGAL KE BINANCE: {res}"); return
            order_id = res['orderId']
            qty_fill = float(res['executedQty'])
            fee_buy = sum([float(f['commission']) * float(f['price']) for f in res['fills']])
            cek_double = sb_select(f"binance_order_id=eq.{order_id}")
            if len(cek_double) > 0: return
            insert_success = False
            for i in range(3):
                insert_res = sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy})
                if len(insert_res) > 0:
                    insert_success = True
                    break
                time.sleep(2)
            if not insert_success: save_to_json({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy}); notif_penting(f"⚠️ <b>DB ERROR 3X</b>\nOrderID: {order_id}\nData disimpan ke JSON")
            if NOTIF_SENT["buy"]!= price_grid: notif_penting(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nFee: {fee_buy:.4f} USDT\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}"); NOTIF_SENT["buy"] = price_grid; NOTIF_SENT["sell"] = None
        except Exception as e: notif_penting(f"❌ ERROR BUY: {repr(e)}")
        finally: BUYING_LOCK.discard(price_grid)

    if side=="SELL":
        qty_db = format_qty(float(order_data['qty']))
        nilai_jual = price_grid * float(qty_db)
        if nilai_jual < BINANCE_RULES['min_notional']: notif_penting(f"❌ GAGAL SELL: Nilai {nilai_jual:.2f} < Min 5 USDT. Qty: {qty_db}"); return
        _, btc = get_all_balance()
        if float(btc) < float(qty_db): notif_penting(f"❌ GAGAL SELL: BTC {btc:.8f} < Qty {qty_db}"); return

        if STATE["paper_mode"]:
            STATE["paper_usdt"] += nilai_jual
            STATE["paper_btc"] -= float(qty_db)
            save_state()
            harga_beli = order_data['price']
            fee_buy_db = order_data.get('fee', 0)
            qty_fill = float(qty_db)
            fee_sell = 0
            profit = (price_grid * qty_fill) - (harga_beli * qty_fill) - fee_buy_db - fee_sell
            DAILY_STATS["profit_usdt"] += profit
            DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            usdt, _ = get_all_balance()
            notif_penting(f"🧪 [PAPER] 🔴 SELL TP\nHarga: {price_grid:.2f}\nProfit: {profit:.4f} USDT\nSaldo USDT: {usdt:.2f}")
            NOTIF_SENT["sell"] = price_grid
            NOTIF_SENT["buy"] = None
        else:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty_db})
            if 'orderId' not in res: log_only(f"❌ SELL GAGAL: {res}"); return
            if order_data and 'fills' in res:
                harga_beli = order_data['price']
                fee_buy_db = order_data.get('fee', 0)
                qty_fill = float(res['executedQty'])
                fee_sell = sum([float(f['commission']) * float(f['price']) for f in res['fills']])
                profit = (price_grid * qty_fill) - (harga_beli * qty_fill) - fee_buy_db - fee_sell
                DAILY_STATS["profit_usdt"] += profit
                DAILY_STATS["trade_count"] += 1
                sb_delete(order_data['id'])
                usdt, _ = get_all_balance()
                if NOTIF_SENT["sell"]!= price_grid: notif_penting(f"🔴 <b>SELL TP</b>\nHarga: {price_grid:.2f}\nProfit: {profit:.4f} USDT\nFee Buy: {fee_buy_db:.4f}\nFee Sell: {fee_sell:.4f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}"); NOTIF_SENT["sell"] = price_grid; NOTIF_SENT["buy"] = None
                if NOTIF_FLAGS["saldo_kurang"] == True: notif_penting(f"✅ <b>DAPAT SALDO DARI TP</b>\nSaldo USDT: {usdt:.2f}")
                if RE_ENTRY_MODE and is_top_grid:
                    if time.time() - LAST_REENTRY_TIME < REENTRY_COOLDOWN: notif_penting(f"⏳ <b>RE-ENTRY DITAHAN</b>\nTunggu {REENTRY_COOLDOWN} detik dulu"); return
                    price_reentry = price_grid
                    qty_reentry = hitung_qty_aman(price_reentry)
                    butuh = hitung_butuh_modal(price_reentry, qty_reentry)
                    usdt_cek, _ = get_all_balance()
                    if usdt_cek >= butuh: LAST_REENTRY_TIME = time.time(); notif_penting(f"♻️ <b>RE-ENTRY LANGSUNG</b>\nHarga: {price_reentry:.2f}\nQty: {qty_reentry}\nButuh: {butuh:.2f}"); place_order_real("BUY", price_reentry, qty_reentry)
                    else: PERLU_REENTRY = True; notif_penting(f"⚠️ <b>RE-ENTRY DITUNDA</b>\nSaldo: {usdt_cek:.2f} | Butuh: {butuh:.2f}")

async def main():
    load_state() # LOAD DULU
    
    # AUTO DETECT MODE: KALAU ADA SALDO RILL PAKSA RILL
    try:
        usdt_rill, btc_rill = get_all_balance()
        if usdt_rill > 1 or btc_rill > 0.00001:
            if STATE["paper_mode"]:
                STATE["paper_mode"] = False
                save_state()
                notif_penting("⚠️ <b>AUTO PAKSA RILL</b>\nDeteksi ada saldo Binance. Mode diganti ke RILL")
    except: pass
    
    notif_penting("1. BOT MULAI")
    global START_TIME, LAST_RECOVERY, PERLU_REENTRY
    START_TIME = time.time()
    notif_penting("2. CEK TABEL")
    cek_tabel_supabase()
    notif_penting("3. AMBIL RULE")
    get_binance_rules(SYMBOL)
    try:
        server_time = requests.get(f"{BASE_URL}/api/v3/time", timeout=5).json()['serverTime']; selisih = abs(server_time - int(time.time()*1000))
        if selisih > 1000: notif_penting(f"⚠️ <b>WAKTU VPS MELENCENG {selisih}ms</b>\nOrder bisa gagal. Restart VPS!")
    except: pass
    notif_penting("4. NUNGGU ATR")
    retry = 0
    while ATR_MANAGER["jarak"] is None: update_atr_manager(); retry += 1
    if retry > 10: ATR_MANAGER["jarak"] = 500; notif_penting("⚠️ ATR Gagal 10x. Pakai jarak default 500")
    await asyncio.sleep(1)
    await asyncio.sleep(2)
    notif_penting("5. RECOVERY")
    recovery_sync()
    LAST_RECOVERY = time.time()
    harga_sekarang = get_price(); saldo_usdt, saldo_btc = get_all_balance()
    mode_uang = "🧪 PAPER" if STATE["paper_mode"] else "💰 REAL"
    notif_penting(f"6. BOT SIAP\n🤖 <b>Bot V12.04 FINAL</b>\n<b>Mode:</b> {mode_uang}\n<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}\n<b>Saldo USDT:</b> {saldo_usdt:.2f}\n<b>Saldo BTC:</b> {saldo_btc:.8f}")
    kirim_keyboard()
    cek_sell_instan_darurat(harga_sekarang)
    await asyncio.sleep(3)
    notif_penting("7. MASUK LOOP UTAMA")
    while True:
        try:
            sync_3_sumber(); bersihin_sampah(); cek_command_telegram()
            if time.time() - LAST_RECOVERY > RECOVERY_INTERVAL: recovery_sync(); LAST_RECOVERY = time.time()
            if PERLU_REENTRY:
                price_sekarang = get_price()
                if price_sekarang!= 0: _, btc_cek = get_all_balance()
                if btc_cek < 0.00001: qty_market = hitung_qty_aman(price_sekarang); usdt_cek, _ = get_all_balance(); butuh = hitung_butuh_modal(price_sekarang, qty_market)
                if usdt_cek >= butuh: notif_penting(f"🔄 <b>EKSEKUSI RE-ENTRY</b>\nSaldo cukup. Buy di harga market {price_sekarang:.2f}"); place_order_real("BUY", price_sekarang, qty_market); PERLU_REENTRY = False; continue
            price = get_price()
            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, order_data, is_top = cek_signal_sell(price)
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            if signal_sell: place_order_real("SELL", grid_sell, hitung_qty_aman(grid_sell), order_data, is_top)
            if NOTIF_FLAGS["error"] == True: notif_penting(f"✅ <b>BOT SUDAH NORMAL KEMBALI</b>\n<b>Error terakhir:</b> <code>{NOTIF_FLAGS['critical_msg']}</code>\n<b>Waktu Pulih:</b> {datetime.now(WIB).strftime('%H:%M:%S')}"); NOTIF_FLAGS["error"]=False; NOTIF_FLAGS["critical_msg"]=""
            gc.collect()
            await asyncio.sleep(LOOP_SEC)
        except Exception as e: 
            error_sekarang = repr(e)
            if not NOTIF_FLAGS["error"]: 
                NOTIF_FLAGS["error"]=True
                NOTIF_FLAGS["critical_msg"]=error_sekarang
                notif_penting(f"❌ <b>CRITICAL ERROR</b>\n<code>{error_sekarang}</code>")


if __name__ == "__main__":
    asyncio.run(main())
