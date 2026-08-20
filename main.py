import os, time, math, requests, logging, signal, asyncio, gc
from binance.client import Client
from binance.exceptions import BinanceAPIException
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("PAIR", "BTCUSDT")
TELE_TOKEN = os.getenv("TELE_TOKEN")
TELE_CHAT_ID = os.getenv("TELE_CHAT_ID")
SUPA_URL = os.getenv("SUPA_URL")
SUPA_KEY = os.getenv("SUPA_KEY")

MIN_GRID = 250; MAX_GRID = 1000; QTY_FIXED = 0.00001
BUFFER = 0.0005; FEE = 0.001
DELAY_FIRST_BUY = 1800

binance = None
SUPA_HEADERS = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}

last_grid = 0; app = None
bot_start_time = time.time()
sedang_kerja = False # KUNCI ANTI TABRAKAN

def get_area(price, grid): return math.floor(price / grid) * grid if grid > 0 else 0

def supa_req(m,u,**k):
    try: return requests.request(m,u,headers=SUPA_HEADERS,timeout=5,**k)
    finally: gc.collect() # HAPUS MEMORI LANGSUNG HABIS REQUEST

def get_positions():
    r = supa_req("GET", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
    data = r.json() if r and r.status_code==200 else []
    return data # DATA LANGSUNG DI BALIKIN, GA DISIMPEN LAMA

def get_price():
    try: return float(binance.get_symbol_ticker(symbol=PAIR)['price']) if binance else 0
    except: return 0

def get_qty(price):
    try:
        info = binance.get_symbol_info(PAIR)
        min_n = float(next(f['minNotional'] for f in info['filters'] if f['filterType']=='MIN_NOTIONAL'))
        step = float(next(f['stepSize'] for f in info['filters'] if f['filterType']=='LOT_SIZE'))
        qty = QTY_FIXED
        if price*qty < min_n: qty = math.ceil(min_n/price/step)*step
        return round(qty, 8)
    except: return QTY_FIXED

async def eksekusi_buy(price, area):
    global sedang_kerja
    sedang_kerja = True
    try:
        await kirim_notif(f"🟢 MOMEN: BUY @`${price:.2f}` AREA `{area}`")
        qty = get_qty(price)
        order = binance.order_market_buy(symbol=PAIR, quantity=qty)
        if order['status']== 'FILLED':
            supa_req("POST", f"{SUPA_URL}/rest/v1/positions", json={"pair": PAIR, "area": area, "buy_price": price, "qty": qty})
            await kirim_notif(f"🟢 BUY SUKSES")
    except Exception as e: await kirim_notif(f"⚠️ BUY GAGAL: `{e}`")
    finally: 
        sedang_kerja = False
        gc.collect() # HAPUS MEMORI HABIS KERJA

async def eksekusi_sell(posisi, price):
    global sedang_kerja
    sedang_kerja = True
    try:
        total_qty = sum(p['qty'] for p in posisi)
        await kirim_notif(f"🔴 MOMEN: SELL SEMUA QTY `{total_qty:.8f}`")
        order = binance.order_market_sell(symbol=PAIR, quantity=total_qty)
        if order['status']== 'FILLED':
            supa_req("DELETE", f"{SUPA_URL}/rest/v1/positions?pair=eq.{PAIR}")
            await kirim_notif(f"🔴 SELL SUKSES")
    except Exception as e: await kirim_notif(f"⚠️ SELL GAGAL: `{e}`")
    finally:
        sedang_kerja = False
        gc.collect() # HAPUS MEMORI HABIS KERJA

async def kirim_notif(msg):
    if TELE_CHAT_ID and app:
        try: await app.bot.send_message(chat_id=TELE_CHAT_ID, text=msg, parse_mode="Markdown")
        except: pass

async def loop_pemantau(context: ContextTypes.DEFAULT_TYPE):
    global last_grid, sedang_kerja
    if sedang_kerja: return # KALAU LAGI KERJA, SKIP. JANGAN NUMPUK
    
    # 1. LIHAT -> AMBIL DATA
    harga = get_price()
    if harga == 0: return
    posisi = get_positions()
    
    # 2. CEK MOMEN -> KALAU GAK ADA LANGSUNG HAPUS DENGAN KELUAR
    ada_momen = False
    if not posisi:
        if (time.time() - bot_start_time) >= DELAY_FIRST_BUY:
            area = get_area(harga, last_grid)
            ada_momen = True
            asyncio.create_task(eksekusi_buy(harga, area)) # PANGGIL TAPI JANGAN DITUNGGU
    else:
        area_tertinggi = max(p['area'] for p in posisi)
        if harga >= area_tertinggi + last_grid:
            ada_momen = True
            asyncio.create_task(eksekusi_sell(posisi, harga)) # PANGGIL TAPI JANGAN DITUNGGU
            
    # 3. SELESAI. VARIABEL `posisi` DAN `harga` LANGSUNG DIBUANG DARI RAM
    del posisi
    gc.collect() # PAKSA HAPUS SAMPAH

async def main():
    global app, last_grid, binance
    logging.info("BOT V28.13 LIHAT-HAPUS MULAI...")
    app = ApplicationBuilder().token(TELE_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Bot Jalan")))

    await asyncio.sleep(10)
    binance = Client(API_KEY, API_SECRET, {"timeout": 5})
    last_grid = 500
    
    app.job_queue.run_repeating(loop_pemantau, interval=3, first=5) # BALIK KE 3 DETIK
    await app.initialize(); await app.start(); await app.updater.start_polling()
    
    logging.info("BOT SIAP LIHAT-HAPUS...")
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

if __name__ == "__main__": asyncio.run(main())
