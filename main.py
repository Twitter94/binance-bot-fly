import asyncio, os, time, requests, hmac, hashlib, gc, sys, json, math, signal
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
    if not os.getenv(v): sys.exit(f"FATAL: {v} belum di set")

SYMBOL = "BTCUSDT"
LOOP_SEC = 3
TABEL = "orders"
TABEL_STATE = "bot_state"
JSON_FILE = "pending_orders.json" # JANGAN dipakai di Fly, cuma fallback
WAIT_FIRST_BUY = 60
NOTIF_MODE = "SILENT"
BASE_URL = "https://api.binance.com"
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}
SB_HEADERS_DELETE = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
SB_UPSERT = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=representation"}
WIB = timezone(timedelta(hours=7))

# ATR CONFIG
ATR_PERIOD = 14; ATR_TIMEFRAME = "1h"; ATR_MULTIPLIER = 0.5; ATR_UPDATE_HOUR = 0
GRID_MIN = 250; GRID_MAX = 1000; GRID_JARAK = 250; LAST_ATR_UPDATE = 0

# GLOBAL - default hanya dipakai kalau Supabase kosong total
STATE = {"paper_mode": True, "paper_usdt": 10000.0, "paper_btc": 0.0, "last_buy_time": 0}
BINANCE_RULES = {'min_notional': 5.0, 'min_qty': 0.00001, 'step_size': 0.00001}
BUYING_LOCK = set(); SELL_LOCK = set(); START_TIME = time.time(); LAST_SYNC = 0

def log(msg):
    """Tukang catat untuk Fly.io, dia print ke console biar kebaca di fly logs dan juga coba tulis ke bot_log.txt sebagai arsip lokal sementara."""
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}")
    try: open("bot_log.txt", "a", encoding="utf-8").write(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {msg}\n")
    except: pass

def notif(msg):
    """Kurir Telegram yang saring notifikasi, dia selalu log dulu baru kirim ke Telegram hanya kalau mode NORMAL atau pesannya penting ada emoji ❌🟢🔴📊 biar gak spam."""
    log(msg)
    if NOTIF_MODE == "NORMAL" or "❌" in msg or "🟢" in msg or "🔴" in msg or "📊" in msg:
        try: requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=5)
        except: pass

def signed_request(method, endpoint, params=None):
    """KTP resmi buat Binance, dia bikin timestamp dan tanda tangan HMAC SHA256 dari secret key lu biar request dianggap sah dan tidak ditolak Binance."""
    if params is None: params = {}
    params['timestamp'] = int(time.time() * 1000); params['recvWindow'] = 60000
    query_string = urlencode(params)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query_string}&signature={signature}"
    try:
        r = requests.request(method, url, headers={'X-MBX-APIKEY': BINANCE_API_KEY}, timeout=10)
        if r.status_code!= 200:
            log(f"BINANCE ERROR {r.status_code}: {r.text}")
            return {"code": r.status_code, "msg": r.text}
        return r.json()
    except Exception as e: return {"code": -1, "msg": str(e)}

