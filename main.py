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
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}
NOTIF_SENT = {"buy": None, "sell": None}

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

def save_to_json(data):
    try:
        pending = []
        if os.path.exists(JSON_FILE):
            with open(JSON_FILE, 'r') as f: pending = json.load(f)
        pending.append(data)
        with open(JSON_FILE, 'w') as f: json.dump(pending, f)
    except Exception as e: send_telegram(f"❌ GAGAL SAVE JSON: {repr(e)}")

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
            send_telegram("✅ Koneksi Supabase OK. Tabel `orders` ada")
            pending = load_and_clear_json()
            if len(pending) > 0:
                send_telegram(f"🔄 Menemukan {len(pending)} order di JSON. Mencoba insert ke DB...")
                for p in pending: sb_insert(p)
        else:
            send_telegram(f"⚠️ Supabase Error: {r.status_code}. Retry 5 detik")
            time.sleep(5)
    except Exception as e:
        send_telegram(f"⚠️ Gagal konek Supabase: {repr(e)}. Retry 5 detik")
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
            send_telegram(f"❌ BINANCE ERROR {r.status_code}\n<code>{r.text}</code>")
            return {}
        return r.json()
    except Exception as e:
        send_telegram(f"❌ SIGNED_REQUEST CRASH\n<code>{repr(e)}</code>")
        return {}

def get_price():
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
        r.raise_for_status()
        return float(r.json()['price'])
    except:
        time.sleep(10)
        return 0

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
    except Exception as e:
        send_telegram(f"❌ GAGAL AMBIL RULE BINANCE: {repr(e)}")

def get_binance_fee():
    try:
        data = signed_request("GET", "/api/v3/account")
        if 'takerCommission' in data:
            fee = float(data['takerCommission']) / 10000
            return fee
        return 0.001
    except:
        return 0.001

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    min_qty = BINANCE_RULES['min_qty']
    qty_floored = int(qty / step) * step
    if qty_floored < min_qty: qty_floored = min_qty
    return f"{qty_floored:.8f}"

# ===== V11.63.30 FIX: PAKSA TEMBUS 5 USDT UNTUK BUY =====
def hitung_qty_aman(harga):
    min_notional = BINANCE_RULES['min_notional']
    min_qty = BINANCE_RULES['min_qty']
    step = BINANCE_RULES['step_size']

    qty_dari_qty = min_qty
    qty_dari_usdt = min_notional / harga
    qty = max(qty_dari_qty, qty_dari_usdt)

    # FIX: CEK LAGI SETELAH FORMAT, KALAU KURANG DARI 5 USDT TAMBAH 1 STEP
    qty_formatted = float(format_qty(qty))
    nilai = harga * qty_formatted

    if nilai < min_notional:
        qty_formatted += step

    return format_qty(qty_formatted)

def hitung_butuh_modal(price, qty):
    fee = get_binance_fee()
    modal = price * float(qty)
    fee_buy = modal * fee
    fee_sell = modal * fee
    total_fee = fee_buy + fee_sell
    return modal + total_fee + BUFFER_USDT

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
    except: pass

def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    try:
        r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=10)
        r.raise_for_status()
        data = r.json(); tr_list = []
        for i in range(1, len(data)):
            high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
            tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
        return sum(tr_list[-period:]) / period
    except Exception as e:
        send_telegram(f"❌ ERROR GET ATR: {repr(e)}")
        return 0

def update_atr_manager():
    global ATR_MANAGER, DAILY_STATS, NOTIF_SENT
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib:
        DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}
        NOTIF_SENT = {"buy": None, "sell": None}
    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        if atr_baru == 0: return
        jarak_mentah = atr_baru * ATR_MULTIPLIER
        jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK))
        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}
        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nJarak: {jarak:.2f}")

def is_price_exist(price):
    jarak = ATR_MANAGER["jarak"] if ATR_MANAGER["jarak"] else MIN_JARAK
    toleransi = jarak / 2
    data = sb_select(f"price=gte.{price-toleransi}&price=lte.{price+toleransi}&side=eq.BUY&status=eq.OPEN")
    return data[0] if len(data) > 0 else None

