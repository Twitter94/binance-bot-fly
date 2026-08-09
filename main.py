import time, os, requests, json, threading, hmac, hashlib
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from flask import Flask # <-- TAMBAHAN FLASK DOANG
load_dotenv('.env_rill')

WIB = timezone(timedelta(hours=7))
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PAIR = os.environ["PAIR"]
LOT = float(os.environ["LOT"])
FILE_SLOTS = os.environ["FILE"]

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]
BASE_URL = "https://api.binance.com"

ATR_PERIOD = 14; ATR_TIMEFRAME = "1h"; ATR_MULTIPLIER = 0.5; ATR_UPDATE_HOUR = 0
GRID_MIN = 250; GRID_MAX = 1000
GRID = 500; TP = 500
harga_sekarang = 0

last_update_id = 0
RUNNING = True
WAITING_FOR_LOT = False
last_grid_buy = 0
last_grid_time = 0
area_yg_aktif = []
data_lock = threading.Lock()
session = requests.Session()
session.headers.update({'X-MBX-APIKEY': BINANCE_API_KEY})
FEE_EST = 0.001
NOTIF_SALDO_KURANG = False
NOTIF_SALDO_0 = False # <-- 1. TAMBAHAN BARU

def binance_sign(params={}):
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"

def get_binance_balance():
    try:
        ts = int(time.time() * 1000)
        query = binance_sign({"timestamp": ts})
        r = session.get(f"{BASE_URL}/api/v3/account?{query}", timeout=3).json()
        for b in r['balances']:
            if b['asset'] == 'USDT': return float(b['free'])
    except: pass
    return 0

def binance_order(side, price, qty):
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR,"side": side,"type": "LIMIT","timeInForce": "GTC","quantity": f"{qty:.6f}","price": f"{price:.2f}","timestamp": ts}
    query = binance_sign(params)
    r = session.post(f"{BASE_URL}/api/v3/order?{query}").json()
    fee_usdt = 0
    if 'fills' in r:
        for fill in r['fills']:
            fee_qty = float(fill['commission'])
            if fill['commissionAsset'] == 'USDT': fee_usdt += fee_qty
            else:
                p = session.get(f"{BASE_URL}/api/v3/ticker/price?symbol={fill['commissionAsset']}USDT", timeout=2).json()
                fee_usdt += fee_qty * float(p['price'])
    return r, fee_usdt

def send_telegram(msg, keyboard=False):
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        if keyboard: data["reply_markup"] = json.dumps({"keyboard": [[{"text": "Status"}],[{"text": "Start"}, {"text": "Stop"}],[{"text": "Ganti LOT"}]], "resize_keyboard": True})
        session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data, timeout=2)
    except: pass

def kirim_status():
    harga = get_harga_binance()
    saldo = get_binance_balance()
    slots = load_slots()
    posisi = len(slots)
    est_profit = 0
    for buy_str, tp_target in slots.items():
        buy = float(buy_str)
        tp = float(tp_target)
        qty = LOT / buy
        profit_kotor = (tp - buy) * qty
        profit_bersih = profit_kotor - (profit_kotor * FEE_EST * 2)
        est_profit += profit_bersih

    pesan = f"📊 *STATUS v5.29*\n" # <-- UBAH VERSI DOANG
    pesan += f"{'🟢 JALAN' if RUNNING else '🔴 PAUSE'} | Harga: ${harga:.2f}\n"
    pesan += f"SALDO: ${saldo:.2f} | GRID: ${GRID:.2f}\n"
    pesan += f"LOT: ${LOT} | Posisi: {posisi}\n\n"
    pesan += f"📍 *POSISI*\n"
    if posisi == 0:
        pesan += "Kosong"
    else:
        for buy_str, tp_target in list(slots.items())[:5]:
            pesan += f"`BUY ${float(buy_str):.0f} -> TP ${float(tp_target):.0f}`\n"
        if posisi > 5: pesan += f"...dan {posisi-5} posisi lain"
    pesan += f"\n\n💰 *Est Profit Bersih: ${est_profit:.4f}*"
    send_telegram(pesan)

def save_env_lot(new_lot):
    global LOT
    LOT_LAMA = LOT
    LOT = float(new_lot)
    lines = open(".env_rill", "r").readlines()
    f = open(".env_rill", "w")
    for line in lines: f.write(f"LOT={new_lot}\n" if line.startswith("LOT=") else line)
    f.close()
    return LOT_LAMA

def load_slots(): return json.load(open(FILE_SLOTS)) if os.path.exists(FILE_SLOTS) else {}
def save_slots(slots): json.dump(slots, open(FILE_SLOTS, "w"))

def get_atr():
    global GRID, TP
    for i in range(3):
        try:
            data = session.get(f"{BASE_URL}/api/v3/klines?symbol={PAIR}&interval={ATR_TIMEFRAME}&limit={ATR_PERIOD+1}", timeout=5).json()
            if 'code' in data: raise Exception(data['msg'])
            tr_list = [max(float(data[i][2])-float(data[i][3]), abs(float(data[i][2])-float(data[i-1][4])), abs(float(data[i][3])-float(data[i-1][4]))) for i in range(1, len(data))]
            atr = sum(tr_list) / len(tr_list)
            GRID_LAMA = GRID
            GRID = max(GRID_MIN, min(atr * ATR_MULTIPLIER, GRID_MAX))
            TP = GRID
            send_telegram(f"🔄 *UPDATE ATR*\n`ATR 14H: ${atr:.2f}`\n`GRID BARU: ${GRID:.2f} | TP: ${TP:.2f}`")
            return
        except: time.sleep(2)
    send_telegram(f"❌ *GAGAL UPDATE ATR 3x*")

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
                        try:
                            new_lot = float(text)
                            lot_lama = save_env_lot(new_lot)
                            send_telegram(f"✅ *LOT BERHASIL DIGANTI*\n`LOT Lama: ${lot_lama}`\n`LOT Baru: ${new_lot}`")
                            WAITING_FOR_LOT = False
                        except: send_telegram("❌ Kirim angka. Contoh: 10")
                        continue
                    if text == "status": kirim_status()
                    if text == "start": RUNNING = True; send_telegram("✅ *JALAN*")
                    if text == "stop": RUNNING = False; send_telegram("🛑 *PAUSE*")
                    if text == "ganti lot": WAITING_FOR_LOT = True; send_telegram(f"💰 `LOT Sekarang: ${LOT}`\nKirim angka baru:")
        except: time.sleep(3)

