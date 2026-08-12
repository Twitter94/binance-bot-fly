import time
import os
import requests
import json
import threading
import hmac
import hashlib
from decimal import Decimal
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from flask import Flask
from supabase import create_client, Client
load_dotenv('.env_rill')

WIB = timezone(timedelta(hours=7))
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PAIR = os.environ["PAIR"]
LOT = float(os.environ["LOT"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BINANCE_API_KEY = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]
BASE_URL = "https://api.binance.com"

ATR_PERIOD = 14; ATR_TIMEFRAME = "1h"; ATR_MULTIPLIER = 0.5; ATR_UPDATE_HOUR = 0
GRID_MIN = 1.5; GRID_MAX = 7
GRID = 2.5; TP = 2.5
harga_sekarang = 0
last_atr = 0
last_atr_check = 0

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
NOTIF_SALDO_0 = False
NOTIF_AWAL_SENT = False # <--- 1. TAMBAH INI

def load_slots():
    res = supabase.table("slots").select("*").execute()
    return {str(row["buy_price"]): row["tp_price"] for row in res.data}

def save_slots(slots):
    supabase.table("slots").delete().neq("buy_price", -1).execute()
    data = [{"buy_price": float(buy), "tp_price": tp, "pair": PAIR} for buy, tp in slots.items()]
    if data:
        supabase.table("slots").insert(data).execute()

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

def get_order_details(symbol, order_id):
    try:
        ts = int(time.time() * 1000)
        query = binance_sign({"symbol": symbol, "orderId": order_id, "timestamp": ts})
        r = session.get(f"{BASE_URL}/api/v3/order?{query}", timeout=3).json()
        return r
    except: return None

def hitung_dari_fills(order_data):
    if not order_data or 'fills' not in order_data or len(order_data['fills']) == 0:
        return 0, 0, 0, 0
    total_qty = Decimal('0')
    total_quote = Decimal('0')
    total_fee_usdt = Decimal('0')
    for f in order_data['fills']:
        qty = Decimal(f['qty']); price = Decimal(f['price']); commission = Decimal(f['commission']); commission_asset = f['commissionAsset']
        total_qty += qty; total_quote += qty * price
        if commission_asset == 'USDT': total_fee_usdt += commission
        else:
            p = session.get(f"{BASE_URL}/api/v3/ticker/price?symbol={commission_asset}USDT", timeout=2).json()
            total_fee_usdt += commission * Decimal(p['price'])
    avg_price = total_quote / total_qty if total_qty > 0 else Decimal('0')
    return float(total_qty), float(total_quote), float(total_fee_usdt), float(avg_price)

def cancel_order(orderId):
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR, "orderId": orderId, "timestamp": ts}
    query = binance_sign(params)
    session.delete(f"{BASE_URL}/api/v3/order?{query}")

def cancel_all_tp_orders():
    try:
        ts = int(time.time() * 1000)
        query = binance_sign({"symbol": PAIR, "timestamp": ts})
        orders = session.get(f"{BASE_URL}/api/v3/openOrders?{query}").json()
        for o in orders:
            if o['side'] == 'SELL': cancel_order(o['orderId'])
    except: pass

def binance_order(side, price, qty):
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR,"side": side,"type": "LIMIT","timeInForce": "GTC","quantity": f"{qty:.6f}","price": f"{price:.2f}","timestamp": ts}
    query = binance_sign(params)
    return session.post(f"{BASE_URL}/api/v3/order?{query}").json()

def binance_market_sell(qty):
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR,"side": "SELL","type": "MARKET","quantity": f"{qty:.6f}","timestamp": ts}
    query = binance_sign(params)
    return session.post(f"{BASE_URL}/api/v3/order?{query}").json()

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
    pesan = f"📊 *STATUS v5.40 DCA ON*"
    pesan += f"\n{'🟢 JALAN' if RUNNING else '🔴 PAUSE'} | Harga: `${harga:.2f}`"
    pesan += f"\nSALDO: `${saldo:.4f}` <- Dari API"
    pesan += f"\nGRID: `${GRID:.2f}` | LOT: `${LOT}` | Posisi: `{posisi}`\n\n"
    pesan += f"📍 *POSISI*\n"
    if posisi == 0: pesan += "Kosong"
    else:
        for buy_str, tp_target in list(slots.items())[:5]:
            pesan += f"`BUY ${float(buy_str):.2f} -> TP ${float(tp_target):.2f}`\n"
        if posisi > 5: pesan += f"...dan {posisi-5} posisi lain"
    pesan += f"\n\n💰 *Est Profit: ${est_profit:.4f}*"
    send_telegram(pesan, keyboard=True)

