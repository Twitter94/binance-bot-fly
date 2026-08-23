import asyncio
import os
import time
import hmac
import hashlib
import gc
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
import httpx

# ========== CONFIG ==========
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_SECRET = os.getenv("API_SECRET")
SUPABASE_URL = os.getenv("SUPA_URL")
SUPABASE_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

SYMBOL = "BTCUSDT"
LOOP_SEC = 3
BUFFER_USDT = 0.5
TABEL = "orders"
TOLERANSI_PERSEN = 0.0005 # 0.05%

# ========== CONFIG ATR + GRID ==========
ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0

GRID_MIN = 250
GRID_MAX = 1000

WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()

# ========== GLOBAL ==========
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
GRID_MANAGER = {"grid_step": GRID_MIN, "date": None, "atr": 0}
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}

LAST_BALANCE_CHECK = 0
CACHED_BALANCE = {"usdt": 0.0, "btc": 0.0}

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

client = httpx.AsyncClient(timeout=10)

# ========== FUNGSI SUPABASE ==========
async def sb_insert(data):
    try: await client.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data)
    except: pass

async def sb_select(filters=""):
    try:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS)
        return r.json()
    except: return []

async def sb_delete(order_id):
    try: await client.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS)
    except: pass

async def auto_create_table(): pass

# ========== FUNGSI BINANCE ==========
async def signed_request(method, endpoint, params=None):
    if params is None: params = {}
    params['timestamp'] = int(time.time() * 1000)
    query_string = urlencode(params)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    r = await client.request(method, url, headers=headers)
    return r.json()

async def get_price():
    r = await client.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}")
    return float(r.json()['price'])

async def get_all_balance():
    global LAST_BALANCE_CHECK, CACHED_BALANCE
    if time.time() - LAST_BALANCE_CHECK < 10:
        return CACHED_BALANCE["usdt"], CACHED_BALANCE["btc"]

    data = await signed_request("GET", "/api/v3/account")
    usdt = float(next((b['free'] for b in data['balances'] if b['asset']=='USDT'), 0))
    btc = float(next((b['free'] for b in data['balances'] if b['asset']=='BTC'), 0))
    CACHED_BALANCE = {"usdt": usdt, "btc": btc}
    LAST_BALANCE_CHECK = time.time()
    return usdt, btc

async def get_binance_rules(symbol):
    r = await client.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={symbol}")
    data = r.json()
    for f in data['symbols'][0]['filters']:
        if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
        if f['filterType']=='LOT_SIZE':
            BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])

def format_qty(qty):
    step = BINANCE_RULES['step_size']
    return float(int(qty / step) * step)

def hitung_qty_aman(harga, target_usdt=5):
    qty = target_usdt / harga
    qty = format_qty(qty)
    if qty * harga < BINANCE_RULES['min_notional']: qty = format_qty(BINANCE_RULES['min_notional'] / harga)
    return qty

def hitung_butuh_modal(price, qty, fee=0.001):
    return (price * qty * (1 + fee)) + BUFFER_USDT

# ========== FUNGSI TELEGRAM ==========
async def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
        await client.post(url, data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"})
    except: pass

# ========== FUNGSI ATR & GRID ==========
async def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    r = await client.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}")
    data = r.json(); tr_list = []
    for i in range(1, len(data)):
        high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
    return sum(tr_list[-period:]) / period

async def update_grid_manager():
    global GRID_MANAGER, DAILY_STATS
    now_wib = datetime.now(WIB); hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    if DAILY_STATS["date"]!= hari_ini_wib: DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}
    if GRID_MANAGER["date"]!= hari_ini_wib and now_wib.hour >= ATR_UPDATE_HOUR:
        atr_baru = await get_atr(SYMBOL)
        grid_step = max(GRID_MIN, min(atr_baru * ATR_MULTIPLIER, GRID_MAX))
        GRID_MANAGER = {"grid_step": grid_step, "atr": atr_baru, "date": hari_ini_wib}
        await send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nGrid Baru: {grid_step:.2f}")

def generate_grid_levels(harga_tengah, grid_step):
    return sorted(list(set([harga_tengah + (i * grid_step) for i in range(-3, 4) if harga_tengah + (i * grid_step) > 0])))

# ========== FUNGSI LOGIKA FIX ==========
async def is_price_exist(price):
    data = await sb_select(f"price=eq.{price}&side=eq.BUY&status=eq.OPEN")
    return data[0] if len(data) > 0 else None

