import os, time, math, requests, logging, signal, asyncio, gc, resource
import ccxt.async_support as ccxt
from aiohttp import web

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
PAIR = os.getenv("PAIR", "BTC/USDT")
PAIR_BINANCE = "BTCUSDT"
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")
FLY_URL = os.getenv("FLY_URL")

MIN_GRID = 250; MAX_GRID = 1000
MIN_USDT = 5
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
DELAY_FIRST_BUY = 1800
FEE_KASAR = 0.0011
SCOUT_INTERVAL = 3 # RUMAH CEK HARGA TIAP 3 DETIK

binance_scout = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_for_atr = 0
last_atr_update_day = 0
is_executing = False; mode_flexible = True
bot_start_time = time.time()
stop_event = asyncio.Event()

def supa_req(m,u,**k): # KAMAR ARSIP - CUMA DIPANGGIL PAS PERLU
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=5,**k)
    except: return None

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]

async def get_balance(binance_conn, asset): # KAMAR TRADING
    try: return float((await binance_conn.fetch_balance())[asset]['free'])
    except: return -1

async def get_price(): # RUMAH CEK HARGA - TIDAK LOGIN
    try: return float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5).json()['price'])
    except: return 0

async def get_atr_grid(force_update=False): # RUMAH HITUNG GRID - TIDAK LOGIN
    global last_grid, last_atr_update_day, base_price_for_atr
    current_day = time.localtime().tm_yday
    price = await get_price()
    if price == 0: return last_grid

    harus_update = force_update or current_day!= last_atr_update_day or abs(price - base_price_for_atr) / base_price_for_atr >= 0.20 if base_price_for_atr > 0 else False
    if current_day!= last_atr_update_day: last_atr_update_day = current_day

    if harus_update:
        try:
            data = requests.get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit={ATR_PERIOD+1}", timeout=5).json()
            closes = [float(c[4]) for c in data]; tr = [abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
            atr = sum(tr)/len(tr) if tr else 500
            grid_baru = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
            if grid_baru!= last_grid: await notif(f"📊 ATR UPDATE: Grid `{last_grid:,.0f}` → `{grid_baru:,.0f}`")
            last_grid = grid_baru; base_price_for_atr = price
        except:
            if last_grid == 0: last_grid = 400
    return last_grid

async def get_qty_aman(binance_conn, price): # KAMAR TRADING - CUMA PAS BUY
    await binance_conn.load_markets()
    m = binance_conn.market(PAIR); step = m['limits']['amount']['min']; min_notional = m['limits']['cost']['min']
    qty = math.ceil((MIN_USDT / price) / step) * step
    if price * qty < min_notional: qty = math.ceil(min_notional / price / step) * step
    return round(qty, 8)

async def get_fee_live(binance_conn): # KAMAR TRADING - CUMA PAS BUY/SELL
    try: return float((await binance_conn.fetch_trading_fee(PAIR))['taker'])
    except: return 0.0011

async def notif(msg): # POS SATPAM
    try: requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage", json={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except: pass

async def get_positions_live(binance_conn): # KAMAR ARSIP - CUMA PAS PERLU
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&order=buy_price.asc")
    return r.json() if r and r.status_code==200 else []

# RUMAH CUMA MANTU. GAK SENTUH KAMAR LAIN
async def scout_loop():
    global last_grid, mode_flexible
    while not stop_event.is_set():
        if not is_executing:
            await get_atr_grid()
            price = await get_price()

            if price > 0:
                positions = await get_positions_live(binance_scout) # CEK ARSIP 1X DOANG

                # LOGIKA MOMENT
                if not positions: # MOMENT 1: AUTO START
                    if mode_flexible and (time.time() - bot_start_time) >= DELAY_FIRST_BUY:
                        await aksi_buy(price, get_area(price, last_grid), "AUTO-START")
                else:
                    area_tertinggi = max(p['area'] for p in positions)
                    if price >= area_tertinggi + last_grid: # MOMENT 2: SELL INSTAN
                        await aksi_sell_instan(price, positions)
                    else:
                        moment_ketemu = False
                        for area in set(p['area'] for p in positions):
                            pos_in_area = get_pos_by_area(area, positions)
                            buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
                            if price >= buy_terendah_area + last_grid: # MOMENT 3: SELL
                                area_atas = get_area(price + last_grid, last_grid)
                                if any(p['area'] == area_atas for p in positions): # ADA AREA ATAS = SELL BIASA
                                    await aksi_sell_area(price, area, pos_in_area, "BIASA")
                                else: # GAK ADA AREA ATAS = SELL REENTRY
                                    await aksi_sell_area(price, area, pos_in_area, "REENTRY")
                                    await aksi_buy(price, get_area(price, last_grid), "RE-ENTRY")
                                moment_ketemu = True; break
                        if not moment_ketemu: # MOMENT 4: BUY GRID
                            buy_trigger = min([p['buy_price'] for p in positions]) - last_grid
                            if price <= buy_trigger:
                                area = get_area(price, last_grid)
                                if not any(p['area'] == area for p in positions):
                                    await aksi_buy(price, area, "GRID")
        await asyncio.sleep(SCOUT_INTERVAL)

# AKSI 1: BUY - RUMAH PANGGIL KAMAR TRADING -> ARSIP -> SATPAM
async def aksi_buy(price, area, reason):
    global is_executing, mode_flexible
    is_executing = True
    try:
        qty = await get_qty_aman(binance_scout, price)
        fee = await get_fee_live(binance_scout)
        usdt_need = price * qty * (1 + fee + fee + BUFFER)
        saldo = await get_balance(binance_scout, "USDT")

        if saldo >= usdt_need:
            order = await binance_scout.create_market_buy_order(PAIR, qty) # PANGGIL KAMAR TRADING
            if order['status']== 'closed':
                supa_req("POST", f"{SUPA_URL}/rest/v1/positions", json={"pair":PAIR_BINANCE,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['id'])}) # PANGGIL KAMAR ARSIP
                await notif(f"🟢 BUY [{reason}] @`{price:.2f}` | QTY `{qty}`") # PANGGIL SATPAM
                mode_flexible = False
    except Exception as e: await notif(f"⚠️ BUY GAGAL: `{e}`")
    finally: is_executing = False