def get_price():
    """Mata bot yang ngintip harga BTCUSDT terbaru dari Binance, kalau gagal karena internet Fly lag dia harusnya return None bukan 80000 biar gak buy di harga ngawur."""
    try: return float(requests.get(f"{BASE_URL}/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()['price'])
    except: return None

def update_grid_atr():
    """Otak grid dinamis yang update tiap jam 00:00 WIB, dia hitung ATR dari 14 candle 1 jam terakhir terus dikali 0.5 dan dikunci di range 250-1000 biar jarak grid adaptif sama volatilitas pasar."""
    global GRID_JARAK, LAST_ATR_UPDATE
    jam = datetime.now(WIB).hour; menit = datetime.now(WIB).minute
    if jam == ATR_UPDATE_HOUR and menit < 5 and time.time() - LAST_ATR_UPDATE > 3600:
        LAST_ATR_UPDATE = time.time()
        try:
            url = f"{BASE_URL}/api/v3/klines?symbol={SYMBOL}&interval={ATR_TIMEFRAME}&limit={ATR_PERIOD+1}"
            klines = requests.get(url, timeout=10).json()
            tr_list = []
            for i in range(1, len(klines)):
                high = float(klines[i][2]); low = float(klines[i][3]); prev_close = float(klines[i-1][4])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                tr_list.append(tr)
            atr = sum(tr_list) / ATR_PERIOD
            grid_baru = atr * ATR_MULTIPLIER
            GRID_JARAK = round(max(GRID_MIN, min(grid_baru, GRID_MAX)), 2)
            notif(f"📊 <b>GRID UPDATE 00:00 WIB</b>\nATR{ATR_PERIOD} {ATR_TIMEFRAME}: ${atr:.2f}\nGrid Final: ${GRID_JARAK:.2f}")
        except Exception as e: log(f"GAGAL UPDATE ATR: {e}")

def format_qty(qty):
    """Tukang potong desimal yang nurutin aturan step_size Binance, dia potong qty kebawah biar tidak ada sisa desimal yang bikin order di-reject LOT_SIZE."""
    step = BINANCE_RULES['step_size']; qty = math.floor(qty / step) * step; return f"{qty:.8f}".rstrip('0').rstrip('.')

def hitung_qty_aman(harga):
    """Kalkulator qty minimal biar lolos aturan 5 USDT Binance, dia mulai dari 5.1 dibagi harga terus nambah step_size sampai totalnya di atas 5.01 USDT biar order buy tidak error MIN_NOTIONAL."""
    step = BINANCE_RULES['step_size']; qty = 5.1 / harga
    while harga * float(format_qty(qty)) < 5.01: qty += step
    return format_qty(qty)

def hitung_butuh_modal(price, qty):
    """Bendahara yang ngitung modal kotor butuh berapa, dia jumlahin modal beli plus fee beli 0.1% plus fee jual 0.1% plus buffer 5x fee biar saldo USDT tidak mepet pas eksekusi di Fly."""
    modal = price * float(qty); fee_1x = modal * 0.001; buffer = fee_1x * 5; return modal + fee_1x + fee_1x + buffer

def to_grid(harga):
    """Penggaris yang bulatin harga acak ke garis grid terdekat, misal harga 67123 dengan grid 250 akan jadi 67000 atau 67250 biar semua posisi rapi sejajar."""
    return round(round(harga / GRID_JARAK) * GRID_JARAK, 2) if GRID_JARAK else harga

def auto_setup_supabase():
    """Tukang cek bangunan yang memastikan tabel orders dan bot_state sudah ada di Supabase, kalau belum ada dia coba bikin baris dummy biar Fly tidak crash pas pertama deploy."""
    log("CEK TABEL SUPABASE...")
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?limit=1", headers=SB_HEADERS, timeout=5)
        if r.status_code not in [200, 201, 204]:
            log(f"TABEL {TABEL} BELUM ADA, butuh bikin manual di Supabase Dashboard")
        r2 = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5)
        if r2.status_code == 200 and not r2.json():
            requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}", headers=SB_UPSERT, json={"id":1, "data": STATE})
            notif("✅ Tabel `bot_state` siap")
    except Exception as e: log(f"SETUP SUPABASE ERROR: {e}")

def load_state():
    """Memori abadi yang jadi kunci request lu, dia ambil mode terakhir (PAPER/RILL) dari Supabase id=1, kalau ada dia timpa STATE global biar pas Fly restart dia lanjut mode terakhir sebelum mati bukan balik ke PAPER."""
    global STATE
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, timeout=5).json()
        if r and len(r) > 0 and 'data' in r[0]:
            STATE.update(r[0]['data'])
            log(f"STATE RESTORED: MODE {'PAPER' if STATE['paper_mode'] else 'RILL'}")
            return True
    except Exception as e: log(f"LOAD STATE ERROR: {e}")
    return False

