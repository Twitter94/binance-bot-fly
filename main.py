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

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
MIN_USDT = 5
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
SELISIH_TOLERANSI = 0.00001
DELAY_FIRST_BUY = 1800
FEE_KASAR = 0.0011
SCOUT_INTERVAL = 3

binance_scout = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; base_price_for_atr = 0
last_atr_update_day = 0
is_executing = False; mode_flexible = True
bot_start_time = time.time()
stop_event = asyncio.Event()

cached_positions = []; cached_pos_time = 0
cached_taker_fee = 0.0011; last_fee_check = 0

error_notified = {"scout": False, "binance": False, "supabase": False}

def supa_req(m,u,**k):
    global error_notified
    try:
        r = requests.request(m,u,headers=SUPA_HEADERS,timeout=5,**k)
        if not error_notified["supabase"] and (not r or r.status_code >= 400):
            asyncio.create_task(notif_status(f"⚠️ SUPABASE ERROR"))
            error_notified["supabase"] = True
        elif error_notified["supabase"] and r and r.status_code < 400:
            asyncio.create_task(notif_status("✅ SUPABASE SUDAH NORMAL"))
            error_notified["supabase"] = False
        return r
    except Exception as e:
        if not error_notified["supabase"]:
            asyncio.create_task(notif_status(f"⚠️ SUPABASE DOWN"))
            error_notified["supabase"] = True
        return None

def get_positions_cache():
    global cached_positions, cached_pos_time
    if time.time() - cached_pos_time < 5: return cached_positions
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&order=buy_price.asc")
    cached_positions = r.json() if r and r.status_code==200 else []
    cached_pos_time = time.time()
    return cached_positions

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def area_aktif(area, positions): return any(p['area'] == area for p in positions)
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]

async def get_balance(binance_conn, asset):
    try: bal = await binance_conn.fetch_balance(); return float(bal[asset]['free'])
    except: return 0

# FIX 1: GET PRICE PAKE API PUBLIC BIAR GAK KENA BLOK
async def get_price(binance_conn):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        return float(r.json()['price'])
    except Exception as e:
        logging.error(f"Gagal get_price public: {e}")
        return 0

# ATR UPDATE: JAM 00:00 WAJIB + CADANGAN 20%
async def get_atr_grid(binance_conn):
    global last_grid, last_atr_update_day, base_price_for_atr
    now = time.time()
    current_day = time.localtime(now).tm_yday
    price = await get_price(binance_conn)
    if price == 0: return last_grid

    harus_update = False
    alasan = ""

    if current_day!= last_atr_update_day:
        harus_update = True
        alasan = "JAM 00:00"
        last_atr_update_day = current_day
    elif base_price_for_atr > 0:
        perubahan = abs(price - base_price_for_atr) / base_price_for_atr
        if perubahan >= 0.20:
            harus_update = True
            alasan = f"GERAK 20%+"

    if harus_update:
        try:
            ohlcv = await binance_conn.fetch_ohlcv(PAIR, '1h', limit=ATR_PERIOD+1)
            closes = [c[4] for c in ohlcv]; tr = [abs(closes[i]-closes[i-1]) for i in range(1,len(closes))]
            atr = sum(tr)/len(tr) if tr else 500
            grid_baru = max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
            if grid_baru!= last_grid:
                await notif_event(f"📊 ATR UPDATE [{alasan}]: Grid `{last_grid:,.0f}` → `{grid_baru:,.0f}`")
            last_grid = grid_baru
            base_price_for_atr = price
        except Exception as e: logging.error(f"Gagal update ATR: {e}")
    return last_grid

async def get_qty_aman(binance_conn, price):
    try:
        market = binance_conn.market(PAIR); step = market['limits']['amount']['min']
        qty_by_usdt = math.ceil(MIN_USDT/price/step)*step; qty = max(qty_by_usdt, QTY_FIXED); return round(qty, 8)
    except: return QTY_FIXED

async def get_fee_binance(binance_conn):
    global last_fee_check, cached_taker_fee
    if time.time() - last_fee_check < 3600: return 0.0011, cached_taker_fee
    try:
        fee = await binance_conn.fetch_trading_fee(PAIR)
        cached_taker_fee = float(fee['taker'])
        last_fee_check = time.time()
        return 0.0011, cached_taker_fee
    except: return 0.0011, 0.0011

async def notif_event(msg):
    url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except: pass

async def notif_status(msg):
    url = f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage"
    keyboard = {"keyboard": [["STATUS"]], "resize_keyboard": True}
    try: requests.post(url, json={"chat_id": TELE_CHAT_ID, "text": msg, "parse_mode": "Markdown", "reply_markup": keyboard}, timeout=5)
    except: pass

async def sinkron_db_dengan_binance(binance_conn):
    positions_db = get_positions_cache()
    balance_coin = await get_balance(binance_conn, PAIR.split('/')[0])
    total_qty_db = sum(p['qty'] for p in positions_db)
    if abs(balance_coin - total_qty_db) > SELISIH_TOLERANSI:
        await notif_status(f"SYNC: DB `{total_qty_db:.8f}` vs BINANCE `{balance_coin:.8f}`. RESET DB")
        supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}")

