import os, time, math, requests, signal, asyncio, gc
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

gc.enable()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
COIN = PAIR.replace("USDT","")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
ATR_MULTIPLIER = 0.5; ATR_PERIOD = 14; BUFFER = 0.0005
FEE = 0.001
SELISIH_TOLERANSI = 0.00001
DELAY_FIRST_BUY = 1800
MIN_NOTIONAL_ENV = float(os.getenv("MIN_NOTIONAL", "10"))

BINANCE_URL = "https://api.binance.com"
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}

last_grid = 0; base_price_start = 0; app = None
is_executing = False; mode_flexible = True
last_status_msg = ""
bot_start_time = time.time()
last_kurang_notif = False
last_roling_notif = False
error_sudah_dikirim = set()

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0
def supa_req(m,u,**k): try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=3,**k) except: return None
def binance_get(path, params={}): try: params['symbol'] = PAIR; r = requests.get(f"{BINANCE_URL}{path}", params=params, headers={"X-MBX-APIKEY":API_KEY}, timeout=3); return r.json() if r.status_code == 200 else {} except: return {}
def binance_post(path, params={}): try: params['symbol'] = PAIR; params['timestamp'] = int(time.time()*1000); r = requests.post(f"{BINANCE_URL}{path}", params=params, headers={"X-MBX-APIKEY":API_KEY}, timeout=5); return r.json() except: return {}
def get_positions(): r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&order=buy_price.asc"); return r.json() if r and r.status_code==200 else []
def area_aktif(area, positions): return any(p['area'] == area for p in positions)
def get_pos_by_area(area, positions): return [p for p in positions if p['area'] == area]
def get_balance(asset): data = binance_get("/api/v3/account"); for b in data.get('balances',[]): if b['asset'] == asset: return float(b['free']); return 0
def get_price(): data = binance_get("/api/v3/ticker/price"); return float(data.get('price', 0))
def get_binance_balance_coin(): return get_balance(COIN)
def get_atr_grid(): data = binance_get("/api/v3/klines", {"interval":"1h", "limit":ATR_PERIOD+1}); if not data: return MIN_GRID; tr = [abs(float(data[i][4])-float(data[i-1][4])) for i in range(1,len(data))]; atr = sum(tr)/len(tr) if tr else 500; return max(MIN_GRID, min(MAX_GRID, round(atr * ATR_MULTIPLIER)))
def get_qty(price): info = binance_get("/api/v3/exchangeInfo"); min_n_binance = 10; step = 0.000001; for s in info.get('symbols',[]): if s['symbol'] == PAIR: for f in s['filters']: if f['filterType']=='MIN_NOTIONAL': min_n_binance = float(f['minNotional']); if f['filterType']=='LOT_SIZE': step = float(f['stepSize']); qty = QTY_FIXED; min_n = max(MIN_NOTIONAL_ENV, min_n_binance); if price*qty < min_n: qty = math.ceil(min_n/price/step)*step; return round(qty, 8)
def binance_order_market(side, qty): return binance_post("/api/v3/order", {"side":side, "type":"MARKET", "quantity":qty})
def binance_get_order(orderId): return binance_get("/api/v3/order", {"orderId":orderId})
KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("STATUS")]], resize_keyboard=True)

async def safe_send(chat_id, msg): try: if app: await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown") except: pass
async def notif_event(msg): if TELE_CHAT_ID: await safe_send(TELE_CHAT_ID, msg)
async def notif_status(msg): global last_status_msg; if not TELE_CHAT_ID: return; if msg == last_status_msg: return; await safe_send(TELE_CHAT_ID, msg); last_status_msg = msg
async def notif_error(tipe, msg): global error_sudah_dikirim; key = f"{tipe}: {msg}"; if key in error_sudah_dikirim: return; error_sudah_dikirim.add(key); await notif_status(f"⚠️ *{tipe}*: `{msg}`")
async def sinkron_db_dengan_binance(): positions_db = get_positions(); balance_coin = get_binance_balance_coin(); total_qty_db = sum(p['qty'] for p in positions_db); if abs(balance_coin - total_qty_db) > SELISIH_TOLERANSI: await notif_status(f"🔄 *SYNC*: DB `{total_qty_db:.8f}` vs BINANCE `{balance_coin:.8f}`. RESET DB"); supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}"); del positions_db; gc.collect()