def save_state():
    """Tombol save abadi yang wajib pakai PATCH bukan POST, dia update baris id=1 di Supabase dengan STATE terbaru tiap kali ada perubahan mode atau saldo biar tidak duplikat dan selalu kesimpen sebelum Fly matiin container."""
    try:
        requests.patch(f"{SUPABASE_URL}/rest/v1/{TABEL_STATE}?id=eq.1", headers=SB_HEADERS, data=json.dumps({"data": STATE}), timeout=5)
    except Exception as e: log(f"SAVE STATE ERROR: {e}")

def sb_select(filters=""):
    """Mata yang baca order dari Supabase, dia filter otomatis sesuai mode PAPER/RILL sekarang dan buang status ZOMBIE biar yang kebaca cuma posisi aktif yang relevan."""
    try:
        mode = "PAPER" if STATE["paper_mode"] else "RILL"
        if filters: filters += f"&mode=eq.{mode}&status=neq.ZOMBIE"
        else: filters = f"mode=eq.{mode}&status=neq.ZOMBIE"
        return requests.get(f"{SUPABASE_URL}/rest/v1/{TABEL}?{filters}", headers=SB_HEADERS, timeout=5).json()
    except: return []

def sb_insert(data):
    """Tangan yang nulis order baru ke Supabase, dia paksa tipe data jadi float dan tambahin mode PAPER/RILL biar history tidak campur antara simulasi dan real di Fly."""
    try:
        data['mode'] = "PAPER" if STATE["paper_mode"] else "RILL"
        data['price'] = float(data['price']); data['qty'] = float(data['qty']); data['fee'] = float(data.get('fee', 0))
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABEL}", headers=SB_HEADERS, json=data, timeout=5)
        return r.json() if r.status_code in [200, 201] else []
    except: return []

def sb_delete(order_id):
    """Penghapus yang coba hapus order di Supabase sampai 5 kali kalau jaringan Fly ke Supabase lag, kalau tetap gagal dia ubah jadi ZOMBIE biar tidak dibaca lagi di loop selanjutnya."""
    for i in range(5):
        try:
            if requests.delete(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS_DELETE, timeout=10).status_code == 204: return True
        except: time.sleep(1)
    try: requests.patch(f"{SUPABASE_URL}/rest/v1/{TABEL}?id=eq.{order_id}", headers=SB_HEADERS, json={"status":"ZOMBIE"})
    except: pass
    return False

def save_json(data):
    """Ban serep lokal yang seharusnya tidak dipakai di Fly karena filesystem hilang pas restart, dia cuma jadi cadangan terakhir kalau Supabase benar-benar down dan tidak bisa di-insert."""
    try:
        pending = json.load(open(JSON_FILE)) if os.path.exists(JSON_FILE) else []
        data['mode'] = "PAPER" if STATE["paper_mode"] else "RILL"
        pending.append(data); json.dump(pending, open(JSON_FILE, 'w'))
    except: pass

def load_json():
    """Tukang ambil ban serep yang baca file pending_orders.json kalau ada, terus hapus filenya dan balikin datanya yang sesuai mode sekarang buat dicoba insert lagi ke Supabase."""
    if not os.path.exists(JSON_FILE): return []
    try:
        data = json.load(open(JSON_FILE)); os.remove(JSON_FILE); return [d for d in data if d.get('mode') == ("PAPER" if STATE["paper_mode"] else "RILL")]
    except: return []

def get_balance():
    """Cek dompet yang beda perilaku tergantung mode, kalau PAPER ambil dari STATE di memori, kalau RILL nanya langsung ke Binance berapa USDT dan BTC free yang beneran ada."""
    if STATE["paper_mode"]: return STATE["paper_usdt"], STATE["paper_btc"]
    try:
        data = signed_request("GET", "/api/v3/account")
        if 'code' in data: return 0.0, 0.0
        usdt = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='USDT'), 0))
        btc = float(next((b['free'] for b in data.get('balances',[]) if b['asset']=='BTC'), 0))
        return usdt, btc
    except: return 0.0, 0.0