def get_harga_binance():
    global harga_sekarang
    try: harga_sekarang = float(session.get(f"{BASE_URL}/api/v3/ticker/price?symbol={PAIR}", timeout=2).json()['price'])
    except: pass
    return harga_sekarang

def place_buy(buy_price):
    global area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG, NOTIF_SALDO_0
    if buy_price <= 0: return
    with data_lock:
        if not RUNNING: return
        buy_price, tp_price = round(buy_price, 2), buy_price + TP; qty = LOT / buy_price
        slots = load_slots()
        if str(buy_price) in slots: return
        saldo = get_binance_balance()

        # <-- 2. LOGIKA BARU DIMULAI DARI SINI - TIDAK DIRUBAH
        if saldo <= 0:
            if NOTIF_SALDO_0 == False:
                send_telegram(f"⚠️ *PAUSE* `Saldo 0 - Menunggu deposit`")
                NOTIF_SALDO_0 = True
            RUNNING = False; return
        elif saldo < LOT:
            if NOTIF_SALDO_KURANG == False:
                send_telegram(f"⚠️ *PAUSE* `Saldo kurang`")
                NOTIF_SALDO_KURANG = True
            RUNNING = False; return
        # <-- SELESAI LOGIKA BARU

        NOTIF_SALDO_KURANG = False
        NOTIF_SALDO_0 = False
        order, fee = binance_order("BUY", buy_price, qty)
        if 'orderId' not in order: return
        area_yg_aktif.append(int(buy_price / GRID) * GRID); slots[str(buy_price)] = tp_price
    save_slots(slots); send_telegram(f"🟢 *BUY*\n`$${buy_price:.2f}`")

def proses_trading():
    global last_grid_buy, last_grid_time, area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG, NOTIF_SALDO_0
    if harga_sekarang == 0: return
    # <-- 3. RESET NOTIF PAS LANJUT - TIDAK DIRUBAH
    if not RUNNING and get_binance_balance() >= LOT:
        RUNNING = True; NOTIF_SALDO_KURANG = False; NOTIF_SALDO_0 = False; send_telegram("✅ *LANJUT OTOMATIS*")
    grid_terdekat = round(harga_sekarang / GRID) * GRID
    if grid_terdekat!= last_grid_buy and (int(grid_terdekat / GRID) * GRID) not in area_yg_aktif and time.time() - last_grid_time > 3:
        place_buy(grid_terdekat); last_grid_buy = grid_terdekat; last_grid_time = time.time()
    slots = load_slots(); to_delete = []
    for buy_str, tp_target in list(slots.items()):
        buy, tp = float(buy_str), float(tp_target)
        if harga_sekarang >= tp:
            with data_lock:
                qty = LOT / buy; order, fee = binance_order("SELL", tp, qty)
                if 'orderId' in order:
                    hasil = tp * qty; profit = hasil - LOT - fee
                    to_delete.append(buy_str); area_yg_aktif.remove(int(buy / GRID) * GRID)
                    reentry = round(tp, 2)
                    if (int(reentry / GRID) * GRID) not in area_yg_aktif: place_buy(reentry)
                    send_telegram(f"🎯 *TP*\n`$${buy:.0f} -> $${tp:.0f}`\n`Profit: ${profit:.4f}`")
    if to_delete:
        for d in to_delete: del slots[d]
        save_slots(slots)

# ===== AWAL =====
app = Flask(__name__) # <-- TAMBAHAN 1

@app.route("/") # <-- TAMBAHAN 2
def health():
    return "OK", 200

def run_bot(): # <-- TAMBAHAN 3
    slots = load_slots()
    get_atr()
    time.sleep(1)
    send_telegram(f"🤖 *BOT v5.29 RILL*\n`GRID: ${GRID:.2f} | LOT: ${LOT}`", keyboard=True)
    harga_awal = get_harga_binance()
    if not slots and RUNNING and harga_awal > 0: place_buy(round(harga_awal / GRID) * GRID)
    for buy_str in slots.keys(): area_yg_aktif.append(int(float(buy_str) / GRID) * GRID)
    threading.Thread(target=cek_command_telegram, daemon=True).start()
    print("BOT v5.29 AKTIF")

    while True:
        try:
            get_harga_binance(); proses_trading()
            now = datetime.now(WIB)
            if now.hour == ATR_UPDATE_HOUR and now.minute == 0 and now.second < 5: get_atr(); time.sleep(5)
            time.sleep(1)
        except: time.sleep(5)

if __name__ == "__main__": # <-- TAMBAHAN 4
    threading.Thread(target=run_bot, daemon=True).start() # Jalanin bot di background
    app.run(host="0.0.0.0", port=8080) # Buka port buat Fly