async def cek_signal_buy(price):
    global FIRST_BUY_DONE, START_TIME
    if not FIRST_BUY_DONE and time.time() - START_TIME > WAIT_FIRST_BUY:
        FIRST_BUY_DONE = True; return True, price

    grids = generate_grid_levels(price, GRID_MANAGER["grid_step"])
    grids_bawah = [g for g in grids if g < price]
    grids_bawah.sort(reverse=True)

    for grid_bawah in grids_bawah:
        toleransi = grid_bawah * TOLERANSI_PERSEN
        if price <= grid_bawah + toleransi and not await is_price_exist(grid_bawah):
            return True, grid_bawah
    return False, 0

async def cek_signal_sell(price):
    grids = generate_grid_levels(price, GRID_MANAGER["grid_step"])
    sell_grid = grids[4]
    order_data = await is_price_exist(sell_grid)
    if order_data: return True, sell_grid, order_data
    return False, 0, None

# ========== SELL DARURAT V2 - CEK DULU ==========
async def cek_sell_instan_darurat(price):
    usdt, btc = await get_all_balance()
    if btc > 0.00001:
        # 1. CEK DULU ADA DATA DI SUPABASE GAK
        data_nyangkut = await sb_select(f"status=eq.OPEN&side=eq.BUY")

        if len(data_nyangkut) == 0:
            # 2. KALAU GAK ADA DATA = BENERAN NYANGKUT. BARU JUAL
            qty = format_qty(btc)
            res = await signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
            if 'orderId' in res:
                await send_telegram(f"⚠️ <b>SELL DARURAT</b>\nJual {qty:.6f} BTC @ {price:.2f}\nStatus: Bersih2 wallet")
        else:
            # 3. KALAU ADA DATA = BIARIN GRID YG JUALIN
            await send_telegram(f"ℹ️ <b>INFO POSISI NYANGKUT</b>\nAda {len(data_nyangkut)} posisi di DB\nBiarin grid yg handle jualnya")

async def place_order_real(side, price_grid, qty, order_data=None):
    usdt, btc = await get_all_balance()
    if side=="BUY":
        if await is_price_exist(price_grid): return
        butuh = hitung_butuh_modal(price_grid, qty)
        if usdt < butuh:
            if not NOTIF_FLAGS["saldo_kurang"]: await send_telegram(f"💰 SALDO KURANG. Butuh {butuh:.2f}")
            NOTIF_FLAGS["saldo_kurang"]=True; return
        res = await signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
        if 'orderId' in res:
            await sb_insert({"price":price_grid, "qty":qty, "side":"BUY", "status":"OPEN", "binance_order_id": res['orderId']})
            await send_telegram(f"🟢 <b>BUY</b> {price_grid:.2f}\nQty: {qty}")
            NOTIF_FLAGS["saldo_kurang"]=False

    if side=="SELL":
        if btc < qty: return
        res = await signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity":qty})
        if 'orderId' in res and order_data:
            harga_beli = order_data['price']
            profit = (price_grid - harga_beli) * qty
            DAILY_STATS["profit_usdt"] += profit; DAILY_STATS["trade_count"] += 1
            await sb_delete(order_data['id'])
            await send_telegram(f"🔴 <b>SELL</b> {price_grid:.2f}\nBeli: {harga_beli:.2f}\nProfit: {profit:.2f} USDT")

async def main():
    global START_TIME; START_TIME = time.time()
    await auto_create_table(); await get_binance_rules(SYMBOL); await update_grid_manager()
    saldo_usdt, saldo_btc = await get_all_balance(); harga_sekarang = await get_price()
    await send_telegram(f"🤖 <b>Bot V11.42 FIX SELL DARURAT</b>\n<b>Harga BTC:</b> {harga_sekarang}\n<b>Saldo:</b>\nUSDT: {saldo_usdt:.2f}\nBTC: {saldo_btc:.6f}")
    await cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)

    while True:
        try:
            price = await get_price(); await update_grid_manager()
            signal_buy, grid_buy = await cek_signal_buy(price)
            signal_sell, grid_sell, order_data = await cek_signal_sell(price)

            if signal_sell: await place_order_real("SELL", grid_sell, hitung_qty_aman(grid_sell), order_data)
            if signal_buy: await place_order_real("BUY", grid_buy, hitung_qty_aman(grid_buy))

            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]: await send_telegram(f"❌ <b>ERROR</b>\n{e}"); NOTIF_FLAGS["error"]=True
            await asyncio.sleep(5); NOTIF_FLAGS["error"]=False

if __name__ == "__main__":
    asyncio.run(main())