def sync_binance():
    """Satpam RILL yang bersihin order nyangkut, tiap 10 detik dia bandingin order OPEN di Supabase dengan openOrders di Binance, kalau di Binance sudah tidak ada berarti nyangkut dan harus dihapus dari Supabase biar tidak numpuk."""
    global LAST_SYNC
    if time.time() - LAST_SYNC < 10: return
    LAST_SYNC = time.time()
    if STATE["paper_mode"]: return
    try:
        data_db = sb_select("status=eq.OPEN&side=eq.BUY")
        db_ids = {str(d['binance_order_id']): d for d in data_db if str(d['binance_order_id']).isdigit()}
        binance_open = signed_request("GET", "/api/v3/openOrders", {"symbol":SYMBOL})
        if 'code' in binance_open: return
        binance_ids = {str(o['orderId']) for o in binance_open}
        for oid, data in db_ids.items():
            if oid not in binance_ids:
                if sb_delete(data['id']): notif(f"🧹 AUTO CLEANUP RILL: Hapus nyangkut {data['price']:.2f}")
    except Exception as e: log(f"SYNC ERROR: {e}")

def cek_buy(harga_asli):
    """Logika rem buy yang nahan jangan asal beli, dia cek apakah harga grid sekarang sudah turun sejauh GRID_JARAK dari buy teratas, kalau belum dia log Nahan BUY biar grid tidak terlalu rapat."""
    harga = to_grid(harga_asli) if harga_asli else None
    if not harga: return
    if len(sb_select(f"status=eq.OPEN&side=eq.BUY&price=eq.{harga}")) > 0: return
    data_atas = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
    if not data_atas:
        if time.time() - STATE["last_buy_time"] > WAIT_FIRST_BUY: place_buy(harga)
        return
    harga_grid_teratas = float(data_atas[0]['price'])
    if harga_grid_teratas - harga >= GRID_JARAK: place_buy(harga)
    else: log(f"[{'PAPER' if STATE['paper_mode'] else 'RILL'}] Nahan BUY. Atas: {harga_grid_teratas} | Skrg: {harga}")

def cek_sell(price):
    """Logika trigger jual yang scan semua posisi BUY, kalau harga sekarang sudah naik minimal GRID_JARAK dari harga belinya dia masukin ke antrian jual dan tandain apakah itu posisi top biar nanti bisa buy lagi di atas."""
    to_sell = []
    if not price: return to_sell
    for order in sb_select("status=eq.OPEN&side=eq.BUY"):
        if price >= float(order['price']) + GRID_JARAK:
            data_atas = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc&limit=1")
            is_top = data_atas and order['id'] == data_atas[0]['id']
            to_sell.append({"order": order, "sell_price": price, "is_top": is_top})
    return to_sell

def place_buy(price):
    """Eksekutor beli yang kunci harga biar tidak double order, dia cek saldo cukup, eksekusi MARKET BUY di Binance atau simulasi PAPER, update last_buy_time dan save_state biar mode terakhir kesimpen sebelum Fly mati."""
    price = to_grid(price)
    if price in BUYING_LOCK: return False
    BUYING_LOCK.add(price)
    try:
        if len(sb_select(f"status=eq.OPEN&side=eq.BUY&price=eq.{price}")) > 0: return False
        qty = hitung_qty_aman(price); usdt, _ = get_balance(); butuh = hitung_butuh_modal(price, qty)
        if usdt < butuh: log(f"Saldo kurang. Butuh ${butuh:.2f} Punya ${usdt:.2f}"); return False
        order_id = f"PAPER_{int(time.time())}"; fee = float(qty)*price*0.001
        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"BUY", "type":"MARKET", "quantity":qty})
            if 'orderId' not in res: notif(f"❌ GAGAL BUY RILL: {res.get('msg', res)}"); return False
            order_id = res['orderId']; qty = res['executedQty']; fee = sum([float(f['commission']) for f in res.get('fills', [])])
        else: STATE["paper_usdt"] -= float(qty)*price + fee; STATE["paper_btc"] += float(qty)
        STATE["last_buy_time"] = time.time(); save_state()
        if not sb_insert({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee}):
            save_json({"price":price, "qty":float(qty), "side":"BUY", "status":"OPEN", "binance_order_id": order_id, "fee": fee})
        notif(f"🟢 <b>BUY TERISI [{('PAPER' if STATE['paper_mode'] else 'RILL')}]</b>\nHarga: ${price:.2f}\nQty: {qty}")
        return True
    except Exception as e: notif(f"❌ ERROR BUY: {e}")
    finally: BUYING_LOCK.discard(price)