# AKSI 2: SELL AREA - RUMAH PANGGIL KAMAR TRADING -> ARSIP -> SATPAM
async def aksi_sell_area(price, area, positions_in_area, mode):
    global is_executing
    is_executing = True
    try:
        total_qty = sum(p['qty'] for p in positions_in_area)
        fee = await get_fee_live(binance_scout)
        order = await binance_scout.create_market_sell_order(PAIR, total_qty) # PANGGIL KAMAR TRADING
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&area=eq.{area}") # PANGGIL KAMAR ARSIP
            avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - fee)
            await notif(f"🔴 SELL [{mode}] @`{price:.2f}` PROFIT `~{profit:.2f}`") # PANGGIL SATPAM
    except Exception as e: await notif(f"⚠️ SELL GAGAL: `{e}`")
    finally: is_executing = False

# AKSI 3: SELL INSTAN - RUMAH PANGGIL KAMAR TRADING -> ARSIP -> SATPAM -> BUY
async def aksi_sell_instan(price, all_positions):
    global is_executing
    is_executing = True
    try:
        total_qty = sum(p['qty'] for p in all_positions)
        fee = await get_fee_live(binance_scout)
        order = await binance_scout.create_market_sell_order(PAIR, total_qty)
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - fee)
            await notif(f"🔴 SELL INSTAN @`{price:.2f}` PROFIT `~{profit:.2f}`")
            await asyncio.sleep(1)
            await aksi_buy(price, get_area(price, last_grid), "REENTRY-INSTAN")
    except Exception as e: await notif(f"⚠️ SELL INSTAN GAGAL: `{e}`")
    finally: is_executing = False

async def handle_webhook(request): # TOMBOL STATUS = LAPORAN KE PEMILIK RUMAH
    data = await request.json()
    if str(data.get("message", {}).get("chat", {}).get("id")) == TELE_CHAT_ID and "STATUS" in data.get("message", {}).get("text", "").upper():
        binance_temp = ccxt.binance({'apiKey': API_KEY,'secret': API_SECRET,'enableRateLimit': True})
        try:
            await binance_temp.load_markets()
            price = await get_price(); pos = await get_positions_live(binance_temp); usdt = await get_balance(binance_temp, "USDT")
            qty_layer = await get_qty_aman(binance_temp, price); modal = price * qty_layer * (1 + FEE_KASAR + FEE_KASAR + BUFFER)
            posisi_txt = "".join([f"`B{p['buy_price']:,.0f} - S{p['buy_price']+last_grid:,.0f}` | A:`{p['area']:,.0f}`\n" for p in sorted(pos, key=lambda x: x['buy_price'], reverse=True)]) or "`- Belum ada posisi -`"
            txt = f"*BOT V30.3.12 RUMAH DIEM*\n*Harga:* `${price:,.2f}` | *Grid:* `${last_grid:,.0f}`\n*Saldo:* `{usdt:.2f}` | *Modal/Layer:* `~{modal:.2f}`\n*POSISI:* `{len(pos)}`\n{posisi_txt}"
            await notif(txt)
        finally: await binance_temp.close()
    return web.Response(text="ok")

async def main():
    global binance_scout, last_grid
    resource.setrlimit(resource.RLIMIT_AS, (180 * 1024 * 1024, 180 * 1024 * 1024))
    binance_scout = ccxt.binance({'apiKey': API_KEY,'secret': API_SECRET,'enableRateLimit': True})

    last_grid = await get_atr_grid(force_update=True)
    positions = await get_positions_live(binance_scout)
    if len(positions) > 0: mode_flexible = False

    app = web.Application(); app.router.add_post(f"/{TELE_TOKEN}", handle_webhook)
    runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, '0.0.0.0', 8080); await site.start()
    requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/setWebhook", json={"url": f"{FLY_URL}/{TELE_TOKEN}"})
    await notif(f"✅ *BOT V30.3.12 RUMAH DIEM JALAN*")

    await scout_loop()

def handle_exit(): stop_event.set()
if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda s,f: handle_exit())
    asyncio.run(main())