async def satpam_buy(price, area, reason="GRID"):
    global is_executing, mode_flexible, last_kurang_notif, last_roling_notif
    if is_executing: return
    is_executing = True; await sinkron_db_dengan_binance(); await notif_event(f"🟢 *BUY* [`{reason}`] @`${price:.2f}` AREA `{area}`")
    try: qty = get_qty(price); usdt_need = price * qty * (1 + FEE*2 + BUFFER); if get_balance("USDT") < usdt_need: if not await cek_dana_dan_jual(usdt_need, price): return
        order = binance_order_market("BUY", qty); if order.get('status')!= 'FILLED': return
        supa_req("POST", f"{SUPA_URL}/rest/v1/positions", json={"pair":PAIR,"area":area,"buy_price":price,"qty":qty,"order_id":str(order['orderId'])}, headers={**SUPA_HEADERS,"Prefer":"resolution=merge-duplicates"})
        await notif_event(f"🟢 *BUY SUKSES*\nHarga: `${price:.2f}`\nArea: `{area}`\nLot: `${price*qty:.2f}` | Qty: `{qty:.8f}`")
        last_kurang_notif = False; last_roling_notif = False; mode_flexible = False
    except Exception as e: await notif_error("BUY GAGAL", str(e))
    finally: is_executing = False; gc.collect()

async def satpam_sell_area(area, positions_in_area, price, mode="BIASA"):
    global is_executing, last_kurang_notif, last_roling_notif
    if is_executing: return
    is_executing = True; await sinkron_db_dengan_binance(); total_qty = sum(p['qty'] for p in positions_in_area); await notif_event(f"🔴 *SELL* [`{mode}`] AREA `{area}` @`${price:.2f}` QTY `{total_qty:.8f}`")
    try: order = binance_order_market("SELL", total_qty); if order.get('status')!= 'FILLED': return; supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}&area=eq.{area}"); avg_buy = sum(p['buy_price']*p['qty'] for p in positions_in_area) / total_qty; profit = (price - avg_buy) * total_qty * (1 - BUFFER)
        await notif_event(f"🔴 *SELL SELESAI*\nMode: `{mode}`\nArea: `{area}`\nHarga: `${price:.2f}`\nLot: `${price*total_qty:.2f}`\nProfit: `~${profit:.2f}`")
        if mode == "ROLING": last_kurang_notif = False; last_roling_notif = False
    except Exception as e: await notif_error("SELL GAGAL", str(e))
    finally: is_executing = False; del positions_in_area; gc.collect()

async def satpam_sell_instansemua(all_positions, price): 
    global is_executing
    if is_executing: return
    is_executing = True; await sinkron_db_dengan_binance(); total_qty = sum(p['qty'] for p in all_positions); await notif_event(f"🔴 *SELL INSTAN* [LUAR GRID] @`${price:.2f}` QTY `{total_qty:.8f}`")
    try: order = binance_order_market("SELL", total_qty); if order.get('status')!= 'FILLED': return; supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}"); avg_buy = sum(p['buy_price']*p['qty'] for p in all_positions) / total_qty; profit = (price - avg_buy) * total_qty
        await notif_event(f"🔴 *SELL INSTAN SELESAI*\nHarga: `${price:.2f}`\nLot: `${price*total_qty:.2f}`\nProfit: `~${profit:.2f}`")
        await asyncio.sleep(1); await satpam_buy(price, get_area(price, last_grid), reason="REENTRY-INSTAN")
    except Exception as e: await notif_error("SELL INSTAN GAGAL", str(e))
    finally: is_executing = False; del all_positions; gc.collect()

async def cek_dana_dan_jual(usdt_need, price): 
    global last_kurang_notif, last_roling_notif
    positions = get_positions()
    if not positions: if not last_kurang_notif: await notif_status(f"⚠️ *SALDO KURANG*. GAK ADA POSISI BUAT ROLING"); last_kurang_notif = True; return False
    if not last_kurang_notif: await notif_status(f"⚠️ *SALDO KURANG*. COBA ROLING DANA..."); last_kurang_notif = True
    for area in set(p['area'] for p in positions):
        pos_in_area = get_pos_by_area(area, positions)
        if price >= min(p['buy_price'] for p in pos_in_area) + last_grid:
            await satpam_sell_area(area, pos_in_area, price, mode="ROLING"); await asyncio.sleep(2)
            if get_balance("USDT") >= usdt_need: await notif_event(f"✅ *ROLING SUKSES*. LANJUT BUY"); del positions; gc.collect(); return True
    if not last_roling_notif: await notif_status(f"⚠️ *ROLING GAGAL*: GAK ADA POSISI YG BISA DI TP"); last_roling_notif = True
    del positions; gc.collect(); return False