def place_sell(data):
    """Eksekutor jual TP yang kunci order biar tidak double sell, dia cek saldo BTC, eksekusi MARKET SELL, hitung profit bersih, hapus order BUY di Supabase, dan kalau yang kejual itu top grid dia langsung buy lagi di atas biar grid naik terus."""
    global STATE
    oid = data['order']['id']
    if oid in SELL_LOCK: return
    SELL_LOCK.add(oid); delete_ok = False; harga_jual = data['sell_price']
    try:
        _, btc = get_balance(); qty = float(data['order']['qty'])
        if float(btc) < qty: log(f"Saldo BTC kurang. Butuh {qty} Punya {btc}"); return
        if not STATE["paper_mode"]:
            res = signed_request("POST", "/api/v3/order", {"symbol":SYMBOL, "side":"SELL", "type":"MARKET", "quantity": f"{qty:.8f}"})
            if 'orderId' not in res: notif(f"❌ GAGAL SELL RILL: {res.get('msg', res)}"); return
            fee = sum([float(f['commission']) for f in res.get('fills', [])]); qty_terjual = float(res['executedQty'])
        else:
            qty_terjual = qty; fee = qty_terjual * harga_jual * 0.001
            STATE["paper_usdt"] += qty_terjual * harga_jual - fee; STATE["paper_btc"] -= qty_terjual
        save_state(); delete_ok = sb_delete(oid)
        profit = (harga_jual - float(data['order']['price'])) * qty_terjual - fee - float(data['order']['fee'])
        notif(f"🔴 <b>SELL TP [{('PAPER' if STATE['paper_mode'] else 'RILL')}]</b>\nBuy: ${float(data['order']['price']):.2f} -> Sell: ${harga_jual:.2f}\nProfit: ${profit:.4f}")
        if data['is_top']: place_buy(to_grid(harga_jual))
    except Exception as e: notif(f"❌ ERROR SELL: {e}")
    finally:
        if delete_ok: SELL_LOCK.discard(oid)

def kirim_status_cantik():
    """Laporan cantik buat Telegram yang rangkum semua kondisi Fly sekarang, mulai dari status jalan/pause, mode PAPER/RILL, harga, grid, saldo, butuh modal, jumlah posisi plus tabel detail BUY dan TP biar bisa dipantau dari HP."""
    try:
        usdt, btc = get_balance(); price = get_price() or 0
        data_open = sb_select("status=eq.OPEN&side=eq.BUY&order=price.desc")
        status_txt = "PAUSE" if usdt < hitung_butuh_modal(price, hitung_qty_aman(price)) else "JALAN"
        emoji_status = "🔴" if status_txt=="PAUSE" else "🟢"
        mode_txt = "SILENT" if NOTIF_MODE == "SILENT" else "NORMAL"
        mode_db = "PAPER" if STATE["paper_mode"] else "RILL"
        tanggal = datetime.now(WIB).strftime("%d_%m_%Y")
        msg = f"""<b>SAFANA GRID ATR {tanggal}</b>\n\n{emoji_status} <i>{status_txt}</i> | Mode: <i>{mode_db}</i> | Notif: <i>{mode_txt}</i>\nHarga: ${price:.2f} | Grid: ${GRID_JARAK:.2f}\nSaldo: ${usdt:.2f} | Posisi: {len(data_open)} | BTC: {btc:.8f}"""
        if data_open:
            msg += "\n\n<i>DETAIL POSISI</i>\n<code>--------------------\nNo | BUY | TP\n--------------------\n"
            for no, d in enumerate(data_open, 1):
                tp = float(d['price']) + GRID_JARAK
                msg += f"{no:2}.| ${float(d['price']):8.2f}| ${tp:8.2f}\n"
            msg += "</code>"
        notif(msg)
    except Exception as e: notif(f"❌ ERROR STATUS: {e}")

