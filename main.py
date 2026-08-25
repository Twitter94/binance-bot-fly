import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
import sys
import json # TAMBAH INI
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
TARGET_USDT_PER_BUY = 5 # INI UDAH GA KEPAKE TAPI BIARIN
RECOVERY_INTERVAL = 3600
RE_ENTRY_MODE = True
REENTRY_COOLDOWN = 60 # FIX 2: COOLDOWN 60 DETIK SAJA

ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_JARAK = 250
MAX_JARAK = 1000
JSON_FILE = "pending_orders.json" # FIX 1: FILE BACKUP JSON

WAIT_FIRST_BUY = 60
FIRST_BUY_DONE = False
START_TIME = time.time()
LAST_RECOVERY = 0
BUYING_LOCK = set()
PERLU_REENTRY = False
LAST_REENTRY_TIME = 0 # FIX 2: CATAT WAKTU REENTRY TERAKHIR

BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": None, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}
NOTIF_SENT = {"buy": None, "sell": None}

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

# FIX 1: FUNGSI JSON BACKUP
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
        os.remove(JSON_FILE) # HAPUS SETELAH DIBACA
        return pending
    except: return []

def cek_tabel_supabase():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?limit=1", headers=SB_HEADERS, timeout=5)
        if r.status_code == 200:
            send_telegram("✅ Koneksi Supabase OK. Tabel `orders` ada")
            # FIX 1: PAS NYALA LANGSUNG CEK JSON
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

# ===== TAMBAH INI: AMBIL FEE ASLI BINANCE =====
def get_binance_fee():
    try:
        data = signed_request("GET", "/api/v3/account")
        if 'takerCommission' in data:
            fee = float(data['takerCommission']) / 10000 # 100 = 0.1%
            return fee
        return 0.001
    except:
        return 0.001
# ==============================================

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    min_qty = BINANCE_RULES['min_qty']
    qty_floored = int(qty / step) * step
    if qty_floored < min_qty: qty_floored = min_qty
    return f"{qty_floored:.8f}"

# ====== EDIT 1: GANTI FUNGSI INI ======
def hitung_qty_aman(harga):
    min_notional = BINANCE_RULES['min_notional'] # 5
    min_qty = BINANCE_RULES['min_qty'] # 0.00001

    # Poin 1: Qty minimal dari Binance
    qty_dari_qty = min_qty

    # Poin 2: Qty minimal biar tembus 5 USDT
    qty_dari_usdt = min_notional / harga

    # Ambil yg PALING GEDE biar 2 syarat kepenuhi
    qty = max(qty_dari_qty, qty_dari_usdt)

    qty = format_qty(qty)
    return qty
# ======================================

# ====== EDIT 2: GANTI FUNGSI INI PAKE FEE ASLI ======
def hitung_butuh_modal(price, qty):
    fee = get_binance_fee() # Ambil fee asli dari akun kamu
    modal = price * float(qty)
    fee_buy = modal * fee
    fee_sell = modal * fee
    total_fee = fee_buy + fee_sell
    return modal + total_fee + BUFFER_USDT
# ====================================================

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

def cek_sell_instan_darurat(price):
    usdt, btc = get_all_balance()
    if btc > 0.00001:
        data_nyangkut = sb_select(f"status=eq.OPEN&side=eq.BUY")
        if len(data_nyangkut) == 0:
            qty = format_qty(btc)
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' in res:
                send_telegram(f"⚠️ <b>SELL DARURAT</b>\n{qty} BTC @ {price:.2f}\nSaldo USDT: {usdt:.2f}")