async def cek_pengaman_restart(price, positions): 
    if not positions: return
    area_tertinggi = max(p['area'] for p in positions); area_terendah = min(p['area'] for p in positions)
    if price >= area_tertinggi + last_grid: await notif_status(f"🛡️ *PENGAMAN RESTART*: HARGA TINGGI. SELL INSTAN"); await satpam_sell_instansemua(positions, price); return
    buy_trigger = area_terendah - last_grid
    if price <= buy_trigger: area = get_area(price, last_grid); if not area_aktif(area, positions): await notif_status(f"🛡️ *PENGAMAN RESTART*: HARGA RENDAH. BUY 1X"); await satpam_buy(price, area, reason="RESTART-DIP")

async def scout_loop(context: ContextTypes.DEFAULT_TYPE): 
    global last_grid, base_price_start, mode_flexible
    if is_executing: gc.collect(); return
    price = get_price(); if price == 0: gc.collect(); return
    positions = get_positions()
    if not positions:
        if mode_flexible and (time.time() - bot_start_time) >= DELAY_FIRST_BUY: area = get_area(price, last_grid); await satpam_buy(price, area, reason="AUTO-START"); base_price_start = price
    else:
        area_tertinggi = max(p['area'] for p in positions)
        if price >= area_tertinggi + last_grid: await satpam_sell_instansemua(positions, price)
        else:
            for area in set(p['area'] for p in positions):
                pos_in_area = get_pos_by_area(area, positions); buy_terendah_area = min(p['buy_price'] for p in pos_in_area)
                if price >= buy_terendah_area + last_grid:
                    area_atas = get_area(price + last_grid, last_grid)
                    if area_aktif(area_atas, positions): await satpam_sell_area(area, pos_in_area, price, mode="BIASA")
                    else: await satpam_sell_area(area, pos_in_area, price, mode="REENTRY"); await satpam_buy(price, get_area(price, last_grid), reason="RE-ENTRY")
                    break
            buy_trigger = min([p['buy_price'] for p in positions]) - last_grid
            if price <= buy_trigger: area = get_area(price, last_grid); if not area_aktif(area, positions): await satpam_buy(price, area, reason="GRID")
    del positions; gc.collect()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    await sinkron_db_dengan_binance(); price = get_price(); usdt = get_balance("USDT"); pos = get_positions()
    mode_txt = "🟢 RUN" if not mode_flexible else "🔴 PAUSE"; txt = f"*STATUS V29.10 STABIL 3S*\n{mode_txt} | *Harga:* `${price:.2f}`\n*SALDO:* `${usdt:.4f}`\n*GRID:* `${last_grid:.2f}` | *LOT:* `${price*get_qty(price):.2f}` | *Min:* `${MIN_NOTIONAL_ENV:.1f}`\n| *Posisi:* `{len(pos)}`"
    if pos: txt += f"\n\n📍 *POSISI*\n"; for p in pos: txt += f"BUY `${p['buy_price']:.2f}` -> TP `${p['buy_price'] + last_grid:.2f}`\n"
    del pos; gc.collect(); await update.message.reply_text(txt, reply_markup=KEYBOARD, parse_mode="Markdown")

async def main(): 
    global app, last_grid, base_price_start, mode_flexible
    app = ApplicationBuilder().token(TELE_TOKEN).pool_timeout(10.0).build() # TAMBAH POOL_TIMEOUT ANTI READERROR
    app.add_handler(CommandHandler("start", status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex('^STATUS$'), status))
    await sinkron_db_dengan_binance(); db = get_positions(); last_grid = get_atr_grid(); base_price_start = get_price(); if len(db) > 0: mode_flexible = False
    await cek_pengaman_restart(base_price_start, db); del db; gc.collect()
    app.job_queue.run_repeating(scout_loop, interval=3, first=5) # UBAH JADI 3 DETIK
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait(); await app.updater.stop(); await app.stop(); await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