def kirim_keyboard():
    """Remote keyboard yang bikin tombol STATUS di Telegram biar lu tidak perlu ngetik manual, plus kasih tau list perintah PAPER RILL SILENT NORMAL buat kontrol bot di Fly."""
    keyboard = {"keyboard": [[{"text": "STATUS"}]], "resize_keyboard": True}
    requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": "✅ <b>Panel Kontrol Grid V14.7 ATR FLY</b>\n`PAPER` `RILL` `SILENT` `NORMAL` `STATUS`", "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)})

def cek_tele():
    """Kuping Telegram yang dengerin perintah dari lu, dia ambil getUpdates terakhir, cek chat_id biar tidak dibajak orang lain, eksekusi perintah ganti mode dan save_state langsung biar mode baru kesimpen abadi di Supabase."""
    global NOTIF_MODE
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates", timeout=3).json()
        if not r.get('result'): return
        u = r['result'][-1]; text = u['message']['text'].strip().upper()
        if str(u['message']['chat']['id'])!= TELE_CHAT_ID: return
        if text == "STATUS": kirim_status_cantik()
        elif text == "PAPER":
            STATE["paper_mode"]=True; save_state()
            requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": "🧪 <b>MODE PAPER AKTIF</b>", "parse_mode": "HTML"})
        elif text == "RILL":
            STATE["paper_mode"]=False; save_state()
            requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", data={"chat_id": TELE_CHAT_ID, "text": "💰 <b>MODE RILL AKTIF</b>", "parse_mode": "HTML"})
        elif text == "SILENT": NOTIF_MODE = "SILENT"; notif("🔇 SILENT")
        elif text == "NORMAL": NOTIF_MODE = "NORMAL"; notif("🔊 NORMAL")
        requests.get(f"https://api.telegram.org/bot{TELE_TOKEN}/getUpdates?offset={u['update_id']+1}")
    except Exception as e: log(f"TELE ERROR: {repr(e)}")

async def main():
    """Jantung utama yang hidup di Fly.io, dia pasang handler SIGTERM biar pas Fly mau matiin container dia sempat save_state dulu, terus setup Supabase, restore mode terakhir, load aturan Binance, dan loop selamanya cek ATR, sync, Telegram, harga, buy, sell."""
    def handle_shutdown(signum, frame):
        log(f"SINYAL {signum} DITERIMA, SAVE DULU..."); save_state(); sys.exit(0)
    signal.signal(signal.SIGTERM, handle_shutdown); signal.signal(signal.SIGINT, handle_shutdown)

    auto_setup_supabase()
    if not load_state(): save_state()
    update_grid_atr()
    try:
        data = requests.get(f"{BASE_URL}/api/v3/exchangeInfo?symbol={SYMBOL}").json()
        for f in data['symbols'][0]['filters']:
            if f['filterType']=='MIN_NOTIONAL': BINANCE_RULES['min_notional']=float(f['minNotional'])
            if f['filterType']=='LOT_SIZE': BINANCE_RULES['min_qty']=float(f['minQty']); BINANCE_RULES['step_size']=float(f['stepSize'])
    except: log("Gagal load exchangeInfo")
    notif(f"🤖 <b>BOT V14.7 FLY ATR</b>\nGrid: ${GRID_JARAK} | Mode: {'PAPER' if STATE['paper_mode'] else 'RILL'}")
    kirim_keyboard()
    while True:
        try:
            update_grid_atr(); sync_binance(); cek_tele()
            for d in load_json(): sb_insert(d)
            price = get_price()
            if price:
                cek_buy(price)
                for sig in cek_sell(price): place_sell(sig)
            gc.collect(); await asyncio.sleep(LOOP_SEC)
        except Exception as e: notif(f"❌ <b>ERROR LOOP</b>\n<code>{repr(e)}</code>"); await asyncio.sleep(10)

if __name__ == "__main__": asyncio.run(main())