def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    update_atr_manager()
    if ATR_MANAGER["jarak"] is None: return False, 0
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
    if ATR_MANAGER["jarak"] is None: return False, 0, None, False
    jarak = ATR_MANAGER["jarak"]
    data_open = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.asc&limit=1")
    if len(data_open) > 0:
        order_data = data_open[0]
        harga_beli = order_data['price']
        if price >= harga_beli + jarak:
            data_tertinggi = sb_select(f"status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
            is_top_grid = False
            if len(data_tertinggi) > 0 and data_tertinggi[0]['id'] == order_data['id']:
                is_top_grid = True
            return True, price, order_data, is_top_grid
    return False, 0, None, False

# ===== V11.63.30 FINAL: PAKSA TEMBUS MIN_NOTIONAL =====
def cek_sell_instan_darurat(price):
    _, btc = get_all_balance()
    if btc < BINANCE_RULES['min_qty']: return

    data_db = sb_select(f"status=eq.OPEN&side=eq.BUY")
    data_json = load_and_clear_json()
    send_telegram(f"🔍 <b>CEK DARURAT</b>\nBTC: {btc:.8f}\nDB: {len(data_db)}\nJSON: {len(data_json)}\nHarga: {price:.2f}")

    # ========== MODE 2: RECOVERY DARURAT ==========
    if len(data_db) == 0 and len(data_json) == 0:
        send_telegram("⚠️ MODE 2: DETEKSI COIN TANPA CATATAN. MENCARI HARGA BELI DI BINANCE...")
        data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 500})
        harga_beli_asli = 0
        qty_asli = 0

        if isinstance(data_binance, list):
            for o in data_binance:
                try:
                    if o.get('side') == 'BUY' and o.get('status') == 'FILLED' and o.get('fills') and len(o['fills']) > 0:
                        harga_beli_asli = float(o['fills'][0]['price'])
                        qty_asli = float(o['executedQty'])
                        break
                except: continue

        if harga_beli_asli > 0: # MODE 2A: KETEMU RIWAYAT BELI
            insert_res = sb_insert({"price":harga_beli_asli, "qty":qty_asli, "side":"BUY", "status":"OPEN", "binance_order_id": "RECOVERY_"+str(int(time.time())), "fee": 0})
            if len(insert_res) > 0:
                send_telegram(f"✅ MODE 2A BERHASIL: Order dicatat ke DB di {harga_beli_asli:.2f}. Lanjut trading normal")
                return
            else:
                qty = format_qty(btc)
                nilai_jual = price * float(qty)
                send_telegram(f"📊 MODE 2A CEK: Qty={qty} | Nilai={nilai_jual:.2f} | Min=5.00")
                if nilai_jual < BINANCE_RULES['min_notional']:
                    send_telegram(f"🛑 MODE 2A DITAHAN: Nilai {nilai_jual:.2f} < Min 5 USDT")
                elif price > harga_beli_asli: # WAJIB PROFIT
                    res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
                    if 'orderId' in res:
                        profit = (price - harga_beli_asli) * qty_asli
                        send_telegram(f"🚨 MODE 2A SELL PROFIT\nJual {qty} @ {price:.2f}\nProfit: {profit:.4f} USDT")

        else: # MODE 2B: GAK KETEMU RIWAYAT = DEPOSIT
            send_telegram("⚠️ MODE 2B: GAK NEMU RIWAYAT BELI. ANGGAP INI HASIL DEPOSIT/TRANSFER")
            qty = format_qty(btc)
            nilai_jual = price * float(qty)
            send_telegram(f"📊 MODE 2B CEK: Qty={qty} | Nilai={nilai_jual:.2f} | Min=5.00")

            # FIX: KALAU KURANG, TAMBAH 1 STEP BIAR TEMBUS 5 USDT
            if nilai_jual < BINANCE_RULES['min_notional']:
                step = BINANCE_RULES['step_size']
                qty_baru = float(qty) + step
                qty_baru_str = format_qty(qty_baru)
                nilai_baru = price * float(qty_baru_str)

                if nilai_baru >= BINANCE_RULES['min_notional'] and qty_baru <= btc + 0.00000001:
                    qty = qty_baru_str
                    nilai_jual = nilai_baru
                    send_telegram(f"⚠️ MODE 2B NAIK 1 STEP: Qty={qty} | Nilai={nilai_jual:.2f}")
                else:
                    harga_butuh = BINANCE_RULES['min_notional'] / float(qty)
                    send_telegram(f"🛑 MODE 2B DITAHAN: Nilai {nilai_jual:.2f} < Min 5 USDT\nNunggu harga >= {harga_butuh:.0f}")
                    return

            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' in res:
                usdt_dapat = float(res['cummulativeQuoteQty'])
                send_telegram(f"✅ MODE 2B SELL BEP\nJual {qty} @ {price:.2f}\nDapat USDT: {usdt_dapat:.2f}")
            else:
                send_telegram(f"❌ MODE 2B GAGAL: {res}")

    # ========== MODE 1: PROFIT DARURAT ==========
    elif len(data_db) > 0:
        try: harga_buy_pertama = min([d['price'] for d in data_db])
        except: harga_buy_pertama = 0

        if harga_buy_pertama > 0 and price > harga_buy_pertama:
            qty = format_qty(btc)
            nilai_jual = price * float(qty)
            send_telegram(f"📊 MODE 1 CEK: Qty={qty} | Nilai={nilai_jual:.2f} | Min=5.00")
            if nilai_jual < BINANCE_RULES['min_notional']:
                send_telegram(f"🛑 MODE 1 DITAHAN: Nilai {nilai_jual:.2f} < Min 5 USDT")
            else:
                send_telegram(f"🚨 MODE 1: HARGA DIATAS BUY PERTAMA {harga_buy_pertama:.2f}. EKSEKUSI SELL DARURAT PROFIT")
                res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
                if 'orderId' in res:
                    for d in data_db: sb_delete(d['id'])
                    profit = (price - harga_buy_pertama) * btc
                    send_telegram(f"✅ MODE 1 SUKSES\nJual {qty} @ {price:.2f}\nProfit Kotor: {profit:.4f} USDT")
        else:
            send_telegram(f"🛑 MODE 1 DITAHAN: Harga {price:.2f} < Buy Pertama {harga_buy_pertama:.2f}")