def save_env_lot(new_lot):
    global LOT
    LOT_LAMA = LOT
    LOT = float(new_lot)
    lines = open(".env_rill", "r").readlines()
    f = open(".env_rill", "w")
    for line in lines: f.write(f"LOT={new_lot}\n" if line.startswith("LOT=") else line)
    f.close()
    return LOT_LAMA

def get_atr(force=False):
    global GRID, TP, last_atr, last_atr_check
    if time.time() - last_atr_check < 60 and force == False: return
    last_atr_check = time.time()
    for i in range(3):
        try:
            data = session.get(f"{BASE_URL}/api/v3/klines?symbol={PAIR}&interval={ATR_TIMEFRAME}&limit={ATR_PERIOD+1}", timeout=5).json()
            if 'code' in data: raise Exception(data['msg'])
            tr_list = [max(float(data[i][2])-float(data[i][3]), abs(float(data[i][2])-float(data[i-1][4])), abs(float(data[i][3])-float(data[i-1][4]))) for i in range(1, len(data))]
            atr_baru = sum(tr_list) / len(tr_list)
            harus_update = False; update_sekarang = False; alasan = ""
            if last_atr > 0:
                naik = atr_baru > last_atr; turun = atr_baru < last_atr; perubahan = abs(atr_baru - last_atr) / last_atr
                if naik and perubahan > 0.20: harus_update = True; update_sekarang = False; alasan = f"📈 ATR NAIK {perubahan*100:.1f}%"
                if turun and perubahan > 0.20: harus_update = True; update_sekarang = True; alasan = f"📉 ATR TURUN {perubahan*100:.1f}%"
            if force: harus_update = True; update_sekarang = True; alasan = "🔄 UPDATE HARIAN JAM 00"
            if harus_update:
                new_grid = max(GRID_MIN, min(atr_baru * ATR_MULTIPLIER, GRID_MAX))
                if update_sekarang:
                    send_telegram(f"⚠️ *{alasan}*\n`Cancel Semua TP + Turunin Sekarang`")
                    cancel_all_tp_orders()
                    slots = load_slots()
                    new_slots = {buy: float(buy) + new_grid for buy in slots.keys()}
                    save_slots(new_slots)
                    area_yg_aktif.clear()
                    send_telegram(f"🔄 *TP DIUPDATE SEKARANG*")
                else: send_telegram(f"⚠️ *{alasan}*\n`TP Baru ${new_grid:.2f} Dipakai Pas Reentry`")
                GRID = new_grid; TP = GRID
                send_telegram(f"`ATR 14H: ${atr_baru:.2f}`\n`GRID BARU: ${GRID:.2f} | TP: ${TP:.2f}`")
            GRID = max(GRID_MIN, min(atr_baru * ATR_MULTIPLIER, GRID_MAX)); TP = GRID
            last_atr = atr_baru; return
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
        if saldo <= 0:
            if NOTIF_SALDO_0 == False: send_telegram(f"⚠️ *PAUSE* `Saldo 0 - Menunggu deposit`"); NOTIF_SALDO_0 = True
            RUNNING = False; return
        elif saldo < LOT:
            if NOTIF_SALDO_KURANG == False: send_telegram(f"⚠️ *PAUSE* `Saldo kurang`"); NOTIF_SALDO_KURANG = True
            RUNNING = False; return
        NOTIF_SALDO_KURANG = False; NOTIF_SALDO_0 = False
        order = binance_order("BUY", buy_price, qty) # <--- INI LIMIT
        if 'orderId' not in order: return
        area_yg_aktif.append(int(buy_price / GRID) * GRID); slots[str(buy_price)] = tp_price
    save_slots(slots)
    detail = get_order_details(PAIR, order['orderId'])
    qty, quote, fee, avg = hitung_dari_fills(detail)
    send_telegram(f"🟢 *BUY TERISI*\n`Harga: ${avg:.4f}`\n`Qty: {qty:.6f}`\n`Modal: ${quote:.4f}`\n`Fee: ${fee:.4f}`")

