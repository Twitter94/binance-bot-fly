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
TARGET_USDT_PER_BUY = 5
RECOVERY_INTERVAL = 3600
RE_ENTRY_MODE = True

ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0
MIN_JARAK = 250
MAX_JARAK = 1000

WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()
LAST_RECOVERY = 0

BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
ATR_MANAGER = {"jarak": None, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}
NOTIF_SENT = {"buy": None, "sell": None}

SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}

def cek_tabel_supabase():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?limit=1", headers=SB_HEADERS, timeout=5)
        if r.status_code == 200:
            send_telegram("✅ Koneksi Supabase OK. Tabel `orders` ada")
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
    except: return {}

def get_price():
    try: # FIX BUG 1: HAPUS RECURSIVE
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5)
        r.raise_for_status()
        return float(r.json()['price'])
    except:
        time.sleep(10)
        return 0 # balikin 0 biar di skip

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
    global ATR_MANAGER, DAILY_STATS
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}
    if ATR_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = get_atr(SYMBOL)
        if atr_baru == 0: return
        jarak_mentah = atr_baru * ATR_MULTIPLIER
        jarak = max(MIN_JARAK, min(jarak_mentah, MAX_JARAK))
        ATR_MANAGER = {"jarak": jarak, "atr": atr_baru, "date": hari_ini_wib}
        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nJarak: {jarak:.2f}")

def is_price_exist(price):
    data = sb_select(f"price=eq.{price}&side=eq.BUY&status=eq.OPEN")
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
        if o['side'] == 'BUY' and o['status'] == 'FILLED':
            if 'fills' not in o or len(o['fills']) == 0:
                continue
            ada_di_db = sb_select(f"binance_order_id=eq.{o['orderId']}")
            if len(ada_di_db) == 0:
                harga = float(o['fills'][0]['price'])
                qty = float(o['executedQty'])
                insert_res = sb_insert({"price":harga, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": o['orderId']})
                if len(insert_res) > 0:
                    count += 1
                    send_telegram(f"✅ <b>RECOVERY</b>\nOrderID: {o['orderId']}\nHarga: {harga:.2f}")
    if count == 0:
        send_telegram("✅ Recovery Selesai: Tidak ada order baru")

def cek_order_binance_sudah_ada(price_target, toleransi=10):
    data = signed_request("GET", "/api/v3/myTrades", {"symbol":SYMBOL, "limit": 500})
    if not isinstance(data, list): return False
    for trade in data:
        if trade['isBuyer'] == True:
            harga_trade = float(trade['price'])
            if abs(harga_trade - price_target) <= toleransi:
                return True
    return False

def place_order_real(side, price_grid, qty, order_data=None, is_top_grid=False):
    global NOTIF_FLAGS, NOTIF_SENT
    usdt, btc = get_all_balance()

    if side=="BUY":
        if is_price_exist(price_grid) or cek_order_binance_sudah_ada(price_grid): return
        butuh = hitung_butuh_modal(price_grid, qty)
        if usdt < butuh:
            if not NOTIF_FLAGS["saldo_kurang"]: send_telegram(f"💰 <b>SALDO KURANG</b>\nUSDT: {usdt:.2f} | Butuh: {butuh:.2f}")
            NOTIF_FLAGS["saldo_kurang"]=True; return
        if NOTIF_FLAGS["saldo_kurang"] == True:
            send_telegram(f"✅ <b>SALDO SUDAH CUKUP</b>\nUSDT: {usdt:.2f}\nLanjut Trading...")
            NOTIF_FLAGS["saldo_kurang"]=False

        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
        if 'orderId' not in res:
            send_telegram(f"❌ BUY GAGAL KE BINANCE: {res}")
            return
        order_id = res['orderId']

        # ====== SELF HEALING: RETRY INSERT 3X ======
        insert_success = False
        for i in range(3):
            insert_res = sb_insert({"price":price_grid, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id})
            if len(insert_res) > 0:
                insert_success = True
                break
            time.sleep(2)

        if not insert_success:
            send_telegram(f"⚠️ <b>DB ERROR 3X</b>\nOrderID: {order_id}\nOTOMATIS JUAL KEMBALI BIAR AMAN")
            sell_res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' in sell_res:
                send_telegram(f"✅ <b>DARURAT SELL BERHASIL</b>\nDana aman. Rugi fee 0.1%")
            else:
                send_telegram(f"❌ <b>KRITIS</b> GAGAL JUAL DARURAT. CEK BINANCE SEKARANG!")
            return

        if NOTIF_SENT["buy"]!= price_grid:
            send_telegram(f"🟢 <b>BUY TERISI</b>\nHarga: {price_grid:.2f}\nQty: {qty}\nButuh: {butuh:.2f}\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}")
            NOTIF_SENT["buy"] = price_grid
            NOTIF_SENT["sell"] = None

    if side=="SELL":
        if float(btc) < float(qty): return
        res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res and order_data:
            harga_beli = order_data['price']
            profit = ((price_grid * 0.999) - (harga_beli * 1.001)) * float(qty)
            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            sb_delete(order_data['id'])
            if NOTIF_SENT["sell"]!= price_grid:
                send_telegram(f"🔴 <b>SELL TP</b>\nHarga: {price_grid:.2f}\nProfit: {profit:.2f} USDT\nSaldo USDT: {usdt:.2f}\nJarak: {ATR_MANAGER['jarak']:.2f}")
                NOTIF_SENT["sell"] = price_grid
                NOTIF_SENT["buy"] = None

            if NOTIF_FLAGS["saldo_kurang"] == True:
                send_telegram(f"✅ <b>DAPAT SALDO DARI TP</b>\nSaldo USDT: {usdt:.2f}")

            if RE_ENTRY_MODE and is_top_grid:
                send_telegram(f"♻️ <b>RE-ENTRY AKTIF</b>\nLangsung Buy lagi di {price_grid:.2f}\nSaldo USDT: {usdt:.2f}")
                place_order_real("BUY", price_grid, qty)

async def main():
    send_telegram("1. BOT MULAI")
    global START_TIME, LAST_RECOVERY
    START_TIME = time.time()
    send_telegram("2. CEK TABEL")
    cek_tabel_supabase()
    send_telegram("3. AMBIL RULE")
    get_binance_rules(SYMBOL)

    # FIX BUG 3: CEK WAKTU BINANCE
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
    send_telegram(f"6. BOT SIAP\n🤖 <b>Bot V11.63.4 FINAL</b>\n<b>Harga:</b> {harga_sekarang}\n<b>Jarak ATR:</b> {ATR_MANAGER['jarak']:.2f}\n<b>Saldo USDT:</b> {saldo_usdt:.2f}\n<b>Saldo BTC:</b> {saldo_btc:.8f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    send_telegram("7. MASUK LOOP UTAMA")
    while True:
        try:
            if time.time() - LAST_RECOVERY > RECOVERY_INTERVAL:
                recovery_sync()
                LAST_RECOVERY = time.time()

            price = get_price()
            if price == 0: # FIX BUG 1: SKIP KALAU HARGA 0
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