async def satpam_buy(binance_conn, price, area, reason="GRID"):
    global is_executing, mode_flexible
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance(binance_conn)
        qty = await get_qty_aman(binance_conn, price)
        _, taker_fee_asli = await get_fee_binance(binance_conn)
        usdt_need = price * qty * (1 + taker_fee_asli + taker_fee_asli + BUFFER)
        await notif_event(f"🟢 BUY [{reason}] @`{price:.2f}` AREA `{area}` | QTY `{qty}` | Fee `{taker_fee_asli*100:.3f}%`")
        if await get_balance(binance_conn, "USDT") < usdt_need:
            if not await cek_dana_dan_jual(binance_conn, usdt_need, price): return
        order = await binance_conn.create_market_buy_order(PAIR, qty)
        if order['status']== 'closed':
            supa_req("POST", f"{SUPA_URL}/rest/v1/positions", json={"pair":PAIR_BINANCE,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['id'])}, headers={**SUPA_HEADERS,"Prefer":"resolution=merge-duplicates"})
            await notif_event(f"🟢 BUY SUKSES"); mode_flexible = False
    except Exception as e: await notif_status(f"⚠️ BUY GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_area(binance_conn, area, positions_in_area, price, mode="BIASA"):
    global is_executing
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance(binance_conn)
        total_qty = sum(p['qty'] for p in positions_in_area)
        _, taker_fee_asli = await get_fee_binance(binance_conn)
        await notif_event(f"🔴 SELL [{mode}] AREA `{area}` @`{price:.2f}` | Fee `{taker_fee_asli*100:.3f}%`")
        order = await binance_conn.create_market_sell_order(PAIR, total_qty)
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}&area=eq.{area}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee_asli - BUFFER)
            await notif_event(f"🔴 SELL SELESAI. PROFIT `~{profit:.2f}`")
    except Exception as e: await notif_status(f"⚠️ SELL GAGAL: `{e}`")
    finally: is_executing = False

async def satpam_sell_instansemua(binance_conn, all_positions, price):
    global is_executing
    if is_executing: return
    is_executing = True
    try:
        await sinkron_db_dengan_binance(binance_conn)
        total_qty = sum(p['qty'] for p in all_positions)
        _, taker_fee_asli = await get_fee_binance(binance_conn)
        await notif_event(f"🔴 SELL INSTAN @`{price:.2f}` | Fee `{taker_fee_asli*100:.3f}%`")
        order = await binance_conn.create_market_sell_order(PAIR, total_qty)
        if order['status']== 'closed':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR_BINANCE}")
            avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty
            profit = (price - avg_buy) * total_qty * (1 - taker_fee_asli)
            await notif_event(f"🔴 SELL INSTAN SELESAI. PROFIT `~{profit:.2f}`")
            await asyncio.sleep(1)
            await satpam_buy(binance_conn, price, get_area(price, last_grid), reason="REENTRY-INSTAN")
    except Exception as e: await notif_status(f"⚠️ SELL INSTAN GAGAL: `{e}`")
    finally: is_executing = False

async def cek_dana_dan_jual(binance_conn, usdt_need, price):
    positions = get_positions_cache()
    if not positions: return False
    await notif_status(f"ROLING: SALDO KURANG")
    for area in set(p['area'] for p in positions):
        pos_in_area = get_pos_by_area(area, positions)
        buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
        if price >= buy_terendah_area + last_grid:
            await satpam_sell_area(binance_conn, area, pos_in_area, price, mode="ROLING")
            await asyncio.sleep(2)
            if await get_balance(binance_conn, "USDT") >= usdt_need:
                await notif_event(f"ROLING SUKSES"); return True
    return False

async def cek_pengaman_restart(binance_conn, price, positions):
    if not positions: return
    area_tertinggi = max(p['area'] for p in positions)
    area_terendah = min(p['area'] for p in positions)
    if price >= area_tertinggi + last_grid:
        await notif_status(f"⚠️ PENGAMAN: SELL INSTAN")
        await satpam_sell_instansemua(binance_conn, positions, price)
        return
    buy_trigger = area_terendah - last_grid
    if price <= buy_trigger:
        area = get_area(price, last_grid)
        if not area_aktif(area, positions):
            await satpam_buy(binance_conn, price, area, reason="RESTART-DIP")