# ===== RECOVERY 3 SUMBER: BINANCE + DB + JSON =====
def recovery_sync():
    global PERLU_REENTRY
    send_telegram("🔄 <b>FORCE RECOVERY 3 SUMBER</b>\nBinance + DB + JSON")
    count = 0
    data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 500})
    data_db = sb_select(f"status=eq.OPEN")
    data_json = load_and_clear_json()

    if not isinstance(data_binance, list): data_binance = []
    if len(data_json) > 0:
        send_telegram(f"🔄 Menemukan {len(data_json)} order di JSON. Memasukkan ke DB...")
        for p in data_json: sb_insert(p)
        data_db = sb_select(f"status=eq.OPEN")

    db_dict = {str(d['binance_order_id']): d for d in data_db if 'binance_order_id' in d}

    for o in data_binance:
        order_id = str(o['orderId'])
        ada_di_db = order_id in db_dict
        if o['side'] == 'BUY' and o['status'] == 'FILLED' and o.get('fills') and len(o['fills']) > 0:
            harga = float(o['fills'][0]['price'])
            qty = float(o['executedQty'])
            fee_buy = sum([float(f['commission']) * float(f['price']) for f in o['fills']])
            if not ada_di_db:
                insert_res = sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy})
                if len(insert_res) > 0:
                    count += 1
                    send_telegram(f"✅ <b>RECOVERY BUY NYANGKUT</b>\nOrderID: {order_id}\nHarga: {harga:.2f}\nQty: {qty}")
        if o['side'] == 'SELL' and o['status'] == 'FILLED':
            if ada_di_db:
                sb_delete(db_dict[order_id]['id'])
                count += 1
                send_telegram(f"✅ <b>RECOVERY SELL</b>\nOrderID: {order_id}\nHapus dari DB")

    for order_id, d in db_dict.items():
        ketemu = False
        for o in data_binance:
            if str(o['orderId']) == order_id:
                ketemu = True
                break
        if not ketemu:
            sb_delete(d['id'])
            count += 1
            send_telegram(f"⚠️ <b>RECOVERY HAPUS ORDER GAGAL</b>\nOrderID: {order_id}\nGak ada di Binance")

    if count == 0: send_telegram("✅ Recovery Selesai: DB sudah sinkron 100%")
    else: send_telegram(f"✅ Recovery Selesai: {count} data diperbaiki")

    cek_sell_instan_darurat(get_price())

