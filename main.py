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
GRID = 1.5; TP = 1.5
BUFFER_FEE = 0.003 # 0.3% buffer biar pasti untung
MAX_GAGAL_SELL = 3
harga_sekarang = 0
last_atr = 0
last_atr_check = 0

last_update_id = 0
RUNNING = True
WAITING_FOR_LOT = False
last_grid_buy = 0
last_grid_time = 0
area_yg_aktif = []
area_gagal = set()
slot_gagal_sell = {}
LAST_FAIL_TIME = 0
data_lock = threading.Lock()
session = requests.Session()
session.headers.update({'X-MBX-APIKEY': BINANCE_API_KEY})
FEE_EST = 0.001
NOTIF_SALDO_KURANG = False
NOTIF_SALDO_0 = False
NOTIF_AWAL_SENT = False

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
        r = session.get(f"{BASE_URL}/api/v3/account?{query}", timeout=5).json()
        for b in r['balances']:
            if b['asset'] == 'USDT': return float(b['free'])
    except Exception as e:
        send_telegram(f"❌ Gagal cek saldo: {e}")
    return 0

def get_order_details(symbol, order_id):
    try:
        ts = int(time.time() * 1000)
        query = binance_sign({"symbol": symbol, "orderId": order_id, "timestamp": ts})
        r = session.get(f"{BASE_URL}/api/v3/order?{query}", timeout=5).json()
        return r
    except: return None

def hitung_dari_fills(order_data):
    if not order_data or 'fills' not in order_data or len(order_data['fills']) == 0: return 0, 0, 0, 0
    total_qty = Decimal('0'); total_quote = Decimal('0'); total_fee_usdt = Decimal('0')
    for f in order_data['fills']:
        qty = Decimal(f['qty']); price = Decimal(f['price']); commission = Decimal(f['commission']); commission_asset = f['commissionAsset']
        total_qty += qty; total_quote += qty * price
        if commission_asset == 'USDT': total_fee_usdt += commission
        else:
            try:
                p = session.get(f"{BASE_URL}/api/v3/ticker/price?symbol={commission_asset}USDT", timeout=2).json()
                total_fee_usdt += commission * Decimal(p['price'])
            except: pass
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
    global RUNNING, LAST_FAIL_TIME
    qty = float(f"{qty:.3f}")
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR,"side": side,"type": "LIMIT","timeInForce": "GTC","quantity": f"{qty:.3f}","price": f"{price:.2f}","timestamp": ts}
    query = binance_sign(params)
    r = session.post(f"{BASE_URL}/api/v3/order?{query}", timeout=5).json()

    if 'code' in r:
        if side == "BUY":
            send_telegram(f"❌ *BUY GAGAL*\n`Pair: {PAIR} ${price}`\n`Error: {r.get('msg')}`\n\n*BOT DI PAUSE*")
            RUNNING = False
            LAST_FAIL_TIME = time.time()
            area_gagal.add(price)
        return None
    return r

def binance_market_sell(qty):
    qty = float(f"{qty:.3f}")
    ts = int(time.time() * 1000)
    params = {"symbol": PAIR,"side": "SELL","type": "MARKET","quantity": f"{qty:.3f}","timestamp": ts}
    query = binance_sign(params)
    return session.post(f"{BASE_URL}/api/v3/order?{query}").json()

def send_telegram(msg, keyboard=False):
    try:
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
        if keyboard:
            data["reply_markup"] = {
                "keyboard": [[{"text": "Status"}],[{"text": "Start"}, {"text": "Stop"}],[{"text": "Ganti LOT"}]],
                "resize_keyboard": True
            }
        session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=data, timeout=3)
    except Exception as e:
        print(f"Gagal kirim telegram: {e}")

def kirim_status():
    harga = get_harga_binance(); saldo = get_binance_balance(); slots = load_slots(); posisi = len(slots)
    pesan = f"📊 *STATUS v5.52 ATR 1.5*"
    pesan += f"\n{'🟢 JALAN' if RUNNING else '🔴 PAUSE'} | Harga: `${harga:.2f}`"
    pesan += f"\nSALDO: `${saldo:.4f}`"
    pesan += f"\nGRID: `${GRID:.2f}` | TP: `${TP:.2f}` | LOT: `${LOT}` | Posisi: `{posisi}`\n\n"
    pesan += f"📍 *POSISI*\n"
    if posisi == 0: pesan += "Kosong"
    else:
        for buy_str, tp_target in list(slots.items())[:5]:
            gagal = slot_gagal_sell.get(buy_str, 0)
            pesan += f"`BUY ${float(buy_str):.2f} -> TP ${float(tp_target):.2f}`"
            if gagal > 0: pesan += f" `Gagal:{gagal}x`"
            pesan += "\n"
        if posisi > 5: pesan += f"...dan {posisi-5} posisi lain"
    send_telegram(pesan, keyboard=True)