def proses_trading():
    global last_grid_buy, last_grid_time, area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG, NOTIF_SALDO_0
    if harga_sekarang == 0: return
    if not RUNNING and get_binance_balance() >= LOT: RUNNING = True; NOTIF_SALDO_KURANG = False; NOTIF_SALDO_0 = False; send_telegram("✅ *LANJUT OTOMATIS*")
    slots = load_slots()
    to_delete_kecepetan = []
    for buy_str, tp_target in list(slots.items()):
        buy = float(buy_str); tp = float(tp_target)
        if harga_sekarang > tp:
            qty = LOT / buy
            order = binance_market_sell(qty)
            if 'orderId' in order:
                detail = get_order_details(PAIR, order['orderId'])
                qty_jual, quote, fee, avg = hitung_dari_fills(detail)
                profit = quote - LOT - fee
                to_delete_kecepetan.append(buy_str)
                area_yg_aktif.remove(int(buy / GRID) * GRID)
                send_telegram(f"🚨 *TP KELEWAT! MARKET SELL*\n`Buy @${buy:.2f} -> Jual @${avg:.4f}`\n`Dapat: ${quote:.4f}`\n`Fee: ${fee:.4f}`\n`*Profit: ${profit:.4f}*`")
                reentry = round(avg, 2)
                if (int(reentry / GRID) * GRID) not in area_yg_aktif: place_buy(reentry)
    if to_delete_kecepetan:
        slots = load_slots()
        for d in to_delete_kecepetan: del slots[d]
        save_slots(slots)
    grid_terdekat = round(harga_sekarang / GRID) * GRID
    area_grid = int(grid_terdekat / GRID) * GRID
    if area_grid not in area_yg_aktif and time.time() - last_grid_time > 3:
        place_buy(grid_terdekat); last_grid_buy = grid_terdekat; last_grid_time = time.time()
    slots = load_slots(); to_delete = []
    for buy_str, tp_target in list(slots.items()):
        buy, tp = float(buy_str), float(tp_target)
        if harga_sekarang >= tp:
            with data_lock:
                qty = LOT / buy; order = binance_order("SELL", tp, qty)
                if 'orderId' in order:
                    detail = get_order_details(PAIR, order['orderId'])
                    qty_jual, quote, fee, avg = hitung_dari_fills(detail)
                    profit = quote - LOT - fee
                    to_delete.append(buy_str); area_yg_aktif.remove(int(buy / GRID) * GRID)
                    reentry = round(avg, 2)
                    if (int(reentry / GRID) * GRID) not in area_yg_aktif: place_buy(reentry)
                    send_telegram(f"🎯 *TP LIMIT TERISI*\n`Buy @${buy:.2f} -> Jual @${avg:.4f}`\n`Dapat: ${quote:.4f}`\n`Fee: ${fee:.4f}`\n`*Profit: ${profit:.4f}*`")
    if to_delete:
        slots = load_slots()
        for d in to_delete: del slots[d]
        save_slots(slots)

app = Flask(__name__)
@app.route("/")
def health(): return "OK", 200

def run_bot():
    global NOTIF_AWAL_SENT # <--- 2. TAMBAH INI
    slots = load_slots()
    get_atr(force=True)
    time.sleep(1)
    send_telegram(f"🤖 *BOT v5.40 DCA ON - 100% API*\n`GRID: ${GRID:.2f} | LOT: ${LOT}`", keyboard=True)
    harga_awal = get_harga_binance()
    if not slots and RUNNING and harga_awal > 0:
        buy_price = round(harga_awal / GRID) * GRID
        place_buy(buy_price)
        if not NOTIF_AWAL_SENT: # <--- 3. TAMBAH INI
            send_telegram(f"📥 *AWAL START*\n`Masang BUY LIMIT pertama di: ${buy_price:.2f}`\n`GRID: ${GRID}`")
            NOTIF_AWAL_SENT = True

    for buy_str in slots.keys(): area_yg_aktif.append(int(float(buy_str) / GRID) * GRID)
    threading.Thread(target=cek_command_telegram, daemon=True).start()
    print("BOT v5.40 AKTIF")

    while True:
        try:
            get_harga_binance(); proses_trading()
            now = datetime.now(WIB)
            if now.hour == ATR_UPDATE_HOUR and now.minute == 0 and now.second < 5:
                get_atr(force=True)
                time.sleep(5)
            get_atr(force=False)
            time.sleep(1)
        except: time.sleep(5)

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
