import asyncio
import os
import time
import requests
import hmac
import hashlib
import gc
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_SECRET = os.getenv("API_SECRET")
SUPABASE_URL = os.getenv("SUPA_URL")
SUPABASE_KEY = os.getenv("SUPA_KEY")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")

SYMBOL = "BTCUSDT"
BASE_COIN = "BTC"
QUOTE_COIN = "USDT"
LOOP_SEC = 3
BUFFER_USDT = 0.5
TABEL = "orders"

# ========== CONFIG ATR + GRID ==========
ATR_PERIOD = 14
ATR_TIMEFRAME = "1h"
ATR_MULTIPLIER = 0.5
ATR_UPDATE_HOUR = 0 # Jam 00.00 WIB

GRID_MIN = 250
GRID_MAX = 1000

# ========== CONFIG MODE PEMANASAN ==========
WAIT_FIRST_BUY = 10
FIRST_BUY_DONE = False
START_TIME = time.time()

# ========== GLOBAL ==========
BASE_URL = "https://api.binance.com"
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
GRID_MANAGER = {"grid_step": GRID_MIN, "date": None, "atr": 0} # HAPUS last_atr
DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": None}
WIB = timezone(timedelta(hours=7))
NOTIF_FLAGS = {"error": False, "saldo_kurang": False}

BUY_HISTORY = set()
LAST_ERROR_MSG = ""

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}

#... SEMUA FUNGSI SUPABASE, BINANCE, TELEGRAM SAMA...

def get_atr(symbol, period=ATR_PERIOD, interval=ATR_TIMEFRAME):
    r = requests.get(f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval={interval}&limit={period+1}", timeout=5)
    data = r.json(); tr_list = []
    for i in range(1, len(data)):
        high, low, prev_close = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close)); tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def update_grid_manager():
    global GRID_MANAGER, DAILY_STATS
    now_wib = datetime.now(WIB)
    hari_ini_wib = now_wib.strftime("%Y-%m-%d")
    jam_sekarang = now_wib.hour

    # 1. Reset statistik harian
    if DAILY_STATS["date"]!= hari_ini_wib:
        DAILY_STATS = {"trade_count": 0, "profit_usdt": 0.0, "date": hari_ini_wib}

    # 2. Cek: udah jam 00.00 dan belum update hari ini?
    if GRID_MANAGER["date"]!= hari_ini_wib and jam_sekarang >= ATR_UPDATE_HOUR:
        print(f"[ATR] Waktunya update harian jam {ATR_UPDATE_HOUR}:00")

        atr_baru = get_atr(SYMBOL) # LANGSUNG AMBIL TANPA CEK 20%

        # 3. Hitung grid baru
        grid_step = atr_baru * ATR_MULTIPLIER
        grid_step = max(GRID_MIN, min(grid_step, GRID_MAX)) # Clamp 250 - 1000

        GRID_MANAGER["grid_step"] = grid_step
        GRID_MANAGER["atr"] = atr_baru
        GRID_MANAGER["date"] = hari_ini_wib

        send_telegram(f"📊 <b>ATR UPDATE 00:00</b>\nATR: {atr_baru:.2f}\nGrid Baru: {grid_step:.2f} = ATR x {ATR_MULTIPLIER}")
        print(f"[ATR UPDATE] ATR: {atr_baru:.2f} | Grid Step: {grid_step:.2f}")

    return GRID_MANAGER["grid_step"]

def generate_grid_levels(harga_tengah, grid_step):
    levels = []
    for i in range(-3, 4):
        level = harga_tengah + (i * grid_step) # TANPA PEMBULATAN
        if level > 0: levels.append(level)
    return sorted(list(set(levels)))

#... SEMUA FUNGSI place_order_real, cek_signal SAMA...

async def main():
    global START_TIME, NOTIF_FLAGS; START_TIME = time.time()
    auto_create_table(); get_binance_rules(SYMBOL); update_grid_manager()
    saldo_usdt, saldo_btc = get_all_balance(); harga_sekarang = get_price()
    send_telegram(f"🤖 <b>Bot V11.39 ATR 00:00 NO TOLERANCE</b>\n<b>Harga BTC:</b> {harga_sekarang}\n<b>Saldo:</b>\nUSDT: {saldo_usdt:.2f}\nBTC: {saldo_btc:.6f}")
    cek_sell_instan_darurat(harga_sekarang); await asyncio.sleep(3)
    print("Bot V11.39 MODE REAL. Menunggu 10 detik untuk buy pertama...")

    while True:
        try:
            price = get_price()
            update_grid_manager() # Cek tiap loop

            signal_buy, grid_buy = cek_signal_buy(price)
            signal_sell, grid_sell, is_reentry = cek_signal_sell(price)

            if signal_sell:
                qty = hitung_qty_aman(grid_sell)
                place_order_real("SELL", grid_sell, qty, is_reentry=is_reentry)
            if signal_buy:
                qty = hitung_qty_aman(grid_buy)
                place_order_real("BUY", grid_buy, qty)

            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e:
            if not NOTIF_FLAGS["error"]:
                send_telegram(f"❌ <b>ERROR BOT</b>\n{e}")
                NOTIF_FLAGS["error"] = True
            print(f"ERROR: {e}")
            await asyncio.sleep(5)
            NOTIF_FLAGS["error"] = False

if __name__ == "__main__":
    asyncio.run(main())