def save_env_lot(new_lot):
    global LOT
    LOT_LAMA = LOT; LOT = float(new_lot)
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
            new_grid = max(GRID_MIN, min(atr_baru * ATR_MULTIPLIER, GRID_MAX))
            if abs(new_grid - GRID) > 0.1:
                send_telegram(f"🔄 *GRID UPDATE*\n`ATR 14H: ${atr_baru:.2f}`\n`GRID BARU: ${new_grid:.2f} | TP: ${new_grid*(1+BUFFER_FEE):.2f}`")
                cancel_all_tp_orders()
                slots = load_slots(); new_slots = {buy: float(buy) + new_grid*(1+BUFFER_FEE) for buy in slots.keys()}
                save_slots(new_slots); area_yg_aktif.clear()
            GRID = new_grid; TP = GRID * (1 + BUFFER_FEE)
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
                            new_lot = float(text); lot_lama = save_env_lot(new_lot)
                            send_telegram(f"✅ *LOT BERHASIL DIGANTI*\n`LOT Lama: ${lot_lama}`\n`LOT Baru: ${new_lot}`")
                            WAITING_FOR_LOT = False
                        except: send_telegram("❌ Kirim angka. Contoh: 10")
                        continue
                    if text == "status": kirim_status()
                    if text == "start": RUNNING = True; area_gagal.clear(); slot_gagal_sell.clear(); send_telegram("✅ *JALAN MANUAL*")
                    if text == "stop": RUNNING = False; send_telegram("🛑 *PAUSE MANUAL*")
                    if text == "ganti lot": WAITING_FOR_LOT = True; send_telegram(f"💰 `LOT Sekarang: ${LOT}`\nKirim angka baru:")
        except: time.sleep(3)

def get_harga_binance():
    global harga_sekarang
    try: harga_sekarang = float(session.get(f"{BASE_URL}/api/v3/ticker/price?symbol={PAIR}", timeout=3).json()['price'])
    except: pass
    return harga_sekarang

def place_buy(buy_price):
    global area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG, NOTIF_SALDO_0
    if buy_price <= 0: return
    with data_lock:
        if not RUNNING: return
        if buy_price in area_gagal: return

        tp_price = buy_price + TP
        buy_price = round(buy_price, 2); tp_price = round(tp_price, 2)
        qty = LOT / buy_price
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

        order = binance_order("BUY", buy_price, qty)
        if order is None: return

        area_yg_aktif.append(int(buy_price / GRID) * GRID); slots[str(buy_price)] = tp_price
    save_slots(slots)

    time.sleep(2)
    detail = get_order_details(PAIR, order['orderId'])
    qty, quote, fee, avg = hitung_dari_fills(detail)

    if avg == 0:
        avg = buy_price
        qty = LOT / avg
        quote = LOT
        fee = LOT * FEE_EST
        send_telegram(f"🟢 *BUY TERISI* `Estimasi`\n`Harga: ${avg:.4f}`\n`TP: ${tp_price:.2f}`\n`Qty: {qty:.6f}`")
    else:
        send_telegram(f"🟢 *BUY TERISI*\n`Harga: ${avg:.4f}`\n`TP: ${tp_price:.2f}`\n`Qty: {qty:.6f}`")