async def scout_loop():
    global last_grid, base_price_start, mode_flexible, error_notified
    while not stop_event.is_set():
        if not is_executing:
            try:
                await get_atr_grid(binance_scout)
                price = await get_price(binance_scout)
                if price == 0: raise Exception("Harga 0")
                if error_notified["binance"]:
                    await notif_status("✅ BINANCE SUDAH NORMAL")
                    error_notified["binance"] = False

                positions = get_positions_cache()
                if not positions:
                    if mode_flexible and (time.time() - bot_start_time) >= DELAY_FIRST_BUY:
                        await satpam_buy(binance_scout, price, get_area(price, last_grid), reason="AUTO-START")
                else:
                    area_tertinggi = max(p['area'] for p in positions)
                    if price >= area_tertinggi + last_grid:
                        await satpam_sell_instansemua(binance_scout, positions, price)
                    else:
                        for area in set(p['area'] for p in positions):
                            pos_in_area = get_pos_by_area(area, positions)
                            buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
                            if price >= buy_terendah_area + last_grid:
                                area_atas = get_area(price + last_grid, last_grid)
                                if area_aktif(area_atas, positions): await satpam_sell_area(binance_scout, area, pos_in_area, price, mode="BIASA")
                                else: await satpam_sell_area(binance_scout, area, pos_in_area, price, mode="REENTRY"); await satpam_buy(binance_scout, price, get_area(price, last_grid), reason="RE-ENTRY")
                                break
                        else:
                            buy_trigger = min([p['buy_price'] for p in positions]) - last_grid
                            if price <= buy_trigger:
                                area = get_area(price, last_grid)
                                if not area_aktif(area, positions): await satpam_buy(binance_scout, price, area, reason="GRID")
            except Exception as e:
                logging.error(f"SCOUT ERROR: {e}")
                if not error_notified["scout"]:
                    await notif_status(f"⚠️ SCOUT ERROR")
                    error_notified["scout"] = True
            finally: gc.collect()
        await asyncio.sleep(SCOUT_INTERVAL)

async def handle_webhook(request):
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text", "").upper()
    chat_id = str(msg.get("chat", {}).get("id"))

    if chat_id == TELE_CHAT_ID and "STATUS" in text:
        binance_temp = ccxt.binance({'apiKey': API_KEY,'secret': API_SECRET,'enableRateLimit': True})
        try:
            price = await get_price(binance_temp) # PAKE PUBLIC JUGA
            pos = get_positions_cache()
            usdt = await get_balance(binance_temp, "USDT")
            qty_kasar = await get_qty_aman(binance_temp, price)
            modal_butuh_kasar = price * qty_kasar * (1 + FEE_KASAR + FEE_KASAR + BUFFER)
            mode = "FLEXIBLE" if mode_flexible else "GRID-KLASIK"
            posisi_txt = ""
            if pos:
                buy_list = sorted(pos, key=lambda x: x['buy_price'], reverse=True)
                for p in buy_list: b = p['buy_price']; s = b + last_grid; posisi_txt += f"`B{b:,.0f} - S{s:,.0f}` | A:`{p['area']:,.0f}` | Q:`{p['qty']}`\n"
            else: posisi_txt = "`- Belum ada posisi -`"
            saldo_status = "✅ AMAN" if usdt >= modal_butuh_kasar else f"⚠️ KURANG {modal_butuh_kasar-usdt:.2f}"
            txt = f"*BOT V30.3.3*\n_Mode: {mode} | Grid: ${last_grid:,.0f}_\n\n*Harga:* `${price:,.2f}`\n*Saldo:* `{usdt:.2f}` {saldo_status}\n*Modal/Layer:* `~{modal_butuh_kasar:.2f}`\n\n*POSISI:* `{len(pos)}`\n{posisi_txt}"
            await notif_status(txt)
        finally: await binance_temp.close()
    return web.Response(text="ok")

async def main():
    global binance_scout, last_grid, base_price_start, base_price_for_atr, last_atr_update_day
    resource.setrlimit(resource.RLIMIT_AS, (180 * 1024 * 1024, 180 * 1024 * 1024))

    binance_scout = ccxt.binance({'apiKey': API_KEY,'secret': API_SECRET,'enableRateLimit': True})

    # FIX 2: CEK FLY_URL SEBELUM SET WEBHOOK
    if not FLY_URL.startswith("https://"):
        logging.error(f"FLY_URL SALAH: {FLY_URL}")
        await notif_status(f"⚠️ FLY_URL SALAH! Harus https://nama.fly.dev")

    try:
        base_price_start = await get_price(binance_scout)
        last_grid = await get_atr_grid(binance_scout)
        base_price_for_atr = base_price_start
        last_atr_update_day = time.localtime(time.time()).tm_yday

        await sinkron_db_dengan_binance(binance_scout)
        if len(get_positions_cache()) > 0: mode_flexible = False
        await cek_pengaman_restart(binance_scout, base_price_start, get_positions_cache())

        app = web.Application()
        app.router.add_post(f"/{TELE_TOKEN}", handle_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 8080)
        await site.start()
        requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/setWebhook", json={"url": f"{FLY_URL}/{TELE_TOKEN}"}, timeout=5)
        await notif_status(f"✅ *BOT V30.3.3 24JAM JALAN*\nGrid: `${last_grid:,.0f}`")

        await scout_loop()
    finally:
        await binance_scout.close()

def handle_exit(): stop_event.set()
if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda s,f: handle_exit())
    asyncio.run(main())
