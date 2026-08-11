import time, os, requests, json, threading, hmac, hashlib
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
load_dotenv()

WIB = timezone(timedelta(hours=7))
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PAIR = os.environ["PAIR"]
LOT = float(os.environ["LOT"])
FEE = 0.001
FILE_SLOTS = os.environ["FILE"]
FILE_ENV = ".env"

# KEY BINANCE RILL
API_KEY = os.environ["BINANCE_API_KEY"]
API_SECRET = os.environ["BINANCE_SECRET_KEY"]
BASE_URL = "https://api.binance.com"

ATR_PERIOD = 14; ATR_TIMEFRAME = "1h"; ATR_MULTIPLIER = 0.5; ATR_UPDATE_HOUR = 0
GRID_MIN = 250; GRID_MAX = 1000
GRID = 500; TP = 500; ATR_NOW = 0
MODUS = "GRID ATR v5.17 RILL BINANCE"
harga_sekarang = 0

last_update_id = 0
RUNNING = True
WAITING_FOR_LOT = False
NOTIF_SALDO_KURANG = False
last_save_time = 0
last_grid_buy = 0
last_grid_time = 0
area_yg_aktif = []
data_lock = threading.Lock()
session = requests.Session()

def send_telegram(msg, keyboard=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    if keyboard: data["reply_markup"] = json.dumps({"keyboard": [[{"text": "Status"}],[{"text": "Start"}, {"text": "Stop"}],[{"text": "Ganti LOT"}]], "resize_keyboard": True})
    try: session.post(url, data=data, timeout=2)
    except: pass

def save_env_lot(new_lot):
    global LOT
    LOT = float(new_lot)
    try:
        lines = open(FILE_ENV, "r").readlines()
        f = open(FILE_ENV, "w")
        for line in lines: f.write(f"LOT={new_lot}\n" if line.startswith("LOT=") else line)
        f.close()
    except: pass

def load_slots():
    try: return json.load(open(FILE_SLOTS)) if os.path.exists(FILE_SLOTS) else {}
    except: return {}
def save_slots(slots):
    try: json.dump(slots, open(FILE_SLOTS, "w"))
    except: pass

# ===== FUNGSI BINANCE BARU =====
def binance_signature(query_string):
    return hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def get_binance_balance():
    try:
        timestamp = int(time.time() * 1000)
        query = f"timestamp={timestamp}"
        sign = binance_signature(query)
        url = f"{BASE_URL}/api/v3/account?{query}&signature={sign}"
        headers = {"X-MBX-APIKEY": API_KEY}
        r = session.get(url, headers=headers, timeout=5).json()
        for asset in r['balances']:
            if asset['asset'] == 'USDT':
                return float(asset['free'])
    except: pass
    return 0

def binance_order(side, price, qty):
    try:
        timestamp = int(time.time() * 1000)
        params = f"symbol={PAIR}&side={side}&type=LIMIT&timeInForce=GTC&quantity={qty:.6f}&price={price:.2f}&timestamp={timestamp}"
        sign = binance_signature(params)
        url = f"{BASE_URL}/api/v3/order?{params}&signature={sign}"
        headers = {"X-MBX-APIKEY": API_KEY}
        r = session.post(url, headers=headers, timeout=5).json()
        return r, FEE
    except Exception as e:
        return {"error": str(e)}, 0
# ===== SELESAI FUNGSI BINANCE =====

def get_atr():
    global GRID, TP, ATR_NOW
    for domain in ["api1", "api2", "api3", "api"]:
        try:
            data = session.get(f"https://{domain}.binance.com/api/v3/klines?symbol={PAIR}&interval={ATR_TIMEFRAME}&limit={ATR_PERIOD+1}", timeout=3).json()
            tr_list = [max(float(data[i][2])-float(data[i][3]), abs(float(data[i][2])-float(data[i-1][4])), abs(float(data[i][3])-float(data[i-1][4]))) for i in range(1, len(data))]
            ATR_NOW = sum(tr_list) / len(tr_list)
            GRID = max(GRID_MIN, min(ATR_NOW * ATR_MULTIPLIER, GRID_MAX))
            TP = GRID
            send_telegram(f"🔄 *UPDATE ATR*\n`GRID: ${GRID:.2f} | TP: ${TP:.2f}`")
            return
        except: continue

def cek_command_telegram():
    global last_update_id, RUNNING, WAITING_FOR_LOT
    while True:
        try:
            r = session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10", timeout=12).json()
            for update in r.get("result", []):
                last_update_id = update["update_id"]
                if "message" in update and "text" in update["message"]:
                    text = update["message"]["text"].lower()
                    if WAITING_FOR_LOT:
                        try: new_lot = float(text); save_env_lot(new_lot); send_telegram(f"✅ *LOT: ${new_lot}*"); WAITING_FOR_LOT = False
                        except: send_telegram("❌ Kirim angka")
                        continue

                    if text == "status":
                        with data_lock:
                            s = load_slots()
                            saldo_rill = get_binance_balance() # CEK RILL
                            status_bot = "🟢 JALAN" if RUNNING else "🔴 PAUSE/STOP"

                        posisi_text = ""
                        total_profit_jalan = 0
                        if len(s) > 0:
                            for i, (buy_str, tp_target) in enumerate(s.items(), 1):
                                buy = float(buy_str)
                                tp = float(tp_target)
                                qty = LOT / buy
                                profit = (tp * qty * (1-FEE)) - LOT * (1+FEE)
                                total_profit_jalan += profit
                                posisi_text += f"\n`{i}. BUY: ${buy:.2f} | TP: ${tp:.2f} | +${profit:.2f}`"
                        else:
                            posisi_text = "\n`Kosong`"

                        msg = f"📊 *STATUS BOT*\n`Status: {status_bot}`"
                        msg += f"\n`Harga: ${harga_sekarang:.2f}`\n`SALDO RILL: ${saldo_rill:.2f}`\n`GRID: ${GRID:.2f}`\n`LOT: ${LOT}`\n`Posisi: {len(s)}`"
                        msg += f"\n\n📍 *DETAIL POSISI*{posisi_text}"
                        msg += f"\n\n💰 *Potensi Profit: ${total_profit_jalan:.2f}*"
                        send_telegram(msg)

                    if text == "start":
                        with data_lock: RUNNING = True
                        send_telegram("✅ *BOT DIJALANKAN*")
                    if text == "stop":
                        with data_lock: RUNNING = False
                        send_telegram("🛑 *BOT DI PAUSE*")
                    if text == "ganti lot": WAITING_FOR_LOT = True; send_telegram(f"💰 `LOT: ${LOT}` Kirim angka baru.")
        except: time.sleep(3)

def get_harga_binance():
    global harga_sekarang
    for domain in ["api1", "api2", "api3", "api"]:
        try:
            data = session.get(f"https://{domain}.binance.com/api/v3/ticker/price?symbol={PAIR}", timeout=2).json()
            harga_sekarang = float(data['price'])
            return harga_sekarang
        except: continue
    return harga_sekarang

def place_buy(buy_price):
    global area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG
    if buy_price <= 0: return

    slots = load_slots()
    if len(slots) >= 1: # ANTI DOBEL
        return

    with data_lock:
        if not RUNNING: return
        buy_price, tp_price = round(buy_price, 2), buy_price + TP; qty = LOT / buy_price
        saldo = get_binance_balance() # CEK RILL

        # Buffer 0.2% buat fee biar gak ke-PAUSE
        if saldo < LOT * 1.002:
            if NOTIF_SALDO_KURANG == False:
                send_telegram(f"⚠️ *PAUSE* `Saldo kurang. Butuh: ${LOT*1.002:.2f}`")
                NOTIF_SALDO_KURANG = True
            RUNNING = False; return

        NOTIF_SALDO_KURANG = False
        order, fee = binance_order("BUY", buy_price, qty)
        if 'orderId' not in order:
            send_telegram(f"❌ Gagal BUY: {order}")
            return
        slots[str(buy_price)] = tp_price
        area_bawah = int(buy_price / GRID) * GRID
        if area_bawah not in area_yg_aktif: area_yg_aktif.append(area_bawah)

    save_slots(slots); send_telegram(f"🟢 *BUY*\n`${buy_price:.2f}` | Qty: {qty:.6f}")

def proses_trading():
    global last_grid_buy, last_grid_time, area_yg_aktif, RUNNING
    if harga_sekarang == 0: return

    saldo = get_binance_balance()
    with data_lock:
        if not RUNNING and saldo >= LOT * 1.002 and NOTIF_SALDO_KURANG:
            RUNNING = True; send_telegram("✅ *LANJUT OTOMATIS*")

    grid_terdekat = round(harga_sekarang / GRID) * GRID
    if grid_terdekat!= last_grid_buy:
        area_bawah = int(grid_terdekat / GRID) * GRID
        with data_lock: area_masih_kosong = area_bawah not in area_yg_aktif
        if area_masih_kosong and saldo >= LOT * 1.002 and time.time() - last_grid_time > 3:
            place_buy(grid_terdekat); last_grid_buy = grid_terdekat; last_grid_time = time.time()

    slots = load_slots(); to_delete = []; ada_perubahan = False
    for buy_str, tp_target in list(slots.items()):
        buy = float(buy_str); tp_target = float(tp_target)
        if harga_sekarang >= tp_target:
            with data_lock:
                qty = LOT / buy
                order, fee = binance_order("SELL", harga_sekarang, qty) # SELL RILL
                profit = (harga_sekarang * qty * (1 - FEE)) - LOT * (1 + FEE)
                to_delete.append(buy_str)
                area_bawah = int(buy / GRID) * GRID
                if area_bawah in area_yg_aktif: area_yg_aktif.remove(area_bawah)
                ada_perubahan = True
            send_telegram(f"🎯 *TP HIT*\n`Entry: ${buy:.2f} | Exit: ${harga_sekarang:.2f}`\n`Profit: ${profit:.4f}`")

    if ada_perubahan:
        for d in to_delete: del slots[d]
        save_slots(slots)

# ===== AWAL JALAN =====
slots = load_slots()
get_atr()
send_telegram(f"🤖 *BOT v5.17 RILL BINANCE*\n`GRID: ${GRID:.2f} | LOT: ${LOT}`", keyboard=True)

if not slots and RUNNING:
    harga_awal = get_harga_binance()
    if harga_awal > 0:
        grid_awal = round(harga_awal / GRID) * GRID
        place_buy(grid_awal)
        send_telegram(f"🚀 *BUY AWAL ${grid_awal:.2f}*")

with data_lock:
    for buy_str in slots.keys():
        area_bawah = int(float(buy_str) / GRID) * GRID
        if area_bawah not in area_yg_aktif: area_yg_aktif.append(area_bawah)

telegram_thread = threading.Thread(target=cek_command_telegram, daemon=True)
telegram_thread.start()

print("BOT v5.17 AKTIF")

while True:
    try:
        get_harga_binance()
        proses_trading()
        now = datetime.now(WIB)
        if now.hour == ATR_UPDATE_HOUR and now.minute == 0 and now.second < 5: get_atr(); time.sleep(5)
        time.sleep(1)
    except Exception as e:
        print("Error: " + str(e))
        time.sleep(5)