def proses_trading():
    global last_grid_buy, last_grid_time, area_yg_aktif, RUNNING, NOTIF_SALDO_KURANG, NOTIF_SALDO_0, LAST_FAIL_TIME, area_gagal, slot_gagal_sell
    if harga_sekarang == 0: return

    saldo = get_binance_balance()

    if not RUNNING and saldo >= LOT and time.time() - LAST_FAIL_TIME > 10:
        RUNNING = True
        area_gagal.clear()
        slot_gagal_sell.clear()
        send_telegram(f"✅ *LANJUT OTOMATIS*\n`Saldo: ${saldo:.2f} >= LOT: ${LOT}`")
        grid_terdekat = round(harga_sekarang / GRID) * GRID
        place_buy(grid_terdekat)

    slots = load_slots()
    to_delete = []
    for buy_str, tp_target in list(slots.items()):
        buy = float(buy_str); tp = float(tp_target)
        
        # SELL INSTAN: KHUS HARGA LONCAT LEWAT TP
        if harga_sekarang >= tp:
            qty = LOT / buy
            order = binance_market_sell(qty) # JUAL MARKET BIAR LANGSUNG KEJUAL
            if 'orderId' in order:
                # SUKSES JUAL
                slot_gagal_sell[buy_str] = 0
                time.sleep(2)
                detail = get_order_details(PAIR, order['orderId'])
                qty_jual, quote, fee, avg = hitung_dari_fills(detail)
                if avg == 0: avg = harga_sekarang; quote = qty * avg; fee = quote * FEE_EST
                profit = quote - LOT - fee - (LOT * FEE_EST)
                to_delete.append(buy_str)
                if int(buy / GRID) * GRID in area_yg_aktif: area_yg_aktif.remove(int(buy / GRID) * GRID)
                send_telegram(f"🚨 *SELL INSTAN TP*\n`Buy @${buy:.2f} -> Jual @${avg:.4f}`\n`*Profit: ${profit:.4f}*`")
                reentry = round(avg, 2)
                if (int(reentry / GRID) * GRID) not in area_yg_aktif: place_buy(reentry)
            else:
                error_msg = order.get('msg', '')
                
                # KASUS 1: BARANG ADA, TAPI TP KEMEPETAN KENA FEE
                if "insufficient balance" in error_msg.lower():
                    tp_baru = buy + TP # UPDATE PAKE TP BARU
                    slots[buy_str] = tp_baru
                    save_slots(slots)
                    send_telegram(f"🔄 *UPDATE TP POSISI LAMA*\n`Buy @${buy:.2f}`\n`TP Lama: ${tp:.2f} -> TP Baru: ${tp_baru:.2f}`\n`Alasan: Gagal jual kena fee`")
                
                # KASUS 2: BARANG GAK ADA / POSISI HANTU
                else:
                    slot_gagal_sell[buy_str] = slot_gagal_sell.get(buy_str, 0) + 1
                    send_telegram(f"❌ *SELL INSTAN GAGAL {slot_gagal_sell[buy_str]}X*\n`Buy @${buy:.2f}`\n`Error: {error_msg}`")
                    
                    if slot_gagal_sell[buy_str] >= MAX_GAGAL_SELL:
                        to_delete.append(buy_str)
                        send_telegram(f"🗑️ *HAPUS SLOT HANTU*\n`Buy @${buy:.2f} dihapus karena gagal {MAX_GAGAL_SELL}x`")

    if to_delete:
        slots = load_slots()
        for d in to_delete: 
            if d in slots: del slots[d]
            if d in slot_gagal_sell: del slot_gagal_sell[d]
        save_slots(slots)

    grid_terdekat = round(harga_sekarang / GRID) * GRID
    area_grid = int(grid_terdekat / GRID) * GRID
    if area_grid not in area_yg_aktif and time.time() - last_grid_time > 3:
        place_buy(grid_terdekat); last_grid_buy = grid_terdekat; last_grid_time = time.time()

app = Flask(__name__)
@app.route("/")
def health(): return "OK", 200

def run_bot():
    global NOTIF_AWAL_SENT
    try:
        slots = load_slots()
        get_atr(force=True)
        time.sleep(1)
        send_telegram(f"🤖 *BOT v5.52 ATR 1.5 ON*\n`GRID: ${GRID:.2f} | TP: ${TP:.2f} | LOT: ${LOT}`", keyboard=True)
        harga_awal = get_harga_binance()
        if not slots and RUNNING and harga_awal > 0:
            buy_price = round(harga_awal / GRID) * GRID
            place_buy(buy_price)
            time.sleep(2)
            slots_baru = load_slots()
            if len(slots_baru) > 0 and not NOTIF_AWAL_SENT:
                send_telegram(f"📥 *AWAL START*\n`Masang BUY LIMIT pertama di: ${buy_price:.2f}`\n`TP: ${buy_price+TP:.2f}`")
                NOTIF_AWAL_SENT = True

        for buy_str in slots.keys(): area_yg_aktif.append(int(float(buy_str) / GRID) * GRID)
        threading.Thread(target=cek_command_telegram, daemon=True).start()
        print("BOT v5.52 AKTIF")

        while True:
            try:
                get_harga_binance(); proses_trading()
                now = datetime.now(WIB)
                if now.hour == ATR_UPDATE_HOUR and now.minute == 0 and now.second < 5:
                    get_atr(force=True)
                    time.sleep(5)
                get_atr(force=False)
                time.sleep(1)
            except Exception as e:
                send_telegram(f"❌ *ERROR DI LOOP*\n`{e}`")
                time.sleep(5)
    except Exception as e:
        send_telegram(f"❌ *ERROR FATAL AWAL*\n`{e}`")

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