def recovery_sync():
    send_telegram("🔄 Mulai Recovery Sync...")
    data_binance = signed_request("GET", "/api/v3/allOrders", {"symbol":SYMBOL, "limit": 100})
    if not isinstance(data_binance, list):
        send_telegram("⚠️ Recovery Gagal: Data Binance kosong")
        return
    count = 0
    for o in data_binance:
        order_id = str(o['orderId'])
        ada_di_db = sb_select(f"binance_order_id=eq.{order_id}")
        if o['side'] == 'BUY' and o['status'] == 'FILLED':
            if 'fills' not in o or len(o['fills']) == 0: continue
            if len(ada_di_db) == 0:
                harga = float(o['fills'][0]['price'])
                qty = float(o['executedQty'])
                fee_buy = sum([float(f['commission']) * float(f['price']) for f in o['fills']]) # TAMBAH INI: AMBIL FEE BUY
                insert_res = sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy}) # TAMBAH FEE
                if len(insert_res) > 0:
                    count += 1
                    send_telegram(f"✅ <b>RECOVERY BUY</b>\nOrderID: {order_id}\nHarga: {harga:.2f}\nFee: {fee_buy:.4f}")
        if o['side'] == 'SELL' and o['status'] == 'FILLED':
            if len(ada_di_db) > 0:
                sb_delete(ada_di_db[0]['id'])
                count += 1
                send_telegram(f"✅ <b>RECOVERY SELL</b>\nOrderID: {order_id}\nHapus dari DB")
    if count == 0:
        send_telegram("✅ Recovery Selesai: Tidak ada order baru")

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

            # TAMBAH INI: AMBIL FEE BUY DARI BINANCE
            qty_fill = float(res['executedQty'])
            fee_buy = sum([float(f['commission']) * float(f['price']) for f in res['fills']])

            cek_double = sb_select(f"binance_order_id=eq.{order_id}")
            if len(cek_double) > 0:
                send_telegram(f"⚠️ <b>ANTI DOUBLE</b>\nOrderID {order_id} sudah ada di DB. Skip insert")
                return

            insert_success = False
            for i in range(3):
                # TAMBAH FEE KE DB
                insert_res = sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy})
                if len(insert_res) > 0:
                    insert_success = True
                    break
                time.sleep(2)

            # FIX 1: JANGAN DARURAT SELL. SIMPAN KE JSON
            if not insert_success:
                data_backup = {"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee_buy} # TAMBAH FEE
                save_to_json(data_backup)
                send_telegram(f"⚠️ <b>DB ERROR 3X</b>\nOrderID: {order_id}\nData disimpan ke JSON. Aman. Tidak dijual darurat")
                if NOTIF_SENT["buy"]!= price_grid:
                    send_telegram(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nFee: {fee_buy:.4f} USDT\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}") # TAMBAH FEE DI NOTIF
                    NOTIF_SENT["buy"] = price_grid
                    NOTIF_SENT["sell"] = None
                return

            if NOTIF_SENT["buy"]!= price_grid:
                send_telegram(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nFee: {fee_buy:.4f} USDT\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}") # TAMBAH FEE DI NOTIF
                NOTIF_SENT["buy"] = price_grid
                NOTIF_SENT["sell"] = None
        except Exception as e: # <-- INI SATU2NYA YG DITAMBAH
            send_telegram(f"❌ ERROR BUY: {repr(e)}")
        finally:
            BUYING_LOCK.discard(price_grid)

    if side=="SELL":
        usdt, btc = get_all_balance()
        if float(btc) < float(qty): return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        # FIX 3: AMBIL FEE DARI BINANCE
        if 'orderId' in res and order_data and 'fills' in res:
            harga_beli = order_data['price']
            fee_buy_db = order_data.get('fee', 0) # TAMBAH INI: AMBIL FEE BUY DARI DB
            qty_fill = float(res['executedQty'])
            fee_sell = sum([float(f['commission']) * float(f['price']) for f in res['fills']]) # GANTI NAMA JADI FEE_SELL
            profit = (price_grid * qty_fill) - (harga_beli * qty_fill) - fee_buy_db - fee_sell # KURANGI FEE BUY + SELL

            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            if NOTIF_SENT["sell"]!= price_grid:
                send_telegram(f"🔴 <b>SELL TP</b>\nHarga: {price_grid:.2f}\nProfit: {profit:.4f} USDT\nFee Buy: {fee_buy_db:.4f}\nFee Sell: {fee_sell:.4f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}") # UPDATE NOTIF
                NOTIF_SENT["sell"] = price_grid
                NOTIF_SENT["buy"] = None
            if NOTIF_FLAGS["saldo_kurang"] == True:
                send_telegram(f"✅ <b>DAPAT SALDO DARI TP</b>\nSaldo USDT: {usdt:.2f}")

            # FIX 2: TAMBAH COOLDOWN
            if RE_ENTRY_MODE and is_top_grid:
                if time.time() - LAST_REENTRY_TIME < REENTRY_COOLDOWN:
                    send_telegram(f"⏳ <b>RE-ENTRY DITAHAN</b>\nTunggu {REENTRY_COOLDOWN} detik dulu")
                    return
                usdt_cek, _ = get_all_balance()
                butuh = hitung_butuh_modal(price_grid, qty)
                if usdt_cek >= butuh:
                    LAST_REENTRY_TIME = time.time() # CATAT WAKTU
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
    send_telegram(f"6. BOT SIAP\n🤖 <b>Bot V11.63.22 FINAL</b>\n<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}\n<b>Saldo USDT:</b> {saldo_usdt:.2f}\n<b>Saldo BTC:</b> {saldo_btc:.8f}")
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
                        # ====== EDIT 3: HAPUS ARGUMEN KEDUA ======
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
            # ====== EDIT 3: HAPUS ARGUMEN KEDUA ======
            if signal_buy: place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))
            gc.collect(); NOTIF_FLAGS["error"]=False; await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]:
                send_telegram(f"❌ <b>CRITICAL</b>\n<code>{repr(e)}</code>")
                NOTIF_FLAGS["error"]=True
            await asyncio.sleep(15)

if __name__ == "__main__":
    asyncio.run(main())