def cek_order_binance_sudah_ada(price_target):
    data = signed_request("GET", "/api/v3/openOrders", {"symbol":SYMBOL})
    if not isinstance(data, list): return False
    for o in data:
        if abs(float(o['price']) - price_target) < 0.01:
            return True
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
                if not NOTIF_FLAGS["saldo_kurang"]: send_telegram(f"💰 <b>SALDO KURANG</b>\nUSDT: {usdt:.2f} | Butuh: {butuh:.2f}")
                NOTIF_FLAGS["saldo_kurang"]=True; return
            if NOTIF_FLAGS["saldo_kurang"] == True:
                send_telegram(f"✅ <b>SALDO SUDAH CUKUP</b>\nUSDT: {usdt:.2f}\nLanjut Trading...")
                NOTIF_FLAGS["saldo_kurang"]=False
            if PERLU_REENTRY:
                send_telegram(f"✅ <b>RE-ENTRY BERHASIL</b>\nGrid sudah ketutup di {price_grid:.2f}")
                PERLU_REENTRY = False

            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res:
                send_telegram(f"❌ BUY GAGAL KE BINANCE: {res}")
                return
            order_id = res['orderId']
            qty_fill = float(res['executedQty'])
            fee_buy = sum([float(f['commission']) * float(f['price']) for f in res['fills']])

            cek_double = sb_select(f"binance_order_id=eq.{order_id}")
            if len(cek_double) > 0:
                send_telegram(f"⚠️ <b>ANTI DOUBLE</b>\nOrderID {order_id} sudah ada di DB. Skip insert")
                return

            insert_success = False
            for i in range(3):
                insert_res = sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy})
                if len(insert_res) > 0:
                    insert_success = True
                    break
                time.sleep(2)

            if not insert_success:
                data_backup = {"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy}
                save_to_json(data_backup)
                send_telegram(f"⚠️ <b>DB ERROR 3X</b>\nOrderID: {order_id}\nData disimpan ke JSON. Aman. Tidak dijual darurat")
                if NOTIF_SENT["buy"]!= price_grid:
                    send_telegram(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nFee: {fee_buy:.4f} USDT\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}")
                    NOTIF_SENT["buy"] = price_grid
                    NOTIF_SENT["sell"] = None
                return

            if NOTIF_SENT["buy"]!= price_grid:
                send_telegram(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nFee: {fee_buy:.4f} USDT\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}")
                NOTIF_SENT["buy"] = price_grid
                NOTIF_SENT["sell"] = None
        except Exception as e:
            send_telegram(f"❌ ERROR BUY: {repr(e)}")
        finally:
            BUYING_LOCK.discard(price_grid)

    if side=="SELL":
        _, btc = get_all_balance()
        if btc < BINANCE_RULES['min_qty']:
            send_telegram(f"❌ GAGAL SELL: BTC {btc} < {BINANCE_RULES['min_qty']}")
            return

        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res and order_data and 'fills' in res:
            harga_beli = order_data['price']
            fee_buy_db = order_data.get('fee', 0)
            qty_fill = float(res['executedQty'])
            fee_sell = sum([float(f['commission']) * float(f['price']) for f in res['fills']])
            profit = (price_grid * qty_fill) - (harga_beli * qty_fill) - fee_buy_db - fee_sell

            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            usdt, _ = get_all_balance()
            if NOTIF_SENT["sell"]!= price_grid:
                send_telegram(f"🔴 <b>SELL TP</b>\nHarga: {price_grid:.2f}\nProfit: {profit:.4f} USDT\nFee Buy: {fee_buy_db:.4f}\nFee Sell: {fee_sell:.4f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}")
                NOTIF_SENT["sell"] = price_grid
                NOTIF_SENT["buy"] = None
            if NOTIF_FLAGS["saldo_kurang"] == True:
                send_telegram(f"✅ <b>DAPAT SALDO DARI TP</b>\nSaldo USDT: {usdt:.2f}")

            if RE_ENTRY_MODE and is_top_grid:
                if time.time() - LAST_REENTRY_TIME < REENTRY_COOLDOWN:
                    send_telegram(f"⏳ <b>RE-ENTRY DITAHAN</b>\nTunggu {REENTRY_COOLDOWN} detik dulu")
                    return
                usdt_cek, _ = get_all_balance()
                butuh = hitung_butuh_modal(price_grid, qty)
                if usdt_cek >= butuh:
                    LAST_REENTRY_TIME = time.time()
                    send_telegram(f"♻️ <b>RE-ENTRY LANGSUNG</b>\nSaldo cukup. Buy {price_grid:.2f}")
                    place_order_real("BUY", price_grid, qty)
                else:
                    PERLU_REENTRY = True
                    send_telegram(f"⚠️ <b>RE-ENTRY DITUNDA</b>\nSaldo kurang. Akan buy di harga market begitu saldo cukup")

async def main():
    send_telegram("1. BOT MULAI")
    global START_TIME, LAST_RECOVERY, PERLU_REENTRY
    START_TIME = time.time()
    send_telegram("2. CEK TABEL")
    cek_tabel_supabase()
    send_telegram("3. AMBIL RULE")
    get_binance_rules(SYMBOL)
    try:
        server_time = requests.get(f"{BASE_URL}/api/v3/time", timeout=5).json()['serverTime']
        selisih = abs(server_time - int(time.time()*1000))
        if selisih > 1000:
            send_telegram(f"⚠️ <b>WAKTU VPS MELENCENG {selisih}ms</b>\nOrder bisa gagal. Restart VPS!")
    except: pass
    send_telegram("4. NUNGGU ATR")
    retry = 0
    while ATR_MANAGER["jarak"] is None:
        update_atr_manager()
        retry += 1
        if retry > 10:
            ATR_MANAGER["jarak"] = 500
            send_telegram("⚠️ ATR Gagal 10x. Pakai jarak default 500")
        await asyncio.sleep(2)
    send_telegram("5. RECOVERY")
    recovery_sync()
    LAST_RECOVERY = time.time()
    harga_sekarang = get_price()
    saldo_usdt, saldo_btc = get_all_balance()
    send_telegram(f"6. BOT SIAP\n🤖 <b>Bot V11.63.30 FINAL</b>\n<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}\n<b>Saldo USDT:</b> {saldo_usdt:.2f}\n<b>Saldo BTC:</b> {saldo_btc:.8f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    send_telegram("7. MASUK LOOP UTAMA")
    while True:
        try:
            if time.time() - LAST_RECOVERY > RECOVERY_INTERVAL:
                recovery_sync()
                LAST_RECOVERY = time.time()

            if PERLU_REENTRY:
                price_sekarang = get_price()
                if price_sekarang!= 0:
                    _, btc_cek = get_all_balance()
                    if btc_cek < 0.00001:
                        qty_market = hitung_qty_aman(price_sekarang)
                        usdt_cek, _ = get_all_balance()
                        butuh = hitung_butuh_modal(price_sekarang, qty_market)
                        if usdt_cek >= butuh:
                            send_telegram(f"🔄 <b>EKSEKUSI RE-ENTRY</b>\nSaldo cukup. Buy di harga market {price_sekarang:.2f}")
                            place_order_real("BUY", price_sekarang, qty_market)
                            PERLU_REENTRY = False
                            continue

            price = get_price()
            if price == 0:
                await asyncio.sleep(10)
                continue
            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, order_data, is_top = cek_signal_sell(price)
            if signal_sell: place_order_real("SELL", grid_sell, hitung_qty_aman(order_data['price']), order_data, is_top)
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            gc.collect(); NOTIF_FLAGS["error"]=False; await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]:
                send_telegram(f"❌ <b>CRITICAL</b>\n<code>{repr(e)}</code>")
                NOTIF_FLAGS["error"]=True
            await asyncio.sleep(15)

if __name__ == "__main__":
    asyncio.run(main())
